#!/usr/bin/env python3
"""Find more wallets with the top-wallet profile: their HIGH-CONVICTION (large-
stake) bets win often on genuinely-uncertain (~0.4-0.6) markets — real edge, not
favorite-riding — and it persists out-of-sample.

TRAIN = conviction bets resolved before June 1. TEST = June 1+ resolved.

A "conviction" bet = one in the top 20% (p80) of THAT wallet's own stake sizes,
computed per-wallet over its full cached window — replacing the old flat $200.
Validated to reproduce flat-$200's win-rate lift while adapting to each wallet's
scale (kept in sync with cache.CONV_PCTILE and trading/index.html).

Profile gates (on TRAIN conviction bets):
  * >= MIN_N conviction bets
  * win rate >= WIN_MIN
  * avg entry in [ENTRY_LO, ENTRY_HI]  (excludes 0.9 favorite-riders)
  * copy-ROI > 0  and  z significant (BH-FDR)
Then validate forward and count how many keep the profile.
"""

import math, os, time
import duckdb

HERE = os.path.dirname(__file__)
JUN1 = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))
CONV_PCTILE = 0.80      # "conviction" = a bet in the top 20% of the wallet's own stakes
MIN_N = 12              # train conviction bets needed
WIN_MIN = 0.65
ENTRY_LO, ENTRY_HI = 0.30, 0.75
MIN_TEST = 3
FDR_Q = 0.05


def r(p, won): return (1 - p) / p if won else -1.0
def sf(z): return 0.5 * math.erfc(z / math.sqrt(2))


def stats(bets):
    n = len(bets); wins = sum(1 for b in bets if b[1])
    exp = sum(b[0] for b in bets); var = sum(b[0] * (1 - b[0]) for b in bets) or 1e-9
    return n, wins, 100 * wins / n, sum(r(b[0], b[1]) for b in bets) / n, \
        (wins - exp) / math.sqrt(var), sum(b[0] for b in bets) / n


def main():
    con = duckdb.connect(os.path.join(HERE, "cache.duckdb"), read_only=True)
    # per-wallet conviction cutoff = p80 of that wallet's own positive stakes, then
    # keep only its bets at/above that cutoff (its top ~20% by size).
    rows = con.execute(
        "WITH thr AS (SELECT wallet, quantile_cont(size, ?) AS t "
        "             FROM bets WHERE size > 0 GROUP BY wallet) "
        "SELECT b.wallet, b.p, b.won, b.res_t "
        "FROM bets b JOIN thr ON b.wallet = thr.wallet "
        "WHERE b.size > 0 AND b.size >= thr.t", [CONV_PCTILE]).fetchall()
    byw = {}
    for w, p, won, rt in rows:
        byw.setdefault(w, []).append((max(0.001, min(0.999, p or 0)), won, rt or 0))

    cand = []
    for w, bets in byw.items():
        tr = [(p, won) for p, won, rt in bets if rt < JUN1]
        if len(tr) < MIN_N:
            continue
        n, wins, wr, roi, z, ap = stats(tr)
        if wr >= WIN_MIN * 100 and ENTRY_LO <= ap <= ENTRY_HI and roi > 0:
            te = [(p, won) for p, won, rt in bets if rt >= JUN1]
            tm = stats(te) if len(te) >= MIN_TEST else None
            cand.append(dict(w=w, n=n, wr=wr, roi=roi, z=z, ap=ap, tm=tm, ntest=len(te)))

    # FDR on the edge p-values
    ps = sorted(sf(c["z"]) for c in cand)
    k = 0
    for i, p in enumerate(ps, 1):
        if p <= FDR_Q * i / len(ps): k = i
    thr = ps[k - 1] if k else 0.0
    sel = sorted([c for c in cand if sf(c["z"]) <= thr and thr > 0],
                 key=lambda c: c["roi"], reverse=True)

    print(f"wallets with >= {MIN_N} conviction bets (top {1-CONV_PCTILE:.0%} by stake) pre-June: {len(byw):,} scanned")
    print(f"matching the profile (win>= {WIN_MIN:.0%}, entry {ENTRY_LO}-{ENTRY_HI}, "
          f"+ROI, FDR-significant): {len(sel)}\n")

    fwd = [c for c in sel if c["tm"]]
    if fwd:
        kept = sum(1 for c in fwd if c["tm"][2] >= WIN_MIN * 100 and c["tm"][3] > 0)
        prof = sum(1 for c in fwd if c["tm"][3] > 0)
        from math import comb
        nf = len(fwd); pt = sum(comb(nf, j) for j in range(prof, nf + 1)) / 2 ** nf
        poolnum = sum(c["tm"][3] * c["ntest"] for c in fwd); poolden = sum(c["ntest"] for c in fwd)
        print(f"FORWARD (June conviction bets, {len(fwd)} wallets w/ >= {MIN_TEST}):")
        print(f"  {prof}/{nf} stayed profitable (binomial p={pt:.4f}) · "
              f"{kept}/{nf} kept the full profile (win>= {WIN_MIN:.0%} AND +ROI)")
        print(f"  pooled forward conviction copy-ROI: {poolnum/poolden:+.1%}\n")

    h = f"{'tr_win':>7}{'tr_roi':>7}{'tr_z':>6}{'entry':>6}{'tr_n':>5}{'fw_win':>7}{'fw_roi':>7}{'fw_n':>5}  wallet"
    print(h); print("-" * len(h))
    for c in sel[:35]:
        t = c["tm"]
        fw = f"{t[2]:.0f}%" if t else "—"; fr = f"{t[3]:+.0%}" if t else "—"
        print(f"{c['wr']:>6.0f}%{c['roi']:>+6.0%}{c['z']:>6.1f}{c['ap']:>6.2f}{c['n']:>5}"
              f"{fw:>7}{fr:>7}{c['ntest']:>5}  {c['w']}")
    import json
    json.dump([{"wallet": c["w"], "name": c["w"][:10], "train_win": round(c["wr"], 1),
                "train_conv_roi": round(c["roi"], 3), "train_z": round(c["z"], 2),
                "avg_entry": round(c["ap"], 2), "train_n": c["n"],
                "fwd_win": round(c["tm"][2], 1) if c["tm"] else None,
                "fwd_conv_roi": round(c["tm"][3], 3) if c["tm"] else None,
                "fwd_n": c["ntest"]} for c in sel],
              open(os.path.join(HERE, "conviction_wallets.json"), "w"), indent=2)
    print(f"\n-> conviction_wallets.json ({len(sel)} wallets)")


if __name__ == "__main__":
    main()
