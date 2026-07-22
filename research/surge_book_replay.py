#!/usr/bin/env python3
"""Virtual bankroll replay over the surge A2 attempts stream.

Re-runs the v1 deployment spec ($100 bank · 5%-of-equity daily stakes with
$1 floor · cash-gated · max 2 open per event · skip-if-open-same-asset ·
fill at best ask inside p_ref*1.05) against the FULL attempt record
(.surge_attempts.pull.jsonl), settling with chain truth where graded
(surge_meas_ledger.jsonl) and provisional payouts otherwise
(.surge2_state.pull.json). Writes research/surge_book.json for the /test
dashboard and the #19 Friday read.

This is the split that fixes v1's flaw: the physical harness samples EVERY
trigger; bankroll specs are simulated here, where cash-gating can no longer
corrupt the sample. Change SPEC below (or add variants) freely — this file
is analysis, not signal; the frozen signal lives in the harness."""
import heapq
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.join(HERE, ".surge_attempts.pull.jsonl")
STATE = os.path.join(HERE, ".surge2_state.pull.json")
LEDGER = os.path.join(HERE, "surge_meas_ledger.jsonl")
OUT = os.path.join(HERE, "surge_book.json")

SPEC = {"bank": 100.0, "stake_pct": 0.05, "stake_floor": 1.0,
        "event_cap": 2, "fee_rate": 0.03, "slip_cap": 0.05}


def load_payouts():
    """asset:ts -> (payout, settled_ts, chain?) — ledger beats state."""
    pay = {}
    try:
        st = json.load(open(STATE))
        for s in st.get("settled", []):
            pay[f"{s['asset']}:{s['ts']}"] = (s["payout"], s["settled_ts"], False)
    except FileNotFoundError:
        pass
    try:
        for ln in open(LEDGER):
            d = json.loads(ln)
            pay[f"{d['asset']}:{d['ts']}"] = (d["chain_payout"],
                                              d["settled_ts"], True)
    except FileNotFoundError:
        pass
    return pay


def main():
    if not os.path.exists(ATT):
        print("[book_replay] no attempts pull yet — skip")
        return 0
    pay = load_payouts()
    atts = []
    for ln in open(ATT):
        try:
            atts.append(json.loads(ln))
        except Exception:
            pass
    atts.sort(key=lambda a: a["ts"])

    cash = SPEC["bank"]
    day = ""
    stake = SPEC["stake_floor"]
    open_lots = {}                 # asset -> lot (v1: one per asset)
    due = []                       # heap of (settle_ts, asset)
    curve = []
    settled = wins = taken = 0
    cash_skip = event_skip = open_skip = crater = unresolved_cap = 0
    pnl_real = 0.0
    graded_n = 0

    def equity():
        return cash + sum(l["cost"] for l in open_lots.values())

    def settle_due(now):
        nonlocal cash, settled, wins, pnl_real, graded_n
        while due and due[0][0] <= now:
            _, asset = heapq.heappop(due)
            lot = open_lots.pop(asset, None)
            if lot is None:
                continue
            p, _, chain = pay[lot["key"]]
            cash += lot["shares"] * p
            pnl_real += lot["shares"] * p - lot["cost"] - lot["fee"]
            settled += 1
            wins += p == 1.0
            graded_n += chain

    for a in atts:
        now = a["ts"]
        settle_due(now)
        d = time.strftime("%Y-%m-%d", time.gmtime(now))
        if d != day:
            day = d
            stake = max(SPEC["stake_floor"], round(SPEC["stake_pct"] * equity(), 2))
        if len(curve) == 0 or now - curve[-1][0] >= 1800:
            curve.append([int(now), round(equity(), 2)])
        asset = a["asset"]
        if asset in open_lots:
            open_skip += 1
            continue
        ev = a.get("event")
        if ev and sum(1 for l in open_lots.values()
                      if l["event"] == ev) >= SPEC["event_cap"]:
            event_skip += 1
            continue
        if cash < stake:
            cash_skip += 1
            continue
        ba = a.get("best_ask")
        cap = min(a["p_ref"] * (1 + SPEC["slip_cap"]), 0.99)
        if not a.get("filled") or ba is None or ba > cap:
            crater += 1
            continue
        key = f"{asset}:{a['ts']}"
        info = pay.get(key)
        shares = stake / ba
        fee = SPEC["fee_rate"] * shares * min(ba, 1 - ba)
        cash -= stake + fee
        open_lots[asset] = {"key": key, "event": ev, "cost": stake,
                            "fee": fee, "shares": shares}
        taken += 1
        if info is not None:
            heapq.heappush(due, (info[1], asset))
        else:
            unresolved_cap += 1    # stays open until a later run grades it
    settle_due(float("inf") if not open_lots else time.time())
    curve.append([int(time.time()), round(equity(), 2)])

    out = {"computed_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "spec": SPEC, "sem_ver_source": "a2 attempts stream",
           "equity": round(equity(), 2), "cash": round(cash, 2),
           "open_n": len(open_lots), "settled": settled, "wins": wins,
           "losses": settled - wins, "pnl_realized": round(pnl_real, 2),
           "chain_graded_settles": graded_n,
           "counters": {"attempts_seen": len(atts), "taken": taken,
                        "cash_skip": cash_skip, "event_skip": event_skip,
                        "open_skip": open_skip, "crater": crater,
                        "open_unresolved": unresolved_cap},
           "curve": curve[-336:]}
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"[book_replay] virtual ${out['equity']:.2f} "
          f"(cash ${out['cash']:.2f}) · {settled} settled {wins}W · "
          f"taken {taken}/{len(atts)} attempts "
          f"(skips c{cash_skip}/e{event_skip}/o{open_skip}, crater {crater})")
    return 0


if __name__ == "__main__":
    main()
