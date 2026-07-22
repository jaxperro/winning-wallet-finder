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


def payouts_for(db, assets):
    """asset -> payout via tape proxy FIRST, then CTF chain truth for the
    rest. THE 2026-07-22 SCORER BUG: 'pending' was treated as ignorable,
    but tape-resolution timing is win-biased — a LOSS keeps its winning
    sibling trading (sibling-veto holds the market open), so losses hid in
    pending while wins scored. Jul-21 audit: tape-resolved fills hit 81%;
    chain-resolving the 'pending' bucket hit 26% (n=329) — combined 53%.
    The surge paper book (chain-graded from day one) was right; this
    scorer was flattering every arm. Chain overlay is now mandatory."""
    out, missing = {}, []
    for a in set(assets):
        r = db.execute("SELECT payout::DOUBLE FROM res_tok WHERE asset=?",
                       [a]).fetchone()
        if r:
            out[a] = r[0]
        else:
            missing.append(a)
    conds = {}
    for a in missing:
        c = db.execute("SELECT any_value(cond) FROM trades WHERE asset=?",
                       [a]).fetchone()[0]
        if c:
            conds[a] = c
    if conds:
        tr = tape.chain_overlay([(c, a) for a, c in conds.items()])
        for a, c in conds.items():
            v = tr.get((c, a))
            if v is not None:
                out[a] = v          # 1.0 / 0.0 / 0.5 (refund)
    return out


def score_flow(db, fz, d, hold_s):
    lo, hi = day_bounds(d)
    t_max = db.execute("SELECT max(ts) FROM trades").fetchone()[0]
    hi = min(hi, t_max)
    S = sf.informed_set(db, lo, fz["top_n"])
    tape.build_resolved(db)
    trig = sf.signals(db, S, lo, hi, fz["window_s"], fz["flow_usd"])
    row = {"triggers": len(trig), "set_size": len(S)}
    pays = payouts_for(db, [t["asset"] for t in trig])
    for mode in ("first", "worst"):
        s = simmod.Sim(db, lag_s=simmod.LAG_P50, hold_s=hold_s, fill=mode)
        agg = dict(fills=0, misses=0, pending=0, refunds=0, pnl=0.0, wins=0)
        for t in trig:
            pay = pays.get(t["asset"])
            r = s.try_buy(t["asset"], t["ts"], t["p_ref"], stake_usd=sf.STAKE)
            if not r["filled"]:
                agg["misses"] += 1
            elif pay is None:       # truly unresolved (chain included)
                agg["pending"] += 1
            elif pay == 0.5:
                agg["refunds"] += 1
                agg["pnl"] += r["shares"] * 0.5 - r["cost"] - r["fee"]
            else:
                agg["fills"] += 1
                agg["pnl"] += r["shares"] * (pay - r["price"]) - r["fee"]
                agg["wins"] += pay == 1.0
        agg["pnl"] = round(agg["pnl"], 2)
        if agg["fills"]:
            agg["ev_per_fill"] = round(agg["pnl"] / agg["fills"], 2)
            agg["hit"] = round(agg["wins"] / agg["fills"], 3)
        row[mode] = agg
    ctl = []
    for seed in CONTROL_SEEDS:
        C = sf.matched_random_set(db, lo, fz["top_n"], seed)
        ctrig = sf.signals(db, C, lo, hi, fz["window_s"], fz["flow_usd"])
        cpays = payouts_for(db, [t["asset"] for t in ctrig])
        s = simmod.Sim(db, lag_s=simmod.LAG_P50, hold_s=hold_s, fill="worst")
        fills = 0
        pnl = 0.0
        for t in ctrig:
            pay = cpays.get(t["asset"])
            r = s.try_buy(t["asset"], t["ts"], t["p_ref"], stake_usd=sf.STAKE)
            if r["filled"] and pay is not None and pay != 0.5:
                fills += 1
                pnl += r["shares"] * (pay - r["price"]) - r["fee"]
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
    sim = simmod.Sim(db, hold_s=hold_s)
    evs = []                         # (asset, edge, sim result) — score after
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
            evs.append((u["asset"], edge,
                        sim.try_buy(u["asset"], ts, float(px),
                                    stake_usd=so.STAKE)))
    # chain-overlay payouts (same 2026-07-22 scorer fix as score_flow)
    pays = payouts_for(db, [a for a, _, r in evs if r["filled"]])
    row = {}
    for asset, edge, r in evs:
        for E in so.EDGE_GRID:
            if edge < E:
                continue
            g = row.setdefault(str(E), {"events": 0, "fills": 0,
                                        "pending": 0, "refunds": 0,
                                        "pnl": 0.0, "wins": 0})
            g["events"] += 1
            if not r["filled"]:
                continue
            pay = pays.get(asset)
            if pay is None:
                g["pending"] += 1
            elif pay == 0.5:
                g["refunds"] += 1
                g["pnl"] += r["shares"] * 0.5 - r["cost"] - r["fee"]
            else:
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
            # EXPLORATORY arm (2026-07-21, user ask): the same surge signal
            # on sub-5c longshots, all niches. NOT pre-registered — 20-50x
            # lottery payoffs mean one hit flips a small sample (first scan:
            # 31 resolved fills, 4 wins, sign set entirely by two esports
            # winners), and the sim has no depth model ($100 at 2c = 5,000
            # shares a longshot book won't hold). Accumulates here until
            # ~100+ resolved fills exist; only then is a pre-registration
            # (or a kill) worth writing.
            band0, nich0 = sf.PRICE_BAND, sf.NICHES
            try:
                sf.PRICE_BAND = (0.005, 0.05)
                sf.NICHES = {"sports", "esports", "tennis", "crypto",
                             "politics", "geo", "other"}
                r3 = score_flow(db, {**fz, "flow_usd": 300}, d, cal["hold_s"])
            finally:
                sf.PRICE_BAND, sf.NICHES = band0, nich0
            fh.write(json.dumps({"study": "flow_sub5c_EXPLORATORY", "day": d,
                                 "computed_at": now, **r3},
                                default=float) + "\n")
            print(f"sub5c  {d}: trig {r3['triggers']} "
                  f"worst {r3['worst'].get('ev_per_fill')} "
                  f"({r3['worst']['fills']} fills, {r3['worst']['pending']} pend)")


if __name__ == "__main__":
    main()
