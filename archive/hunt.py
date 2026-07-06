#!/usr/bin/env python3
"""Ring hunt — sweep news-driven event markets, score traders, detect funding
clusters (operator rings). Uses insider.py's machinery across many markets."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import insider

KEY = insider.sm_load_key()
TOP = 30  # top traders by notional per market


def hunt_market(cond, title):
    cands, _ = insider.market_traders(cond, top=TOP)
    if not cands:
        return
    name = {c["wallet"]: c["username"] for c in cands}
    rows = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(insider.analyze, c) for c in cands]
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
    rows.sort(key=lambda r: r["score"], reverse=True)
    print(f"\n{'='*78}\n{title[:60]}  ({len(cands)} traders scored)\n{'='*78}", flush=True)
    print("top by improbability:")
    for r in rows[:4]:
        print(f"  susp {r['score']:>4} z {r['z']:>5} p {insider.fmt_p(r['pval']):>8} "
              f"{r['wins']}/{r['n']} pre24 {r['pre24_pct']:>3}% trades {r['trades']:>6}  "
              f"{r['username'][:20]}", flush=True)
    # funding-cluster ring detection
    if KEY:
        groups, links = insider.cluster([c["wallet"] for c in cands], KEY)
        if groups:
            print(f"  ** RING: {len(groups)} operator cluster(s) via shared personal hub:", flush=True)
            for g in sorted(groups, key=len, reverse=True):
                print("       " + "  +  ".join(name.get(w, w[:10]) for w in g), flush=True)
        else:
            print("  no operator rings (traders independently / exchange-funded)", flush=True)


# news-driven, unscheduled-resolution markets (insider-prone)
MARKETS = [
    ("0x9352c559e9648ab4cab236087b64ca85c5b7123a4c7d9d7d4efde4a39c18056f", "Iranian regime fall by June 30?"),
    ("0x6114a8a3f9ac214f48a7e20d169f1c7a5c84082cb6f7058ed9fe1137b11fd0e7", "US x Iran permanent peace deal by June 30?"),
    ("0x68fbeb8d823552abf9d35f3ebdb8619a1a1d51b650da9101be62f09308fd18d2", "US announces Iran agreement/ceasefire extension"),
    ("0xdfd4d487d004c266493bdf32551d7f018c7eb4b9325f42ac368dd5075eec36a9", "Trump restart Project Freedom by June 30?"),
]

if __name__ == "__main__":
    print(f"RING HUNT · {len(MARKETS)} markets · top {TOP} traders each · "
          f"clustering {'ON' if KEY else 'OFF (no key)'}", flush=True)
    for cond, title in MARKETS:
        try:
            hunt_market(cond, title)
        except Exception as e:
            print(f"\n{title[:40]}: error {e}", flush=True)
    print("\nhunt complete.", flush=True)
