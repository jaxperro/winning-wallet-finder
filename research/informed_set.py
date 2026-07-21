#!/usr/bin/env python3
"""Publish the frozen-method informed set for the surge paper harness.

Writes params/informed_set.json — the top-150 wallets by the FROZEN Study-A
scoring (study_flow.informed_set, params/study_flow.json), as-of now. The
wwf-surgebot worker fetches this from raw.githubusercontent at boot and
every 6h; nightly.sh regenerates + commits it daily so the harness trades
the same signal the forward ledger scores. Identity showed NO lift over
random controls (#16) — the set is kept anyway because the FROZEN signal
definition uses it, and the verdict must validate exactly what was frozen.
"""
import json
import os
import time

import tape
import study_flow as sf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "params", "informed_set.json")


def main():
    db = tape.connect()
    now = db.execute("SELECT max(ts) FROM trades").fetchone()[0]
    P = json.load(open(os.path.join(HERE, "params", "study_flow.json")))
    fz = P["frozen"]
    wallets = sf.informed_set(db, now, fz["top_n"])
    json.dump({"generated_at": int(time.time()), "as_of_tape_ts": int(now),
               "method": "study_flow.informed_set FROZEN "
                         f"(top_n={fz['top_n']}, z>={sf.SET_MIN_Z}, "
                         f"n>={sf.SET_MIN_BETS})",
               "wallets": wallets},
              open(OUT, "w"), indent=1)
    print(f"[informed_set] {len(wallets)} wallets -> {OUT}")


if __name__ == "__main__":
    main()
