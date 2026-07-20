#!/usr/bin/env python3
"""Forward verdict ledger — the only place belief comes from.

Each run re-scores the FROZEN studies on the last RESCORE_DAYS UTC days of
tape and appends one row per (study, day) to forward_ledger.jsonl. Days are
recomputed on later runs so pending (unresolved-at-the-time) triggers
resolve into their day's row; readers keep the newest computed_at per key.

Studies:
  flow      frozen params from params/study_flow.json — informed set as-of
            each day's 00:00 UTC, scored at p50 lag, first- AND worst-print
            fills; plus 3 FIXED control seeds (identity-lift tracking).
  oracle    params/study_oracle.json grid — ALL edge levels tracked until
            one accumulates >= 30 forward fills (then the selection rule in
            the pre-registration applies). Skips days without tick coverage.

Verdicts are pre-registered in the study issues; this script only reports.
"""
import json
import os
import time

import tape
import sim as simmod
import study_flow as sf
import study_oracle as so

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "forward_ledger.jsonl")
RESCORE_DAYS = 3
CONTROL_SEEDS = (1, 2, 3)


def day_bounds(d):
    lo = time.mktime(time.strptime(d, "%Y-%m-%d")) - time.timezone
    return lo, lo + 86400


def score_flow(db, fz, d, hold_s):
    lo, hi = day_bounds(d)
    t_max = db.execute("SELECT max(ts) FROM trades").fetchone()[0]
    hi = min(hi, t_max)
    S = sf.informed_set(db, lo, fz["top_n"])
    tape.build_resolved(db)
    trig = sf.signals(db, S, lo, hi, fz["window_s"], fz["flow_usd"])
    row = {"triggers": len(trig), "set_size": len(S)}
    for mode in ("first", "worst"):
        s = simmod.Sim(db, lag_s=simmod.LAG_P50, hold_s=hold_s, fill=mode)
        agg = dict(fills=0, misses=0, pending=0, pnl=0.0, wins=0)
        for t in trig:
            pay = db.execute("SELECT payout::DOUBLE FROM res_tok WHERE asset=?",
                             [t["asset"]]).fetchone()
            r = s.try_buy(t["asset"], t["ts"], t["p_ref"], stake_usd=sf.STAKE)
            if not r["filled"]:
                agg["misses"] += 1
            elif pay is None:
                agg["pending"] += 1
            else:
                agg["fills"] += 1
                agg["pnl"] += r["shares"] * (pay[0] - r["price"]) - r["fee"]
                agg["wins"] += pay[0] == 1.0
        agg["pnl"] = round(agg["pnl"], 2)
        if agg["fills"]:
            agg["ev_per_fill"] = round(agg["pnl"] / agg["fills"], 2)
            agg["hit"] = round(agg["wins"] / agg["fills"], 3)
        row[mode] = agg
    ctl = []
    for seed in CONTROL_SEEDS:
        C = sf.matched_random_set(db, lo, fz["top_n"], seed)
        ctrig = sf.signals(db, C, lo, hi, fz["window_s"], fz["flow_usd"])
        s = simmod.Sim(db, lag_s=simmod.LAG_P50, hold_s=hold_s, fill="worst")
        fills = 0
        pnl = 0.0
        for t in ctrig:
            pay = db.execute("SELECT payout::DOUBLE FROM res_tok WHERE asset=?",
                             [t["asset"]]).fetchone()
            r = s.try_buy(t["asset"], t["ts"], t["p_ref"], stake_usd=sf.STAKE)
            if r["filled"] and pay is not None:
                fills += 1
                pnl += r["shares"] * (pay[0] - r["price"]) - r["fee"]
        ctl.append({"seed": seed, "fills": fills, "pnl": round(pnl, 2)})
    row["controls_worst"] = ctl
    return row


def score_oracle(db, P, d, hold_s):
    lo, hi = day_bounds(d)
    series = {s: so.TickSeries(tape.load_ticks(db, s))
              for s in ("btcusdt", "ethusdt", "solusdt", "xrpusdt",
                        "bnbusdt", "dogeusdt")}
    have = [s for s in series.values() if s.ts and s.ts[0] < hi and s.ts[-1] > lo]
    if not have:
        return {"skipped": "no tick coverage"}
    outcomes = so.outcome_map(db)
    tape.build_resolved(db)
    uni = so.crypto_universe(db, outcomes, series)
    payout = {a: p for a, p in db.execute(
        "SELECT asset, payout::DOUBLE FROM res_tok").fetchall()}
    sim = simmod.Sim(db, hold_s=hold_s)
    row = {}
    for u in uni:
        prints = db.execute("""SELECT ts, price FROM trades WHERE asset=?
            AND ts > ? AND ts <= ? ORDER BY ts""", [u["asset"], lo, hi]).fetchall()
        s = series[u["mkt"]["sym"]]
        last_ev = 0.0
        for ts, px in prints:
            if ts - last_ev < so.COOLDOWN_S:
                continue
            f = so.fair_value(u["mkt"], u["up"], s.at(ts), s.vol_1s(ts), ts)
            if f is None:
                continue
            edge = f - float(px)
            if edge < min(so.EDGE_GRID):
                continue
            last_ev = ts
            r = sim.try_buy(u["asset"], ts, float(px), stake_usd=so.STAKE)
            for E in so.EDGE_GRID:
                if edge < E:
                    continue
                g = row.setdefault(str(E), {"events": 0, "fills": 0,
                                            "pending": 0, "pnl": 0.0, "wins": 0})
                g["events"] += 1
                if not r["filled"]:
                    continue
                pay = payout.get(u["asset"])
                if pay is None:
                    g["pending"] += 1
                    continue
                g["fills"] += 1
                g["pnl"] += r["shares"] * (pay - r["price"]) - r["fee"]
                g["wins"] += pay == 1.0
    for g in row.values():
        g["pnl"] = round(g["pnl"], 2)
        if g["fills"]:
            g["ev_per_fill"] = round(g["pnl"] / g["fills"], 2)
            g["hit"] = round(g["wins"] / g["fills"], 3)
    return row


def main():
    db = tape.connect()
    cal = json.load(open(os.path.join(HERE, "params", "sim_calibration.json")))
    flow_p = json.load(open(os.path.join(HERE, "params", "study_flow.json")))
    fz = flow_p["frozen"]
    frozen_at = flow_p["frozen_at"]
    t_max = db.execute("SELECT max(ts) FROM trades").fetchone()[0]
    days = [time.strftime("%Y-%m-%d", time.gmtime(t_max - i * 86400))
            for i in range(RESCORE_DAYS)]
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    with open(LEDGER, "a") as fh:
        for d in days:
            r1 = score_flow(db, fz, d, cal["hold_s"])
            fh.write(json.dumps({"study": "flow", "day": d, "computed_at": now,
                                 "frozen_at": frozen_at, **r1},
                                default=float) + "\n")
            print(f"flow   {d}: trig {r1['triggers']} "
                  f"worst {r1['worst'].get('ev_per_fill')} "
                  f"({r1['worst']['fills']} fills, {r1['worst']['pending']} pend)")
            r2 = score_oracle(db, None, d, cal["hold_s"])
            fh.write(json.dumps({"study": "oracle", "day": d, "computed_at": now,
                                 **r2}, default=float) + "\n")
            print(f"oracle {d}: " + (r2.get("skipped") or
                  " ".join(f"E{E}:{g.get('ev_per_fill')}({g['fills']}f)"
                           for E, g in sorted(r2.items()))))


if __name__ == "__main__":
    main()
