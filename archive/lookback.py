#!/usr/bin/env python3
"""240-day lookback on a short list of wallets, split into halves.

We selected these wallets on their last 120 days. The *older* half (240->120
days ago) is data that played no part in selection — so consistency there is
backward out-of-sample evidence the edge is real, not a lucky recent stretch.
"""

import statistics
import sys
import time
from collections import defaultdict

import smart_money as sm

WEEK = 7 * 86400
PAGES = 160   # generous for this focused 5-wallet run


def parse_end(end):
    if not end:
        return 0
    end = end.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(end, fmt))
        except ValueError:
            continue
    return 0


def resolved(wallet, cutoff):
    now = time.time()
    out = []
    off = 0
    while off < PAGES * 50:
        page = sm.get_json("/closed-positions",
                           {"user": wallet, "limit": 50, "offset": off,
                            "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        if not page:
            break
        for p in page:
            if p.get("timestamp", 0) >= cutoff:
                out.append({"ts": p["timestamp"], "pnl": p.get("realizedPnl", 0),
                            "stake": p.get("avgPrice", 0) * p.get("totalBought", 0)})
        off += 50
        if len(page) < 50 or page[-1].get("timestamp", 0) < cutoff:
            break
    off = 0
    while off < PAGES * 50:
        page = sm.get_json("/positions",
                           {"user": wallet, "limit": 50, "offset": off,
                            "sizeThreshold": 0.0})
        if not page:
            break
        for p in page:
            end = parse_end(p.get("endDate"))
            if cutoff <= end < now:
                out.append({"ts": end, "pnl": p.get("cashPnl", 0),
                            "stake": p.get("initialValue", 0)})
        off += 50
        if len(page) < 50:
            break
    return out


def stats(bets):
    if not bets:
        return None
    by_week = defaultdict(lambda: [0.0, 0.0])
    for b in bets:
        wk = int(b["ts"] // WEEK)
        by_week[wk][0] += b["pnl"]
        by_week[wk][1] += b["stake"]
    weeks = sorted(by_week)
    wpnl = [by_week[w][0] for w in weeks]
    wroi = [by_week[w][0] / by_week[w][1] if by_week[w][1] else 0 for w in weeks]
    tot_pnl = sum(wpnl)
    tot_stake = sum(by_week[w][1] for w in weeks)
    gw = sum(p for p in wpnl if p > 0)
    gl = abs(sum(p for p in wpnl if p < 0))
    mean = statistics.mean(wroi)
    std = statistics.pstdev(wroi) if len(wroi) > 1 else 0
    return {
        "weeks": len(weeks), "bets": len(bets),
        "green": round(sum(1 for p in wpnl if p > 0) / len(weeks) * 100),
        "pf": round(gw / gl, 2) if gl else 999,
        "sharpe": round(mean / std, 2) if std else 0,
        "roi": round(tot_pnl / tot_stake * 100, 1) if tot_stake else 0,
        "pnl": round(tot_pnl),
    }


def line(label, s):
    if not s:
        print(f"    {label:<8} (no resolved bets in this period)")
        return
    print(f"    {label:<8} {s['weeks']:>2}wk {s['bets']:>5}bets "
          f"{s['green']:>3}%grn  PF {s['pf']:>6}  Sharpe {s['sharpe']:>5}  "
          f"ROI {s['roi']:>6}%  ${s['pnl']:>12,}")


def main(wallets):
    now = time.time()
    mid = now - 120 * 86400
    for name, w in wallets:
        bets = resolved(w, now - 240 * 86400)
        older = [b for b in bets if b["ts"] < mid]   # 240->120d (not used to select)
        recent = [b for b in bets if b["ts"] >= mid]  # 120->0d (selection window)
        print(f"\n{name}  ({w[:16]}…)")
        line("240d all", stats(bets))
        line("older½", stats(older))   # out-of-sample
        line("recent½", stats(recent))  # in-sample


if __name__ == "__main__":
    # name, wallet  — passed as alternating argv or hardcoded by caller
    pairs = [(sys.argv[i], sys.argv[i + 1]) for i in range(1, len(sys.argv), 2)]
    main(pairs)
