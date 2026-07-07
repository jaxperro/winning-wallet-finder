#!/usr/bin/env python3
"""Pin the live paper bot's per-wallet conviction floors to the backtest's.

PARITY FIX (2026-07-07): the bot used to derive floors AT BOOT from the
data-api's most recent ~500 positions (copybot.derive_floor). The backtest
(portfolio.py) uses the TRUSTED cache p80 over ~180 days. When a wallet's
recent bets ran bigger than its long-term norm the two diverged hard —
fortuneking's boot floor hit $1,498 vs the backtest's $892, so the live bot
silently filtered out conviction bets the backtest counted (and the floor
drifted every restart). This writes the backtest's floor as a PINNED
`floor` on every wallet in copybot.paper.json, so both books gate on the
same threshold and boots are deterministic.

Uses the exact backtest method: trusted_wallet_rows (trust.py) deduped to
one row per market (largest stake), then cache.conv_cutoff (p80). Wired into
daily.sh after portfolio.py (cache warm, floors match that run); the publish
step commits copybot.paper.json so the next bot restart picks them up.

    python3 sync_floors.py        # writes live/copybot.paper.json floors
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache      # noqa: E402
import trust      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER = os.path.join(HERE, "copybot.paper.json")


def trusted_p80(wallet, now=None):
    """The wallet's conviction floor = p80 of its TRUSTED bet stakes, one row
    per market (largest stake) — identical to portfolio.py's conv_thr."""
    now = int(now or time.time())
    rows = trust.trusted_wallet_rows(cache.query, wallet, now)
    best = {}
    for cond, asset, won, p, res_t, size in rows:
        if cond not in best or size > best[cond]:
            best[cond] = size
    return cache.conv_cutoff(best.values())


def main():
    cfg = json.load(open(PAPER))
    ws = cfg.get("wallets") or []
    if not ws:
        print("[floors] copybot.paper.json has no wallets — nothing to do")
        return
    trust.ensure_cons(cache.query)
    now = int(time.time())
    changed = []
    for w in ws:
        p80 = trusted_p80(w["wallet"], now)
        if p80 == float("inf"):
            continue                        # no trusted sized bets — leave as-is
        new = round(p80, 2)
        old = w.get("floor")
        w["floor"] = new
        if old is None or abs((old or 0) - new) > 0.5:
            changed.append((w.get("name", w["wallet"][:8]), old, new))
    tmp = PAPER + ".tmp"
    json.dump(cfg, open(tmp, "w"), indent=2)
    os.replace(tmp, PAPER)
    print(f"[floors] pinned {len(ws)} trusted-p80 floors to copybot.paper.json"
          + (f"; changed: " + ", ".join(f"{n} {o}→{v:g}" for n, o, v in changed)
             if changed else " (no material change)"))


if __name__ == "__main__":
    main()
