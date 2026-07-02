#!/usr/bin/env python3
"""Collect EVERY candidate wallet's resolved bets into the cache, up to present.

One-time (per refresh window) comprehensive pull so the whole candidate pool is
local. Resumable: cache.get_bets skips wallets pulled within MAX_AGE_DAYS, so
killing and re-running continues where it left off. Most-active wallets first,
so a partial cache already covers the wallets most likely to be skilled.

    python3 collect.py
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cache

HERE = os.path.dirname(__file__)
WORKERS = 16
# Bound each run: every never-pulled wallet is collected, but at most STALE_CAP of
# the already-cached ones are refreshed (stalest first). Without the cap, the day
# the bulk-ingested pool crosses MAX_AGE_DAYS together, a "daily" run balloons into
# a ~40h re-pull that blocks every scoring step behind it in daily.sh and holds the
# DuckDB write lock (even read_only connections fail) all day. At 2,500/day the
# whole pool still turns over well inside the 14-day freshness window.
STALE_CAP = int(os.environ.get("STALE_CAP", 2500))


def main():
    cands = json.load(open(os.path.join(HERE, "candidates.json")))
    cands.sort(key=lambda c: c.get("markets_seen", 0), reverse=True)
    ages = cache.pulled_ages()
    fresh_cut = time.time() - cache.MAX_AGE_DAYS * 86400
    new = [c["wallet"] for c in cands if c["wallet"] not in ages]
    stale = sorted((c["wallet"] for c in cands
                    if 0 < ages.get(c["wallet"], 0) < fresh_cut), key=ages.get)
    wallets = new + stale[:STALE_CAP]
    print(f"collecting {len(wallets):,} wallets ({len(new):,} new + "
          f"{len(stale[:STALE_CAP]):,} of {len(stale):,} stale, cap {STALE_CAP}) · "
          f"{WORKERS} workers", flush=True)
    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(cache.get_bets, w) for w in wallets]
        for _ in as_completed(futs):
            done += 1
            if done % 200 == 0:
                w, b = cache.stats()
                rate = done / max(1e-9, time.time() - t0)
                eta = (len(wallets) - done) / max(1e-9, rate) / 3600
                print(f"  {done:,}/{len(wallets):,} · cache {w:,}w/{b:,}bets · "
                      f"{rate:.1f}/s · ETA {eta:.1f}h", flush=True)
    w, b = cache.stats()
    print(f"DONE {time.strftime('%F %T')} — cache: {w:,} wallets, {b:,} bets", flush=True)


if __name__ == "__main__":
    main()
