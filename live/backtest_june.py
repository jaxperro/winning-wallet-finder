#!/usr/bin/env python3
"""Forward copy-test: copy the FAVORITE-rider skilled wallets' new entries from
June 1 to now, $1000 bankroll, NO execution lag (we get their exact fill price).

Method: collect every BUY these wallets made on/after June 1 (data-api), take the
first entry per market (basket consensus, one position per market), deploy $1000
split equally across them, then settle each via the CLOB winner flag (resolved)
or mark to current price (still open). Reports realized + unrealized P&L.

    python3 backtest_june.py            # favorites, from 2026-06-01
    python3 backtest_june.py value      # test the value/longshot archetype instead
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(__file__)
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com/markets"
CTX = ssl._create_unverified_context()
START = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))
BANKROLL = 1000.0
ARCH = sys.argv[1] if len(sys.argv) > 1 else "favorite"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())


def trades_since(wallet):
    """All BUY trades on/after START for one wallet."""
    out, off = [], 0
    for _ in range(8):
        try:
            page = get(f"{DATA}/activity?user={wallet}&type=TRADE&limit=500&offset={off}")
        except Exception:
            break
        if not page:
            break
        for t in page:
            if (t.get("timestamp") or 0) < START:
                return out
            if t.get("side") == "BUY" and t.get("conditionId"):
                out.append(t)
        off += 500
        if len(page) < 500:
            break
    return out


_mkt = {}
def market(cond):
    if cond not in _mkt:
        try:
            _mkt[cond] = get(f"{CLOB}/{cond}")
        except Exception:
            _mkt[cond] = None
    return _mkt[cond]


def settle(cond, outcome_idx, outcome_name):
    """-> (status, value_per_share). status in won/lost/open/unknown."""
    m = market(cond)
    if not m:
        return "unknown", None
    toks = m.get("tokens") or []
    tok = None
    if outcome_idx is not None and outcome_idx < len(toks):
        tok = toks[outcome_idx]
    if tok is None:
        for t in toks:
            if (t.get("outcome") or "").lower() == (outcome_name or "").lower():
                tok = t; break
    if tok is None:
        return "unknown", None
    if tok.get("winner") is True:
        return "won", 1.0
    if tok.get("winner") is False:
        return "lost", 0.0
    return "open", float(tok.get("price") or 0)   # not resolved -> mark to price


def main():
    wl = json.load(open(os.path.join(HERE, os.environ.get("BT_WATCH", "watch_skilled.json"))))
    wallets = [w for w in wl if (w["avg_entry"] >= 0.85 if ARCH == "favorite"
                                 else w["avg_entry"] < 0.5 if ARCH == "value"
                                 else True)]
    print(f"{ARCH}: {len(wallets)} wallets · copying BUYs from "
          f"{time.strftime('%Y-%m-%d', time.localtime(START))} to now, ${BANKROLL:.0f}, no lag\n",
          flush=True)

    # gather every favorite's June+ buys, keep the FIRST entry per market
    picks = {}   # cond -> trade (earliest)
    with ThreadPoolExecutor(max_workers=10) as ex:
        for ts in ex.map(trades_since, [w["wallet"] for w in wallets]):
            for t in ts:
                c = t["conditionId"]
                if c not in picks or t["timestamp"] < picks[c]["timestamp"]:
                    picks[c] = t
    n = len(picks)
    if not n:
        print("no copied entries in the window."); return
    stake = BANKROLL / n
    print(f"{n} unique markets entered → ${stake:.2f} per position\n", flush=True)

    won = lost = openc = unk = 0
    realized = unreal_val = realized_cost = open_cost = 0.0
    rows = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(
            lambda kv: (kv[1], settle(kv[0], kv[1].get("outcomeIndex"), kv[1].get("outcome"))),
            picks.items()))
    for t, (status, vps) in results:
        p = t.get("price") or 0.01
        shares = stake / max(p, 0.001)
        title = (t.get("title") or "")[:46]
        if status == "won":
            won += 1; realized += shares * 1.0; realized_cost += stake
            rows.append((shares - stake, status, p, title))
        elif status == "lost":
            lost += 1; realized += 0.0; realized_cost += stake
            rows.append((-stake, status, p, title))
        elif status == "open":
            openc += 1; unreal_val += shares * vps; open_cost += stake
            rows.append((shares * vps - stake, status, p, title))
        else:
            unk += 1; unreal_val += stake; open_cost += stake   # unknown -> hold at cost

    realized_pl = realized - realized_cost
    equity = realized + unreal_val + 0.0      # all $1000 deployed
    total_pl = equity - BANKROLL
    print(f"resolved: {won}W / {lost}L  ·  still open: {openc}  ·  unknown: {unk}")
    print(f"REALIZED P&L: {realized_pl:+,.2f}  (on ${realized_cost:,.0f} settled)")
    print(f"open positions marked to market: ${unreal_val:,.2f} (cost ${open_cost:,.0f})")
    print(f"\nFINAL EQUITY: ${equity:,.2f}   TOTAL P&L: {total_pl:+,.2f}  "
          f"({100*total_pl/BANKROLL:+.1f}% on ${BANKROLL:.0f})\n")
    rows.sort()
    print("worst / best copied bets:")
    for pl, st, p, title in rows[:4] + rows[-4:]:
        print(f"  {pl:+8.2f}  {st:>5} @{p:.2f}  {title}")


if __name__ == "__main__":
    main()
