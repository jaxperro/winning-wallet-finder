#!/usr/bin/env python3
"""Study A — flow-state signal: trade the informed herd's lean, not any one
wallet's print.

Hypothesis (pre-registered, issue TBD): when the trailing net $-flow of the
tape-scored informed set crosses a threshold in one in-play sports/esports
market, the market's resolution probability exceeds its price by enough to
clear real execution (calibrated sim: lag, FAK crater, fees, ~2c optimism
bias) — because the herd's aggregate lean IS the event detector, arriving
before makers reprice.

Discipline:
  * informed set as-of T uses ONLY tape < T (scoring + resolutions as-of T).
  * FIT day and grid are fixed below; selection rule: highest after-fee EV
    per trigger at p50 lag with >= MIN_FILLS fills. Params freeze into
    params/study_flow.json; the holdout day is scored ONCE with frozen
    params; forward days accrue via forward.py.
  * Controls: 3 activity-matched shuffled sets through the identical
    pipeline — the edge must vanish when the wallets are random.

Signal: per token (sports/esports niche only), rolling W-second sum of
signed informed flow (+buy$ / -sell$). Trigger when sum >= F with the
triggering print inside PRICE_BAND; COOLDOWN_S per token. Entry at the
triggering print's price through sim; hold to resolution; stake $100 flat.
"""
import json
import os
import time

import tape
import sim as simmod

HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS_F = os.path.join(HERE, "params", "study_flow.json")

# pre-registered exploration grid + universe (do not widen after the fact)
GRID = {"top_n": [50, 150], "window_s": [60, 300], "flow_usd": [300, 1000]}
PRICE_BAND = (0.10, 0.90)
NICHES = {"sports", "esports"}
COOLDOWN_S = 900
STAKE = 100.0
MIN_FILLS = 30
SET_MIN_Z, SET_MIN_BETS = 2.5, 6


def informed_set(db, before_ts, top_n):
    """Top-N wallets by improbability z using ONLY tape before before_ts."""
    tape.build_resolved(db, t_end=before_ts)
    rows = db.execute(f"""
    WITH bets AS (
      SELECT tr.wallet,
             any_value(tk.payout) payout,
             sum(CASE WHEN tr.side='BUY' THEN tr.size ELSE -tr.size END) net,
             sum(CASE WHEN tr.side='BUY' THEN tr.size*tr.price END)
               / nullif(sum(CASE WHEN tr.side='BUY' THEN tr.size END),0) vwap
      FROM trades tr JOIN res_tok tk ON tr.asset = tk.asset
      WHERE tr.ts <= {before_ts}
      GROUP BY tr.wallet, tr.asset
      HAVING net >= 5 AND vwap BETWEEN 0.05 AND 0.95
    )
    SELECT wallet,
           count(*) n,
           sum(CASE WHEN payout=1.0 THEN 1 ELSE 0 END) wins,
           sum(vwap) exp_w, sum(vwap*(1-vwap)) var_s,
           sum(net*(payout - vwap)) pnl
    FROM bets GROUP BY wallet
    HAVING n >= {SET_MIN_BETS} AND var_s > 0 AND pnl > 0
    """).fetchall()
    scored = []
    for w, n, wins, exp_w, var_s, pnl in rows:
        z = (wins - exp_w) / (var_s ** 0.5)
        if z >= SET_MIN_Z:
            scored.append((z, w))
    scored.sort(reverse=True)
    return [w for _, w in scored[:top_n]]


def matched_random_set(db, before_ts, size, seed):
    """Activity-matched control: wallets with >= 20 trades before before_ts,
    deterministic pseudo-shuffle by md5(wallet||seed) — no RNG state."""
    rows = db.execute(f"""
    SELECT wallet FROM trades WHERE ts <= {before_ts}
    GROUP BY wallet HAVING count(*) >= 20
    ORDER BY md5(wallet || '{seed}') LIMIT {size}""").fetchall()
    return [w for (w,) in rows]


def signals(db, wallets, t_lo, t_hi, window_s, flow_usd):
    """Rolling-window triggers over the informed set's prints."""
    if not wallets:
        return []
    rows = db.execute("""
    SELECT tr.asset, tr.ts, tr.side, tr.price, tr.size, any_value(tr.title)
    FROM trades tr
    WHERE tr.ts > ? AND tr.ts <= ?
      AND tr.wallet IN (SELECT unnest(?::varchar[]))
    GROUP BY tr.asset, tr.ts, tr.side, tr.price, tr.size, tr.tx
    ORDER BY tr.asset, tr.ts""", [t_lo, t_hi, wallets]).fetchall()
    trig, cur, buf, last_trig = [], None, [], {}
    for asset, ts, side, price, size, title in rows:
        if asset != cur:
            cur, buf = asset, []
        if tape.niche(title) not in NICHES:
            continue
        usd = price * size * (1 if side == "BUY" else -1)
        buf.append((ts, usd))
        while buf and buf[0][0] < ts - window_s:
            buf.pop(0)
        flow = sum(u for _, u in buf)
        if flow >= flow_usd and PRICE_BAND[0] <= price <= PRICE_BAND[1] \
                and ts - last_trig.get(asset, 0) >= COOLDOWN_S:
            last_trig[asset] = ts
            trig.append({"asset": asset, "ts": ts, "p_ref": price,
                         "flow": round(flow), "title": title})
    return trig


def score(db, triggers, lag_s, hold_s):
    """Sim each trigger; outcome from res_tok (resolved-by-tape-end only)."""
    s = simmod.Sim(db, lag_s=lag_s, hold_s=hold_s)
    res = dict(fills=0, misses=0, pending=0, pnl=0.0, wins=0, staked=0.0)
    for t in triggers:
        pay = db.execute("SELECT payout::DOUBLE FROM res_tok WHERE asset = ?",
                         [t["asset"]]).fetchone()
        r = s.try_buy(t["asset"], t["ts"], t["p_ref"], stake_usd=STAKE)
        if not r["filled"]:
            res["misses"] += 1
            continue
        if pay is None:                 # fired, filled, not yet resolved
            res["pending"] += 1
            continue
        res["fills"] += 1
        res["staked"] += r["cost"]
        pnl = r["shares"] * (pay[0] - r["price"]) - r["fee"]
        res["pnl"] += pnl
        res["wins"] += pay[0] == 1.0
    if res["fills"]:
        res["ev_per_fill"] = round(res["pnl"] / res["fills"], 2)
        res["hit"] = round(res["wins"] / res["fills"], 3)
    res["pnl"] = round(res["pnl"], 2)
    return res


def run_cell(db, as_of, t_lo, t_hi, top_n, window_s, flow_usd, hold_s, wallets=None):
    S = wallets if wallets is not None else informed_set(db, as_of, top_n)
    tape.build_resolved(db)             # scoring truth = full tape
    trig = signals(db, S, t_lo, t_hi, window_s, flow_usd)
    out = {"triggers": len(trig), "set_size": len(S)}
    for lag, tag in ((simmod.LAG_P50, "p50"), (simmod.LAG_P90, "p90")):
        out[tag] = score(db, trig, lag, hold_s)
    return out, trig


def main():
    db = tape.connect()
    cal = json.load(open(os.path.join(HERE, "params", "sim_calibration.json")))
    hold_s = cal["hold_s"]
    lo, hi = db.execute("SELECT min(ts), max(ts) FROM trades").fetchone()
    day = lambda d, h=0: time.mktime(time.strptime(f"2026-07-{d:02d}", "%Y-%m-%d")) \
        - time.timezone + h * 3600
    fit_lo, fit_hi = day(19), day(20)          # fit day: Jul 19 UTC
    hold_lo, hold_hi = day(20), hi             # holdout: Jul 20 (partial)

    print("== FIT (Jul 19, set as-of Jul 19 00:00 UTC) ==")
    results = []
    for tn in GRID["top_n"]:
        for w in GRID["window_s"]:
            for f in GRID["flow_usd"]:
                r, _ = run_cell(db, fit_lo, fit_lo, fit_hi, tn, w, f, hold_s)
                ev = r["p50"].get("ev_per_fill")
                results.append(((tn, w, f), r))
                print(f"top{tn:<4} W={w:<4} F=${f:<5} -> trig {r['triggers']:>4} "
                      f"fills {r['p50']['fills']:>3} miss {r['p50']['misses']:>3} "
                      f"EV/fill {ev if ev is not None else '—'} "
                      f"hit {r['p50'].get('hit', '—')}")
    eligible = [(p, r) for p, r in results
                if r["p50"]["fills"] >= MIN_FILLS and "ev_per_fill" in r["p50"]]
    if not eligible:
        print("\nNO cell reached MIN_FILLS — study inconclusive at this tape size.")
        return
    best_p, best_r = max(eligible, key=lambda pr: pr[1]["p50"]["ev_per_fill"])
    tn, w, f = best_p
    print(f"\nFROZEN: top_n={tn} window={w}s flow=${f} "
          f"(fit EV/fill {best_r['p50']['ev_per_fill']}, hit {best_r['p50']['hit']})")

    print("\n== CONTROLS (fit day, matched random sets) ==")
    controls = []
    for seed in (1, 2, 3):
        S = matched_random_set(db, fit_lo, tn, seed)
        r, _ = run_cell(db, fit_lo, fit_lo, fit_hi, tn, w, f, hold_s, wallets=S)
        controls.append(r)
        print(f"seed {seed}: trig {r['triggers']} fills {r['p50']['fills']} "
              f"EV/fill {r['p50'].get('ev_per_fill', '—')} "
              f"hit {r['p50'].get('hit', '—')}")

    print("\n== HOLDOUT (Jul 20 partial, set as-of Jul 20 00:00 UTC) ==")
    hr, htrig = run_cell(db, hold_lo, hold_lo, hold_hi, tn, w, f, hold_s)
    print(f"trig {hr['triggers']} fills {hr['p50']['fills']} "
          f"miss {hr['p50']['misses']} pending {hr['p50']['pending']} "
          f"EV/fill {hr['p50'].get('ev_per_fill', '—')} "
      f"hit {hr['p50'].get('hit', '—')} (p90 lag: EV "
          f"{hr['p90'].get('ev_per_fill', '—')})")

    json.dump({"frozen": {"top_n": tn, "window_s": w, "flow_usd": f,
                          "hold_s": hold_s, "price_band": PRICE_BAND,
                          "niches": sorted(NICHES), "cooldown_s": COOLDOWN_S,
                          "stake": STAKE,
                          "set_min_z": SET_MIN_Z, "set_min_bets": SET_MIN_BETS},
               "fit": best_r, "fit_grid": [{"params": p, **r} for p, r in results],
               "controls": controls, "holdout": hr,
               "frozen_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
               "pending_triggers": htrig},
              open(PARAMS_F, "w"), indent=1, default=float)
    print(f"\nfroze {PARAMS_F}")


if __name__ == "__main__":
    main()
