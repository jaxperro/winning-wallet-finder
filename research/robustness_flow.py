#!/usr/bin/env python3
"""Study A robustness: (1) pessimistic fill — pay the WORST in-band print in
the hold window (burst tops), not the first; (2) identity lift vs 10
activity-matched control sets. Decides how the pre-registration is framed."""
import json
import os
import statistics as st
import time

import tape
import sim as simmod
import study_flow as sf

HERE = os.path.dirname(os.path.abspath(__file__))


class PessimisticSim(simmod.Sim):
    def try_buy(self, asset, t_sig, p_ref, stake_usd=100.0, lag_s=None):
        lag = self.lag_s if lag_s is None else lag_s
        arrive = t_sig + lag
        cap = min(p_ref * (1 + self.slip_cap), 0.99)
        r = self.db.execute("""SELECT max(price) FROM trades
             WHERE asset = ? AND ts > ? AND ts <= ? AND price <= ?""",
             [asset, arrive, arrive + self.hold_s, cap]).fetchone()
        if not r or r[0] is None:
            return {"filled": False, "reason": "no print inside band"}
        px = float(r[0])
        shares = stake_usd / px
        return {"filled": True, "price": px, "shares": shares,
                "cost": shares * px, "fee": simmod.fee(shares, px, self.fee_rate),
                "fill_ts": arrive}


def score_with(simcls, db, triggers, lag_s, hold_s):
    s = simcls(db, lag_s=lag_s, hold_s=hold_s)
    fills = wins = 0
    pnl = 0.0
    prices = []
    for t in triggers:
        pay = db.execute("SELECT payout::DOUBLE FROM res_tok WHERE asset = ?",
                         [t["asset"]]).fetchone()
        if pay is None:
            continue
        r = s.try_buy(t["asset"], t["ts"], t["p_ref"])
        if not r["filled"]:
            continue
        fills += 1
        prices.append(r["price"])
        pnl += r["shares"] * (pay[0] - r["price"]) - r["fee"]
        wins += pay[0] == 1.0
    return {"fills": fills, "ev": round(pnl / fills, 2) if fills else None,
            "hit": round(wins / fills, 3) if fills else None,
            "avg_px": round(st.mean(prices), 3) if prices else None}


def main():
    db = tape.connect()
    P = json.load(open(os.path.join(HERE, "params", "study_flow.json")))
    fz = P["frozen"]
    day = lambda d: time.mktime(time.strptime(f"2026-07-{d:02d}", "%Y-%m-%d")) \
        - time.timezone
    fit_lo, fit_hi = day(19), day(20)

    S = sf.informed_set(db, fit_lo, fz["top_n"])
    tape.build_resolved(db)
    trig = sf.signals(db, S, fit_lo, fit_hi, fz["window_s"], fz["flow_usd"])
    opt = score_with(simmod.Sim, db, trig, simmod.LAG_P50, fz["hold_s"])
    pes = score_with(PessimisticSim, db, trig, simmod.LAG_P50, fz["hold_s"])
    print(f"informed  first-print: {opt} \n          worst-print: {pes}")

    evs = []
    for seed in range(1, 11):
        C = sf.matched_random_set(db, fit_lo, fz["top_n"], seed)
        ctrig = sf.signals(db, C, fit_lo, fit_hi, fz["window_s"], fz["flow_usd"])
        c = score_with(PessimisticSim, db, ctrig, simmod.LAG_P50, fz["hold_s"])
        evs.append(c)
        print(f"control {seed:>2} worst-print: {c}")
    with_ev = [c["ev"] for c in evs if c["ev"] is not None]
    tot_fills = sum(c["fills"] for c in evs)
    wt = sum(c["ev"] * c["fills"] for c in evs if c["ev"] is not None) \
        / max(tot_fills, 1)
    print(f"\ncontrols: {len(with_ev)} scored · pooled fills {tot_fills} · "
          f"fill-weighted EV {wt:+.2f} · mean {st.mean(with_ev):+.2f} · "
          f"informed pessimistic EV {pes['ev']:+.2f}")
    json.dump({"informed_first": opt, "informed_worst": pes,
               "controls_worst": evs, "controls_pooled_ev": round(wt, 2)},
              open(os.path.join(HERE, "params", "study_flow_robustness.json"),
                   "w"), indent=1)


if __name__ == "__main__":
    main()
