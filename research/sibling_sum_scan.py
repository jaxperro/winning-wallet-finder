#!/usr/bin/env python3
"""T4 EXPLORATORY (2026-07-23) — sibling-sum consistency: how often do a
binary market's two tokens price to YES+NO != $1 beyond fees, and does it
persist long enough to execute? Model-free structural arb:
  sum < 1 − fees  → buy both, merge to $1 (venue split/merge is native)
  sum > 1 + fees  → split $1, sell both
v0 substrate is PRINTS (co-active minutes: both legs printed in the same
UTC minute — staleness-safe, undercounts violations that sat in books
without printing). Fees modeled taker-side both legs (worst case; merge/
split itself is free). Persistence = consecutive violating minutes.
Phase 2 (only if rich): a live book scanner. NOT pre-registered."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402

FEE = 0.03
MIN_PROFIT = 0.005                 # 0.5c/share after fees


def main():
    db = tape.connect()
    print("pairing binary conds (exactly 2 assets on tape)…")
    db.execute("""
    CREATE TEMP TABLE pairs AS
    SELECT cond, min(asset) a1, max(asset) a2
    FROM (SELECT DISTINCT cond, asset FROM trades
          WHERE cond IS NOT NULL AND cond != '')
    GROUP BY cond HAVING count(DISTINCT asset) = 2""")
    n_pairs, = db.execute("SELECT count(*) FROM pairs").fetchone()
    print(f"binary markets: {n_pairs:,}")
    rows = db.execute(f"""
    WITH m AS (
      SELECT t.cond, cast(floor(t.ts/60) AS BIGINT) mnt, t.asset,
             arg_max(t.price, t.ts) px,
             any_value(t.title) title
      FROM trades t JOIN pairs p ON t.cond = p.cond
      GROUP BY 1, 2, 3
    ), co AS (
      SELECT cond, mnt, any_value(title) title,
             min(px) pa, max(px) pb, sum(px) s, count(*) legs
      FROM m GROUP BY cond, mnt HAVING count(*) = 2
    )
    SELECT cond, mnt, title, pa, pb, s,
           (1 - s) - {FEE}*(least(pa,1-pa) + least(pb,1-pb)) buy_profit,
           (s - 1) - {FEE}*(least(pa,1-pa) + least(pb,1-pb)) sell_profit
    FROM co""").fetchall()
    n_co = len(rows)
    buys = [r for r in rows if r[6] > MIN_PROFIT]
    sells = [r for r in rows if r[7] > MIN_PROFIT]
    print(f"co-active market-minutes: {n_co:,}")
    print(f"buy-both arbs  (sum<1−fees): {len(buys):,} "
          f"({100*len(buys)/max(n_co,1):.2f}%)")
    print(f"split-sell arbs (sum>1+fees): {len(sells):,} "
          f"({100*len(sells)/max(n_co,1):.2f}%)")
    for tag, vs, idx in (("BUY-BOTH", buys, 6), ("SPLIT-SELL", sells, 7)):
        if not vs:
            continue
        prof = sorted(r[idx] for r in vs)
        print(f"\n{tag}: profit/share p50 {prof[len(prof)//2]*100:.1f}c · "
              f"p90 {prof[int(len(prof)*.9)]*100:.1f}c · "
              f"max {prof[-1]*100:.1f}c")
        # persistence: consecutive violating minutes per cond
        by_cond = {}
        for r in vs:
            by_cond.setdefault(r[0], []).append(r[1])
        runs = []
        for mins in by_cond.values():
            mins.sort()
            run = 1
            for i in range(1, len(mins)):
                if mins[i] == mins[i-1] + 1:
                    run += 1
                else:
                    runs.append(run)
                    run = 1
            runs.append(run)
        runs.sort()
        print(f"  persistence: {len(by_cond)} markets · runs p50 "
              f"{runs[len(runs)//2]}min · p90 {runs[int(len(runs)*.9)]}min "
              f"· max {runs[-1]}min")
        top = sorted(vs, key=lambda r: -r[idx])[:5]
        for r in top:
            print(f"  {r[idx]*100:5.1f}c/sh · sum {r[5]:.3f} · {r[2][:56]}")


if __name__ == "__main__":
    main()
