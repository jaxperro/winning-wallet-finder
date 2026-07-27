#!/usr/bin/env python3
"""leanbot — Study C Stage-2 (#26): maker inventory-leans at REAL execution.

PAPER ONLY. No keys, no orders, no bot imports — self-contained (baked
into the wwf-leanbot image). Study C (#22) PASSED its tape-scored window
(+$2.17/lean, hit .576, n=20,461, 6 days) but every entry was booked at
the lean-side LAST PRINT — an entry no real order can have. Study B died
of exactly that assumption (tape-positive, −$8.51/fill at a real book).
This box answers the only question that matters: when a screened
maker-sharp's inventory crosses the conviction line, is the lean side
still buyable near that print?

Two legs per lean, both recorded:
  observational  ask premium over the lean-side last print at trigger time
                 (median premium >= 8c over 3 days = the tape's entry was a
                 mirage — pre-registered kill-switch, no paper sample needed)
  paper          $100 FAK walk at the real ask, gated by the pre-declared
                 premium cap (+3c over the print; tighter than lagbot's 4c
                 because a lean carries no urgency — nothing is crashing)

THE VERDICT BINDS TO #26. Entries graded nightly against chain truth
(grade_lean.py). Any semantics-altering fix bumps SEM_VER and restarts
the shakedown clock.

FROZEN spec (from #22 + pre-declared; do not tune here):
  set        maker-sharps screened on strictly-prior tape, published
             nightly by maker_set.py (the box never screens itself)
  inventory  rolling net/gross USD per (wallet, token) from the
             orders_matched stream — inventory absorbed while quoting
  trigger    first crossing per (wallet, token, day) of
             |net|*px >= $150 AND |net|/gross >= 0.6, lean px in [0.05,0.95]
  band       follow arm only: lean size $150-500 (the #22 verdict arm)
  entry      $100 FAK walk at asks <= min(print + 0.03, 0.95)
             fee 3%*sh*min(p,1-p); MAX_PER_EVENT=2 concurrent
Live-only plumbing (never signal): 30-min warmup, worker pool + bounded
queue (throttle counted), map trims, registry-style settle passes.
"""
import json
import os
import queue
import ssl
import threading
import time
import urllib.request

import websocket

WS_URL = "wss://ws-live-data.polymarket.com"
CLOB = "https://clob.polymarket.com"
SET_URL = ("https://raw.githubusercontent.com/jaxperro/winning-wallet-finder/"
           "main/research/params/maker_set.json")
STATE = os.environ.get("LEAN_STATE", "/data/lean_state.json")
ATTEMPTS = os.environ.get("LEAN_ATTEMPTS", "/data/lean_attempts.jsonl")
SETTLES = os.environ.get("LEAN_SETTLES", "/data/lean_settles.jsonl")
SEM_VER = "n1"                    # bump on ANY semantics-altering change

# ── FROZEN — #22 params + pre-declared execution rule ──────────────────────
LEAN_USD_MIN = 150.0              # == maker_lean.LEAN_USD
LEAN_USD_MAX = 500.0              # the #22 follow arm's upper edge
NET_GROSS = 0.6
BAND = (0.05, 0.95)
PREMIUM_CAP = 0.03
STAKE = 100.0
FEE_RATE = 0.03
MAX_PER_EVENT = 2
# ── LIVE-ONLY plumbing ─────────────────────────────────────────────────────
WARMUP_S = 1800
SET_REFRESH_S = 3600
BOOK_WORKERS = 4
BOOK_QUEUE = 16
CLOB_PASS_CAP = 25
SETTLED_TRIM = 10000
SKIPS_TRIM = 500
INV_TRIM = 20000                  # bounded inventory map
MAP_IDLE_TRIM_S = 86400
TOP_LEVELS = 5
SSL_CTX = ssl._create_unverified_context()


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


def get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


def walk_asks(asks, cap, stake_usd=STAKE):
    """FAK against a CLOB ask list within cap; partial fills kept; top raw
    levels recorded for offline replays. asks arrive UNSORTED."""
    lv = sorted(((float(a["price"]), float(a["size"])) for a in asks or []))
    top = [[p, s] for p, s in lv[:TOP_LEVELS]]
    if not lv:
        return {"filled": False, "best_ask": None, "top": top}
    best = lv[0][0]
    spent = shares = 0.0
    levels = 0
    for pxx, sz in lv:
        if pxx > cap or spent >= stake_usd - 1e-9:
            break
        take = min(sz, (stake_usd - spent) / pxx)
        if take <= 0:
            break
        shares += take
        spent += take * pxx
        levels += 1
    if not shares:
        return {"filled": False, "best_ask": best, "top": top}
    return {"filled": True, "vwap": spent / shares, "shares": shares,
            "usd": spent, "levels": levels, "best_ask": best, "top": top,
            "partial": spent < stake_usd - 1e-9}


def fresh_state():
    return {
        "sem_ver": SEM_VER, "boot_ts": int(time.time()),
        "open": {}, "settled": [], "skips": [],
        "pnl_realized": 0.0,
        "counters": {"crossings": 0, "attempts": 0, "fills": 0,
                     "craters": 0, "premium_skips": 0, "throttle_skip": 0,
                     "book_fail": 0, "settled_total": 0, "settle_clob": 0,
                     "skip": {"warmup": 0, "not_in_set": 0, "band_usd": 0,
                              "band_px": 0, "no_print": 0, "event_cap": 0,
                              "no_complement": 0, "already_fired": 0}}}


class LeanBot:
    def __init__(self):
        self.lock = threading.Lock()
        self.boot = time.time()
        self.state = fresh_state()
        if os.path.exists(STATE):
            try:
                old = json.load(open(STATE))
                if old.get("sem_ver") == SEM_VER:
                    self.state = old
                    self.state["boot_ts"] = int(time.time())
                    log(f"state resumed: {len(old.get('open', {}))} open · "
                        f"{old['counters']['settled_total']} settled")
                else:
                    log(f"SEM_VER changed {old.get('sem_ver')}->{SEM_VER} — "
                        f"fresh state (shakedown clock restarts)")
            except Exception as e:
                log(f"state load failed ({e}) — fresh")
        self.sharps = set()
        self.set_meta = {}
        self.set_ts = 0
        self.inv = {}          # (wallet, asset) -> [net_usd, gross_usd, ts]
        self.fired = set()     # (wallet, asset, day) already triggered
        self.last_px = {}      # asset -> (ts, price)
        self.cond_assets = {}  # cond -> {asset, ...}
        self.tok_meta = {}     # asset -> (cond, event, title, outcome)
        self.book_q = queue.Queue(BOOK_QUEUE)

    # ── the screened set (never computed here — published by maker_set.py) ──
    def refresh_set(self):
        try:
            d = get_json(SET_URL, timeout=15)
            ws = {w.lower() for w in d.get("wallets") or []}
            if ws:
                with self.lock:
                    self.sharps = ws
                    self.set_meta = {"n": len(ws), "as_of": d.get("as_of"),
                                     "computed_at": d.get("computed_at")}
                    self.set_ts = time.time()
                log(f"maker set: {len(ws)} wallets (as-of "
                    f"{time.strftime('%F', time.gmtime(d.get('as_of') or 0))})")
        except Exception as e:
            log(f"⚠ maker set fetch failed: {e}")

    # ── stream ─────────────────────────────────────────────────────────────
    def on_trade(self, p):
        """activity/trades — price marks + token metadata (the orders_matched
        payload often carries empty conditionId/eventSlug; trades do not)."""
        a = p.get("asset")
        if not a:
            return
        try:
            px = float(p.get("price") or 0)
        except (TypeError, ValueError):
            return
        now = time.time()
        with self.lock:
            if 0 < px < 1:
                self.last_px[a] = (now, px)
            cond = p.get("conditionId") or ""
            if cond:
                self.cond_assets.setdefault(cond, set()).add(a)
                self.tok_meta[a] = (cond, p.get("eventSlug") or "",
                                    (p.get("title") or "")[:80],
                                    p.get("outcome") or "")

    def on_match(self, p):
        """activity/orders_matched — the MAKER side of every match. Inventory
        absorbed while quoting, which is the #22 signal substrate."""
        w = (p.get("proxyWallet") or "").lower()
        a = p.get("asset")
        if not w or not a:
            return
        try:
            px = float(p.get("price") or 0)
            sz = float(p.get("size") or 0)
        except (TypeError, ValueError):
            return
        if px <= 0 or sz <= 0:
            return
        now = time.time()
        usd = px * sz
        with self.lock:
            cond = p.get("conditionId") or ""
            if cond:
                self.cond_assets.setdefault(cond, set()).add(a)
                self.tok_meta.setdefault(a, (cond, p.get("eventSlug") or "",
                                             (p.get("title") or "")[:80],
                                             p.get("outcome") or ""))
            if w not in self.sharps:
                self.state["counters"]["skip"]["not_in_set"] += 1
                return
            key = (w, a)
            st = self.inv.get(key) or [0.0, 0.0, now]
            st[0] += usd if p.get("side") == "BUY" else -usd
            st[1] += usd
            st[2] = now
            self.inv[key] = st
            if len(self.inv) > INV_TRIM:
                for k in sorted(self.inv, key=lambda k: self.inv[k][2])[:2000]:
                    del self.inv[k]
            if time.time() - self.boot < WARMUP_S:
                self.state["counters"]["skip"]["warmup"] += 1
                return
            self._maybe_fire(w, a, st, px, now)

    def _maybe_fire(self, w, a, st, px, now):   # under lock
        net, gross = st[0], st[1]
        if gross < 1e-9:
            return
        lean_usd = abs(net)
        if lean_usd < LEAN_USD_MIN or abs(net) / gross < NET_GROSS:
            return
        day = int(now // 86400)
        if (w, a, day) in self.fired:
            self.state["counters"]["skip"]["already_fired"] += 1
            return
        self.state["counters"]["crossings"] += 1
        self.fired.add((w, a, day))
        if lean_usd >= LEAN_USD_MAX:            # follow arm is $150-500
            self.state["counters"]["skip"]["band_usd"] += 1
            return
        # lean side: net>0 = they are accumulating THIS token; net<0 = they
        # are net short it, so the lean is the complement token
        if net > 0:
            target, lean_px = a, px
        else:
            cond = (self.tok_meta.get(a) or ("",))[0]
            sibs = [x for x in self.cond_assets.get(cond, set()) if x != a]
            if len(sibs) != 1:
                self.state["counters"]["skip"]["no_complement"] += 1
                return
            target = sibs[0]
            lean_px = 1 - px
        lp = self.last_px.get(target)
        if not lp or now - lp[0] > 3600:
            self.state["counters"]["skip"]["no_print"] += 1
            return
        print_px = lp[1]
        if not (BAND[0] <= print_px <= BAND[1]):
            self.state["counters"]["skip"]["band_px"] += 1
            return
        cond, event, title, outcome = self.tok_meta.get(
            target, ("", "", "", ""))
        if event and sum(1 for l in self.state["open"].values()
                         if l.get("event") == event) >= MAX_PER_EVENT:
            self.state["counters"]["skip"]["event_cap"] += 1
            return
        job = {"wallet": w, "src_asset": a, "asset": target, "cond": cond,
               "event": event, "title": title, "outcome": outcome,
               "lean_usd": round(lean_usd, 2), "net": round(net, 2),
               "gross": round(gross, 2), "side": 1 if net > 0 else -1,
               "print": round(print_px, 4), "lean_px": round(lean_px, 4),
               "ts": round(now, 3)}
        try:
            self.book_q.put_nowait(job)
            self.state["counters"]["attempts"] += 1
        except queue.Full:
            self.state["counters"]["throttle_skip"] += 1
            self._skip(job, "throttle", None)
            self.log_attempt({**job, "filled": False, "why": "throttle"})

    # ── execution (workers; network OUT of the lock) ───────────────────────
    def book_worker(self):
        while True:
            job = self.book_q.get()
            try:
                self.attempt(job)
            except Exception as e:
                log(f"⚠ attempt error: {e}")
                with self.lock:
                    self.state["counters"]["book_fail"] += 1

    def attempt(self, job):
        t0 = time.time()
        try:
            bk = get_json(f"{CLOB}/book?token_id={job['asset']}")
        except Exception:
            with self.lock:
                self.state["counters"]["book_fail"] += 1
            return
        lat_ms = int((time.time() - t0) * 1000)
        cap = min(job["print"] + PREMIUM_CAP, BAND[1])
        r = walk_asks(bk.get("asks"), cap)
        premium = (r["best_ask"] - job["print"]) if r["best_ask"] else None
        rec = {**job, "latency_ms": lat_ms, "best_ask": r["best_ask"],
               "premium": round(premium, 4) if premium is not None else None,
               "top": r["top"], "cap": round(cap, 4), "filled": r["filled"]}
        with self.lock:
            c = self.state["counters"]
            if not r["filled"]:
                why = "crater" if r["best_ask"] is None else "premium"
                c["craters" if why == "crater" else "premium_skips"] += 1
                rec["why"] = why
                self._skip(job, why, r["best_ask"])
                log(f"{why.upper()} {job['title'][:34]} print "
                    f"{job['print']:.2f} ask "
                    f"{r['best_ask'] if r['best_ask'] is not None else '—'} "
                    f"(lean ${job['lean_usd']:.0f}, {lat_ms}ms)")
                self.persist()
            else:
                fee = FEE_RATE * r["shares"] * min(r["vwap"], 1 - r["vwap"])
                lot = {**job, "price": round(r["vwap"], 5),
                       "best_ask": r["best_ask"], "premium": rec["premium"],
                       "shares": round(r["shares"], 4),
                       "cost": round(r["usd"], 2), "fee": round(fee, 4),
                       "levels": r["levels"], "partial": r["partial"],
                       "latency_ms": lat_ms}
                self.state["open"][f"{job['asset']}:{job['ts']}"] = lot
                c["fills"] += 1
                rec.update({"price": lot["price"], "shares": lot["shares"],
                            "usd": lot["cost"], "fee": lot["fee"],
                            "partial": lot["partial"]})
                log(f"FILL {job['title'][:34]} @ {lot['price']:.3f} "
                    f"(print {job['print']:.2f} prem "
                    f"{rec['premium'] if rec['premium'] is not None else 0:+.3f}"
                    f", lean ${job['lean_usd']:.0f}, ${lot['cost']:.2f}, "
                    f"{lat_ms}ms)")
                self.persist()
        self.log_attempt(rec)

    def log_attempt(self, rec):
        try:
            with open(ATTEMPTS, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            log(f"⚠ attempts log write failed: {e}")

    def _skip(self, job, why, ba):     # under lock
        self.state["skips"].append(
            {"ts": int(job["ts"]), "asset": job["asset"], "why": why,
             "print": job["print"], "ba": ba, "title": job["title"],
             "lean_usd": job["lean_usd"]})
        del self.state["skips"][:-SKIPS_TRIM]

    # ── settles: CLOB backstop -> nightly chain truth ──────────────────────
    def settle_clob_pass(self):
        with self.lock:
            now = time.time()
            due = list(self.state["open"].keys())
            due.sort(key=lambda lid: self.state["open"][lid].get("clob_ck", 0))
            due = due[:CLOB_PASS_CAP]
            snap = {}
            for lid in due:
                lot = self.state["open"][lid]
                lot["clob_ck"] = now
                snap[lid] = (lot.get("cond"), lot["asset"])
        verdicts = {}
        for lid, (cond, asset) in snap.items():
            if not cond:
                continue
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
                    self._settle(lid, lot, pay)
                    n += 1
            if n:
                self.persist()

    def _settle(self, lid, lot, pay):  # under lock
        pnl = round(lot["shares"] * pay - lot["cost"] - lot["fee"], 2)
        self.state["pnl_realized"] = round(self.state["pnl_realized"] + pnl, 2)
        c = self.state["counters"]
        c["settled_total"] += 1
        c["settle_clob"] += 1
        rec = {**lot, "payout": pay, "provisional": True,
               "settled_ts": int(time.time()), "pnl": pnl}
        self.state["settled"].append(rec)
        del self.state["settled"][:-SETTLED_TRIM]
        del self.state["open"][lid]
        try:
            with open(SETTLES, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            log(f"⚠ settles log write failed: {e}")
        word = "WON" if pay == 1.0 else "refund" if pay == 0.5 else "lost"
        log(f"SETTLE {word} {lot['title'][:34]} {pnl:+.2f} · "
            f"realized {self.state['pnl_realized']:+.2f}")

    # ── plumbing ───────────────────────────────────────────────────────────
    def persist(self):
        tmp = STATE + ".tmp"
        json.dump(self.state, open(tmp, "w"))
        os.replace(tmp, STATE)

    def trim_maps(self):               # under lock (minute loop)
        now = time.time()
        if len(self.last_px) > 400000:
            self.last_px = {a: v for a, v in self.last_px.items()
                            if now - v[0] < MAP_IDLE_TRIM_S}
        day = int(now // 86400)
        self.fired = {k for k in self.fired if k[2] >= day - 1}

    def feed_dict(self):               # under lock
        stt = self.state
        return {
            "mode": "paper", "study": "lean (Study C stage-2)",
            "sem_ver": SEM_VER, "updated": int(time.time()),
            "boot_ts": stt["boot_ts"],
            "warmup_left_s": max(0, int(self.boot + WARMUP_S - time.time())),
            "frozen": {"lean_usd": [LEAN_USD_MIN, LEAN_USD_MAX],
                       "net_gross": NET_GROSS, "premium_cap": PREMIUM_CAP,
                       "stake": STAKE, "band": BAND, "fee_rate": FEE_RATE,
                       "max_per_event": MAX_PER_EVENT},
            "set": self.set_meta, "set_age_s": int(time.time() - self.set_ts),
            "tracked_inventories": len(self.inv),
            "counters": stt["counters"], "pnl_realized": stt["pnl_realized"],
            "open_n": len(stt["open"]),
            "open": [{**l, "id": lid}
                     for lid, l in list(stt["open"].items())[-200:]],
            "settled": stt["settled"][-200:], "skips": stt["skips"][-100:],
            "note": "verdict binds to #26; median ask-premium >= 8c over 3 "
                    "days is the observational kill-switch (graded nightly "
                    "from the attempts stream)"}


SUB = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "activity", "type": "trades", "filters": ""},
    {"topic": "activity", "type": "orders_matched", "filters": ""}]})


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
            if m.get("topic") != "activity":
                return
            try:
                if m.get("type") == "trades":
                    bot.on_trade(m.get("payload") or {})
                elif m.get("type") == "orders_matched":
                    bot.on_match(m.get("payload") or {})
            except Exception as e:
                log(f"⚠ on_message error: {e}")

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
    """Boot gate — hard exit on failure."""
    bot = LeanBot.__new__(LeanBot)
    bot.state = fresh_state()
    bot.lock = threading.Lock()
    bot.boot = time.time() - WARMUP_S - 1
    bot.sharps = {"0xsharp"}
    bot.set_meta, bot.set_ts = {}, time.time()
    bot.inv, bot.fired = {}, set()
    bot.last_px, bot.cond_assets, bot.tok_meta = {}, {}, {}
    bot.book_q = queue.Queue(4)
    jobs = []
    bot.book_q.put_nowait = lambda j: jobs.append(j)   # capture, no network

    def mk(w, a, side, px, sz, cond="c1"):
        return {"proxyWallet": w, "asset": a, "side": side, "price": px,
                "size": sz, "conditionId": cond, "eventSlug": "ev1",
                "title": "T", "outcome": "X"}
    bot.on_trade({"asset": "tokA", "price": 0.30, "conditionId": "c1",
                  "eventSlug": "ev1", "title": "T", "outcome": "X"})
    bot.on_trade({"asset": "tokB", "price": 0.70, "conditionId": "c1",
                  "eventSlug": "ev1", "title": "T", "outcome": "Y"})
    # not in set -> ignored
    bot.on_match(mk("0xrando", "tokA", "BUY", 0.30, 2000))
    assert not jobs, "non-set wallet fired"
    # in set, sub-threshold ($60) -> no fire
    bot.on_match(mk("0xsharp", "tokA", "BUY", 0.30, 200))
    assert not jobs, "sub-threshold fired"
    # crosses $150 one-sided -> fires on tokA
    bot.on_match(mk("0xsharp", "tokA", "BUY", 0.30, 400))
    assert len(jobs) == 1, f"expected 1 job, got {len(jobs)}"
    assert jobs[0]["asset"] == "tokA" and jobs[0]["side"] == 1
    assert jobs[0]["print"] == 0.30, jobs[0]["print"]
    # same (wallet, token, day) cannot fire twice
    bot.on_match(mk("0xsharp", "tokA", "BUY", 0.30, 400))
    assert len(jobs) == 1, "re-fired same day"
    # net-short lean routes to the complement token
    bot.on_match(mk("0xsharp", "tokB", "SELL", 0.70, 400))
    assert len(jobs) == 2 and jobs[1]["asset"] == "tokA", jobs[1]
    assert jobs[1]["side"] == -1
    # over the $500 band edge -> counted, no job
    bot.on_match(mk("0xsharp", "tokZ", "BUY", 0.50, 4000))
    assert len(jobs) == 2, "over-band fired"
    assert bot.state["counters"]["skip"]["band_usd"] == 1
    # walk_asks: cap respected, partials kept
    r = walk_asks([{"price": 0.33, "size": 100}, {"price": 0.40, "size": 900}],
                  0.33)
    assert r["filled"] and abs(r["usd"] - 33.0) < 1e-6 and r["partial"], r
    r2 = walk_asks([{"price": 0.99, "size": 10}], 0.33)
    assert not r2["filled"] and r2["best_ask"] == 0.99, r2
    assert WARMUP_S >= 60, "warmup must exceed a burst window"
    log("selftest OK")


def main():
    selftest()
    bot = LeanBot()
    bot.refresh_set()
    if not bot.sharps:
        log("⚠ no maker set yet — idling until the nightly publishes one")
    threading.Thread(target=serve_feed, args=(bot,), daemon=True).start()
    for i in range(BOOK_WORKERS):
        threading.Thread(target=bot.book_worker, daemon=True).start()
    for tag in ("a", "b"):
        threading.Thread(target=run_conn, args=(tag, bot), daemon=True).start()
    tick = 0
    while True:
        time.sleep(60)
        tick += 1
        try:
            if time.time() - bot.set_ts > SET_REFRESH_S:
                bot.refresh_set()
            if tick % 2 == 0:
                bot.settle_clob_pass()
            with bot.lock:
                bot.trim_maps()
                if tick % 5 == 0:
                    bot.persist()
                c = bot.state["counters"]
                log(f"paper pnl {bot.state['pnl_realized']:+.2f} · open "
                    f"{len(bot.state['open'])} · cross {c['crossings']} "
                    f"att {c['attempts']} fill {c['fills']} "
                    f"prem-skip {c['premium_skips']} crat {c['craters']} · "
                    f"inv {len(bot.inv)} · set {len(bot.sharps)} · "
                    f"settled {c['settled_total']}")
        except Exception as e:
            log(f"⚠ loop error: {e}")


if __name__ == "__main__":
    main()
