#!/usr/bin/env python3
"""Execution-realistic re-ranking of the selected wallets.

Pulls entry timestamps (cached) for the strategy.py shortlist, then judges each
wallet on whether we could actually FOLLOW it with $1,000:

  * lead time = resolution - entry. Bets that resolve within MIN_LEAD_H of the
    wallet's entry (live/in-game/instant markets) are NOT copyable — you can't
    see and mirror the trade in time. We drop them.
  * cadence = distinct markets entered per active day. Extreme cadence = a bot
    we can't hand-follow.

Then it recomputes forward (June1+, resolved) copy-ROI on ONLY the followable
bets — the realistic number — and re-ranks. Output: watch_final.json.

    python3 followability.py
"""

import json
import os
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cache

HERE = os.path.dirname(__file__)
TEST_START = time.mktime(time.strptime(os.environ.get("TEST_START", "2026-06-01"), "%Y-%m-%d"))
MIN_LEAD_H = 1.0          # a bet must resolve >= 1h after entry to be followable
MED_LEAD_MIN = 2.0        # wallet's median lead must clear this to qualify
MAX_CADENCE = 50.0        # entries/day above this = bot-like, hard to follow
MIN_FOLL_FRAC = 0.5       # >= half the wallet's bets must be followable
MIN_FWD_FOLL = 5          # need this many followable forward bets to rank


def ret(p, won):
    return (1 - p) / p if won else -1.0


def assess(wallet):
    ent = cache.get_entries(wallet)            # {cond: first_buy_ts}
    bets = cache.get_bets(wallet)              # cached
    leads = []
    for b in bets:
        e = ent.get(b["cond"])
        if e and b.get("res_t"):
            lh = (b["res_t"] - e) / 3600.0
            if lh >= 0:
                leads.append((b, lh))
    if not leads:
        return None
    med_lead = st.median([lh for _, lh in leads])
    foll_frac = sum(1 for _, lh in leads if lh >= MIN_LEAD_H) / len(leads)
    ts = sorted(ent.values())
    span = max(1.0, (ts[-1] - ts[0]) / 86400) if len(ts) > 1 else 1.0
    cadence = len(ent) / span
    # forward, followable only
    fwd_foll = [b for b, lh in leads if b["res_t"] >= TEST_START and lh >= MIN_LEAD_H]
    fwd_all = [b for b, lh in leads if b["res_t"] >= TEST_START]
    foll_roi = (sum(ret(b["p"], b["won"]) for b in fwd_foll) / len(fwd_foll)
                if fwd_foll else None)
    raw_roi = (sum(ret(b["p"], b["won"]) for b in fwd_all) / len(fwd_all)
               if fwd_all else None)
    wins = sum(1 for b in fwd_foll if b["won"])
    return dict(wallet=wallet, med_lead=med_lead, foll_frac=foll_frac, cadence=cadence,
                fwd_foll_n=len(fwd_foll), foll_roi=foll_roi, raw_roi=raw_roi,
                fwd_win=100 * wins / len(fwd_foll) if fwd_foll else 0)


def main():
    sel = json.load(open(os.path.join(HERE, "selection.json")))
    wallets = [c["wallet"] for c in sel]
    meta = {c["wallet"]: c for c in sel}
    print(f"assessing followability of {len(wallets)} selected wallets "
          f"(entry-time pull, cached)…\n", flush=True)

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for r in ex.map(assess, wallets):
            done += 1
            if r:
                rows.append(r)
            if done % 30 == 0:
                print(f"  {done}/{len(wallets)}", flush=True)

    # followability gates
    foll = [r for r in rows if
            r["med_lead"] >= MED_LEAD_MIN and
            r["foll_frac"] >= MIN_FOLL_FRAC and
            r["cadence"] <= MAX_CADENCE]
    ranked = [r for r in foll if r["fwd_foll_n"] >= MIN_FWD_FOLL]
    ranked.sort(key=lambda r: r["foll_roi"], reverse=True)

    dropped = len(rows) - len(foll)
    print(f"\n{len(rows)} assessed · {dropped} dropped as un-followable "
          f"(fast/live markets or bot cadence) · {len(foll)} copyable\n")
    if ranked:
        pooled_num = sum(r["foll_roi"] * r["fwd_foll_n"] for r in ranked)
        pooled_den = sum(r["fwd_foll_n"] for r in ranked)
        pos = sum(1 for r in ranked if r["foll_roi"] > 0)
        print(f"FOLLOWABLE forward verdict ({len(ranked)} wallets w/ >= {MIN_FWD_FOLL} "
              f"followable June+ bets):")
        print(f"  {pos}/{len(ranked)} profitable · pooled followable copy-ROI "
              f"{pooled_num/pooled_den:+.1%} · median {st.median([r['foll_roi'] for r in ranked]):+.1%}\n")

    h = (f"{'foll_roi':>9}{'raw_roi':>8}{'medLeadH':>9}{'foll%':>6}{'cad/d':>7}"
         f"{'fwd_n':>6}{'tr_z':>6}  wallet")
    print(h); print("-" * len(h))
    for r in ranked[:40]:
        m = meta[r["wallet"]]
        raw = f"{r['raw_roi']:+.0%}" if r["raw_roi"] is not None else "—"
        print(f"{r['foll_roi']:>+8.0%}{raw:>8}{r['med_lead']:>8.1f}h{r['foll_frac']*100:>5.0f}%"
              f"{r['cadence']:>7.1f}{r['fwd_foll_n']:>6}{m['train_z']:>6.1f}  {r['wallet']}")

    out = [{"wallet": r["wallet"], "name": r["wallet"][:10],
            "foll_fwd_copy_roi": round(r["foll_roi"], 4),
            "med_lead_h": round(r["med_lead"], 1), "cadence_per_day": round(r["cadence"], 1),
            "followable_frac": round(r["foll_frac"], 2), "fwd_followable_n": r["fwd_foll_n"],
            "train_z": meta[r["wallet"]]["train_z"],
            "train_copy_roi": meta[r["wallet"]]["train_copy_roi"]} for r in ranked]
    json.dump(out, open(os.path.join(HERE, "watch_final.json"), "w"), indent=2)
    print(f"\n-> watch_final.json ({len(out)} execution-realistic wallets)")


if __name__ == "__main__":
    main()
