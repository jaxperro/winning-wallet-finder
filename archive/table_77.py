#!/usr/bin/env python3
"""Aggregate the 77 copyable wallets into one table: total staked, PnL, ROI,
consistency. Prints sorted by ROI and writes copyable_77.csv."""

import csv
import json
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import smart_money as sm
from lookback import resolved   # reuse the 120d+ resolved-bet puller

WEEK = 7 * 86400


def compute(r):
    cutoff = time.time() - 120 * 86400
    bets = resolved(r["wallet"], cutoff)
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
    total_bet = sum(b["stake"] for b in bets)
    total_pnl = sum(b["pnl"] for b in bets)
    gw = sum(p for p in wpnl if p > 0)
    gl = abs(sum(p for p in wpnl if p < 0))
    mean = statistics.mean(wroi)
    std = statistics.pstdev(wroi) if len(wroi) > 1 else 0
    oldest_days = round((time.time() - min(b["ts"] for b in bets)) / 86400)
    return {
        "username": r["username"], "wallet": r["wallet"],
        "weeks": len(weeks), "bets": len(bets),
        "total_bet": round(total_bet), "total_pnl": round(total_pnl),
        "roi_pct": round(total_pnl / total_bet * 100, 1) if total_bet else 0,
        "pct_weeks_green": round(sum(1 for p in wpnl if p > 0) / len(weeks) * 100),
        "profit_factor": round(gw / gl, 2) if gl else 999,
        "weekly_sharpe": round(mean / std, 2) if std else 0,
        "hold_pct": r["copy"]["hold_pct"],
        "history_days": oldest_days,
        "avg_bet": round(total_bet / len(bets)) if bets else 0,
    }


def main():
    cop = [r for r in json.load(open("edge_profitable.json")) if r.get("copyable")]
    out = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(compute, r): r for r in cop}
        for f in as_completed(futs):
            r = f.result()
            if r:
                out.append(r)
    out.sort(key=lambda r: r["roi_pct"], reverse=True)

    cols = ["username", "roi_pct", "total_bet", "total_pnl", "avg_bet",
            "pct_weeks_green", "profit_factor", "weekly_sharpe", "weeks",
            "bets", "hold_pct", "history_days", "wallet"]
    with open("copyable_77.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    print(f"{'#':>3} {'Trader':<20}{'ROI%':>7}{'TotalBet':>13}{'TotalPnL':>13}"
          f"{'AvgBet':>9}{'%grn':>6}{'PF':>6}{'Shrp':>6}{'wks':>4}{'hist_d':>7}")
    print("-" * 100)
    for i, r in enumerate(out, 1):
        print(f"{i:>3} {r['username'][:20]:<20}{r['roi_pct']:>6}%"
              f"{'$'+format(r['total_bet'], ','):>13}{'$'+format(r['total_pnl'], ','):>13}"
              f"{'$'+format(r['avg_bet'], ','):>9}{r['pct_weeks_green']:>5}%"
              f"{r['profit_factor']:>6.1f}{r['weekly_sharpe']:>6.2f}{r['weeks']:>4}"
              f"{r['history_days']:>7}")
    print("-" * 100)
    print(f"{len(out)} copyable wallets · saved to copyable_77.csv")
    print(f"  total staked across all: ${sum(r['total_bet'] for r in out):,}")
    print(f"  median history: {statistics.median([r['history_days'] for r in out])} days")


if __name__ == "__main__":
    main()
