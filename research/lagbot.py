#!/usr/bin/env python3
"""lagbot — T9 Stage-2: same-event lead-lag at REAL execution.

PAPER ONLY. No keys, no orders, no bot imports — self-contained (baked
into the wwf-lagbot image). Stage-1 (research/event_leadlag.py) read
+$9.73/$100 buying name-matched siblings at their STALE LAST PRINT after
a leader burst — an entry no real order can have. This box answers the
only question that matters: when the leader moves, is the sibling's
STANDING ASK still stale (edge real) or already repriced (mirage)?

Two legs per episode, both recorded:
  observational  ask premium over the sibling's stale last print at burst
                 time (median premium >= 8c over 3 days = mirage,
                 pre-registered kill-switch — no paper sample needed)
  paper          $100 FAK walk at the real ask, gated by the pre-declared
                 premium cap (+4c over stale — derived from Stage-1's
                 ~5c/share measured edge, frozen here, not tuned)

Timing realism is the instrument: RTDS burst detection ~1s + book read
~300ms = the reaction a real bot would have. THE VERDICT BINDS TO THIS
STUDY'S PRE-REGISTRATION ISSUE; entries graded nightly against chain
truth (grade_lag.py). Any semantics-altering fix bumps SEM_VER and
restarts the shakedown clock.

FROZEN spec (from Stage-1 + pre-declared; do not tune here):
  burst      leader print moves >= 10c within 120s · leader = most-printed
             asset of its (event, outcome-name) group · cooldown 600s/event
  siblings   <= 2 name-matched (same lowercased outcome; yes/no/over/under
             excluded) · down-moves target the sibling's complement token
  entry      $100 FAK walk at asks <= min(stale_print + 0.04, 0.95);
             entry-side price band [0.05, 0.95]; fee 3%*sh*min(p,1-p)
Live-only plumbing (never signal): 30-min warmup, worker pool + bounded
queue (throttle counted), map/print trims, registry-style settle passes.
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
STATE = os.environ.get("LAG_STATE", "/data/lag_state.json")
ATTEMPTS = os.environ.get("LAG_ATTEMPTS", "/data/lag_attempts.jsonl")
SETTLES = os.environ.get("LAG_SETTLES", "/data/lag_settles.jsonl")
SEM_VER = "l1"                    # bump on ANY semantics-altering change

# ── FROZEN — Stage-1 params + pre-declared execution rule ──────────────────
MOVE_C = 0.10
MOVE_WIN = 120
COOLDOWN_S = 600                  # per event
MAX_SIBS = 2
PREMIUM_CAP = 0.04                # ask <= stale + 4c (from Stage-1 edge)
STAKE = 100.0
BAND = (0.05, 0.95)
FEE_RATE = 0.03
SKIP_OUTCOMES = {"yes", "no", "over", "under", ""}
# ── LIVE-ONLY plumbing ─────────────────────────────────────────────────────
WARMUP_S = 1800
BOOK_WORKERS = 4
BOOK_QUEUE = 16
CLOB_PASS_CAP = 25
SETTLED_TRIM = 10000
SKIPS_TRIM = 500
MAP_IDLE_TRIM_S = 86400           # drop groups quiet > 24h
TOP_LEVELS = 5
BID_LEVELS = 3
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
        "counters": {"bursts": 0, "sib_evals": 0, "attempts": 0, "fills": 0,
                     "craters": 0, "premium_skips": 0, "throttle_skip": 0,
                     "book_fail": 0, "settled_total": 0, "settle_clob": 0,
                     "skip": {"warmup": 0, "no_sibs": 0, "no_stale": 0,
                              "band": 0, "no_complement": 0}}}


class LagBot:
    def __init__(self):
        self.state = fresh_state()
        if os.path.exists(STATE):
            try:
                loaded = json.load(open(STATE))
                if loaded.get("sem_ver") != SEM_VER:
                    log(f"⚠ SEM_VER {loaded.get('sem_ver')} -> {SEM_VER} — "
                        f"shakedown clock restarts here")
                    loaded["sem_ver"] = SEM_VER
                self.state = loaded
                log(f"resumed: pnl {self.state['pnl_realized']:+.2f} · "
                    f"{len(self.state['open'])} open")
            except Exception as e:
                log(f"⚠ state load failed ({e}) — fresh book")
        # live maps (rebuilt from the stream; not persisted — warmup covers)
        self.groups = {}     # (event, outc) -> {asset: [print_count, last_ts]}
        self.cond_tok = {}   # cond -> {outc: asset}
        self.asset_info = {} # asset -> (event, outc, cond, title)
        self.last_px = {}    # asset -> (ts, px)
        self.lead_hist = {}  # leader asset -> [(ts, px)] trailing MOVE_WIN
        self.last_burst = {} # event -> ts
        self.boot = time.time()
        self.lock = threading.Lock()
        self.book_q = queue.Queue(BOOK_QUEUE)

    # ── stream ─────────────────────────────────────────────────────────────
    def on_trade(self, p):
        ev = p.get("eventSlug") or ""
        outc = (p.get("outcome") or "").lower()
        asset = str(p.get("asset") or "")
        cond = p.get("conditionId") or ""
        try:
            px = float(p.get("price") or 0)
        except (TypeError, ValueError):
            return
        if not (ev and asset and cond and 0 < px < 1):
            return
        now = time.time()
        with self.lock:
            self.last_px[asset] = (now, px)
            self.cond_tok.setdefault(cond, {})[outc] = asset
            if outc in SKIP_OUTCOMES:
                return
            g = self.groups.setdefault((ev, outc), {})
            st = g.setdefault(asset, [0, now])
            st[0] += 1
            st[1] = now
            if asset not in self.asset_info:
                self.asset_info[asset] = (ev, outc, cond,
                                          (p.get("title") or "")[:60])
            # leader = most-printed asset of the group
            leader = max(g.items(), key=lambda kv: kv[1][0])[0]
            if asset != leader:
                return
            hist = self.lead_hist.setdefault(asset, [])
            hist.append((now, px))
            while hist and hist[0][0] < now - MOVE_WIN:
                hist.pop(0)
            base = hist[0][1]
            mv = px - base
            c = self.state["counters"]
            if abs(mv) < MOVE_C:
                return
            if now - self.boot < WARMUP_S:
                c["skip"]["warmup"] += 1
                return
            if now - self.last_burst.get(ev, 0) < COOLDOWN_S:
                return
            self.last_burst[ev] = now
            c["bursts"] += 1
            sgn = 1 if mv > 0 else -1
            sibs = [a for a in g if a != asset][:MAX_SIBS]
            if not sibs:
                c["skip"]["no_sibs"] += 1
                return
            for sib in sibs:
                self._queue_sibling(ev, outc, asset, sib, mv, sgn, px, now)

    def _queue_sibling(self, ev, outc, leader, sib, mv, sgn, lead_px, now):
        """Resolve the tradable target (sibling side in the leader's
        direction) + its stale reference, then queue the book read."""
        c = self.state["counters"]
        c["sib_evals"] += 1
        info = self.asset_info.get(sib)
        cond = info[2] if info else None
        title = info[3] if info else ""
        if sgn > 0:
            target, stale_src = sib, sib
        else:
            # down-move: buy the sibling market's complement token
            toks = self.cond_tok.get(cond or "", {})
            others = [a for o, a in toks.items() if a != sib]
            if not others:
                c["skip"]["no_complement"] += 1
                return
            target = others[0]
            stale_src = target
        lp = self.last_px.get(stale_src)
        if lp is None:
            # complement never printed: derive stale from the sibling side
            lp_s = self.last_px.get(sib)
            if lp_s is None:
                c["skip"]["no_stale"] += 1
                return
            stale = 1 - lp_s[1]
        else:
            stale = lp[1]
        if not (BAND[0] <= stale <= BAND[1]):
            c["skip"]["band"] += 1
            return
        job = {"event": ev, "outc": outc, "leader": leader,
               "lead_mv": round(mv, 3), "lead_px": round(lead_px, 3),
               "sgn": sgn, "asset": target, "cond": cond,
               "title": title, "stale": round(stale, 4),
               "ts": round(now, 3)}
        try:
            self.book_q.put_nowait(job)
            c["attempts"] += 1
        except queue.Full:
            c["throttle_skip"] += 1
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

    def attempt(self, job):
        t_req = time.time()
        try:
            book = get_json(f"{CLOB}/book?token_id={job['asset']}")
        except Exception:
            with self.lock:
                self.state["counters"]["book_fail"] += 1
                self._skip(job, "book_fail", None)
            self.log_attempt({**job, "filled": False, "why": "book_fail"})
            return
        lat_ms = int((time.time() - t_req) * 1000)
        cap = min(job["stale"] + PREMIUM_CAP, 0.95)
        r = walk_asks(book.get("asks"), cap)
        bid_top = sorted(((float(b["price"]), float(b["size"]))
                          for b in book.get("bids") or []),
                         reverse=True)[:BID_LEVELS]
        premium = (round(r["best_ask"] - job["stale"], 4)
                   if r["best_ask"] is not None else None)
        rec = {**job, "cap": round(cap, 4), "latency_ms": lat_ms,
               "premium": premium, "top": r["top"],
               "bid_top": [[p, s] for p, s in bid_top],
               "best_ask": r["best_ask"], "filled": r["filled"]}
        with self.lock:
            c = self.state["counters"]
            if not r["filled"]:
                if r["best_ask"] is not None and r["best_ask"] > cap:
                    c["premium_skips"] += 1
                    why = "premium"
                else:
                    c["craters"] += 1
                    why = "crater"
                self._skip(job, why, r["best_ask"])
                ba = "—" if r["best_ask"] is None else f"{r['best_ask']:.3f}"
                log(f"{why.upper()} {job['title'][:36]} stale "
                    f"{job['stale']:.2f} ask {ba} "
                    f"(lead {job['lead_mv']:+.2f}, {lat_ms}ms)")
                self.persist()
            else:
                fee = FEE_RATE * r["shares"] * min(r["vwap"], 1 - r["vwap"])
                lot = {**job, "price": round(r["vwap"], 5),
                       "best_ask": r["best_ask"], "premium": premium,
                       "shares": round(r["shares"], 4),
                       "cost": round(r["usd"], 2), "fee": round(fee, 4),
                       "levels": r["levels"], "partial": r["partial"],
                       "latency_ms": lat_ms}
                self.state["open"][f"{job['asset']}:{job['ts']}"] = lot
                c["fills"] += 1
                rec.update({"price": lot["price"], "shares": lot["shares"],
                            "usd": lot["cost"], "fee": lot["fee"],
                            "partial": lot["partial"]})
                log(f"FILL {job['title'][:36]} @ {lot['price']:.3f} "
                    f"(stale {job['stale']:.2f} prem "
                    f"{premium if premium is not None else 0:+.3f}, "
                    f"lead {job['lead_mv']:+.2f}, ${lot['cost']:.2f}, "
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
             "stale": job["stale"], "ba": ba, "title": job["title"]})
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
        dead = [k for k, g in self.groups.items()
                if all(now - st[1] > MAP_IDLE_TRIM_S for st in g.values())]
        for k in dead:
            del self.groups[k]
        if len(self.last_px) > 400000:
            self.last_px = {a: v for a, v in self.last_px.items()
                            if now - v[0] < MAP_IDLE_TRIM_S}

    def feed_dict(self):               # under lock
        stt = self.state
        return {
            "mode": "paper", "study": "leadlag (T9 stage-2)",
            "sem_ver": SEM_VER, "updated": int(time.time()),
            "boot_ts": stt["boot_ts"],
            "warmup_left_s": max(0, int(self.boot + WARMUP_S - time.time())),
            "frozen": {"move_c": MOVE_C, "move_win_s": MOVE_WIN,
                       "cooldown_s": COOLDOWN_S, "max_sibs": MAX_SIBS,
                       "premium_cap": PREMIUM_CAP, "stake": STAKE,
                       "band": BAND, "fee_rate": FEE_RATE},
            "groups": len(self.groups),
            "counters": stt["counters"], "pnl_realized": stt["pnl_realized"],
            "open_n": len(stt["open"]),
            "open": [{**l, "id": lid}
                     for lid, l in list(stt["open"].items())[-200:]],
            "settled": stt["settled"][-200:], "skips": stt["skips"][-100:],
            "note": "verdict binds to the Study D pre-registration; median "
                    "ask-premium >= 8c over 3 days is the observational "
                    "kill-switch (graded nightly from the attempts stream)"}


SUB = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "activity", "type": "trades", "filters": ""}]})


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
            if m.get("topic") == "activity" and m.get("type") == "trades":
                try:
                    bot.on_trade(m.get("payload") or {})
                except Exception as e:
                    log(f"⚠ on_trade error: {e}")

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
    bot = LagBot.__new__(LagBot)
    bot.state = fresh_state()
    bot.groups, bot.cond_tok, bot.asset_info = {}, {}, {}
    bot.last_px, bot.lead_hist, bot.last_burst = {}, {}, {}
    bot.boot = time.time() - WARMUP_S - 1
    bot.lock = threading.Lock()
    bot.book_q = queue.Queue(2)
    jobs = []
    bot._queue_sibling = lambda *a: jobs.append(a)   # capture
    mk = lambda a, o, px, ev="ev1", cond=None: {     # noqa: E731
        "eventSlug": ev, "outcome": o, "asset": a,
        "conditionId": cond or f"c-{a}", "price": px, "title": f"T{a}"}
    # build a group: leader la (3 prints), sibling sb (1 print)
    bot.on_trade(mk("la", "TeamX", 0.50))
    bot.on_trade(mk("sb", "TeamX", 0.48))
    bot.on_trade(mk("la", "TeamX", 0.52))
    bot.on_trade(mk("la", "TeamX", 0.63))            # +13c within window
    assert len(jobs) == 1 and jobs[0][3] == "sb", jobs   # burst -> sibling
    assert bot.state["counters"]["bursts"] == 1
    bot.on_trade(mk("la", "TeamX", 0.75))            # cooldown blocks
    assert bot.state["counters"]["bursts"] == 1
    r = walk_asks([{"price": "0.52", "size": "300"}], cap=0.52)
    assert r["filled"] and not r["partial"] and r["best_ask"] == 0.52
    r = walk_asks([{"price": "0.56", "size": "300"}], cap=0.52)
    assert not r["filled"] and r["best_ask"] == 0.56     # premium skip case
    assert walk_asks([], 0.5)["filled"] is False
    # complement resolution on down-moves
    bot2 = LagBot.__new__(LagBot)
    bot2.state = fresh_state()
    bot2.cond_tok = {"cX": {"teamy": "tk_yes", "teamz": "tk_no"}}
    bot2.asset_info = {"tk_yes": ("ev", "teamy", "cX", "t")}
    bot2.last_px = {"tk_no": (0, 0.40)}
    out = []
    bot2.book_q = type("Q", (), {"put_nowait": lambda s, j: out.append(j)})()
    bot2._skip = lambda *a: None
    bot2.log_attempt = lambda *a: None
    bot2._queue_sibling("ev", "teamy", "lead", "tk_yes", -0.12, -1, 0.3,
                        time.time())
    assert out and out[0]["asset"] == "tk_no" and out[0]["stale"] == 0.40, out
    log("selftest OK")


def main():
    selftest()
    bot = LagBot()
    threading.Thread(target=serve_feed, args=(bot,), daemon=True).start()
    for _ in range(BOOK_WORKERS):
        threading.Thread(target=bot.book_worker, daemon=True).start()
    for tag in ("a", "b"):
        threading.Thread(target=run_conn, args=(tag, bot), daemon=True).start()
    n = 0
    while True:
        time.sleep(60)
        n += 1
        with bot.lock:
            bot.trim_maps()
        if n % 2 == 0:
            bot.settle_clob_pass()
        if n % 5 == 0:
            with bot.lock:
                bot.persist()
        with bot.lock:
            c = bot.state["counters"]
            msg = (f"paper pnl {bot.state['pnl_realized']:+.2f} · open "
                   f"{len(bot.state['open'])} · bursts {c['bursts']} att "
                   f"{c['attempts']} fill {c['fills']} prem-skip "
                   f"{c['premium_skips']} crat {c['craters']} · groups "
                   f"{len(bot.groups)} · settled {c['settled_total']}")
        log(msg)


if __name__ == "__main__":
    main()
