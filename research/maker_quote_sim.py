#!/usr/bin/env python3
"""T1 EXPLORATORY (2026-07-23) — crypto maker quoting on the oracle feed.

Every taker study died at the requote wall (60-83% craters; makers repriced
in <4s). This sims BEING the maker: a resting bid pinned to oracle-fair
minus a margin, refreshed with latency R (the stale-quote window is the
risk), filled when a tape print crosses it, graded to chain truth.

Model per print at t: our active bid = fair(S(t-R), vol(t)) - m (vol drift
over R<=4s is negligible; S(t-R) is the staleness that matters). Fill if
print px <= bid (the book crossed our level — queue-position optimism
stated). Fill price = our bid. Maker pays no taker fee. Per-token cooldown
60s, max 5 lots. Sprints only inside their window (no s0 lookahead —
harness rule, not the tape scorer's).

Grid (pre-declared, not tuned after): m in {2c, 4c, 7c} x R in {1s, 4s}.
Readouts: EV/fill (chain), hit, fills/day, fair-markout at +60s (adverse
selection: how far fair moves against us right after we're filled).
NOT pre-registered — Stage 1 of the maker pivot; a live paper maker arm
only if this survives its own optimism caveats."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import study_oracle as so                      # noqa: E402
import forward as fwd                          # noqa: E402

MARGINS = (0.02, 0.04, 0.07)
LATENCIES = (1.0, 4.0)
COOLDOWN_S = 60
MAX_LOTS = 5
STAKE = 100.0


def main():
    db = tape.connect()
    series = {s: so.TickSeries(tape.load_ticks(db, s))
              for s in ("btcusdt", "ethusdt", "solusdt", "xrpusdt",
                        "bnbusdt", "dogeusdt")}
    outcomes = so.outcome_map(db)
    tape.build_resolved(db)
    uni = so.crypto_universe(db, outcomes, series)
    tick_lo = min(s.ts[0] for s in series.values() if s.ts)
    tick_hi = max(s.ts[-1] for s in series.values() if s.ts)
    span_d = (tick_hi - tick_lo) / 86400
    print(f"universe {len(uni)} tokens · tick span {span_d:.1f}d")
    cells = {(m, R): dict(fills=[], last=0.0, n_tok={})
             for m in MARGINS for R in LATENCIES}
    for u in uni:
        mkt = u["mkt"]
        prints = db.execute("""SELECT ts, price FROM trades
            WHERE asset = ? AND ts >= ? ORDER BY ts""",
            [u["asset"], tick_lo]).fetchall()
        if not prints:
            continue
        s = series[mkt["sym"]]
        last_fill = {k: 0.0 for k in cells}
        n_tok = {k: 0 for k in cells}
        for ts, px in prints:
            px = float(px)
            if mkt["kind"] == "sprint" and ts < (mkt["t0"] or 0):
                continue                     # no pre-window quoting
            sig = s.vol_1s(ts)
            if sig is None:
                continue
            for R in LATENCIES:
                S_stale = s.at(ts - R)
                f = so.fair_value(mkt, u["up"], S_stale, sig, ts)
                if f is None:
                    continue
                for m in MARGINS:
                    k = (m, R)
                    bid = f - m
                    if not (0.02 <= bid <= 0.95):
                        continue
                    if px > bid:
                        continue             # print didn't reach our level
                    if ts - last_fill[k] < COOLDOWN_S or n_tok[k] >= MAX_LOTS:
                        continue
                    last_fill[k] = ts
                    n_tok[k] += 1
                    # adverse selection: where is fair 60s after our fill
                    f60 = so.fair_value(mkt, u["up"], s.at(ts + 60), sig,
                                        ts + 60)
                    cells[k]["fills"].append(
                        {"asset": u["asset"], "ts": ts, "bid": bid,
                         "fair": f, "mo60": (f60 - bid) if f60 else None})
    filled_assets = {f["asset"] for c in cells.values() for f in c["fills"]}
    pays = fwd.payouts_for(db, list(filled_assets))
    print(f"grading {len(filled_assets)} filled tokens (chain overlay)…")
    for (m, R), c in sorted(cells.items()):
        fs = c["fills"]
        graded = [(f, pays.get(f["asset"])) for f in fs]
        graded = [(f, p) for f, p in graded if p is not None and p != 0.5]
        if not graded:
            print(f"m={m:.2f} R={R:.0f}s: {len(fs)} fills, none graded")
            continue
        pnl = wins = 0.0
        for f, p in graded:
            sh = STAKE / f["bid"]
            pnl += sh * (p - f["bid"])       # maker: no taker fee
            wins += p == 1.0
        mo = [f["mo60"] for f, _ in graded if f["mo60"] is not None]
        n = len(graded)
        print(f"m={m:.2f} R={R:.0f}s: fills {len(fs)} ({n} graded) · "
              f"{len(fs)/span_d:.0f}/day · EV/fill {pnl/n:+7.2f} · "
              f"hit {wins/n:.2f} · avg bid "
              f"{sum(f['bid'] for f,_ in graded)/n:.2f} · "
              f"fair-markout60 {sum(mo)/len(mo)*100:+.1f}c"
              if mo else "")


if __name__ == "__main__":
    main()
