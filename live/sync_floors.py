#!/usr/bin/env python3
"""Recompute the copy bot's per-wallet p80 conviction floors from the cache.

Keeps config.json's `follow.per_wallet_min_usd` in exact parity with the
dashboard's "top 20% by stake" gate as the watched wallets keep trading — using
the same cache.conv_cutoff() (p80) over each wallet's own bet sizes that the
pipeline and trading/index.html use. Only the *floors* are rewritten; the
curated watchlist itself is never touched.

Reads the wallet set from config.json's "watchlist" (falls back to "watch"), so
if you re-curate the portfolio the floors follow automatically. config.json is
gitignored, so this stays local and is never committed.

    python3 sync_floors.py        # run standalone; also wired into daily.sh
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache  # noqa: E402  — local bet cache + conv_cutoff (p80)

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "..", "config.json")


def main():
    if not os.path.exists(CFG):
        print("[floors] no ../config.json — nothing to do")
        return
    cfg = json.load(open(CFG))
    wallets = cfg.get("watchlist") or [w["wallet"] for w in cfg.get("watch", [])]
    if not wallets:
        print("[floors] config has no watchlist — nothing to do")
        return

    floors = {}
    for w in wallets:
        sizes = [b["size"] for b in cache.get_bets(w) if b.get("size")]
        p80 = cache.conv_cutoff(sizes)          # the dashboard's top-20% threshold
        if p80 != float("inf"):                 # skip wallets with no sized bets
            floors[w.lower()] = round(p80, 2)

    cfg.setdefault("follow", {})["per_wallet_min_usd"] = floors
    tmp = CFG + ".tmp"
    json.dump(cfg, open(tmp, "w"), indent=2)
    os.replace(tmp, CFG)                        # atomic write
    print(f"[floors] {len(floors)} p80 conviction floors updated: " +
          ", ".join(f"{w[:8]}…=${v:g}" for w, v in floors.items()))


if __name__ == "__main__":
    main()
