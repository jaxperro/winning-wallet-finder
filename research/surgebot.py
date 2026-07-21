#!/usr/bin/env python3
"""surgebot — Study A's surge-momentum signal as a real-time PAPER harness.

PAPER ONLY. No keys, no orders, no bot imports — this file is self-contained
(recorder-style: baked into the wwf-surgebot image, must not depend on the
repo to boot). It exists to answer the two questions the tape sim cannot:
does the signal compute in real time on the live stream, and what does the
paper book do at the $100/5%-stake deployment spec (#16 sprint plan). Its
entries are graded nightly against chain truth on the Mac (grade_surge.py);
believing ANY of it is gated on the pre-registered #16 forward verdict.

FROZEN signal (params/study_flow.json, 2026-07-20 — do not tune here):
  informed set   top-150 (fetched from the repo, regenerated nightly)
  trigger        net informed flow >= $300 in 60s, one market
  band           entry price 0.10-0.90 · niches sports+esports
  cooldown       900s per token
Deployment spec (2026-07-21 sizing discussion):
  bank $100 paper · stake 5% of equity, set once per UTC day, $1 floor
  cash-gated all-or-nothing · max 2 open positions per real-world event
Paper fill = live CLOB best ask inside p_ref*1.05, else crater (the same
FAK model the copybot's paper mode uses); venue fee 3% * shares * min(p,1-p).
Settles provisionally from the CLOB closed/winner flags; the nightly
re-grades with CTF payout vectors (operator-resolved markets lie here).
"""
import json
import os
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
STATE = os.environ.get("SURGE_STATE", "/data/surge_state.json")

BANK = 100.0
STAKE_PCT = 0.05
EVENT_CAP = 2
FLOW_USD = 300.0
WINDOW_S = 60
BAND = (0.10, 0.90)
COOLDOWN_S = 900
FEE_RATE = 0.03
SLIP_CAP = 0.05
NICHES = {"sports", "esports"}
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


class Surge:
    def __init__(self):
        self.state = {"cash": BANK, "day": "", "day_stake": 5.0,
                      "open": {}, "settled": [], "skips": [],
                      "counters": {"triggers": 0, "fills": 0, "craters": 0,
                                   "cash_skip": 0, "event_skip": 0}}
        if os.path.exists(STATE):
            try:
                self.state = json.load(open(STATE))
                log(f"resumed: cash ${self.state['cash']:.2f} · "
                    f"{len(self.state['open'])} open")
            except Exception as e:
                log(f"⚠ state load failed ({e}) — fresh book")
        self.informed = set()
        self.set_meta = {}
        self.win = {}                  # asset -> [(ts, ±usd)]
        self.last_trig = {}
        self.lock = threading.Lock()
        self._roll_day(force=True)

    # ── config / sizing ────────────────────────────────────────────────────
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

    def equity(self):
        return self.state["cash"] + sum(p["cost"] for p in
                                        self.state["open"].values())

    def _roll_day(self, force=False):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if force or day != self.state["day"]:
            self.state["day"] = day
            self.state["day_stake"] = max(1.0, round(STAKE_PCT * self.equity(), 2))
            log(f"day {day}: stake ${self.state['day_stake']:.2f} "
                f"(5% of ${self.equity():.2f} equity)")

    def persist(self):
        tmp = STATE + ".tmp"
        json.dump(self.state, open(tmp, "w"))
        os.replace(tmp, STATE)

    # ── signal ─────────────────────────────────────────────────────────────
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
            self.state["counters"]["triggers"] += 1
            self.execute(p, asset, px, flow, title)

    # ── paper execution (called under lock) ────────────────────────────────
    def execute(self, p, asset, p_ref, flow, title):
        self._roll_day()
        c = self.state["counters"]
        ev = event_key(p.get("eventSlug") or p.get("slug") or "")
        if asset in self.state["open"]:
            return
        n_ev = sum(1 for o in self.state["open"].values() if o["event"] == ev)
        if ev and n_ev >= EVENT_CAP:
            c["event_skip"] += 1
            log(f"skip (event cap) {title[:40]}")
            return
        stake = self.state["day_stake"]
        if self.state["cash"] < stake:
            c["cash_skip"] += 1
            log(f"skip (no cash: ${self.state['cash']:.2f}) {title[:40]}")
            return
        try:
            book = get_json(f"{CLOB}/book?token_id={asset}")
            asks = [float(a["price"]) for a in (book.get("asks") or [])]
            ba = min(asks) if asks else None
        except Exception as e:
            log(f"skip (book fetch failed: {e}) {title[:40]}")
            return
        cap = min(p_ref * (1 + SLIP_CAP), 0.99)
        if ba is None or ba > cap:
            c["craters"] += 1
            self.state["skips"].append({"ts": int(time.time()), "asset": asset,
                                        "p_ref": p_ref, "ba": ba, "flow": flow,
                                        "title": title[:60], "why": "crater"})
            del self.state["skips"][:-500]
            log(f"CRATER {title[:40]} ref {p_ref:.2f} ask "
                f"{('%.2f' % ba) if ba else '—'}")
            self.persist()
            return
        shares = stake / ba
        fee = FEE_RATE * shares * min(ba, 1 - ba)
        self.state["cash"] -= stake + fee
        self.state["open"][asset] = {
            "ts": int(time.time()), "cond": p.get("conditionId"),
            "event": ev, "title": title[:60],
            "outcome": p.get("outcome"), "p_ref": p_ref, "price": ba,
            "shares": round(shares, 4), "cost": stake, "fee": round(fee, 4),
            "flow": round(flow)}
        c["fills"] += 1
        log(f"FILL {p.get('outcome')} · {title[:40]} @ {ba:.3f} "
            f"(${stake:.2f}, flow ${flow:.0f}) · cash ${self.state['cash']:.2f}")
        self.persist()

    # ── provisional settles (nightly re-grades with chain truth) ───────────
    def settle_pass(self):
        for asset, pos in list(self.state["open"].items()):
            try:
                m = get_json(f"{CLOB}/markets/{pos['cond']}")
            except Exception:
                continue
            if not m.get("closed"):
                continue
            pay = None
            for t in m.get("tokens") or []:
                if str(t.get("token_id")) == str(asset):
                    pay = 1.0 if t.get("winner") else 0.0
            if pay is None:
                continue
            self.state["cash"] += pos["shares"] * pay
            self.state["settled"].append({**pos, "asset": asset, "payout": pay,
                                          "provisional": True,
                                          "settled_ts": int(time.time()),
                                          "pnl": round(pos["shares"] * pay
                                                       - pos["cost"] - pos["fee"], 2)})
            del self.state["open"][asset]
            log(f"SETTLE {'WON' if pay else 'lost'} {pos['title'][:38]} "
                f"{'+' if pay else ''}{pos['shares'] * pay - pos['cost'] - pos['fee']:.2f} "
                f"· cash ${self.state['cash']:.2f}")
        self.persist()


def run_conn(tag, bot):
    backoff = 2
    while True:
        state = {"fresh": time.time()}

        def on_open(ws):
            ws.send(json.dumps({"action": "subscribe", "subscriptions":
                                [{"topic": "activity", "type": "trades",
                                  "filters": ""}]}))
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


def main():
    bot = Surge()
    bot.load_set()
    for tag in ("a", "b"):
        threading.Thread(target=run_conn, args=(tag, bot), daemon=True).start()
    last_set = time.time()
    n = 0
    while True:
        time.sleep(60)
        n += 1
        with bot.lock:
            bot._roll_day()
        if n % 2 == 0:
            with bot.lock:
                bot.settle_pass()
        if time.time() - last_set > 6 * 3600:
            bot.load_set()
            last_set = time.time()
        c = bot.state["counters"]
        log(f"paper ${bot.equity():.2f} (cash ${bot.state['cash']:.2f}) · "
            f"open {len(bot.state['open'])} · trig {c['triggers']} "
            f"fill {c['fills']} crater {c['craters']} "
            f"skip {c['cash_skip']}c/{c['event_skip']}e")


if __name__ == "__main__":
    main()
