#!/usr/bin/env python3
"""Train/test wallet-selection study, entirely from the local cache.

TRAIN  = bets resolved before --train-end (default 2026-05-30): pick wallets.
TEST   = bets resolved on/after --test-start (default 2026-06-01), resolved only:
         validate which picks were actually profitable to follow forward.

Lens: we have $1,000 and copy flat-size, so the key metric is COPY-ROI — the
mean per-bet return if we mirror each entry with the same stake:
    win  -> (1-p)/p   per $1   (entry at price p pays out at 1)
    loss -> -1
A wallet with positive copy-ROI would have made us money (before lag/fees).
We also compute z (beats entry prices) and the wallet's own $ ROI.

Selection gates (all on TRAIN):
  * n >= MIN_N resolved bets
  * z significant (Benjamini-Hochberg FDR)
  * copy-ROI > 0  (copying them actually paid)
  * consistent: positive copy-ROI in >= CONSISTENCY of monthly buckets
  * copyable proxies: median bet size in [$5,$5000] (not dust, not whale),
    not at the ~2000-bet HFT cap, not one-market-dependent

    python3 strategy.py
"""

import math
import os
import sys
import time
from collections import defaultdict

import duckdb

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "cache.duckdb")
TRAIN_END = time.mktime(time.strptime(os.environ.get("TRAIN_END", "2026-05-30"), "%Y-%m-%d"))
TEST_START = time.mktime(time.strptime(os.environ.get("TEST_START", "2026-06-01"), "%Y-%m-%d"))
MIN_N = 30                # train bets needed to judge a wallet
MIN_N_TEST = 5            # forward bets needed to report a forward number
CAP = 1990               # >= this looks API-capped (HFT/truncated) -> deprioritize
SIZE_LO, SIZE_HI = 5.0, 5000.0     # copyable median bet size ($)
CONSISTENCY = 0.6        # fraction of monthly buckets that must be profitable
FDR_Q = 0.05


def ret(p, won):
    return (1 - p) / p if won else -1.0       # copy return per $1 staked


def metrics(bets):
    n = len(bets)
    wins = sum(1 for b in bets if b["won"])
    exp = sum(b["p"] for b in bets)
    var = sum(b["p"] * (1 - b["p"]) for b in bets) or 1e-9
    z = (wins - exp) / math.sqrt(var)
    copy_roi = sum(ret(b["p"], b["won"]) for b in bets) / n
    staked = sum(b["size"] for b in bets) or 1e-9
    dollars = sum((b["size"] * (1 - b["p"]) / b["p"] if b["won"] else -b["size"]) for b in bets)
    return dict(n=n, wins=wins, win_rate=100 * wins / n, z=z, copy_roi=copy_roi,
                own_roi=dollars / staked, exp=exp)


def consistency(bets):
    """fraction of calendar-month buckets (by res_t) with positive copy-ROI."""
    buck = defaultdict(list)
    for b in bets:
        buck[time.strftime("%Y-%m", time.localtime(b["res_t"] or 0))].append(b)
    months = [m for m in buck.values() if len(m) >= 5]
    if not months:
        return 0.0, 0
    good = sum(1 for m in months if sum(ret(b["p"], b["won"]) for b in m) > 0)
    return good / len(months), len(months)


def concentration(bets):
    """share of total copy-return coming from the single best market."""
    by = defaultdict(float)
    for b in bets:
        by[b["cond"]] += ret(b["p"], b["won"])
    tot = sum(v for v in by.values() if v > 0) or 1e-9
    return max(by.values()) / tot if by else 1.0


def norm_sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2))


def main():
    con = duckdb.connect(DB, read_only=True)
    wallets = [r[0] for r in con.execute("SELECT DISTINCT wallet FROM bets").fetchall()]
    print(f"loaded {len(wallets):,} wallets; train<{time.strftime('%F', time.localtime(TRAIN_END))}"
          f" test>={time.strftime('%F', time.localtime(TEST_START))}\n", flush=True)

    cand = []
    for w in wallets:
        rows = con.execute(
            "SELECT won,p,res_t,size,cond FROM bets WHERE wallet=?", [w]).fetchall()
        bets = [dict(won=x[0], p=max(0.001, min(0.999, x[1] or 0)),
                     res_t=x[2], size=x[3] or 0, cond=x[4]) for x in rows]
        train = [b for b in bets if (b["res_t"] or 0) < TRAIN_END]
        if len(train) < MIN_N:
            continue
        m = metrics(train)
        med = sorted(b["size"] for b in train)[len(train) // 2]
        cons, nmonths = consistency(train)
        conc = concentration(train)
        test = [b for b in bets if (b["res_t"] or 0) >= TEST_START]
        tm = metrics(test) if len(test) >= MIN_N_TEST else None
        cand.append(dict(w=w, m=m, med=med, cons=cons, nmonths=nmonths, conc=conc,
                         capped=len(train) >= CAP, tm=tm, ntest=len(test)))

    # FDR over the candidate edge p-values
    pvals = sorted(norm_sf(c["m"]["z"]) for c in cand)
    k = 0
    for i, p in enumerate(pvals, 1):
        if p <= FDR_Q * i / len(pvals):
            k = i
    thr = pvals[k - 1] if k else 0.0

    # selection gates (all on TRAIN)
    sel = [c for c in cand if
           norm_sf(c["m"]["z"]) <= thr and thr > 0 and
           c["m"]["copy_roi"] > 0 and
           c["cons"] >= CONSISTENCY and
           SIZE_LO <= c["med"] <= SIZE_HI and
           not c["capped"] and
           c["conc"] < 0.5]
    sel.sort(key=lambda c: c["m"]["copy_roi"], reverse=True)

    print(f"{len(cand):,} wallets with >= {MIN_N} train bets · BH thr p<= {thr:.1e}")
    print(f"SELECTED (skilled + consistent + copyable on TRAIN): {len(sel)}\n")

    # how did the SELECTED set do FORWARD?
    fwd = [c for c in sel if c["tm"]]
    if fwd:
        avg_fwd = sum(c["tm"]["copy_roi"] for c in fwd) / len(fwd)
        win_fwd = sum(1 for c in fwd if c["tm"]["copy_roi"] > 0)
        print(f"FORWARD (June1+, resolved): {len(fwd)} selected wallets had test bets · "
              f"{win_fwd}/{len(fwd)} stayed profitable · mean fwd copy-ROI {avg_fwd:+.1%}\n")

    h = (f"{'train_roi':>10}{'fwd_roi':>9}{'tr_z':>6}{'tr_wr':>6}{'cons':>6}"
         f"{'medSz':>7}{'tr_n':>6}{'fwd_n':>6}  wallet")
    print(h); print("-" * len(h))
    for c in sel[:40]:
        t = c["tm"]
        fr = f"{t['copy_roi']:+.0%}" if t else "  —"
        fn = c["ntest"] if t else 0
        print(f"{c['m']['copy_roi']:>+9.0%}{fr:>9}{c['m']['z']:>6.1f}{c['m']['win_rate']:>5.0f}%"
              f"{c['cons']:>6.0%}{c['med']:>7.0f}{c['m']['n']:>6}{fn:>6}  {c['w']}")
    # persist the selection for the followability pull + watchlist
    import json
    json.dump([{"wallet": c["w"], "train_copy_roi": round(c["m"]["copy_roi"], 4),
                "train_z": round(c["m"]["z"], 2), "train_n": c["m"]["n"],
                "fwd_copy_roi": round(c["tm"]["copy_roi"], 4) if c["tm"] else None,
                "fwd_n": c["ntest"], "med_size": round(c["med"], 1),
                "consistency": round(c["cons"], 2)} for c in sel],
              open(os.path.join(HERE, "selection.json"), "w"), indent=2)
    print(f"\n-> selection.json ({len(sel)} wallets)")


if __name__ == "__main__":
    main()
