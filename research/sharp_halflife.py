#!/usr/bin/env python3
"""T15 EXPLORATORY (2026-07-23) — SHARP HALF-LIFE: how long does a wallet
stay sharp after we detect it? The live follow set has principled entry
criteria and no exit criteria; this measures whether detection-day edge
decays with age-in-set and how fast wallets churn out of the published
screen.

Detection dates are REAL as-of history, not reconstruction: the git
commit series of live/watch_sharps.json (published daily by the pipeline
since 2026-06-18) — a wallet's detection date is the first commit that
contains it. Wallets already present in the FIRST commit are
left-censored (true detection unknown) and excluded from the age curves.

Performance is the wallet's OWN bets from the cache (live/cache.duckdb
bets, read-only snapshot taken first so the payouts writer never
contends), graded to CHAIN TRUTH per bet via payouts.ensure/truth —
the cache's won/res_t columns are never trusted (the res_t=ts poison).
EV is per $100 at the wallet's own entry price, feeless: this measures
SIGNAL decay, not our execution (T3/T11 own execution).

Outputs: EV/bet by age-in-set bucket (0-2d / 3-6d / 7-13d / 14d+),
pooled AND per-wallet-day mean-of-means (concentration guard), plus the
survival curve (fraction of detected wallets still published at age k).
NOT pre-registered — informs a rotation policy for the follow set."""
import collections
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "live"))
BAND = (0.05, 0.95)
BUCKETS = [(0, 3, "age 0-2d"), (3, 7, "age 3-6d"),
           (7, 14, "age 7-13d"), (14, 10**6, "age 14d+")]


def set_history():
    """[(date_str, {wallets})] oldest-first from git history."""
    log = subprocess.run(
        ["git", "log", "--reverse", "--format=%ad %H", "--date=short",
         "--", "live/watch_sharps.json"],
        cwd=ROOT, capture_output=True, text=True).stdout.split()
    pairs = list(zip(log[0::2], log[1::2]))
    out = []
    for date, sha in pairs:
        try:
            blob = subprocess.run(
                ["git", "show", f"{sha}:live/watch_sharps.json"],
                cwd=ROOT, capture_output=True, text=True).stdout
            rows = json.loads(blob)
            ws = {r["wallet"].lower() for r in rows if r.get("wallet")}
            if ws:
                out.append((date, ws))
        except Exception:
            continue
    # one snapshot per date (last commit of the day wins)
    byday = {}
    for date, ws in out:
        byday[date] = ws
    return sorted(byday.items())


def main():
    hist = set_history()
    print(f"set snapshots: {len(hist)} days "
          f"({hist[0][0]} .. {hist[-1][0]})", flush=True)
    first_day, censored = {}, set()
    for i, (date, ws) in enumerate(hist):
        for w in ws:
            if w not in first_day:
                first_day[w] = date
                if i == 0:
                    censored.add(w)
    pool = set(first_day) - censored
    print(f"wallets ever published: {len(first_day)} · "
          f"left-censored (first snapshot): {len(censored)} · "
          f"age-eligible: {len(pool)}", flush=True)

    day_n = {d: time.mktime(time.strptime(d, "%Y-%m-%d")) // 86400
             for d, _ in hist}
    det_n = {w: day_n[first_day[w]] for w in pool}

    # survival: still-published at age k (right-censored by last snapshot)
    last_n = day_n[hist[-1][0]]
    print("\nSURVIVAL (still in the published set at age k):", flush=True)
    for k in (1, 3, 7, 14, 21, 28):
        elig = [w for w in pool if det_n[w] + k <= last_n]
        if not elig:
            continue
        alive = 0
        for w in elig:
            # snapshot on-or-after detection+k (carry-forward between days)
            snap = None
            for d, ws in hist:
                if day_n[d] <= det_n[w] + k:
                    snap = ws
                else:
                    break
            alive += snap is not None and w in snap
        print(f"  age {k:>2}d: {alive}/{len(elig)} = "
              f"{alive/len(elig):.0%}", flush=True)

    # bets snapshot (read-only, close before payouts writes the same file)
    import duckdb
    con = duckdb.connect(os.path.join(ROOT, "live", "cache.duckdb"),
                         read_only=True)
    wl = ",".join(f"'{w}'" for w in pool)
    bets = con.execute(f"""
        SELECT lower(wallet) w, cond, asset,
               any_value(p::DOUBLE) p, min(ts) ts
        FROM bets
        WHERE lower(wallet) IN ({wl}) AND p BETWEEN {BAND[0]} AND {BAND[1]}
              AND resolved
        GROUP BY lower(wallet), cond, asset""").fetchall()
    con.close()
    print(f"\nresolved cache bets for age-eligible wallets: {len(bets)}",
          flush=True)

    import payouts
    conds = sorted({b[1] for b in bets if b[1]})
    print(f"ensuring {len(conds)} conds against chain…", flush=True)
    payouts.ensure(conds)

    per_bucket = {tag: [] for _, _, tag in BUCKETS}
    per_wd = {tag: collections.defaultdict(list) for _, _, tag in BUCKETS}
    graded = skipped = 0
    for w, cond, asset, p, ts in bets:
        if ts // 86400 < det_n[w]:
            continue                    # pre-detection bet
        pay = payouts.truth(cond, asset)
        if pay is None or pay == 0.5:
            skipped += 1
            continue
        graded += 1
        age = int(ts // 86400 - det_n[w])
        ev = (100.0 / p) * (pay - p)
        for lo, hi, tag in BUCKETS:
            if lo <= age < hi:
                per_bucket[tag].append(ev)
                per_wd[tag][(w, int(ts // 86400))].append(ev)
                break
    print(f"chain-graded post-detection bets: {graded} "
          f"(ungraded/refund skipped: {skipped})\n", flush=True)

    print("EV BY AGE-IN-SET (per $100 at the wallet's own entry, feeless):",
          flush=True)
    for _, _, tag in BUCKETS:
        evs = per_bucket[tag]
        if not evs:
            print(f"  {tag:>10}: n=0")
            continue
        pooled = sum(evs) / len(evs)
        wd = [sum(v) / len(v) for v in per_wd[tag].values()]
        mom = sum(wd) / len(wd)
        nw = len({k[0] for k in per_wd[tag]})
        print(f"  {tag:>10}: n={len(evs):>5} · pooled EV/bet {pooled:+6.2f}"
              f" · wallet-day mean {mom:+6.2f} · {nw} wallets", flush=True)
    # concentration guard: top-5 wallet share of |pnl| overall
    by_w = collections.defaultdict(float)
    for _, _, tag in BUCKETS:
        for (w, _), v in per_wd[tag].items():
            by_w[w] += sum(v)
    if by_w:
        tot = sum(by_w.values())
        top = sorted(by_w.items(), key=lambda kv: -abs(kv[1]))[:5]
        print(f"\nconcentration: total {tot:+.0f} · top-5 wallets "
              f"{[(w[:8], round(v)) for w, v in top]}", flush=True)


if __name__ == "__main__":
    main()
