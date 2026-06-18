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


def main():
    cands = json.load(open(os.path.join(HERE, "candidates.json")))
    cands.sort(key=lambda c: c.get("markets_seen", 0), reverse=True)
    wallets = [c["wallet"] for c in cands]
    print(f"collecting {len(wallets):,} wallets up to present · {WORKERS} workers", flush=True)
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
