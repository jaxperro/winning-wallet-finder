#!/usr/bin/env python3
"""Execution replayer calibrated on the live bot's OWN ledger.

Model: a signal at t with reference price p_ref becomes a marketable FAK
arriving at t+lag with protected cap p_ref*(1+slip_cap). The tape has no
book stream, so standing liquidity at arrival is proxied by PRINTS: the
order fills at the first trade print on the token inside
(arrive, arrive+hold_s] whose price is inside the cap — else it dies
no-match (the crater). hold_s is NOT a free choice: `calibrate()` fits it
so the model best separates the bot's real live fills (should fill) from
its real FAK-rejected misses (should miss), and reports fill-price error
with the bot's own prints EXCLUDED (else the validation is circular — our
fill is itself a tape print).

Fees mirror the venue: fee = rate * shares * min(p, 1-p) (verified against
the live ledger: 7.81sh @ .64 -> $0.0844, 5.26sh @ .95 -> $0.0075).

Everything is deterministic — scenarios (lag percentiles) not RNG.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOT_WALLET = "0x455e252e45ee46d6c4cc1c8fadd3899d68f245a1"

FEE_RATE = 0.03
LAG_P50, LAG_P90 = 6.7, 66.4       # live ledger 2026-07-20 (102 BUY fills)


def fee(shares, price, rate=FEE_RATE):
    return rate * shares * min(price, 1.0 - price)


class Sim:
    def __init__(self, db, lag_s=LAG_P50, slip_cap=0.05, hold_s=10,
                 fee_rate=FEE_RATE, exclude_wallet=None, fill="first"):
        self.db = db
        self.lag_s = lag_s
        self.slip_cap = slip_cap
        self.hold_s = hold_s
        self.fee_rate = fee_rate
        self.excl = (exclude_wallet or "").lower()
        self.fill = fill               # "first" print, or "worst" (pessimistic)

    def first_print(self, asset, t0, t1, cap=None):
        """First trade print on asset in (t0, t1], optionally inside cap."""
        q = """SELECT ts, price FROM trades
               WHERE asset = ? AND ts > ? AND ts <= ?"""
        args = [asset, t0, t1]
        if self.excl:
            q += " AND lower(wallet) != ?"
            args.append(self.excl)
        if cap is not None:
            q += " AND price <= ?"
            args.append(cap)
        q += " ORDER BY ts LIMIT 1"
        r = self.db.execute(q, args).fetchone()
        return r  # (ts, price) or None

    def try_buy(self, asset, t_sig, p_ref, stake_usd=100.0, lag_s=None):
        """-> dict(filled, price, shares, cost, fee) — FAK with protected cap."""
        lag = self.lag_s if lag_s is None else lag_s
        arrive = t_sig + lag
        cap = min(p_ref * (1 + self.slip_cap), 0.99)
        if self.fill == "worst":       # pay the top of the burst
            # (ORDER BY form: max(ts),max(price) trips a duckdb-internal
            # statistics-propagation assertion on this temp-table layout)
            pr = self.db.execute("""SELECT ts, price FROM trades
                 WHERE asset = ? AND ts > ? AND ts <= ? AND price <= ?
                 ORDER BY price DESC LIMIT 1""",
                 [asset, arrive, arrive + self.hold_s, cap]).fetchone()
        else:
            pr = self.first_print(asset, arrive, arrive + self.hold_s, cap)
        if not pr:
            return {"filled": False, "reason": "no print inside band (crater)"}
        px = float(pr[1])
        shares = stake_usd / px
        return {"filled": True, "price": px, "shares": shares,
                "cost": shares * px, "fee": fee(shares, px, self.fee_rate),
                "fill_ts": pr[0]}

    def markout(self, asset, t_fill, horizon_s):
        """Last print at/before t_fill+horizon (None if nothing printed)."""
        r = self.db.execute("""SELECT price FROM trades WHERE asset = ?
              AND ts > ? AND ts <= ? ORDER BY ts DESC LIMIT 1""",
              [asset, t_fill, t_fill + horizon_s]).fetchone()
        return r[0] if r else None


# ── calibration against the live ledger ─────────────────────────────────────

def _live_attempts(tape_lo, tape_hi):
    """Real BUY attempts inside the tape window:
    fills from copybot_fills.live.jsonl (label filled=True) and FAK
    no-match misses from copybot_state.live.json (label filled=False)."""
    fills = []
    for ln in open(os.path.join(ROOT, "copybot_fills.live.jsonl")):
        r = json.loads(ln)
        if r.get("untracked") or r.get("side") == "SELL":
            continue
        if r.get("detect_lag_s") is None or not r.get("their_price"):
            continue
        t_sig = r["ts"] - r["detect_lag_s"]
        if not (tape_lo <= t_sig <= tape_hi - 120):
            continue
        fills.append({"filled": True, "asset": str(r["token"]),
                      "t_sig": t_sig, "p_ref": r["their_price"],
                      "lag": r["detect_lag_s"], "actual_px": r["my_price"]})
    st = json.load(open(os.path.join(ROOT, "copybot_state.live.json")))
    misses = []
    for m in st.get("missed", []):
        if "no orders found to match" not in str(m.get("reason", "")):
            continue
        if not (tape_lo <= m["ts"] <= tape_hi - 120):
            continue
        misses.append({"filled": False, "asset": str(m["token"]),
                       "t_sig": m["ts"], "p_ref": m["price"], "lag": LAG_P50})
    return fills, misses


def calibrate(db, tape_lo, tape_hi, holds=(3, 5, 10, 20, 45, 90)):
    """Fit hold_s on real outcomes; report the confusion + price error."""
    fills, misses = _live_attempts(tape_lo, tape_hi)
    out = {"n_fills": len(fills), "n_misses": len(misses), "grid": {}}
    best = None
    for h in holds:
        sim = Sim(db, hold_s=h, exclude_wallet=BOT_WALLET)
        tp = sum(1 for a in fills
                 if sim.try_buy(a["asset"], a["t_sig"], a["p_ref"],
                                lag_s=a["lag"])["filled"])
        tn = sum(1 for a in misses
                 if not sim.try_buy(a["asset"], a["t_sig"], a["p_ref"],
                                    lag_s=a["lag"])["filled"])
        acc = (tp + tn) / max(len(fills) + len(misses), 1)
        out["grid"][h] = {"fill_recall": tp / max(len(fills), 1),
                          "miss_recall": tn / max(len(misses), 1),
                          "acc": round(acc, 3)}
        if best is None or acc > best[1]:
            best = (h, acc)
    out["hold_s"] = best[0]
    sim = Sim(db, hold_s=best[0], exclude_wallet=BOT_WALLET)
    errs, signed = [], []
    for a in fills:
        r = sim.try_buy(a["asset"], a["t_sig"], a["p_ref"], lag_s=a["lag"])
        if r["filled"]:
            errs.append(abs(r["price"] - a["actual_px"]))
            signed.append(r["price"] - a["actual_px"])
    errs.sort()
    if errs:
        out["px_err_p50"] = round(errs[len(errs) // 2], 4)
        out["px_err_p90"] = round(errs[int(len(errs) * 0.9)], 4)
        out["px_within_1c"] = round(sum(e <= 0.01 for e in errs) / len(errs), 3)
        # signed bias: negative = sim fills cheaper than reality = OPTIMISTIC
        # (study EVs must clear |bias| + noise before they mean anything)
        out["px_bias_mean"] = round(sum(signed) / len(signed), 4)
    return out


if __name__ == "__main__":
    import tape
    db = tape.connect()
    lo, hi = db.execute("SELECT min(ts), max(ts) FROM trades").fetchone()
    cal = calibrate(db, lo, hi)
    print(json.dumps(cal, indent=2))
    json.dump(cal, open(os.path.join(HERE, "params", "sim_calibration.json"),
                        "w"), indent=1)
