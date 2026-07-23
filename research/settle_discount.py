#!/usr/bin/env python3
"""T7 EXPLORATORY (2026-07-23) — settlement-discount harvesting ("the last
3 cents"): outcome-known markets keep printing below $1 until formal
resolution, and redemption is fee-free. Two reads, both chain-true:

1. THE NICHE'S INCUMBENTS: wallets systematically BUYING at >=0.90 —
   their realized hit, per-share edge, holding time (proxy: token's last
   tape print = resolution-adjacent), and return on capital.
2. THE RESIDUAL: per entry-price bucket (90-95/95-97/97-99c), what did
   buying every such print return after refund/loss risk — the passive
   version of the trade at our size.

Kill: bucket edge < the copy book's return on the same capital-days, or
the incumbent census shows <5 wallets soaking all volume (saturated)."""
import sys

sys.path.insert(0, "/Users/jaxmakielski/polymarket-smart-money/research")
import tape                                    # noqa: E402

BUCKETS = [(0.90, 0.95, "90-95c"), (0.95, 0.97, "95-97c"),
           (0.97, 0.995, "97-99c")]


def main():
    db = tape.connect()
    tape.build_resolved(db)
    # every high-price BUY-side print on a tape-resolved token, joined to
    # payout + the token's terminal print time (holding proxy)
    rows = db.execute("""
      SELECT t.wallet, t.price::DOUBLE, t.size::DOUBLE, t.ts, tk.payout::DOUBLE, tk.last_ts,
             t.asset
      FROM trades t JOIN res_tok tk ON t.asset = tk.asset
      WHERE t.side = 'BUY' AND t.price >= 0.90 AND t.price < 0.995
        AND t.ts < tk.last_ts""").fetchall()
    print(f"high-price buy prints on resolved tokens: {len(rows):,}")
    # SCORER-LAW bias bound: high-price buys on tokens NOT tape-resolved
    # (pending/vetoed) are excluded — round 3 says upsets linger there, so
    # bucket loss-rates are LOWER BOUNDS. Size the exclusion:
    excl_vol, excl_n = db.execute("""
      SELECT coalesce(sum(t.price::DOUBLE * t.size::DOUBLE),0), count(*)
      FROM trades t LEFT JOIN res_tok tk ON t.asset = tk.asset
      WHERE t.side='BUY' AND t.price >= 0.90 AND t.price < 0.995
        AND tk.asset IS NULL""").fetchone()
    incl_vol = sum(r[1] * r[2] for r in rows)
    print(f"excluded (unresolved-on-tape) volume: ${excl_vol:,.0f} "
          f"({excl_n:,} prints) vs included ${incl_vol:,.0f} — loss rates "
          f"below are LOWER BOUNDS (round-3 direction)")
    # 2. residual per bucket
    for lo, hi, tag in BUCKETS:
        rs = [r for r in rows if lo <= r[1] < hi]
        if not rs:
            continue
        n = len(rs)
        usd = sum(r[1] * r[2] for r in rs)
        pnl = sum((r[4] - r[1]) * r[2] for r in rs)
        losses = sum(1 for r in rs if r[4] == 0.0)
        hold_h = sum((r[5] - r[3]) for r in rs) / n / 3600
        ret = pnl / usd if usd else 0
        ann = ret / max(hold_h / 8760, 1e-9)
        print(f"  {tag}: {n:,} prints · ${usd:,.0f} vol · ret {ret*100:+.2f}%"
              f" · loss-rate {losses/n:.3%} · avg hold {hold_h:.1f}h · "
              f"annualized {ann*100:+,.0f}%")
    # 1. incumbent census (>=0.95 specialists)
    agg = {}
    for w, p, z, ts, pay, lts, a in rows:
        if p < 0.95:
            continue
        d = agg.setdefault(w, [0, 0.0, 0.0, 0.0])
        d[0] += 1
        d[1] += p * z
        d[2] += (pay - p) * z
        d[3] += (lts - ts) * p * z          # capital-seconds
    inc = [(v[1], w, v) for w, v in agg.items()
           if v[0] >= 20 and v[1] >= 500]      # rank census by VOLUME
    inc.sort(reverse=True)
    print(f"\nincumbents (>=20 buys @>=0.95, >=$500 vol): {len(inc)}")
    tot_vol = sum(v[1] for _, _, v in inc)
    print(f"their combined volume: ${tot_vol:,.0f} · "
          f"top5 share {sum(v[1] for _,_,v in inc[:5])/max(tot_vol,1):.0%}")
    for _, w, (n, usd, pnl, capsec) in inc[:8]:
        hold_h = (capsec / usd) / 3600 if usd else 0
        print(f"  {w[:14]} n={n:<5} vol ${usd:>10,.0f} · ret "
              f"{pnl/usd*100:+.2f}% · avg hold {hold_h:.1f}h")


if __name__ == "__main__":
    main()
