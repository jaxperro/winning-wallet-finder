#!/usr/bin/env python3
"""Study B — crypto oracle fair value vs the book.

The tape's `crypto_prices` aux stream IS the venue's settlement feed
(Binance-sourced, ms-stamped, ~1/s per symbol since 2026-07-19 19:55). Every
strike/sprint crypto market is a digital option on that feed, so fair value
is computable tick-by-tick with no basis risk:

    above K, expiry T:  fair(Yes) = Phi( ln(S_t/K) / (sigma*sqrt(tau)) )
    between K1..K2:     Phi(ln(K2/S)/sv) - Phi(ln(K1/S)/sv)
    sprint (window t0..t1): strike = S_{t0} read from the same feed
    Down/No tokens: 1 - fair(up-side).   sigma = trailing 30min realized
    vol of 1s log-returns (drift negligible at these horizons).

Signal: at a market print, edge = fair - print >= E for that token ->
simulated FAK entry (calibrated sim), hold to resolution (tape truth).

IMPORTANT scope honesty: tick coverage is ~21h, so there is no holdout —
this run only CHOOSES E (grid below) and freezes it; ALL belief is deferred
to the forward ledger. Also grades the live bot's own crypto fills against
fair value at their fill times (objective score of the 0xbadaf319-class
copies)."""
import bisect
import json
import math
import os
import statistics as st
import time

import tape
import sim as simmod

HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS_F = os.path.join(HERE, "params", "study_oracle.json")

EDGE_GRID = [0.04, 0.07, 0.10]
VOL_WIN_S = 1800
TAU_MIN, TAU_MAX = 60, 12 * 3600
COOLDOWN_S = 300
STAKE = 100.0
MIN_FILLS = 30
UP_WORDS = {"up", "yes"}
DOWN_WORDS = {"down", "no"}


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class TickSeries:
    def __init__(self, ticks):
        self.ts = [t for t, _ in ticks]
        self.px = [p for _, p in ticks]

    def at(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        return self.px[i] if i >= 0 else None

    def vol_1s(self, t, win=VOL_WIN_S):
        """stdev of 1s log returns over the trailing window (per-sqrt-second)."""
        lo = bisect.bisect_left(self.ts, t - win)
        hi = bisect.bisect_right(self.ts, t)
        if hi - lo < 60:
            return None
        rets = []
        for i in range(lo + 1, hi):
            dt = self.ts[i] - self.ts[i - 1]
            if dt <= 0:
                continue
            r = math.log(self.px[i] / self.px[i - 1]) / math.sqrt(dt)
            rets.append(r)
        return st.pstdev(rets) if len(rets) >= 30 else None


def fair_value(mkt, up_side, S, sigma, t):
    tau = mkt["t1"] - t
    if not (TAU_MIN <= tau <= TAU_MAX) or not S or not sigma:
        return None
    sv = sigma * math.sqrt(tau)
    if sv <= 0:
        return None
    k = mkt["kind"]
    if k == "sprint":
        if mkt.get("s0") is None:
            return None
        f_up = phi(math.log(S / mkt["s0"]) / sv)
    elif k == "above":
        f_up = phi(math.log(S / mkt["k1"]) / sv)
    elif k == "below":
        f_up = 1.0 - phi(math.log(S / mkt["k1"]) / sv)
    elif k == "between":
        f_up = phi(math.log(mkt["k2"] / S) / sv) - phi(math.log(mkt["k1"] / S) / sv)
    else:
        return None
    return f_up if up_side else 1.0 - f_up


def outcome_map(db):
    """asset -> lowercase outcome name, from the orders_matched aux stream."""
    rows = db.execute("""
      SELECT json_extract_string(payload,'$.asset'),
             lower(any_value(json_extract_string(payload,'$.outcome')))
      FROM aux WHERE type = 'orders_matched'
        AND json_extract_string(payload,'$.outcome') != ''
      GROUP BY 1""").fetchall()
    return {a: o for a, o in rows if a and o}


def crypto_universe(db, outcomes, series):
    """Parseable crypto tokens with a knowable side + tick coverage."""
    rows = db.execute("""
      SELECT asset, any_value(title), min(ts), max(ts)
      FROM trades GROUP BY asset""").fetchall()
    out = []
    for asset, title, lo, hi in rows:
        mkt = tape.crypto_parse(title or "")
        if not mkt or mkt["sym"] not in series:
            continue
        o = outcomes.get(asset, "")
        up = o in UP_WORDS or (o == "" and mkt["kind"] != "sprint")
        if o and o not in UP_WORDS | DOWN_WORDS:
            continue                      # unknown side label — skip honestly
        if mkt["kind"] == "sprint":
            if not o:
                continue                  # sprints NEED the Up/Down label
            mkt["s0"] = series[mkt["sym"]].at(mkt["t0"])
            if mkt["s0"] is None:
                continue
        out.append({"asset": asset, "mkt": mkt, "up": up, "title": title})
    return out


def run_study(db, hold_s):
    series = {s: TickSeries(tape.load_ticks(db, s))
              for s in ("btcusdt", "ethusdt", "solusdt", "xrpusdt",
                        "bnbusdt", "dogeusdt")}
    tick_lo = min(s.ts[0] for s in series.values() if s.ts)
    outcomes = outcome_map(db)
    tape.build_resolved(db)
    uni = crypto_universe(db, outcomes, series)
    payout = {a: p for a, p in db.execute(
        "SELECT asset, payout::DOUBLE FROM res_tok").fetchall()}
    print(f"crypto universe: {len(uni)} tokens with side + ticks "
          f"({sum(1 for u in uni if u['asset'] in payout)} resolved in-tape)")

    events = []                          # candidate mispricings at prints
    for u in uni:
        prints = db.execute("""SELECT ts, price FROM trades
            WHERE asset = ? AND ts >= ? ORDER BY ts""",
            [u["asset"], tick_lo]).fetchall()
        s = series[u["mkt"]["sym"]]
        last_ev = 0.0
        for ts, px in prints:
            if ts - last_ev < COOLDOWN_S:
                continue
            S = s.at(ts)
            sig = s.vol_1s(ts)
            f = fair_value(u["mkt"], u["up"], S, sig, ts)
            if f is None:
                continue
            edge = f - float(px)
            if edge > 0.02:              # collect loosely; grid filters below
                last_ev = ts
                events.append({"asset": u["asset"], "ts": ts, "p_ref": float(px),
                               "fair": round(f, 4), "edge": round(edge, 4),
                               "kind": u["mkt"]["kind"], "title": u["title"]})
    print(f"candidate mispricing events (edge > 2c): {len(events)}")

    sim = simmod.Sim(db, hold_s=hold_s)
    grid = {}
    for E in EDGE_GRID:
        sel = [e for e in events if e["edge"] >= E]
        fills = wins = 0
        pnl = staked = 0.0
        misses = pending = 0
        for e in sel:
            r = sim.try_buy(e["asset"], e["ts"], e["p_ref"], stake_usd=STAKE)
            if not r["filled"]:
                misses += 1
                continue
            pay = payout.get(e["asset"])
            if pay is None:
                pending += 1
                continue
            fills += 1
            staked += r["cost"]
            pnl += r["shares"] * (pay - r["price"]) - r["fee"]
            wins += pay == 1.0
        grid[E] = {"events": len(sel), "fills": fills, "misses": misses,
                   "pending": pending,
                   "ev_per_fill": round(pnl / fills, 2) if fills else None,
                   "hit": round(wins / fills, 3) if fills else None,
                   "pnl": round(pnl, 2)}
        print(f"E >= {E:.2f}: {grid[E]}")
    eligible = [(E, g) for E, g in grid.items()
                if g["fills"] >= MIN_FILLS and g["ev_per_fill"] is not None]
    frozen_E = max(eligible, key=lambda eg: eg[1]["ev_per_fill"])[0] \
        if eligible else None
    return {"grid": grid, "frozen_edge": frozen_E, "n_universe": len(uni),
            "tick_lo": tick_lo}, events


def grade_our_fills(db):
    """Fair-value edge of the live bot's own crypto fills at fill time."""
    series = {}
    graded = []
    for ln in open(os.path.join(tape.ROOT, "copybot_fills.live.jsonl")):
        r = json.loads(ln)
        if r.get("side") == "SELL" or r.get("untracked"):
            continue
        mkt = tape.crypto_parse(r.get("title") or "")
        if not mkt:
            continue
        sym = mkt["sym"]
        if sym not in series:
            series[sym] = TickSeries(tape.load_ticks(db, sym))
        s = series[sym]
        if not s.ts or r["ts"] < s.ts[0] or mkt["kind"] == "sprint":
            continue
        S, sig = s.at(r["ts"]), s.vol_1s(r["ts"])
        up = (r.get("outcome") or "").lower() in UP_WORDS
        f = fair_value(mkt, up, S, sig, r["ts"])
        if f is None:
            continue
        graded.append({"title": r["title"][:60], "outcome": r.get("outcome"),
                       "px": r["my_price"], "fair": round(f, 3),
                       "edge": round(f - r["my_price"], 3),
                       "wallet": r.get("name")})
    return graded


def main():
    db = tape.connect()
    cal = json.load(open(os.path.join(HERE, "params", "sim_calibration.json")))
    res, events = run_study(db, cal["hold_s"])
    graded = grade_our_fills(db)
    print(f"\nour crypto fills graded vs fair value: {len(graded)}")
    for g in graded:
        print(f"  {g['edge']:+.3f}  {g['wallet']:<12} {g['outcome']:<4} "
              f"@{g['px']:.3f} fair {g['fair']:.3f}  {g['title']}")
    json.dump({**res, "our_fills_graded": graded,
               "frozen_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
               "note": "NO holdout exists (21h ticks) — belief deferred "
                       "entirely to forward_ledger"},
              open(PARAMS_F, "w"), indent=1, default=float)
    print(f"\nfroze {PARAMS_F}")


if __name__ == "__main__":
    main()
