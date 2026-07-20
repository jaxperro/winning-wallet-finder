#!/usr/bin/env python3
"""Crater -> requote timing: after an aggressive up-move print (>= 3c above
the previous print, the shape our FAK misses die in), how long until the
SAME token prints again — i.e. how long does the crater stay empty?

Directly tunes copybot's fak_retry_s (currently a flat 10s): the retry
should arrive when liquidity is back, per niche. Pure measurement, no bot
changes here.
"""
import json
import os
import time

import tape

JUMP = 0.03


def run():
    db = tape.connect()
    rows = db.execute(f"""
    WITH p AS (
      SELECT asset, ts, price, title,
             lag(price) OVER (PARTITION BY asset ORDER BY ts, tx) prev_p,
             lead(ts)   OVER (PARTITION BY asset ORDER BY ts, tx) next_ts
      FROM trades
    )
    SELECT title, ts, next_ts - ts AS gap
    FROM p
    WHERE prev_p IS NOT NULL AND price - prev_p >= {JUMP}
      AND next_ts IS NOT NULL
    """).fetchall()
    by = {}
    for title, ts, gap in rows:
        by.setdefault(tape.niche(title), []).append(gap)
    out = {}
    print(f"{len(rows):,} crater prints (>= {JUMP:.02f} up-moves)\n")
    print(f"{'niche':<10} {'n':>8} {'p50':>7} {'p75':>7} {'p90':>7} "
          f"{'<=4s':>6} {'<=10s':>6} {'<=25s':>6}")
    for niche, gaps in sorted(by.items(), key=lambda kv: -len(kv[1])):
        gaps.sort()
        n = len(gaps)
        q = lambda f: gaps[min(int(n * f), n - 1)]
        frac = lambda s: sum(g <= s for g in gaps) / n
        out[niche] = {"n": n, "p50": q(.5), "p75": q(.75), "p90": q(.9),
                      "within_4s": round(frac(4), 3),
                      "within_10s": round(frac(10), 3),
                      "within_25s": round(frac(25), 3)}
        print(f"{niche:<10} {n:>8,} {q(.5):>7.1f} {q(.75):>7.1f} {q(.9):>7.1f} "
              f"{frac(4):>6.0%} {frac(10):>6.0%} {frac(25):>6.0%}")
    out["_meta"] = {"jump": JUMP, "generated": time.strftime("%Y-%m-%d %H:%M")}
    json.dump(out, open(os.path.join(tape.HERE, "params", "requote_timing.json"),
                        "w"), indent=1)


if __name__ == "__main__":
    run()
