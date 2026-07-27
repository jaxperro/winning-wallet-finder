#!/usr/bin/env python3
"""Publish the walk-forward screened maker-sharp set for wwf-leanbot (#26).

The bot must NEVER screen on its own live data — that is how a study
starts selecting the wallets that happened to win during its own window.
So the set is computed here, on tape strictly BEFORE today, exactly as
maker_lean.py's screen_asof does (the frozen #22 method), and published
to research/params/maker_set.json for the box to fetch.

Wired into nightly.sh before the graders. Same contract as
informed_set.py (surgebot's input): committed file = frozen input.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tape                                    # noqa: E402
import maker_lean as ml                        # noqa: E402

OUT = os.path.join(HERE, "params", "maker_set.json")


def main():
    db = tape.connect()
    # midnight UTC today — the set may only see strictly-prior tape
    t_cut = int(time.time() // 86400) * 86400
    sharps = ml.screen_asof(db, t_cut)
    db.close()
    out = {
        "computed_at": int(time.time()),
        "as_of": t_cut,
        "method": "maker_lean.screen_asof (z>=2.5, >=6 resolved maker bets, "
                  "pnl>0) on orders_matched strictly before as_of",
        "frozen_params": {"lean_usd": [ml.LEAN_USD, 500.0],
                          "net_gross": ml.NET_GROSS, "band": list(ml.BAND)},
        "n": len(sharps),
        "wallets": sorted(sharps),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w"), indent=1)
    os.replace(tmp, OUT)
    print(f"[maker_set] {len(sharps)} wallets as-of "
          f"{time.strftime('%F', time.gmtime(t_cut))} -> params/maker_set.json",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
