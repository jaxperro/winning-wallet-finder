#!/usr/bin/env python3
"""Rank wallets by edge from the ingested DuckDB — with the guardrails that
stop a massive scan from just surfacing luck.

Edge metric: z = (wins - Σp)/√Σp(1-p), wins above what entry odds implied.
A high win rate alone is meaningless (buy 95¢ favorites -> 90% wins, no edge),
and on this data win rate isn't even biased-high the way the data-api is.

Guardrails:
  * min_n resolved bets and a market-maker cap on num_trades (a 300k-trade
    grinder posts huge z with no information — see FINDINGS.md / bjprolo).
  * Benjamini-Hochberg FDR: scan 100k wallets and thousands clear z>3 by
    chance. We report how many survive a 5% false-discovery rate.
  * Out-of-sample: --cutoff scores wallets on bets resolved on/before a date,
    then measures the SAME wallets forward. Edge that's real persists; edge
    that's curve-fit (every strategy we tested) reverts to z~0 / 50%.

    python3 score.py --min-n 30 --max-trades 5000 --top 40
    python3 score.py --cutoff 2026-04-30 --min-n 30   # in-sample vs forward
"""

import argparse
import math
import time

import duckdb

DB = "pmkt.duckdb"


def norm_sf(z):
    """One-sided P(Z > z): the probability this z came from luck."""
    return 0.5 * math.erfc(z / math.sqrt(2)) if z is not None else 1.0


def load_sql(cutoff_ts, min_n):
    sql = open("edge.sql").read()
    # these are ints we control (not user strings) -> safe to template
    return sql.replace(":cutoff_ts", str(int(cutoff_ts))).replace(":min_n", str(int(min_n)))


def rank(con, cutoff_ts, min_n, max_n):
    rows = con.execute(load_sql(cutoff_ts, min_n)).fetchall()
    cols = [d[0] for d in con.description]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        # num_trades is null in this subgraph, so use the resolved-bet count as
        # the market-maker proxy: a wallet with thousands of bets is grinding a
        # systematic edge (bjprolo-style), not trading on information.
        if max_n and d["n"] > max_n:
            continue
        d["pval"] = norm_sf(d["z"])
        out.append(d)
    return out


def bh_fdr(rows, q=0.05):
    """Benjamini-Hochberg: how many discoveries survive a q false-discovery rate."""
    m = len(rows)
    if not m:
        return 0, 1.0
    ps = sorted(r["pval"] for r in rows)
    k = 0
    for i, p in enumerate(ps, 1):
        if p <= q * i / m:
            k = i
    thresh = ps[k - 1] if k else 0.0
    return k, thresh


def to_ts(date_str):
    if not date_str:
        return 0
    return time.mktime(time.strptime(date_str, "%Y-%m-%d"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-n", type=int, default=15, help="min resolved bets")
    ap.add_argument("--max-n", type=int, default=3000,
                    help="exclude wallets with more resolved bets (market-maker proxy); 0=off")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--cutoff", help="YYYY-MM-DD: in-sample/out-of-sample split")
    args = ap.parse_args()

    con = duckdb.connect(DB, read_only=True)
    cutoff_ts = to_ts(args.cutoff)
    rows = rank(con, cutoff_ts, args.min_n, args.max_n)
    rows.sort(key=lambda r: (r["z"] is not None, r["z"]), reverse=True)

    k, thresh = bh_fdr(rows)
    label = f"on/before {args.cutoff}" if args.cutoff else "all resolved bets"
    print(f"\nscored {len(rows):,} wallets (min_n={args.min_n}, "
          f"max_n={args.max_n or '∞'}) · {label}")
    print(f"Benjamini-Hochberg @5% FDR: {k:,} wallets survive (p ≤ {thresh:.1e}) "
          f"— the rest of the high-z tail is consistent with luck\n")

    hdr = f"{'z':>6}{'p(luck)':>10}{'rec':>13}{'win%':>7}{'avgP':>7}{'volume':>12}{'profit':>11}  wallet"
    print(hdr); print("-" * len(hdr))
    for r in rows[:args.top]:
        rec = f"{r['wins']}/{r['n']}(E{r['exp_wins']:.0f})"
        p = r["pval"]
        ps = "<1e-12" if p <= 0 else (f"{p:.1e}" if p < 1e-3 else f"{p:.3f}")
        star = " *" if p <= thresh and thresh > 0 else "  "
        print(f"{r['z']:>6.1f}{ps:>10}{rec:>13}{r['win_rate']:>6.1f}%{r['avg_entry']:>7.2f}"
              f"{(r['volume'] or 0):>12,.0f}{(r['profit'] or 0):>11,.0f}{star}{r['user_id']}")

    if args.cutoff:
        forward_oos(con, rows[:args.top], cutoff_ts, args.min_n)


def forward_oos(con, picks, cutoff_ts, min_n):
    """For the in-sample top picks, measure their record AFTER the cutoff."""
    print(f"\n{'='*70}\nOUT-OF-SAMPLE: same wallets, only bets resolved AFTER cutoff")
    print(f"{'='*70}")
    ids = [r["user_id"] for r in picks]
    if not ids:
        return
    # reuse the same join but flip the time filter and restrict to these wallets
    sql = open("edge.sql").read()
    sql = sql.replace("WHERE b.resolution_ts <= :cutoff_ts OR :cutoff_ts = 0",
                      f"WHERE b.resolution_ts > {int(cutoff_ts)} "
                      f"AND b.user_id IN ({','.join(repr(i) for i in ids)})")
    sql = sql.replace("HAVING count(*) >= :min_n", "HAVING count(*) >= 1")
    fwd = {r[0]: r for r in con.execute(sql).fetchall()}
    cols = [d[0] for d in con.description]
    zi, wi, ni, wri = cols.index("z"), cols.index("wins"), cols.index("n"), cols.index("win_rate")
    print(f"{'in-sample z':>12}{'fwd z':>8}{'fwd rec':>12}{'fwd win%':>9}  wallet")
    for r in picks:
        f = fwd.get(r["user_id"])
        if f:
            fz = f"{f[zi]:.1f}" if f[zi] is not None else "n/a"
            print(f"{r['z']:>12.1f}{fz:>8}{f'{f[wi]}/{f[ni]}':>12}{f[wri]:>8.1f}%  {r['user_id']}")
        else:
            print(f"{r['z']:>12.1f}{'—':>8}{'(no fwd bets)':>12}{'—':>9}  {r['user_id']}")
    fz = [fwd[i][zi] for i in ids if i in fwd and fwd[i][zi] is not None]
    if fz:
        print(f"\nmedian forward z of in-sample winners: {sorted(fz)[len(fz)//2]:.2f}  "
              f"(near 0 = the in-sample edge did NOT persist)")


if __name__ == "__main__":
    main()
