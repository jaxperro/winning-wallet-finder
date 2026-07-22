#!/usr/bin/env python3
"""surgebot A2 — Study A's surge signal as a real-time MEASUREMENT harness.

PAPER ONLY. No keys, no orders, no bot imports — self-contained (baked into
the wwf-surgebot image, must not depend on the repo to boot).

WHY A2 (2026-07-22 post-mortem of the v1 paper book): v1 rehearsed the
$100/5% deployment spec physically — cash-gating meant it attempted ~2% of
triggers, and that subsample was ADVERSELY SELECTED (its 102 chain-graded
fills: −$9/fill at $100-scale, while the sim scored ALL of the same day's
triggers at +$41/fill; the sim run on v1's own triggers agreed with v1's
losses within ~$2 — execution physics validated, sampling condemned).
A cash-gated book cannot measure the signal AND rehearse the spec at once.

A2 splits them: this box attempts EVERY cooldown-passed trigger (flat $100
paper FAK walking the live asks inside p_ref*1.05, partial fills kept) and
appends one line per attempt — including top-5 raw ask levels — to
/data/surge_attempts.jsonl. Any bankroll spec (the $100/5% book, other
banks, caps, tighter slip) is then replayed OFFLINE from that stream
(research/surge_book_replay.py -> research/surge_book.json, nightly).

FROZEN signal semantics (params/study_flow.json — do not tune here):
  informed set   top-150 (fetched from the repo, regenerated nightly)
  trigger        net informed flow >= $300 in 60s, one market
  band           entry price 0.10-0.90 · niches sports+esports
  cooldown       900s per token, set at trigger time
Unchanged from v1: signal code path is verbatim. Changed in A2 (execution/
accounting only): every-trigger attempts (no cash gate, no event cap, no
skip-if-open — those live in the offline replay), $100 book-walk fills
(ledger-comparable; v1 was best-ask at ~$5), attempts log, multi-lot per
asset. THE #16 VERDICT BINDS TO forward_ledger.jsonl ONLY; this harness
measures capturability. Settles are provisional (CLOB closed/winner) until
the nightly CTF payout re-grade (grade_surge.py -> surge_meas_ledger.jsonl;
v1's surge_paper_ledger.jsonl is closed and untouched, as is its state at
/data/surge_state.json). Any semantics-altering fix bumps SEM_VER and
restarts the shakedown clock.
"""
import json
import os
import queue
import re
import ssl
import threading
import time
import urllib.request

import websocket

WS_URL = "wss://ws-live-data.polymarket.com"
CLOB = "https://clob.polymarket.com"
SET_URL = ("https://raw.githubusercontent.com/jaxperro/winning-wallet-finder/"
           "main/research/params/informed_set.json")
STATE = os.environ.get("SURGE_STATE", "/data/surge2_state.json")
ATTEMPTS = os.environ.get("SURGE_ATTEMPTS", "/data/surge_attempts.jsonl")
SEM_VER = "a2"                    # bump on ANY semantics-altering change

# ── FROZEN — must equal params/study_flow.json / forward.py ────────────────
FLOW_USD = 300.0
WINDOW_S = 60
BAND = (0.10, 0.90)
COOLDOWN_S = 900
NICHES = {"sports", "esports"}
STAKE = 100.0                     # ledger stake (sf.STAKE) — walk the book
FEE_RATE = 0.03
SLIP_CAP = 0.05
# ── LIVE-ONLY (execution/plumbing, never signal) ───────────────────────────
BOOK_WORKERS = 4
BOOK_QUEUE = 16                   # overflow => throttle (counted)
CLOB_PASS_CAP = 40                # settle GETs per pass, round-robin
SETTLED_TRIM = 10000              # nightly ledger is the durable record
SKIPS_TRIM = 500
TOP_LEVELS = 5                    # raw ask levels recorded per attempt

NICHE_PATTERNS = [
    ("esports", ["lol:", "dota", "cs2", "csgo", "valorant", "esports",
                 "bilibili", "map ", "game 1", "game 2", "game 3"]),
    ("tennis", ["tennis", "atp", "wta", "wimbledon", "set winner"]),
    ("sports", [" vs. ", " vs ", " @ ", "mlb", "nba", "nhl", "ufc",
                "world cup", "f1", "grand prix", "fifa"]),
]
SSL_CTX = ssl._create_unverified_context()


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


def niche(title):
    t = (title or "").lower()
    for label, pats in NICHE_PATTERNS:
        if any(p in t for p in pats):
            return label
    return "other"


def event_key(slug):
    m = re.match(r"(.*?\d{4}-\d{2}-\d{2})", slug or "")
    return m.group(1) if m else (slug or None)


def get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


def iso_ts(s):
    """CLOB end_date_iso -> epoch (None on any parse trouble)."""
    try:
        import calendar
        return calendar.timegm(time.strptime(
            s.replace("+00:00", "Z")[:20], "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


def walk_asks(asks, cap, stake_usd=STAKE):
    """FAK against a CLOB ask list: consume ascending price levels <= cap
    until stake_usd is spent; partial fills kept (that IS what a real FAK
    gets). Returns top raw levels too so ANY smaller stake / tighter cap can
    be replayed offline. asks arrive UNSORTED from /book."""
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
        "counters": {"triggers": 0, "attempts": 0, "fills": 0, "craters": 0,
                     "throttle_skip": 0, "book_fail": 0,
                     "settled_total": 0, "settle_clob": 0}}


class Surge:
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
        self.informed = set()
        self.set_meta = {}
        self.win = {}                  # asset -> [(ts, ±usd)]
        self.last_trig = {}
        self.lock = threading.Lock()
        self.book_q = queue.Queue(BOOK_QUEUE)

    # ── config ─────────────────────────────────────────────────────────────
    def load_set(self):
        try:
            d = get_json(SET_URL, timeout=15)
            self.informed = {w.lower() for w in d["wallets"]}
            self.set_meta = {"generated_at": d.get("generated_at"),
                             "n": len(self.informed)}
            age_h = (time.time() - (d.get("generated_at") or 0)) / 3600
            log(f"informed set: {len(self.informed)} wallets "
                f"({age_h:.0f}h old)" + (" ⚠ STALE >48h" if age_h > 48 else ""))
        except Exception as e:
            log(f"⚠ informed set fetch failed ({e}) — "
                f"keeping {len(self.informed)} cached")

    # ── signal — FROZEN, verbatim v1 path ──────────────────────────────────
    def on_trade(self, p):
        w = (p.get("proxyWallet") or "").lower()
        if w not in self.informed:
            return
        title = p.get("title") or ""
        if niche(title) not in NICHES:
            return
        asset = p.get("asset")
        px = float(p.get("price") or 0)
        usd = px * float(p.get("size") or 0)
        if p.get("side") == "SELL":
            usd = -usd
        now = time.time()
        with self.lock:
            buf = self.win.setdefault(asset, [])
            buf.append((now, usd))
            while buf and buf[0][0] < now - WINDOW_S:
                buf.pop(0)
            flow = sum(u for _, u in buf)
            if flow < FLOW_USD or not (BAND[0] <= px <= BAND[1]):
                return
            if now - self.last_trig.get(asset, 0) < COOLDOWN_S:
                return
            self.last_trig[asset] = now
            c = self.state["counters"]
            c["triggers"] += 1
            # A2: EVERY trigger becomes an attempt (no cash/event/open gate —
            # bankroll specs are replayed offline from the attempts log)
            job = {"asset": asset, "cond": p.get("conditionId"),
                   "event": event_key(p.get("eventSlug") or p.get("slug")
                                      or ""),
                   "title": title[:60], "outcome": p.get("outcome"),
                   "niche": niche(title), "ts": round(now, 3),
                   "p_ref": px, "flow": round(flow)}
            try:
                self.book_q.put_nowait(job)
                c["attempts"] += 1
            except queue.Full:
                c["throttle_skip"] += 1
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
                self.state["counters"]["book_fail"] += 1
                self._skip(job, "book_fail", None)
            self.log_attempt({**job, "filled": False, "why": "book_fail"})
            return
        lat_ms = int((time.time() - t_req) * 1000)
        cap = min(job["p_ref"] * (1 + SLIP_CAP), 0.99)
        r = walk_asks(book.get("asks"), cap)
        end_ts = None                    # expected resolution (dashboard ETA)
        if r["filled"]:
            try:
                m = get_json(f"{CLOB}/markets/{job.get('cond')}", timeout=5)
                end_ts = iso_ts(m.get("end_date_iso") or "")
            except Exception:
                pass
        rec = {**job, "cap": round(cap, 4), "latency_ms": lat_ms,
               "top": r["top"], "best_ask": r["best_ask"],
               "filled": r["filled"]}
        with self.lock:
            c = self.state["counters"]
            if not r["filled"]:
                c["craters"] += 1
                self._skip(job, "crater", r["best_ask"])
                ba = "—" if r["best_ask"] is None else f"{r['best_ask']:.3f}"
                log(f"CRATER {job['title'][:38]} ref {job['p_ref']:.3f} "
                    f"ask {ba} flow ${job['flow']} ({lat_ms}ms)")
            else:
                fee = FEE_RATE * r["shares"] * min(r["vwap"], 1 - r["vwap"])
                lot = {**job, "end_ts": end_ts, "price": round(r["vwap"], 5),
                       "best_ask": r["best_ask"],
                       "shares": round(r["shares"], 4),
                       "cost": round(r["usd"], 2), "fee": round(fee, 4),
                       "levels": r["levels"], "partial": r["partial"],
                       "latency_ms": lat_ms}
                self.state["open"][f"{job['asset']}:{job['ts']}"] = lot
                c["fills"] += 1
                rec.update({"price": lot["price"], "shares": lot["shares"],
                            "usd": lot["cost"], "fee": lot["fee"],
                            "partial": lot["partial"], "end_ts": end_ts})
                log(f"FILL {job['outcome']} · {job['title'][:38]} @ "
                    f"{lot['price']:.3f} (${lot['cost']:.2f}"
                    f"{' partial' if lot['partial'] else ''}, "
                    f"flow ${job['flow']}, {lat_ms}ms)")
            self.persist()
        self.log_attempt(rec)

    def log_attempt(self, rec):
        """Append-only full attempt stream — the offline-replay dataset.
        One JSON line per attempt (fills, craters, fails, throttles)."""
        try:
            with open(ATTEMPTS, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            log(f"⚠ attempts log write failed: {e}")

    def _skip(self, job, why, ba):     # under lock
        self.state["skips"].append(
            {"ts": int(job["ts"]), "asset": job["asset"], "why": why,
             "p_ref": job["p_ref"], "flow": job["flow"], "ba": ba,
             "title": job["title"]})
        del self.state["skips"][:-SKIPS_TRIM]

    # ── provisional settles (nightly re-grades with chain truth) ───────────
    def settle_clob_pass(self):
        with self.lock:
            now = time.time()
            due = list(self.state["open"].keys())
            due.sort(key=lambda lid: self.state["open"][lid].get("clob_ck", 0))
            due = due[:CLOB_PASS_CAP]
            snap = {}
            for lid in due:
                lot = self.state["open"][lid]
                lot["clob_ck"] = now   # round-robin stamp
                snap[lid] = (lot["cond"], lot["asset"], lot.get("end_ts"))
        verdicts = {}
        ends = {}
        for lid, (cond, asset, end_ts) in snap.items():
            try:
                m = get_json(f"{CLOB}/markets/{cond}")
            except Exception:
                continue
            if end_ts is None:         # backfill ETAs for pre-ETA fills
                ends[lid] = iso_ts(m.get("end_date_iso") or "")
            if not m.get("closed"):
                continue
            for t in m.get("tokens") or []:
                if str(t.get("token_id")) == str(asset):
                    verdicts[lid] = 1.0 if t.get("winner") else 0.0
        with self.lock:
            for lid, e in ends.items():
                if lid in self.state["open"]:
                    self.state["open"][lid]["end_ts"] = e
            n = 0
            for lid, pay in verdicts.items():
                lot = self.state["open"].get(lid)
                if lot:
                    self._settle(lid, lot, pay)
                    n += 1
            if n or ends:
                self.persist()

    def _settle(self, lid, lot, pay):  # under lock
        pnl = round(lot["shares"] * pay - lot["cost"] - lot["fee"], 2)
        self.state["pnl_realized"] = round(self.state["pnl_realized"] + pnl, 2)
        c = self.state["counters"]
        c["settled_total"] += 1
        c["settle_clob"] += 1
        self.state["settled"].append(
            {**lot, "payout": pay, "provisional": True,
             "settled_ts": int(time.time()), "pnl": pnl})
        del self.state["settled"][:-SETTLED_TRIM]
        del self.state["open"][lid]
        word = "WON" if pay == 1.0 else "refund" if pay == 0.5 else "lost"
        log(f"SETTLE {word} {lot['title'][:36]} {pnl:+.2f} · "
            f"realized {self.state['pnl_realized']:+.2f}")

    # ── plumbing ───────────────────────────────────────────────────────────
    def persist(self):
        tmp = STATE + ".tmp"
        json.dump(self.state, open(tmp, "w"))
        os.replace(tmp, STATE)

    def feed_dict(self):               # under lock
        stt = self.state
        return {
            "mode": "paper-measurement", "study": "surge-A (#16)",
            "sem_ver": SEM_VER, "updated": int(time.time()),
            "boot_ts": stt["boot_ts"],
            "frozen": {"flow_usd": FLOW_USD, "window_s": WINDOW_S,
                       "band": BAND, "cooldown_s": COOLDOWN_S,
                       "stake": STAKE, "fee_rate": FEE_RATE,
                       "slip_cap": SLIP_CAP},
            "counters": stt["counters"], "pnl_realized": stt["pnl_realized"],
            "informed": self.set_meta, "open_n": len(stt["open"]),
            "open": [{**l, "id": lid}
                     for lid, l in list(stt["open"].items())[-200:]],
            "settled": stt["settled"][-200:], "skips": stt["skips"][-100:],
            "note": "verdict binds to forward_ledger.jsonl (#16); this feed "
                    "measures capturability — bankroll specs are replayed "
                    "offline from the attempts log (surge_book.json)"}


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
    """Read-only public feed for the /test dashboard (paper data only —
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
    """Boot gate — hard exit on failure."""
    assert niche("LoL: T1 vs GenG map 2") == "esports"
    assert niche("Pittsburgh Pirates vs. New York Yankees: O/U 9.5") == "sports"
    assert niche("Will it rain in NYC") == "other"
    assert event_key("mlb-pit-nyy-2026-07-22-game") == "mlb-pit-nyy-2026-07-22"
    r = walk_asks([{"price": "0.60", "size": "200"},
                   {"price": "0.55", "size": "100"}], cap=0.62)
    assert r["filled"] and abs(r["usd"] - 100.0) < 1e-6, r
    assert r["levels"] == 2 and r["best_ask"] == 0.55 and not r["partial"], r
    assert r["top"][0] == [0.55, 100.0] and len(r["top"]) == 2, r
    r = walk_asks([{"price": "0.55", "size": "10"}], cap=0.60)
    assert r["filled"] and r["partial"] and abs(r["usd"] - 5.5) < 1e-9, r
    r = walk_asks([{"price": "0.70", "size": "10"}], cap=0.60)
    assert not r["filled"] and r["best_ask"] == 0.70, r
    assert not walk_asks([], 0.5)["filled"]
    log("selftest OK")


def main():
    selftest()
    bot = Surge()
    bot.load_set()
    threading.Thread(target=serve_feed, args=(bot,), daemon=True).start()
    for _ in range(BOOK_WORKERS):
        threading.Thread(target=bot.book_worker, daemon=True).start()
    for tag in ("a", "b"):
        threading.Thread(target=run_conn, args=(tag, bot), daemon=True).start()
    last_set = time.time()
    n = 0
    while True:
        time.sleep(60)
        n += 1
        now = time.time()
        with bot.lock:
            bot.last_trig = {a: t for a, t in bot.last_trig.items()
                             if now - t < COOLDOWN_S}
            # trim flow windows for tokens that went quiet
            bot.win = {a: b for a, b in bot.win.items()
                       if b and b[-1][0] > now - 2 * WINDOW_S}
        if n % 2 == 0:
            bot.settle_clob_pass()
        if n % 5 == 0:
            with bot.lock:
                bot.persist()
        if time.time() - last_set > 6 * 3600:
            bot.load_set()
            last_set = time.time()
        with bot.lock:
            c = bot.state["counters"]
            msg = (f"paper pnl {bot.state['pnl_realized']:+.2f} · "
                   f"open {len(bot.state['open'])} · trig {c['triggers']} "
                   f"att {c['attempts']} fill {c['fills']} "
                   f"crat {c['craters']} thr {c['throttle_skip']} · "
                   f"settled {c['settled_total']}")
        log(msg)


if __name__ == "__main__":
    main()
