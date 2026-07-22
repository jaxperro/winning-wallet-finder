#!/usr/bin/env python3
"""EXPLORATORY (2026-07-22, user ask) — markout-exit curve for the surge
signal: does the in-play under-reaction edge peak early and decay, i.e.
should Study A2-scalp EXIT at a fixed horizon instead of holding to
resolution? Motivated by the v1 book post-mortem: >3h holds bled −21.9%
while <3h holds were +4.1%.

NOT pre-registered — output shapes a possible A2-scalp pre-registration,
nothing more. Prints-based v0: exits are marked at the LAST PRINT <= t+H
(sim.markout), which is optimistic vs hitting the real bid — the live
harnesses now record best bid/ask at +60/300/1800s per fill precisely to
haircut this in v1 of the study. Exit fee charged same as entry.

Method: per tape day — informed set as-of 00:00 UTC (frozen method),
signals() with frozen params (flow>=$300/60s, band 10-90c, cooldown 900s),
worst-print $100 entries at p50 lag / calibrated hold. For each RESOLVED
fill: hold-to-resolution PnL (res_tok) vs exit PnL at each horizon.
Coverage (no print in window => no exit) reported per horizon."""
import json
import os
import time

import tape
import sim as simmod
import study_flow as sf

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
    payout = {a: (p, lts) for a, p, lts in db.execute(
        "SELECT asset, payout::DOUBLE, last_ts FROM res_tok").fetchall()}
    sim = simmod.Sim(db, lag_s=simmod.LAG_P50, hold_s=cal["hold_s"],
                     fill="worst")
    rows = []
    for d in days:
        lo, hi = day_bounds(d)
        hi = min(hi, t_max)
        S = sf.informed_set(db, lo, fz["top_n"])
        trig = sf.signals(db, S, lo, hi, fz["window_s"], fz["flow_usd"])
        forward = d >= "2026-07-21"          # after the 07-20 freeze
        for t_ in trig:
            r = sim.try_buy(t_["asset"], t_["ts"], t_["p_ref"],
                            stake_usd=sf.STAKE)
            if not r["filled"]:
                continue
            pay = payout.get(t_["asset"])
            if pay is None:
                continue                     # resolution leg required
            row = {"day": d, "fwd": forward, "px": r["price"],
                   "shares": r["shares"], "efee": r["fee"],
                   "hold_s": pay[1] - r["fill_ts"],
                   "hold_pnl": r["shares"] * (pay[0] - r["price"]) - r["fee"],
                   "win": pay[0] == 1.0, "mo": {}}
            for H in HORIZONS:
                m = sim.markout(t_["asset"], r["fill_ts"], H)
                if m is None:
                    continue
                xfee = FEE * r["shares"] * min(m, 1 - m)
                row["mo"][H] = r["shares"] * (m - r["price"]) - r["fee"] - xfee
            rows.append(row)
        print(f"{d}: {len(trig)} triggers scored")

    def report(tag, rs):
        if not rs:
            return
        n = len(rs)
        hold = sum(r["hold_pnl"] for r in rs)
        print(f"\n== {tag} — {n} resolved fills · hold-to-resolution "
              f"EV/fill {hold/n:+.2f} · hit {sum(r['win'] for r in rs)/n:.2f}")
        for H in HORIZONS:
            sub = [r for r in rs if H in r["mo"]]
            if not sub:
                continue
            mo = sum(r["mo"][H] for r in sub)
            hold_sub = sum(r["hold_pnl"] for r in sub)
            print(f"  exit +{H:>5}s: EV/fill {mo/len(sub):+7.2f} vs hold "
                  f"{hold_sub/len(sub):+7.2f} on same {len(sub)} "
                  f"({100*len(sub)/n:.0f}% coverage)")

    fwd = [r for r in rows if r["fwd"]]
    ins = [r for r in rows if not r["fwd"]]
    report("IN-SAMPLE days (<= 07-20)", ins)
    report("FORWARD days (>= 07-21)", fwd)
    report("FORWARD · hold > 3h (the v1 bleed bucket)",
           [r for r in fwd if r["hold_s"] > 10800])
    report("FORWARD · hold < 3h", [r for r in fwd if r["hold_s"] <= 10800])
    for lo_, hi_, tag in [(0, .3, "entry 0-30c"), (.3, .5, "entry 30-50c"),
                          (.5, .7, "entry 50-70c"), (.7, .95, "entry 70-95c")]:
        report(f"FORWARD · {tag}",
               [r for r in fwd if lo_ <= r["px"] < hi_])


if __name__ == "__main__":
    main()
