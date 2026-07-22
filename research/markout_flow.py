#!/usr/bin/env python3
"""EXPLORATORY (2026-07-22, user ask; CORRECTED same night) — markout-exit
curve for the surge signal: given the round-3 corrected verdict (surge
holds LOSE −$6/fill under chain truth, #16 KILL met), does exiting at a
fixed horizon beat holding — i.e. was there a scalp hiding inside a dead
hold-to-resolution strategy?

v0 of this script used res_tok only and concluded "hold wins everywhere"
(+$43/fill holds) — that conclusion was resolution-timing survivorship
(FINDINGS round 3): the resolved cohort IS the winners. This version
scores every fill with forward.payouts_for() (tape proxy + mandatory CTF
chain overlay, refunds as scratches) and splits cohorts explicitly.

Still prints-based on the EXIT leg (last print <= t+H ≈ optimistic vs the
real bid; the harnesses' markout re-reads at +60/300/1800s accrue the real
bid marks to haircut this). Exit fee charged same as entry. NOT
pre-registered — shapes (or kills) a possible A3-scalp hypothesis only."""
import json
import os
import time

import tape
import sim as simmod
import study_flow as sf
import forward as fwd

HERE = os.path.dirname(os.path.abspath(__file__))
HORIZONS = (60, 300, 1800, 7200)
FEE = simmod.FEE_RATE


def day_bounds(d):
    lo = time.mktime(time.strptime(d, "%Y-%m-%d")) - time.timezone
    return lo, lo + 86400


def main():
    db = tape.connect()
    cal = json.load(open(os.path.join(HERE, "params", "sim_calibration.json")))
    fz = json.load(open(os.path.join(HERE, "params", "study_flow.json")))["frozen"]
    t_min, t_max = db.execute("SELECT min(ts), max(ts) FROM trades").fetchone()
    days = []
    t = t_min
    while t < t_max:
        days.append(time.strftime("%Y-%m-%d", time.gmtime(t)))
        t += 86400
    tape.build_resolved(db)
    tape_resolved = {a for (a,) in db.execute(
        "SELECT asset FROM res_tok").fetchall()}
    sim = simmod.Sim(db, lag_s=simmod.LAG_P50, hold_s=cal["hold_s"],
                     fill="worst")
    fills = []
    for d in days:
        lo, hi = day_bounds(d)
        hi = min(hi, t_max)
        S = sf.informed_set(db, lo, fz["top_n"])
        trig = sf.signals(db, S, lo, hi, fz["window_s"], fz["flow_usd"])
        for t_ in trig:
            r = sim.try_buy(t_["asset"], t_["ts"], t_["p_ref"],
                            stake_usd=sf.STAKE)
            if r["filled"]:
                fills.append({"day": d, "fwd": d >= "2026-07-21",
                              "asset": t_["asset"], **r})
        print(f"{d}: {len(trig)} triggers")
    pays = fwd.payouts_for(db, [f["asset"] for f in fills])
    rows = []
    for f in fills:
        pay = pays.get(f["asset"])
        if pay is None:
            continue                     # truly unresolved even on chain
        row = {"day": f["day"], "fwd": f["fwd"], "px": f["price"],
               "cohort": "tape" if f["asset"] in tape_resolved else "chain",
               "hold_pnl": f["shares"] * (pay - f["price"]) - f["fee"],
               "win": pay == 1.0, "refund": pay == 0.5, "mo": {}}
        for H in HORIZONS:
            m = sim.markout(f["asset"], f["fill_ts"], H)
            if m is None:
                continue
            xfee = FEE * f["shares"] * min(m, 1 - m)
            row["mo"][H] = f["shares"] * (m - f["price"]) - f["fee"] - xfee
        rows.append(row)

    def report(tag, rs):
        if not rs:
            return
        n = len(rs)
        hold = sum(r["hold_pnl"] for r in rs)
        print(f"\n== {tag} — {n} chain-graded fills · HOLD EV/fill "
              f"{hold/n:+.2f} · hit {sum(r['win'] for r in rs)/n:.2f}"
              + (f" · {sum(r['refund'] for r in rs)} refunds"
                 if any(r["refund"] for r in rs) else ""))
        for H in HORIZONS:
            sub = [r for r in rs if H in r["mo"]]
            if not sub:
                continue
            mo = sum(r["mo"][H] for r in sub)
            hold_sub = sum(r["hold_pnl"] for r in sub)
            print(f"  exit +{H:>5}s: EV/fill {mo/len(sub):+7.2f} vs hold "
                  f"{hold_sub/len(sub):+7.2f} on same {len(sub)} "
                  f"({100*len(sub)/n:.0f}% coverage)")

    fwd_rows = [r for r in rows if r["fwd"]]
    report("ALL days", rows)
    report("FORWARD days (>= 07-21)", fwd_rows)
    report("FORWARD · tape-resolved cohort (the old scorer's sample)",
           [r for r in fwd_rows if r["cohort"] == "tape"])
    report("FORWARD · chain-only cohort (the hidden losses)",
           [r for r in fwd_rows if r["cohort"] == "chain"])
    for lo_, hi_, tag in [(0, .3, "entry 0-30c"), (.3, .5, "entry 30-50c"),
                          (.5, .7, "entry 50-70c"), (.7, .95, "entry 70-95c")]:
        report(f"FORWARD · {tag}",
               [r for r in fwd_rows if lo_ <= r["px"] < hi_])


if __name__ == "__main__":
    main()
