#!/usr/bin/env python3
"""Set-replay harness: sweep candidate wallet SETS over the recorder tape
(2026-07-21, per the set-design discussion — "how many wallets can a
bankroll carry, and which composition?").

SEARCH TOOL ONLY. Verdicts still come exclusively from
research/forward_ledger.jsonl (README silo rules). What this buys over the
per-wallet bench: SET-level interactions — shared-equity compounding (a hot
wallet inflates everyone's 4% stakes), capital contention (all-or-nothing
cash gate), and paired comparison on the SAME tape (two live paper books
watch different weeks; replays of two sets watch identical ones).

Mechanics mirrored from the engine (copytrade.py, cited, NOT imported —
silo rule; parameters are read from live/copybot.paper.json read-only so
parity survives config edits):
  stake_usd   L322: class_pct × (cash + open cost), halved under 80% HWM,
              capped at THEIR cumulative stake, floored at min_order_usd.
  gate_buy    L384: all-or-nothing — cash < stake is a MISS, never partial.
  buy mirror  L403/_handle_their_buy: opens AND adds; per-tx clip merge;
              conviction floor on their trade USD; entry band.
  sell mirror _handle_their_sell: proportional (their_size/their_prev of
              OUR shares).
Execution = sim.Sim (calibrated FAK-print model: lag, +5c protected band =
price_guard_abs, crater no-match). Resolution = tape.build_resolved (the
742/742 chain-validated proxy); unresolved positions mark at last print.

Known v1 biases (identical across sets — rankings robust, absolutes soft):
  - no-backfill unknowable pre-tape: every first tape BUY counts as an OPEN
    (the real bot skips positions a wallet held before watching began);
  - exits fill at their sell print VWAP (no crater model on the way out);
  - sim optimism ≈ -2c/fill documented in FINDINGS (thresholds sit 2x out);
  - FAK re-quote retry (2026-07-20) not modelled — craters count as misses.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import tape                                    # noqa: E402
import sim as simmod                           # noqa: E402

DD_THRESHOLD, DD_FACTOR = 0.80, 0.5            # copytrade.py L306
OUT_DIR = os.path.join(HERE, "replay_out")


def paper_params():
    """Parity params read (read-only) from the paper bot's config."""
    c = json.load(open(os.path.join(ROOT, "live", "copybot.paper.json")))
    f = c["follow"]
    return {
        "class_pct": f.get("class_pct", {"volume": 0.04}),
        "min_their_usd": f.get("min_their_usd", 25.0),
        "min_entry": f.get("min_entry", 0.0),
        "max_entry": f.get("max_entry", 0.95),
        "buy_only": f.get("buy_only", True),
        "min_order_usd": c.get("risk", {}).get("min_order_usd", 5.0),
        "slip_cap": c.get("price_guard_abs", 0.05),
        "current_set": [{"wallet": w["wallet"].lower(),
                         "name": w.get("name", w["wallet"][:10]),
                         "class": w.get("class", "volume"),
                         "floor": w.get("floor")}
                        for w in c["wallets"]],
    }


def tape_p80_floor(db, wallet):
    """Conviction floor for a wallet with no pinned floor: p80 of its own
    tape BUY stakes — the same top-20% rule sync_floors pins from the
    trusted cache, derived from the only history the tape has."""
    r = db.execute("""
        SELECT quantile_cont(usd, 0.8) FROM (
          SELECT sum(price*size) usd FROM trades
          WHERE lower(wallet) = ? AND side = 'BUY'
          GROUP BY tx, asset)""", [wallet.lower()]).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def signals(db, wallets, t_lo=None, t_hi=None):
    """Per-tx clip-merged trades of the watched wallets, time-ordered.
    -> [{ts, wallet, asset, cond, side, vwap, size, usd, title}]"""
    ws = sorted({w.lower() for w in wallets})
    q = """SELECT min(ts) ts, lower(wallet) wallet, asset, any_value(cond) cond,
                  side, sum(price*size)/nullif(sum(size),0) vwap,
                  sum(size) size, sum(price*size) usd, any_value(title) title
           FROM trades WHERE lower(wallet) IN ({}) {} {}
           GROUP BY tx, lower(wallet), asset, side
           ORDER BY ts""".format(
        ",".join("?" * len(ws)),
        "AND ts >= ?" if t_lo else "", "AND ts <= ?" if t_hi else "")
    args = ws + ([t_lo] if t_lo else []) + ([t_hi] if t_hi else [])
    cols = ("ts", "wallet", "asset", "cond", "side", "vwap", "size", "usd", "title")
    return [dict(zip(cols, r)) for r in db.execute(q, args).fetchall()]


class Book:
    """The engine's book mechanics, replayed. One instance per (set, bankroll)."""

    def __init__(self, bankroll, prm, sim):
        self.cash = bankroll
        self.bankroll = bankroll
        self.prm = prm
        self.sim = sim
        self.hwm = bankroll
        self.pos = {}                   # asset -> {shares, cost, wallet}
        self.their = defaultdict(float)  # (wallet, asset) -> shares
        self.bets = []                  # closed + open records
        self.miss = defaultdict(list)   # family -> [records]
        self.dep_curve = []             # (ts, deployed, equity)

    def open_cost(self):
        return sum(p["cost"] for p in self.pos.values())

    def stake_usd(self, klass, their_total):
        eq = self.cash + self.open_cost()
        self.hwm = max(self.hwm, eq)
        frac = self.prm["class_pct"].get(klass, 0.04)
        if eq < DD_THRESHOLD * self.hwm:
            frac *= DD_FACTOR
        stake = frac * eq
        if their_total and stake > their_total:
            stake = their_total
        return max(stake, self.prm["min_order_usd"])

    def on_buy(self, s, klass, floor=None):
        their_prev = self.their[(s["wallet"], s["asset"])]
        self.their[(s["wallet"], s["asset"])] = their_prev + s["size"]
        if s["usd"] < (floor if floor else self.prm["min_their_usd"]):
            return                     # below the wallet's conviction floor
        if not (self.prm["min_entry"] <= s["vwap"] <= self.prm["max_entry"]):
            self.miss["entry_band"].append(s)
            return
        mine = self.pos.get(s["asset"])
        # ceiling arg is SHARES (their_prev + their_size), mirroring the
        # engine call site verbatim (copytrade L512/L524)
        stake_rule = self.stake_usd(klass, their_prev + s["size"])
        if mine:
            # ADD: one-market-one-stake — grow proportionally but never past
            # the stake rule for the whole position (copytrade L507-521)
            frac = s["size"] / their_prev if their_prev > 0 else 0
            room = stake_rule - mine["cost"]
            if room < self.prm["min_order_usd"]:
                return                          # silent skip, like the bot
            want = min(mine["shares"] * frac * s["vwap"], room)
            if want < self.prm["min_order_usd"]:
                return
        else:
            want = stake_rule
        if self.cash < want:
            self.miss["capital"].append({**s, "stake": want})
            return
        r = self.sim.try_buy(s["asset"], s["ts"], s["vwap"], stake_usd=want)
        if not r["filled"]:
            self.miss["crater"].append({**s, "stake": want})
            return
        self.cash -= r["cost"] + r["fee"]
        p = self.pos.setdefault(s["asset"], {"shares": 0.0, "cost": 0.0,
                                             "wallet": s["wallet"],
                                             "cond": s["cond"],
                                             "title": s["title"] or ""})
        p["shares"] += r["shares"]
        p["cost"] += r["cost"] + r["fee"]
        self.bets.append({"asset": s["asset"], "wallet": s["wallet"],
                          "ts": s["ts"], "price": r["price"],
                          "shares": r["shares"], "cost": r["cost"] + r["fee"],
                          "pnl": None})
        self.dep_curve.append((s["ts"], self.open_cost(),
                               self.cash + self.open_cost()))

    def on_sell(self, s):
        their_prev = self.their[(s["wallet"], s["asset"])]
        self.their[(s["wallet"], s["asset"])] = max(0.0, their_prev - s["size"])
        p = self.pos.get(s["asset"])
        if not p:
            return
        frac = 1.0 if their_prev <= 0 else min(1.0, s["size"] / their_prev)
        sh = p["shares"] * frac
        proceeds = sh * s["vwap"]
        f = simmod.fee(sh, s["vwap"])
        avg_cost = p["cost"] / p["shares"]
        self.cash += proceeds - f
        self._book_pnl(s["asset"], sh, proceeds - f - avg_cost * sh,
                       p["wallet"])
        p["shares"] -= sh
        p["cost"] -= avg_cost * sh
        if p["shares"] < 1e-9:
            del self.pos[s["asset"]]

    def _book_pnl(self, asset, shares, pnl, wallet):
        for b in self.bets:
            if b["asset"] == asset and b["pnl"] is None:
                b["pnl"] = pnl                  # first open lot takes it
                return
        self.bets.append({"asset": asset, "wallet": wallet, "ts": 0,
                          "price": 0, "shares": shares, "cost": 0, "pnl": pnl})

    def settle(self, payouts, marks):
        """Tape-end: proxy-resolved positions pay 1/0; the rest mark."""
        realized = sum(b["pnl"] for b in self.bets if b["pnl"] is not None)
        unresolved_mark = 0.0
        for a, p in list(self.pos.items()):
            pay = payouts.get(a)
            if pay is not None:
                self.cash += p["shares"] * pay        # redeem free
                self._book_pnl(a, p["shares"], p["shares"] * pay - p["cost"],
                               p["wallet"])
                realized += p["shares"] * pay - p["cost"]
                del self.pos[a]
            else:
                unresolved_mark += p["shares"] * marks.get(a, 0.0) - p["cost"]
        return realized, unresolved_mark


def replay(db, wallets_cfg, bankroll, prm, sim, t_lo=None, t_hi=None):
    klass = {w["wallet"]: w.get("class", "volume") for w in wallets_cfg}
    floors = {w["wallet"]: (w.get("floor") or tape_p80_floor(db, w["wallet"]))
              for w in wallets_cfg}
    book = Book(bankroll, prm, sim)
    for s in signals(db, list(klass), t_lo, t_hi):
        if s["side"] == "BUY":
            book.on_buy(s, klass[s["wallet"]], floors.get(s["wallet"]))
        else:
            book.on_sell(s)     # buy_only: their SELLs only ever CLOSE ours
    # resolution + marks
    tape.build_resolved(db)
    payouts = {a: float(p) for a, p in db.execute(
        "SELECT asset, payout FROM res_tok WHERE payout IS NOT NULL").fetchall()}
    marks = {}
    if book.pos:
        marks = {a: float(m) for a, m in db.execute(
            "SELECT asset, arg_max(price, ts) FROM trades WHERE asset IN ({}) "
            "GROUP BY asset".format(",".join("?" * len(book.pos))),
            list(book.pos)).fetchall()}
    realized, mark = book.settle(payouts, marks)
    dep = [d for _, d, _ in book.dep_curve]
    eqs = [e for _, _, e in book.dep_curve]
    per_wallet = defaultdict(float)
    for b in book.bets:
        if b["pnl"] is not None:
            per_wallet[b["wallet"]] += b["pnl"]
    return {
        "bankroll": bankroll, "copies": len(book.bets),
        "realized": round(realized, 2), "open_mark": round(mark, 2),
        "end_equity": round(book.cash + book.open_cost() + mark, 2),
        "misses": {k: len(v) for k, v in book.miss.items()},
        "capital_miss_hypo": round(_hypo(book.miss.get("capital", []),
                                         payouts), 2),
        "peak_deploy_pct": round(100 * max((d / e for d, e in zip(dep, eqs)),
                                           default=0.0), 1),
        "mean_deploy": round(sum(dep) / len(dep), 2) if dep else 0.0,
        "per_wallet": {w: round(p, 2) for w, p in sorted(per_wallet.items())},
    }


def _hypo(capital_misses, payouts):
    """What the capital misses would have paid at resolution (stake-sized)."""
    tot = 0.0
    for m in capital_misses:
        pay = payouts.get(m["asset"])
        if pay is not None and m["vwap"] > 0:
            tot += m["stake"] / m["vwap"] * pay - m["stake"]
    return tot


def main():
    ap = argparse.ArgumentParser(description="replay wallet sets over the tape")
    ap.add_argument("--sets", default=os.path.join(HERE, "params",
                                                   "replay_sets.json"))
    ap.add_argument("--bankrolls", default="500,1000,2000,5000")
    ap.add_argument("--loo", action="store_true",
                    help="leave-one-out marginals at each bankroll")
    ap.add_argument("--lag", type=float, default=simmod.LAG_P50)
    args = ap.parse_args()

    prm = paper_params()
    sets = {"current": prm["current_set"]}
    if os.path.exists(args.sets):
        for name, ws in json.load(open(args.sets)).items():
            sets[name] = [{"wallet": w["wallet"].lower(),
                           "name": w.get("name", w["wallet"][:10]),
                           "class": w.get("class", "volume")} for w in ws]

    db = tape.connect()
    lo, hi = db.execute("SELECT min(ts), max(ts) FROM trades").fetchone()
    print(f"tape window: {time.strftime('%m-%d %H:%M', time.gmtime(lo))} -> "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC "
          f"({(hi - lo) / 86400:.2f} days)")
    sim = simmod.Sim(db, lag_s=args.lag, slip_cap=prm["slip_cap"],
                     exclude_wallet=simmod.BOT_WALLET)

    out = {"ran_at": int(time.time()), "tape": [lo, hi], "lag_s": args.lag,
           "results": {}}
    for name, ws in sets.items():
        for bank in [float(b) for b in args.bankrolls.split(",")]:
            r = replay(db, ws, bank, prm, sim)
            out["results"][f"{name}@{bank:.0f}"] = r
            m = r["misses"]
            print(f"{name:24s} ${bank:>6.0f}  copies {r['copies']:3d}  "
                  f"realized {r['realized']:+9.2f}  open {r['open_mark']:+8.2f} "
                  f" deploy μ${r['mean_deploy']:.0f}/pk{r['peak_deploy_pct']}%"
                  f"  miss cap:{m.get('capital', 0)} crater:{m.get('crater', 0)}"
                  f" band:{m.get('entry_band', 0)}"
                  f"  capmiss_hypo {r['capital_miss_hypo']:+.2f}")
            if args.loo and len(ws) > 1 and bank == 1000.0:
                base = r["realized"]
                for drop in ws:
                    sub = [w for w in ws if w is not drop]
                    rr = replay(db, sub, bank, prm, sim)
                    print(f"    -{drop['name']:20s} marginal "
                          f"{base - rr['realized']:+9.2f}  "
                          f"(set realized {rr['realized']:+9.2f})")
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"replay_{int(time.time())}.json")
    json.dump(out, open(path, "w"), indent=1)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
