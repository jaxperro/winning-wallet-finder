#!/usr/bin/env python3
"""oraclebot — Study B's crypto-oracle fair-value signal as a real-time
PAPER harness.

PAPER ONLY. No keys, no orders, no bot imports — self-contained
(recorder-style: baked into the wwf-oraclebot image, must not depend on the
repo to boot). It answers the two questions the tape sim cannot (#17): does
fair value compute in real time on the venue's own settlement feed, and is
the mispricing CAPTURABLE at real book latency (~200ms)? The sim's 86%
crater rate and winner's-curse inversion come from a 6.7s-lag fill model
fitted on sports FAKs; this box re-measures that race directly.

THE #17 VERDICT BINDS TO forward_ledger.jsonl ONLY. A good harness cannot
rescue a forward KILL — it can only seed a new pre-registered v2. Entries
are graded nightly against chain truth on the Mac (grade_oracle.py); any
semantics-altering fix bumps SEM_VER and restarts the shakedown clock.

FROZEN semantics (mirror research/forward.py::score_oracle; constants must
equal study_oracle.py / tape.py verbatim — do not tune here):
  fair       Phi(ln(S/K)/(sigma*sqrt(tau))); sigma = trailing 30-min
             realized vol of 1s log-returns; tau in [60s, 12h];
             Down/No token = 1 - fair(up-side); sprint strike = S at t0
  trigger    at a trade print, edge = fair - print >= 0.04; cooldown 300s
             per token set at event time; each event counted in every grid
             cell it clears (cell keys str(E): "0.04"/"0.07"/"0.1")
  execution  $100 FAK vs live CLOB asks inside min(p_ref*1.05, 0.99);
             fee 3% * shares * min(p, 1-p); partial fills kept
Live-only guards (each can only SUPPRESS a trigger the tape sim would
count, never add one): warmup, tick staleness, sprint pre-window gate (the
tape scorer reads s0 from the FUTURE window-open tick for prints before t0
— live can never know it), s0/t1 slack, book-fetch throttle.

Clocks: prints are stamped at ARRIVAL and crypto ticks at the payload's
venue ms timestamp — the same two clocks the tape records (recorder stamps
trades on arrival; load_ticks prefers payload ms). Staleness guards use
the arrival clock only, so venue clock skew can't fake freshness.

OBSERVATIONAL instrumentation (2026-07-22, NOT semantics — SEM_VER
unchanged): every attempt (fill/crater/fail/throttle) appends a line with
top-5 asks + top-3 bids + fair/edge to /data/oracle_attempts.jsonl (real
book depth at mispricing moments — maker-study groundwork; state's skips
list trims at 500 and loses this otherwise), and every fill schedules book
re-reads at +60s/+300s/+1800s (skipping offsets past t1) whose best
bid/ask land in /data/oracle_markouts.jsonl. Pending re-reads die on
restart (counted, acceptable — observation only).
"""
import bisect
import calendar
import heapq
import json
import math
import os
import queue
import re
import ssl
import statistics as st
import threading
import time
import urllib.request

import websocket

WS_URL = "wss://ws-live-data.polymarket.com"
CLOB = "https://clob.polymarket.com"
STATE = os.environ.get("ORACLE_STATE", "/data/oracle_state.json")
ATTEMPTS = os.environ.get("ORACLE_ATTEMPTS", "/data/oracle_attempts.jsonl")
MARKOUTS = os.environ.get("ORACLE_MARKOUTS", "/data/oracle_markouts.jsonl")
SETTLES = os.environ.get("ORACLE_SETTLES", "/data/oracle_settles.jsonl")
DEBUG = bool(os.environ.get("ORACLE_DEBUG"))
SEM_VER = "b1"                    # bump on ANY semantics-altering change
MARKOUT_OFFSETS = (60, 300, 1800)  # observational re-reads per fill
BID_LEVELS = 3
TOP_LEVELS = 5                    # raw ask levels recorded per attempt

# ── FROZEN — must equal study_oracle.py verbatim ────────────────────────────
EDGE_GRID = [0.04, 0.07, 0.10]
VOL_WIN_S = 1800
TAU_MIN, TAU_MAX = 60, 12 * 3600
COOLDOWN_S = 300
STAKE = 100.0
UP_WORDS = {"up", "yes"}
DOWN_WORDS = {"down", "no"}
# ── FROZEN — must equal sim.py / surgebot.py verbatim ───────────────────────
FEE_RATE = 0.03
SLIP_CAP = 0.05
# ── LIVE-ONLY — can only suppress, never add, triggers ──────────────────────
WARMUP_S = 1800                   # per-symbol; must contain COOLDOWN_S (the
COOLDOWN_CONTAINMENT = True       # restart argument: cooldowns are not
                                  # persisted — safe because warmup blocks
                                  # all triggers past any pre-restart
                                  # cooldown horizon; selftest asserts it)
TICK_STALE_S = 10                 # arrival-clock; stale feed => no fair
TICK_RETAIN_S = 7200              # covers vol window + hourly-sprint t0
PROV_SLACK_S = 5                  # deciding tick must be this close to t0/t1
TIE_EPS = 1e-4                    # 1bp of boundary => defer to CLOB/chain
SETTLE_GRACE_S = 120
BOOK_WORKERS = 4
BOOK_QUEUE = 16                   # overflow => throttle_skip (still counted)
CLOB_PASS_CAP = 25                # GETs per backstop pass, round-robin
SETTLED_TRIM = 10000              # nightly ledger is the durable record
SKIPS_TRIM = 500
DST_WARN_TS = calendar.timegm((2026, 10, 25, 0, 0, 0))   # EDT ends 11-01
YEAR_WARN_TS = calendar.timegm((2026, 12, 15, 0, 0, 0))  # YEAR rolls
SSL_CTX = ssl._create_unverified_context()

SYMS = ("btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "dogeusdt")


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


def get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


# ── title parsing — FROZEN, ported verbatim from research/tape.py ───────────

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

SYMBOLS = {"bitcoin": "btcusdt", "btc": "btcusdt",
           "ethereum": "ethusdt", "eth": "ethusdt",
           "solana": "solusdt", "sol": "solusdt",
           "xrp": "xrpusdt", "bnb": "bnbusdt", "doge": "dogeusdt",
           "dogecoin": "dogeusdt"}

ET_OFF = 4 * 3600                 # EDT (July) = UTC-4
YEAR = 2026                       # tape era; date guard warns at year roll

RE_SPRINT = re.compile(
    r"(?i)^(\w+)\s+up or down\s*-\s*(\w+)\s+(\d+),\s*"
    r"(\d+)(?::(\d+))?(am|pm)-(\d+)(?::(\d+))?(pm|am)\s*et")
RE_HOURLY = re.compile(
    r"(?i)^(\w+)\s+(above|below)\s+([\d,\.]+)\s+on\s+(\w+)\s+(\d+),\s*"
    r"(\d+)(?::(\d+))?\s*(am|pm)\s*et")
RE_DAILY = re.compile(
    r"(?i)price of (\w+) be (above|below|between)\s+\$?([\d,\.]+)"
    r"(?:\s+and\s+\$?([\d,\.]+))?\s+on\s+(\w+)\s+(\d+)")
# One-touch "dip to / reach $K" titles are NOT parsed on purpose (barrier,
# not terminal digital) — crypto_parse returns None and they never trigger.


def _num(s):
    return float(s.replace(",", "")) if s else None


def _clock(h, m, ap):
    h = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
    return h, int(m or 0)


def _et(mon, day, hh, mm):
    # mktime(struct, isdst=0) - time.timezone == timegm on any libc that
    # honors isdst=0 (trivially exact on the UTC Fly box); selftest proves
    # the identity on the running box with canned-title epochs.
    return time.mktime(time.struct_time(
        (YEAR, mon, day, 0, 0, 0, 0, 0, 0))) - time.timezone + hh * 3600 \
        + mm * 60 + ET_OFF


def crypto_parse(title):
    """-> dict(sym, kind, k1, k2, t0, t1) or None. kind: sprint/above/
    below/between. t0 only for sprints (window open)."""
    t = title or ""
    m = RE_SPRINT.match(t)
    if m:
        sym = SYMBOLS.get(m.group(1).lower())
        mon = MONTHS.get(m.group(2).lower())
        if not sym or not mon:
            return None
        day = int(m.group(3))
        h0, m0 = _clock(m.group(4), m.group(5), m.group(6))
        h1, m1 = _clock(m.group(7), m.group(8), m.group(9))
        return {"sym": sym, "kind": "sprint", "k1": None, "k2": None,
                "t0": _et(mon, day, h0, m0), "t1": _et(mon, day, h1, m1)}
    m = RE_HOURLY.match(t)
    if m:
        sym, mon = SYMBOLS.get(m.group(1).lower()), MONTHS.get(m.group(4).lower())
        if not sym or not mon:
            return None
        h, mi = _clock(m.group(6), m.group(7), m.group(8))
        return {"sym": sym, "kind": m.group(2).lower(), "k1": _num(m.group(3)),
                "k2": None, "t0": None,
                "t1": _et(mon, int(m.group(5)), h, mi)}
    m = RE_DAILY.search(t)
    if m:
        sym, mon = SYMBOLS.get(m.group(1).lower()), MONTHS.get(m.group(5).lower())
        if not sym or not mon:
            return None
        return {"sym": sym, "kind": m.group(2).lower(), "k1": _num(m.group(3)),
                "k2": _num(m.group(4)), "t0": None,
                "t1": _et(mon, int(m.group(6)), 12, 0)}   # dailies: 12PM ET
    return None


# ── fair value — FROZEN, ported verbatim from research/study_oracle.py ──────

def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value(mkt, up_side, S, sigma, t):
    tau = mkt["t1"] - t
    if not (TAU_MIN <= tau <= TAU_MAX) or not S or not sigma:
        return None
    sv = sigma * math.sqrt(tau)
    if sv <= 0:
        return None
    k = mkt["kind"]
    if k == "sprint":
        if mkt.get("s0") is None:
            return None
        f_up = phi(math.log(S / mkt["s0"]) / sv)
    elif k == "above":
        f_up = phi(math.log(S / mkt["k1"]) / sv)
    elif k == "below":
        f_up = 1.0 - phi(math.log(S / mkt["k1"]) / sv)
    elif k == "between":
        f_up = phi(math.log(mkt["k2"] / S) / sv) - phi(math.log(mkt["k1"] / S) / sv)
    else:
        return None
    return f_up if up_side else 1.0 - f_up


# ── paper FAK ───────────────────────────────────────────────────────────────

def walk_asks(asks, cap, stake_usd=STAKE):
    """FAK against a CLOB ask list: consume ascending price levels <= cap
    until stake_usd is spent; partial fills kept (that IS what a real FAK
    gets). The first level's price is also the best-ask-only read (~$5-
    deployable). asks arrive UNSORTED from /book."""
    lv = sorted(((float(a["price"]), float(a["size"])) for a in asks or []))
    top = [[p, s] for p, s in lv[:TOP_LEVELS]]
    if not lv:
        return {"filled": False, "best_ask": None, "top": top}
    best = lv[0][0]
    spent = shares = 0.0
    levels = 0
    for px, sz in lv:
        if px > cap or spent >= stake_usd - 1e-9:
            break
        take = min(sz, (stake_usd - spent) / px)
        if take <= 0:
            break
        shares += take
        spent += take * px
        levels += 1
    if not shares:
        return {"filled": False, "best_ask": best, "top": top}
    return {"filled": True, "vwap": spent / shares, "shares": shares,
            "usd": spent, "levels": levels, "best_ask": best, "top": top,
            "partial": spent < stake_usd - 1e-9}


# ── live tick engine ────────────────────────────────────────────────────────

class LiveTicks:
    """Per-symbol venue-time series. at()/vol_1s() are FROZEN verbatim from
    study_oracle.TickSeries; the rest is live-only maintenance (monotonic
    dedupe absorbs the twin sockets, retention trim, warmup/freshness)."""

    def __init__(self):
        self.ts = []
        self.px = []
        self.last_arrival = 0.0
        self.ooo = 0                   # duplicate/out-of-order drops

    def add(self, t, p, now):
        self.last_arrival = now
        if not p:
            return                     # mirrors load_ticks' `if v`
        if self.ts and t <= self.ts[-1]:
            self.ooo += 1
            return
        self.ts.append(t)
        self.px.append(p)

    def at(self, t):                   # FROZEN
        i = bisect.bisect_right(self.ts, t) - 1
        return self.px[i] if i >= 0 else None

    def at_ts(self, t):
        """venue timestamp of the tick at() would use — for slack guards."""
        i = bisect.bisect_right(self.ts, t) - 1
        return self.ts[i] if i >= 0 else None

    def vol_1s(self, t, win=VOL_WIN_S):  # FROZEN
        lo = bisect.bisect_left(self.ts, t - win)
        hi = bisect.bisect_right(self.ts, t)
        if hi - lo < 60:
            return None
        rets = []
        for i in range(lo + 1, hi):
            dt = self.ts[i] - self.ts[i - 1]
            if dt <= 0:
                continue
            r = math.log(self.px[i] / self.px[i - 1]) / math.sqrt(dt)
            rets.append(r)
        return st.pstdev(rets) if len(rets) >= 30 else None

    def trim(self, now):
        cut = bisect.bisect_left(self.ts, now - TICK_RETAIN_S)
        if cut:
            del self.ts[:cut]
            del self.px[:cut]

    def warm(self, now):
        return bool(self.ts) and self.ts[0] <= now - WARMUP_S

    def fresh(self, now):
        return now - self.last_arrival <= TICK_STALE_S


# ── the bot ─────────────────────────────────────────────────────────────────

def fresh_state():
    return {
        "sem_ver": SEM_VER, "boot_ts": int(time.time()),
        "open": {}, "settled": [], "skips": [],
        "cells": {str(E): {"events": 0, "attempts": 0, "fills": 0,
                           "craters": 0, "throttle": 0, "wins": 0,
                           "refunds": 0, "staked": 0.0, "pnl": 0.0}
                  for E in EDGE_GRID},
        "pnl_realized": 0.0,
        "counters": {"trades_seen": 0, "crypto_prints": 0, "events": 0,
                     "attempts": 0, "fills": 0, "craters": 0,
                     "throttle_skip": 0, "settled_total": 0,
                     "settle_tick": 0, "settle_clob": 0,
                     "skip": {"unknown_side": 0, "sprint_no_label": 0,
                              "pre_window": 0, "s0_gap": 0, "warmup": 0,
                              "stale": 0, "no_vol": 0, "tau": 0,
                              "book_fail": 0}}}


class OracleBot:
    def __init__(self):
        self.state = fresh_state()
        if os.path.exists(STATE):
            try:
                loaded = json.load(open(STATE))
                if loaded.get("sem_ver") != SEM_VER:
                    log(f"⚠ SEM_VER {loaded.get('sem_ver')} -> {SEM_VER} — "
                        f"semantics changed; shakedown clock restarts here")
                    loaded["sem_ver"] = SEM_VER
                self.state = loaded
                log(f"resumed: pnl {self.state['pnl_realized']:+.2f} · "
                    f"{len(self.state['open'])} open · "
                    f"{self.state['counters']['fills']} lifetime fills")
            except Exception as e:
                log(f"⚠ state load failed ({e}) — fresh book")
        self.ticks = {s: LiveTicks() for s in SYMS}
        self.last_trig = {}            # not persisted — see WARMUP_S comment
        self.lock = threading.Lock()
        self.book_q = queue.Queue(BOOK_QUEUE)
        self.mo_heap = []              # (due_ts, asset, fill_id, offset)
        self.trades_seen = 0           # unlocked cosmetic counter (firehose
                                       # rate) — folded into state at heartbeat

    # ── streams ────────────────────────────────────────────────────────────
    def on_tick(self, payload):
        now = time.time()
        items = payload if isinstance(payload, list) else [payload]
        with self.lock:
            for it in items:
                try:
                    s = self.ticks.get((it.get("symbol") or "").lower())
                    if s is None:
                        continue
                    t = float(it.get("timestamp") or 0)
                    t = t / 1000.0 if t > 1e12 else (t or now)
                    s.add(t, float(it.get("value") or 0), now)
                except Exception:
                    continue

    def on_trade(self, p):
        title = p.get("title") or ""
        mkt = crypto_parse(title)      # pure — outside the lock
        if not mkt:
            return
        asset = p.get("asset")
        try:
            px = float(p.get("price") or 0)
        except (TypeError, ValueError):
            return
        if not asset or not (0 < px < 1):
            return
        s = self.ticks.get(mkt["sym"])
        if s is None:
            return
        now = time.time()              # print clock = arrival (tape mirror)
        o = (p.get("outcome") or "").lower()
        with self.lock:
            c = self.state["counters"]
            sk = c["skip"]
            c["crypto_prints"] += 1
            # side — FROZEN mirror of study_oracle.crypto_universe()
            if o and o not in UP_WORDS | DOWN_WORDS:
                sk["unknown_side"] += 1
                return
            up = o in UP_WORDS or (o == "" and mkt["kind"] != "sprint")
            if mkt["kind"] == "sprint" and not o:
                sk["sprint_no_label"] += 1
                return
            # LIVE-ONLY gates (suppress-only)
            if not s.warm(now):
                sk["warmup"] += 1
                return
            if not s.fresh(now):
                sk["stale"] += 1
                return
            s0 = None
            if mkt["kind"] == "sprint":
                if now < mkt["t0"]:
                    sk["pre_window"] += 1   # tape scorer's s0 lookahead
                    return
                s0 = s.at(mkt["t0"])
                t0_tick = s.at_ts(mkt["t0"])
                if s0 is None or mkt["t0"] - t0_tick > PROV_SLACK_S:
                    sk["s0_gap"] += 1
                    return
                mkt = {**mkt, "s0": s0}
            S = s.at(now)
            sig = s.vol_1s(now)
            f = fair_value(mkt, up, S, sig, now)
            if f is None:
                tau = mkt["t1"] - now
                sk["tau" if not (TAU_MIN <= tau <= TAU_MAX) else "no_vol"] += 1
                return
            edge = f - px
            if DEBUG:
                log(f"DBG ts={now:.3f} asset={asset} kind={mkt['kind']} "
                    f"sym={mkt['sym']} fair={f:.4f} print={px:.3f} "
                    f"edge={edge:+.4f} vol={sig:.2e} tau={int(mkt['t1']-now)}")
            if edge < min(EDGE_GRID):
                return
            if now - self.last_trig.get(asset, 0) < COOLDOWN_S:
                return
            self.last_trig[asset] = now  # set PRE-fetch: twin dups self-block
            c["events"] += 1
            for E in EDGE_GRID:
                if edge >= E:
                    self.state["cells"][str(E)]["events"] += 1
            job = {"asset": asset, "cond": p.get("conditionId"),
                   "title": title[:60], "kind": mkt["kind"], "sym": mkt["sym"],
                   "outcome": p.get("outcome"), "up": up,
                   "k1": mkt.get("k1"), "k2": mkt.get("k2"),
                   "t0": mkt.get("t0"), "t1": mkt["t1"], "s0": s0,
                   "ts": round(now, 3), "p_ref": px, "fair": round(f, 4),
                   "edge": round(edge, 4), "sig": round(sig, 8)}
            try:
                self.book_q.put_nowait(job)
                c["attempts"] += 1
                for E in EDGE_GRID:
                    if edge >= E:
                        self.state["cells"][str(E)]["attempts"] += 1
            except queue.Full:
                c["throttle_skip"] += 1
                for E in EDGE_GRID:
                    if edge >= E:
                        self.state["cells"][str(E)]["throttle"] += 1
                self._skip(job, "throttle", None)
                self.log_attempt({**job, "filled": False, "why": "throttle"})

    # ── paper execution (workers; network OUT of the lock) ─────────────────
    def book_worker(self):
        while True:
            job = self.book_q.get()
            try:
                self.attempt(job)
            except Exception as e:
                log(f"⚠ attempt error: {e}")

    def attempt(self, job):
        t_req = time.time()
        try:
            book = get_json(f"{CLOB}/book?token_id={job['asset']}")
        except Exception:
            with self.lock:
                self.state["counters"]["skip"]["book_fail"] += 1
                self._skip(job, "book_fail", None)
            self.log_attempt({**job, "filled": False, "why": "book_fail"})
            return
        lat_ms = int((time.time() - t_req) * 1000)
        cap = min(job["p_ref"] * (1 + SLIP_CAP), 0.99)
        r = walk_asks(book.get("asks"), cap)
        bid_top = sorted(((float(b["price"]), float(b["size"]))
                          for b in book.get("bids") or []),
                         reverse=True)[:BID_LEVELS]
        rec = {**job, "cap": round(cap, 4), "latency_ms": lat_ms,
               "top": r["top"], "bid_top": [[p, s] for p, s in bid_top],
               "best_ask": r["best_ask"], "filled": r["filled"]}
        with self.lock:
            c = self.state["counters"]
            if not r["filled"]:
                c["craters"] += 1
                for E in EDGE_GRID:
                    if job["edge"] >= E:
                        self.state["cells"][str(E)]["craters"] += 1
                self._skip(job, "crater", r["best_ask"])
                ba = "—" if r["best_ask"] is None else f"{r['best_ask']:.3f}"
                log(f"CRATER {job['title'][:38]} ref {job['p_ref']:.3f} "
                    f"ask {ba} edge {job['edge']:+.3f} ({lat_ms}ms)")
                self.persist()
            else:
                fee = FEE_RATE * r["shares"] * min(r["vwap"], 1 - r["vwap"])
                lot = {**job, "cap": round(cap, 4),
                       "price": round(r["vwap"], 5),
                       "best_ask": r["best_ask"],
                       "shares": round(r["shares"], 4),
                       "cost": round(r["usd"], 2), "fee": round(fee, 4),
                       "levels": r["levels"], "partial": r["partial"],
                       "latency_ms": lat_ms}
                self.state["open"][f"{job['asset']}:{job['ts']}"] = lot
                c["fills"] += 1
                for E in EDGE_GRID:
                    if job["edge"] >= E:
                        cell = self.state["cells"][str(E)]
                        cell["fills"] += 1
                        cell["staked"] = round(cell["staked"] + lot["cost"], 2)
                rec.update({"price": lot["price"], "shares": lot["shares"],
                            "usd": lot["cost"], "fee": lot["fee"],
                            "partial": lot["partial"]})
                for off in MARKOUT_OFFSETS:   # observational exit marks
                    if job["t1"] and job["ts"] + off > job["t1"]:
                        continue
                    heapq.heappush(self.mo_heap,
                                   (job["ts"] + off, job["asset"],
                                    f"{job['asset']}:{job['ts']}", off))
                log(f"FILL {job['outcome']} · {job['title'][:38]} @ "
                    f"{lot['price']:.3f} (${lot['cost']:.2f}"
                    f"{' partial' if lot['partial'] else ''}, "
                    f"edge {job['edge']:+.3f}, {lat_ms}ms)")
                self.persist()
        self.log_attempt(rec)

    def _skip(self, job, why, ba):     # under lock
        self.state["skips"].append(
            {"ts": int(job["ts"]), "asset": job["asset"], "why": why,
             "p_ref": job["p_ref"], "edge": job["edge"], "ba": ba,
             "title": job["title"]})
        del self.state["skips"][:-SKIPS_TRIM]

    def log_attempt(self, rec):
        """Append-only full attempt stream (observational — maker-study
        groundwork; the state's skips list trims and loses book detail)."""
        try:
            with open(ATTEMPTS, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            log(f"⚠ attempts log write failed: {e}")

    def markout_worker(self):
        """Observational: due book re-reads -> best bid/ask marks. Never
        touches the signal path; pending marks die on restart (counted)."""
        while True:
            due = None
            with self.lock:
                if self.mo_heap and self.mo_heap[0][0] <= time.time():
                    due = heapq.heappop(self.mo_heap)
            if due is None:
                time.sleep(2)
                continue
            due_ts, asset, fid, off = due
            rec = {"fill_id": fid, "asset": asset, "offset_s": off,
                   "due_ts": round(due_ts, 1),
                   "read_ts": round(time.time(), 3)}
            try:
                book = get_json(f"{CLOB}/book?token_id={asset}")
                bids = sorted(((float(b["price"]), float(b["size"]))
                               for b in book.get("bids") or []), reverse=True)
                asks = sorted(((float(a["price"]), float(a["size"]))
                               for a in book.get("asks") or []))
                rec["bid"], rec["bid_sz"] = (bids[0] if bids else (None, None))
                rec["ask"], rec["ask_sz"] = (asks[0] if asks else (None, None))
                key = "mo_done"
            except Exception as e:
                rec["err"] = str(e)[:40]
                key = "mo_err"
            with self.lock:
                c = self.state["counters"]
                c[key] = c.get(key, 0) + 1
            try:
                with open(MARKOUTS, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except Exception as e:
                log(f"⚠ markouts log write failed: {e}")

    # ── settlement ─────────────────────────────────────────────────────────
    # (a) instant, from the same feed the venue settles on (under lock)
    def settle_provisional(self):
        now = time.time()
        n = 0
        for lid, lot in list(self.state["open"].items()):
            if now < lot["t1"] + SETTLE_GRACE_S:
                continue
            s = self.ticks[lot["sym"]]
            S1 = s.at(lot["t1"])
            t1_tick = s.at_ts(lot["t1"])
            if S1 is None or lot["t1"] - t1_tick > PROV_SLACK_S:
                continue               # buffer gap at t1 -> CLOB/chain layers
            k = lot["kind"]
            if k == "sprint":
                bounds, yes = [lot["s0"]], S1 > lot["s0"]
            elif k == "above":
                bounds, yes = [lot["k1"]], S1 > lot["k1"]
            elif k == "below":
                bounds, yes = [lot["k1"]], S1 < lot["k1"]
            elif k == "between":
                bounds = [lot["k1"], lot["k2"]]
                yes = lot["k1"] <= S1 <= lot["k2"]
            else:
                continue
            # boundary tie: the venue's exact settle print can differ — defer
            if any(b and abs(S1 - b) / b < TIE_EPS for b in bounds):
                continue
            self._settle(lid, lot, 1.0 if yes == lot["up"] else 0.0, "tick")
            n += 1
        if n:
            self.persist()

    # (b) CLOB closed/winner backstop for what (a) couldn't decide
    def settle_clob_pass(self):
        with self.lock:
            now = time.time()
            due = [lid for lid, lot in self.state["open"].items()
                   if now > lot["t1"] + SETTLE_GRACE_S]
            due.sort(key=lambda lid: self.state["open"][lid].get("clob_ck", 0))
            due = due[:CLOB_PASS_CAP]
            snap = {}
            for lid in due:
                lot = self.state["open"][lid]
                lot["clob_ck"] = now   # round-robin stamp
                snap[lid] = (lot["cond"], lot["asset"])
        verdicts = {}
        for lid, (cond, asset) in snap.items():
            try:
                m = get_json(f"{CLOB}/markets/{cond}")
            except Exception:
                continue
            if not m.get("closed"):
                continue
            for t in m.get("tokens") or []:
                if str(t.get("token_id")) == str(asset):
                    verdicts[lid] = 1.0 if t.get("winner") else 0.0
        if not verdicts:
            return
        with self.lock:
            n = 0
            for lid, pay in verdicts.items():
                lot = self.state["open"].get(lid)
                if lot:
                    self._settle(lid, lot, pay, "clob")
                    n += 1
            if n:
                self.persist()

    def _settle(self, lid, lot, pay, source):   # under lock
        pnl = round(lot["shares"] * pay - lot["cost"] - lot["fee"], 2)
        self.state["pnl_realized"] = round(self.state["pnl_realized"] + pnl, 2)
        for E in EDGE_GRID:
            if lot["edge"] >= E:
                cell = self.state["cells"][str(E)]
                cell["pnl"] = round(cell["pnl"] + pnl, 2)
                cell["wins"] += pay == 1.0
                cell["refunds"] += pay == 0.5
        c = self.state["counters"]
        c["settled_total"] += 1
        c["settle_tick" if source == "tick" else "settle_clob"] += 1
        rec = {**lot, "payout": pay, "source": source, "provisional": True,
               "settled_ts": int(time.time()), "pnl": pnl}
        self.state["settled"].append(rec)
        del self.state["settled"][:-SETTLED_TRIM]
        del self.state["open"][lid]
        try:                            # durable append-log: SETTLED_TRIM can
            with open(SETTLES, "a") as fh:   # never rotate a settle away
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            log(f"⚠ settles log write failed: {e}")
        word = "WON" if pay == 1.0 else "refund" if pay == 0.5 else "lost"
        log(f"SETTLE[{source}] {word} {lot['title'][:36]} {pnl:+.2f} · "
            f"realized {self.state['pnl_realized']:+.2f}")

    # ── plumbing ───────────────────────────────────────────────────────────
    def persist(self):
        tmp = STATE + ".tmp"
        json.dump(self.state, open(tmp, "w"))
        os.replace(tmp, STATE)

    def feed_dict(self):               # under lock
        now = time.time()
        syms = {}
        for sym, s in self.ticks.items():
            syms[sym] = {
                "px": s.px[-1] if s.px else None,
                "tick_age_s": round(now - s.last_arrival, 1)
                if s.last_arrival else None,
                "n_buf": len(s.ts), "warm": s.warm(now),
                "warm_eta_s": max(0, int(s.ts[0] + WARMUP_S - now))
                if s.ts else None,
                "fresh": s.fresh(now), "vol_1s": s.vol_1s(now)}
        stt = self.state
        return {
            "mode": "paper", "study": "oracle-B (#17)", "sem_ver": SEM_VER,
            "updated": int(now), "boot_ts": stt["boot_ts"],
            "frozen": {"edge_grid": EDGE_GRID, "vol_win_s": VOL_WIN_S,
                       "tau_min": TAU_MIN, "tau_max": TAU_MAX,
                       "cooldown_s": COOLDOWN_S, "stake": STAKE,
                       "fee_rate": FEE_RATE, "slip_cap": SLIP_CAP},
            "symbols": syms, "cells": stt["cells"],
            "pnl_realized": stt["pnl_realized"], "open_n": len(stt["open"]),
            "open": [{**l, "id": lid}
                     for lid, l in list(stt["open"].items())[-200:]],
            "settled": stt["settled"][-200:], "skips": stt["skips"][-100:],
            "counters": stt["counters"],
            "note": "verdict binds to forward_ledger.jsonl (#17); this feed "
                    "measures real-time capturability only"}


# ── streams / feed / boot — surgebot pattern ────────────────────────────────

SUB = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "activity", "type": "trades", "filters": ""},
    {"topic": "crypto_prices", "type": "*", "filters": ""}]})


def run_conn(tag, bot):
    backoff = 2
    while True:
        state = {"fresh": time.time()}

        def on_open(ws):
            ws.send(SUB)
            state["fresh"] = time.time()
            log(f"ws[{tag}]: connected")

            def ping():
                while ws.keep_running:
                    time.sleep(5)
                    try:
                        ws.send('{"action":"ping"}')
                    except Exception:
                        break
                    if time.time() - state["fresh"] > 20:
                        try:
                            ws.close()
                        except Exception:
                            pass
                        break
            threading.Thread(target=ping, daemon=True).start()

        def on_message(ws, raw):
            state["fresh"] = time.time()
            try:
                m = json.loads(raw)
            except Exception:
                return
            topic = m.get("topic")
            if topic == "activity" and m.get("type") == "trades":
                bot.trades_seen += 1
                try:
                    bot.on_trade(m.get("payload") or {})
                except Exception as e:
                    log(f"⚠ on_trade error: {e}")
            elif topic == "crypto_prices":
                try:
                    bot.on_tick(m.get("payload") or {})
                except Exception as e:
                    log(f"⚠ on_tick error: {e}")

        try:
            app = websocket.WebSocketApp(WS_URL, on_open=on_open,
                                         on_message=on_message)
            app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            log(f"ws[{tag}]: {str(e)[:60]}")
        time.sleep(backoff + (1 if tag == "b" else 0))
        backoff = min(backoff * 2, 30)
        if time.time() - state["fresh"] < 60:
            backoff = 2


def serve_feed(bot, port=8080):
    """Read-only public feed for the /oracle dashboard (paper data only —
    nothing here can place an order or mutate state). CORS-open."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] != "/feed":
                self.send_response(404)
                self.end_headers()
                return
            with bot.lock:
                body = json.dumps(bot.feed_dict()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def selftest():
    """Boot gate — hard exit on failure. Proves the _et/mktime identity on
    THIS box, the parsers, fair-value spots, the FAK walk, and the
    restart-containment invariant."""
    m = crypto_parse("Bitcoin Up or Down - July 22, 3PM-4PM ET")
    assert m and m["kind"] == "sprint" and m["sym"] == "btcusdt", m
    assert m["t0"] == calendar.timegm((2026, 7, 22, 19, 0, 0)), m["t0"]
    assert m["t1"] == calendar.timegm((2026, 7, 22, 20, 0, 0)), m["t1"]
    m = crypto_parse("Ethereum above 3,600 on July 22, 4PM ET")
    assert m and m["kind"] == "above" and m["k1"] == 3600.0, m
    assert m["t1"] == calendar.timegm((2026, 7, 22, 20, 0, 0)), m["t1"]
    m = crypto_parse("Will the price of Solana be between $150 and $170 "
                     "on July 25?")
    assert m and m["kind"] == "between" and m["k2"] == 170.0, m
    assert m["t1"] == calendar.timegm((2026, 7, 25, 16, 0, 0)), m["t1"]
    assert crypto_parse("Will Bitcoin dip to $110,000 in July?") is None
    assert phi(0) == 0.5
    mkt = {"kind": "above", "k1": 100.0, "k2": None, "t0": None, "t1": 1000.0}
    assert abs(fair_value(mkt, True, 100.0, 1e-4, 400.0) - 0.5) < 1e-9
    assert fair_value(mkt, True, 120.0, 2e-4, 400.0) > 0.99
    assert fair_value(mkt, False, 120.0, 2e-4, 400.0) < 0.01
    assert fair_value(mkt, True, 100.0, 1e-4, 970.0) is None       # tau<60
    assert fair_value(mkt, True, 100.0, 1e-4, 1000 - 13 * 3600) is None
    r = walk_asks([{"price": "0.60", "size": "200"},
                   {"price": "0.55", "size": "100"}], cap=0.62)
    # sizes are SHARES: 100sh@0.55 = $55, then 75sh@0.60 = $45 -> full $100
    assert r["filled"] and abs(r["usd"] - 100.0) < 1e-6, r
    assert r["levels"] == 2 and r["best_ask"] == 0.55 and not r["partial"], r
    assert 0.55 < r["vwap"] < 0.60 and abs(r["shares"] - 175.0) < 1e-6, r
    r = walk_asks([{"price": "0.55", "size": "10"}], cap=0.60)
    assert r["filled"] and r["partial"] and abs(r["usd"] - 5.5) < 1e-9, r
    r = walk_asks([{"price": "0.70", "size": "10"}], cap=0.60)
    assert not r["filled"] and r["best_ask"] == 0.70, r
    assert not walk_asks([], 0.5)["filled"]
    s = LiveTicks()
    p = 100.0
    for i in range(120):
        p *= math.exp(1e-4 if i % 2 == 0 else -1e-4)
        s.add(1000.0 + i, p, 0)
    v = s.vol_1s(1000.0 + 119)
    assert v and abs(v - 1e-4) < 2e-5, v
    assert WARMUP_S >= COOLDOWN_S      # restart-containment invariant
    log("selftest OK")


def main():
    selftest()
    bot = OracleBot()
    threading.Thread(target=serve_feed, args=(bot,), daemon=True).start()
    for _ in range(BOOK_WORKERS):
        threading.Thread(target=bot.book_worker, daemon=True).start()
    for _ in range(2):
        threading.Thread(target=bot.markout_worker, daemon=True).start()
    for tag in ("a", "b"):
        threading.Thread(target=run_conn, args=(tag, bot), daemon=True).start()
    n = 0
    while True:
        time.sleep(60)
        n += 1
        now = time.time()
        with bot.lock:
            for s in bot.ticks.values():
                s.trim(now)
            bot.last_trig = {a: t for a, t in bot.last_trig.items()
                             if now - t < COOLDOWN_S}
            bot.settle_provisional()
        if n % 2 == 0:
            bot.settle_clob_pass()
        if n % 5 == 0:
            with bot.lock:
                bot.persist()
        if now > DST_WARN_TS:
            log("⚠ ET_OFF=EDT expires at DST end 2026-11-01 — review parser")
        if now > YEAR_WARN_TS:
            log("⚠ YEAR=2026 rolls soon — parser dates will break")
        with bot.lock:
            c = bot.state["counters"]
            c["trades_seen"] = bot.trades_seen
            ticks = " ".join(f"{sym[:3]}:{len(s.ts)}"
                             for sym, s in bot.ticks.items())
            msg = (f"paper pnl {bot.state['pnl_realized']:+.2f} · "
                   f"open {len(bot.state['open'])} · ev {c['events']} "
                   f"att {c['attempts']} fill {c['fills']} "
                   f"crat {c['craters']} thr {c['throttle_skip']} · "
                   f"settle t{c['settle_tick']}/c{c['settle_clob']} · "
                   f"ticks {ticks}")
        log(msg)


if __name__ == "__main__":
    main()
