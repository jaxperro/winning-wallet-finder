#!/usr/bin/env python3
"""Scan many wallets for a RELIABLE, COPYABLE weekly edge.

Two passes:
  1. metrics  — for every candidate, bucket resolved-bet PnL by week over the
     window and compute consistency (% green weeks, profit factor, Sharpe, ROI).
     Results stream to a JSONL file so a long run is crash-safe.
  2. copyability — for the wallets that look profitable, pull /activity and
     measure how much they hold to resolution (mirrorable) vs trade around
     (not mirrorable by copying entries).

    python3 edge_research.py --pool 1500 --days 120

Outputs:  edge_metrics.jsonl  (raw, all wallets)
          edge_profitable.json (filtered + copyability, ranked)
"""

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import smart_money as sm

WEEK = 7 * 86400
MAX_PAGES = 40          # per endpoint, bounds runtime on hyperactive wallets


def _parse_end(end):
    if not end:
        return 0
    end = end.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(end, fmt))
        except ValueError:
            continue
    return 0


def candidates(pool):
    seen = {}
    for window in ("7d", "30d", "all"):
        offset = 0
        while offset < pool and offset < 2000:
            page = sm.get_json("/v1/leaderboard",
                               {"window": window, "limit": 50, "offset": offset})
            if not page:
                break
            for u in page:
                w = u.get("proxyWallet")
                if w and w not in seen:
                    seen[w] = {"wallet": w,
                               "username": u.get("userName") or w[:10] + "...",
                               "lb_pnl": u.get("pnl", 0)}
            offset += 50
            if len(page) < 50:
                break
        if len(seen) >= pool:
            break
    return list(seen.values())[:pool]


def resolved_with_stake(wallet, cutoff):
    now = time.time()
    out = []
    off = 0
    while off < MAX_PAGES * 50:
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
    while off < MAX_PAGES * 50:
        page = sm.get_json("/positions",
                           {"user": wallet, "limit": 50, "offset": off,
                            "sizeThreshold": 0.0})
        if not page:
            break
        for p in page:
            end = _parse_end(p.get("endDate"))
            if cutoff <= end < now:
                out.append({"ts": end, "pnl": p.get("cashPnl", 0),
                            "stake": p.get("initialValue", 0)})
        off += 50
        if len(page) < 50:
            break
    return out


def metrics(cand, cutoff):
    bets = resolved_with_stake(cand["wallet"], cutoff)
    if len(bets) < 20:
        return None
    by_week = defaultdict(lambda: [0.0, 0.0])
    for b in bets:
        wk = int(b["ts"] // WEEK)
        by_week[wk][0] += b["pnl"]
        by_week[wk][1] += b["stake"]
    weeks = sorted(by_week)
    wpnl = [by_week[w][0] for w in weeks]
    wroi = [by_week[w][0] / by_week[w][1] if by_week[w][1] else 0 for w in weeks]
    total_pnl = sum(wpnl)
    total_stake = sum(by_week[w][1] for w in weeks)
    gw = sum(p for p in wpnl if p > 0)
    gl = abs(sum(p for p in wpnl if p < 0))
    mean_roi = statistics.mean(wroi)
    std_roi = statistics.pstdev(wroi) if len(wroi) > 1 else 0
    return {
        "wallet": cand["wallet"], "username": cand["username"],
        "lb_pnl": round(cand["lb_pnl"]),
        "n_weeks": len(weeks), "n_bets": len(bets),
        "pct_weeks_pos": round(sum(1 for p in wpnl if p > 0) / len(weeks) * 100),
        "mean_weekly_roi": round(mean_roi * 100, 1),
        "weekly_sharpe": round(mean_roi / std_roi, 2) if std_roi else 0,
        "profit_factor": round(gw / gl, 2) if gl else 999,
        "total_pnl": round(total_pnl),
        "total_roi": round(total_pnl / total_stake * 100, 1) if total_stake else 0,
    }


def copyability(wallet):
    trades, off = [], 0
    while off < 2000:                 # cap fills for speed
        p = sm.get_json("/activity",
                        {"user": wallet, "type": "TRADE", "limit": 500, "offset": off})
        if not p:
            break
        trades += p
        off += 500
        if len(p) < 500:
            break
    by_mkt = defaultdict(lambda: {"buy_usd": 0.0, "sell_usd": 0.0, "sold": False})
    for t in trades:
        m = by_mkt[t.get("conditionId")]
        if t.get("side") == "BUY":
            m["buy_usd"] += t.get("usdcSize", 0)
        else:
            m["sell_usd"] += t.get("usdcSize", 0)
            m["sold"] = True
    n = len(by_mkt) or 1
    hold = sum(1 for m in by_mkt.values() if not m["sold"])
    return {"markets": len(by_mkt), "hold_pct": round(hold / n * 100),
            "fills": len(trades)}


def run(pool, days, workers):
    cutoff = time.time() - days * 86400
    out_path, prof_path = "edge_metrics.jsonl", "edge_profitable.json"
    print(f"[{time.strftime('%H:%M:%S')}] pulling up to {pool} candidates...", flush=True)
    cands = candidates(pool)
    print(f"[{time.strftime('%H:%M:%S')}] {len(cands)} candidates · "
          f"window {days}d · analyzing (workers={workers})", flush=True)

    done = kept = 0
    with open(out_path, "w") as fout, ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(metrics, c, cutoff): c for c in cands}
        for f in as_completed(futs):
            done += 1
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                kept += 1
                fout.write(json.dumps(r) + "\n")
                fout.flush()
            if done % 50 == 0 or done == len(cands):
                print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(cands)} analyzed "
                      f"· {kept} with enough history", flush=True)

    rows = [json.loads(l) for l in open(out_path)]
    # "looks profitable" screen
    prof = [r for r in rows if r["n_weeks"] >= max(4, days // 7 * 0.4)
            and r["n_bets"] >= 30 and r["total_pnl"] > 0 and r["total_roi"] > 0
            and r["pct_weeks_pos"] >= 60 and r["profit_factor"] >= 1.3]
    print(f"\n[{time.strftime('%H:%M:%S')}] {len(prof)} wallets pass the profitable "
          f"screen · checking copyability...", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(copyability, r["wallet"]): r for r in prof}
        for f in as_completed(futs):
            r = futs[f]
            try:
                r["copy"] = f.result()
            except Exception:
                r["copy"] = {"markets": 0, "hold_pct": 0, "fills": 0}

    for r in prof:
        r["copyable"] = r["copy"]["hold_pct"] >= 70
        # composite: reward consistency, profit factor, and ROI
        r["score"] = round(r["pct_weeks_pos"] / 100 * r["profit_factor"]
                           * (1 + r["total_roi"] / 100), 2)
    prof.sort(key=lambda r: (r["copyable"], r["score"]), reverse=True)
    json.dump(prof, open(prof_path, "w"), indent=2)

    print(f"\n{'='*94}")
    print(f"  PROFITABLE & COPYABLE wallets (window {days}d, pool {len(cands)})")
    print(f"{'='*94}")
    h = (f"{'Trader':<20}{'wks':>4}{'bets':>6}{'%wk+':>6}{'PF':>6}"
         f"{'Sharpe':>7}{'totROI':>8}{'hold%':>7}{'copy':>6}{'90d PnL':>13}")
    print(h)
    print("-" * len(h))
    for r in prof:
        print(f"{r['username'][:20]:<20}{r['n_weeks']:>4}{r['n_bets']:>6}"
              f"{r['pct_weeks_pos']:>5}%{r['profit_factor']:>6.2f}"
              f"{r['weekly_sharpe']:>7.2f}{r['total_roi']:>7}%"
              f"{r['copy']['hold_pct']:>6}%{'yes' if r['copyable'] else 'no':>6}"
              f"{'$'+format(r['total_pnl'], ','):>13}")
    print("-" * len(h))
    cop = sum(1 for r in prof if r["copyable"])
    print(f"{len(prof)} profitable · {cop} of them copyable (hold-to-resolution ≥70%)")
    print(f"Full detail: {prof_path}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=int, default=1500)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    run(args.pool, args.days, args.workers)


if __name__ == "__main__":
    main()
