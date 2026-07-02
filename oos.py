#!/usr/bin/env python3
"""Out-of-sample test of the copy-the-edge-wallets strategy.

SELECT wallets using only resolved bets in [Feb 1 - Apr 30] (z-score), with NO
knowledge of May-June. Then COPY those wallets' entries forward from May 30,
compounding. If the forward return is strong, the edge is real; if it collapses,
the in-sample +545% was selection theater.
"""

import math
import time
from collections import defaultdict

import smart_money as sm
from insider import resolved_bets, norm_sf
from copyback import entries_after, outcomes, _parse, BET_K, START_BANKROLL

SEL_T0 = _parse("2026-02-01")     # selection window start (resolution time)
SEL_T1 = _parse("2026-04-30")     # selection window end — nothing after this is seen
TEST_START = _parse("2026-05-30") # copy entries on/after this
Z_PICK = 4.0                      # insider-grade cut, judged AS OF Apr 30
MIN_BETS = 15


def score_pre(wallet):
    """z-score over resolved bets in the selection window only."""
    bets = [b for b in resolved_bets(wallet, SEL_T0 - 10 * 86400)
            if SEL_T0 <= b["res_t"] <= SEL_T1]
    if len(bets) < MIN_BETS:
        return None
    for b in bets:                       # v2 returns raw p — clamp for the z math
        b["p"] = max(0.001, min(0.999, b["p"] or 0))
    wins = sum(1 for b in bets if b["won"])
    exp = sum(b["p"] for b in bets)
    var = sum(b["p"] * (1 - b["p"]) for b in bets) or 1e-9
    z = (wins - exp) / math.sqrt(var)
    return {"wallet": wallet, "n": len(bets), "wins": wins, "z": z, "pval": norm_sf(z)}


def candidate_pool():
    import csv
    seen = {}
    try:
        for r in csv.DictReader(open("huntwide.csv")):
            seen[r["wallet"]] = r["username"]
    except FileNotFoundError:
        pass
    return seen


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pool = candidate_pool()
    print(f"candidate pool: {len(pool)} wallets · scoring on [Feb1–Apr30] only...", flush=True)

    selected = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(score_pre, w): w for w in pool}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                r = f.result()
            except Exception:
                r = None
            if r and r["z"] >= Z_PICK:
                r["name"] = pool[r["wallet"]]
                selected.append(r)
            if done % 50 == 0:
                print(f"  {done}/{len(pool)}", flush=True)

    selected.sort(key=lambda r: r["z"], reverse=True)
    print(f"\nINSIDER-GRADE AS OF APR 30 (z>={Z_PICK}): {len(selected)} wallets")
    for r in selected:
        print(f"   {r['name'][:18]:18} z={r['z']:>4.1f} p={r['pval']:.1e} "
              f"{r['wins']}/{r['n']} (pre-period)")
    if not selected:
        print("\nNo wallets were insider-grade as of Apr 30 — the edge wallets are "
              "too new to have a pre-period track record. That itself is the answer.")
        return

    # forward copy from May 30, z(pre)-weighted, compounding
    tot_z = sum(r["z"] for r in selected)
    weights = {r["wallet"]: r["z"] / tot_z for r in selected}
    names = {r["wallet"]: r["name"] for r in selected}
    print(f"\ncopying {len(selected)} wallets forward from 2026-05-30 "
          f"(z-pre weighted, compounding)...", flush=True)

    bets = []
    now = time.time()
    for r in selected:
        w = r["wallet"]
        ent = entries_after(w, TEST_START)
        outc = outcomes(w)
        for a, (price, ts, title) in ent.items():
            if a not in outc or not (0 < price < 1):
                continue
            cur, end = outc[a]
            bets.append({"w": w, "ts": ts, "price": price, "mark": cur,
                         "res_ts": end or now, "resolved": bool(end and end < now)})
    print(f"forward copied bets: {len(bets)}", flush=True)

    events = []
    for i, b in enumerate(bets):
        events.append((b["ts"], 0, i))
        if b["resolved"]:
            events.append((b["res_ts"], 1, i))
    events.sort()

    cash = START_BANKROLL
    open_cost = 0.0
    posn = {}
    wins = losses = 0
    realized = 0.0
    bw = defaultdict(float)
    for ts, kind, i in events:
        b = bets[i]
        if kind == 0:
            bankroll = cash + open_cost
            stake = min(weights[b["w"]] * BET_K * bankroll, cash)
            if stake < 1:
                continue
            posn[i] = (stake / b["price"], stake)
            cash -= stake
            open_cost += stake
        else:
            if i not in posn:
                continue
            shares, stake = posn.pop(i)
            payout = shares * (1 if b["mark"] >= 0.5 else 0)
            cash += payout
            open_cost -= stake
            realized += payout - stake
            bw[names[b["w"]]] += payout - stake
            wins += b["mark"] >= 0.5
            losses += b["mark"] < 0.5
    open_val = sum(sh * bets[i]["mark"] for i, (sh, st) in posn.items())
    equity = cash + open_val

    print(f"\n{'='*64}")
    print(f"  OUT-OF-SAMPLE forward result (selection knew nothing past Apr 30)")
    print(f"  resolved: {wins+losses} ({wins}W/{losses}L"
          f"{f' · {wins/(wins+losses)*100:.0f}%' if wins+losses else ''}) "
          f"· open: {len(posn)}")
    print(f"  realized P&L: ${realized:+,.2f} ({realized/START_BANKROLL*100:+.1f}%)")
    print(f"  unrealized:   ${open_val-open_cost:+,.2f}")
    print(f"  ── ending equity ${equity:,.2f}  -> {(equity/START_BANKROLL-1)*100:+.1f}% "
          f"on $1,000 over {(now-TEST_START)/86400:.0f}d")
    print(f"{'='*64}")
    for n in sorted(bw, key=lambda k: bw[k], reverse=True):
        print(f"    {n[:18]:18} {bw[n]:+,.2f}")


if __name__ == "__main__":
    main()
