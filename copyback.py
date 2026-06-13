#!/usr/bin/env python3
"""Backtest: copy the z-validated edge wallets' fresh entries from a start date,
weighted by edge strength, reinvesting 100% of profits (compounding).

Honest model:
  - We copy each wallet's FIRST buy of a market entered on/after the start date,
    filling at THEIR entry price (optimistic — ignores the seconds-to-minutes
    lag you'd really face; that's the next thing to test forward).
  - Outcome = how that market resolved (curPrice 1 won / 0 lost), or current
    price if still open (marked to market).
  - Sizing: stake = wallet_weight * BET_K * current_bankroll, capped by cash.
    Bankroll = cash + open-position cost, so profits compound into bet size.
"""

import time
from collections import defaultdict

import smart_money as sm

START = "2026-05-30"
BET_K = 0.10          # aggressiveness: top-weight wallet risks ~weight*K of bankroll/bet
START_BANKROLL = 1000.0

# z-validated edge wallets (z-proportional weights — reward edge strength)
EDGE = [
    ("Famecesgoal", "0x0b0f92507bbc340762d38eca43eba1e11ee37af1", 9.6),
    ("JAMJAMJAM4",  "0xe8c4d68aff65b38cac46987b9b65e01eb47d395d", 5.7),
    ("Domerina",    "0xf4ad6aedf5475f2023912cd191eff5ec90ead00b", 5.3),
    ("MyLastStand", "0x419f32f30814030554f5df3e9f508ad7394ce853", 4.2),
]


def _parse(d):
    return time.mktime(time.strptime(d.replace("Z", "")[:19],
                       "%Y-%m-%dT%H:%M:%S" if "T" in d else "%Y-%m-%d")) if d else 0


def entries_after(wallet, cutoff):
    """Earliest BUY (price, ts) per asset, for buys on/after cutoff."""
    out = {}
    off = 0
    while off < 4000:
        page = sm.get_json("/activity", {"user": wallet, "type": "TRADE",
                                         "limit": 500, "offset": off})
        if not page:
            break
        for t in page:
            if t.get("side") == "BUY" and t.get("asset") and t.get("timestamp", 0) >= cutoff:
                a = t["asset"]
                if a not in out or t["timestamp"] < out[a][1]:
                    out[a] = (t.get("price", 0), t["timestamp"], t.get("title", "?")[:40])
        off += 500
        if len(page) < 500 or page[-1].get("timestamp", 0) < cutoff:
            break
    return out


def outcomes(wallet):
    """asset -> (curPrice, endTs) from open + closed positions."""
    o = {}
    for ep in ("/positions", "/closed-positions"):
        off = 0
        while off < 2000:
            params = {"user": wallet, "limit": 50, "offset": off}
            if "closed" in ep:
                params.update(sortBy="TIMESTAMP", sortDirection="DESC")
            else:
                params["sizeThreshold"] = 0.0
            page = sm.get_json(ep, params)
            if not page:
                break
            for p in page:
                if p.get("asset"):
                    o.setdefault(p["asset"], (p.get("curPrice", 0), _parse(p.get("endDate", ""))))
            off += 50
            if len(page) < 50:
                break
    return o


def main():
    cutoff = _parse(START)
    now = time.time()
    tot_z = sum(z for _, _, z in EDGE)
    weights = {w: z / tot_z for _, w, z in EDGE}
    names = {w: n for n, w, z in EDGE}

    print(f"Copy-trade backtest from {START} · start ${START_BANKROLL:,.0f} · "
          f"reinvest 100% · BET_K={BET_K}")
    print("weights (z-proportional):")
    for n, w, z in EDGE:
        print(f"   {n:14} z={z:>4}  weight={weights[w]*100:>4.1f}%")

    # gather all copied bets
    bets = []
    for n, w, z in EDGE:
        ent = entries_after(w, cutoff)
        outc = outcomes(w)
        for a, (price, ts, title) in ent.items():
            if a not in outc or not (0 < price < 1):
                continue
            cur, end = outc[a]
            resolved = end and end < now
            bets.append({"w": w, "name": n, "ts": ts, "price": price,
                         "mark": cur, "res_ts": end or now, "resolved": bool(resolved),
                         "title": title})
    print(f"\ncopied bets entered since {START}: {len(bets)}")

    # discrete-event sim: enter at entry ts, free capital as bets resolve
    events = []
    for i, b in enumerate(bets):
        events.append((b["ts"], 0, i))                       # 0=enter
        if b["resolved"]:
            events.append((b["res_ts"], 1, i))               # 1=resolve
    events.sort()

    cash = START_BANKROLL
    open_cost = 0.0
    pos = {}            # i -> (shares, stake)
    wins = losses = skipped = 0
    realized_pnl = 0.0
    by_wallet = defaultdict(float)

    for ts, kind, i in events:
        b = bets[i]
        if kind == 0:                                        # ENTER
            bankroll = cash + open_cost
            stake = min(weights[b["w"]] * BET_K * bankroll, cash)
            if stake < 1:
                skipped += 1
                continue
            shares = stake / b["price"]
            pos[i] = (shares, stake)
            cash -= stake
            open_cost += stake
        else:                                                # RESOLVE
            if i not in pos:
                continue
            shares, stake = pos.pop(i)
            payout = shares * (1 if b["mark"] >= 0.5 else 0)
            cash += payout
            open_cost -= stake
            realized_pnl += payout - stake
            by_wallet[b["name"]] += payout - stake
            if b["mark"] >= 0.5:
                wins += 1
            else:
                losses += 1

    # mark any still-open copied bets to current price
    open_val = 0.0
    for i, (shares, stake) in pos.items():
        mark = bets[i]["mark"]
        open_val += shares * mark
        by_wallet[bets[i]["name"]] += shares * mark - stake

    equity = cash + open_val
    unreal = open_val - open_cost
    print(f"\n{'='*64}")
    print(f"  resolved copied bets: {wins+losses}  ({wins}W / {losses}L"
          f"{f' · {wins/(wins+losses)*100:.0f}% hit' if wins+losses else ''})")
    print(f"  still open (marked to market): {len(pos)}   · skipped (no cash): {skipped}")
    print(f"\n  REALIZED P&L (locked, resolved bets only): ${realized_pnl:+,.2f}"
          f"  -> {realized_pnl/START_BANKROLL*100:+.1f}%")
    print(f"  UNREALIZED (open positions marked to current price): ${unreal:+,.2f}"
          f"  -> {unreal/START_BANKROLL*100:+.1f}%")
    print(f"  ending equity: ${equity:,.2f}  ({(equity/START_BANKROLL-1)*100:+.1f}% "
          f"over {(now-cutoff)/86400:.0f}d) — but {open_val/equity*100:.0f}% of it is UNREALIZED")
    print(f"{'='*64}")
    print("  P&L by wallet:")
    for n, _, _ in EDGE:
        print(f"    {n:14} {by_wallet[n]:+,.2f}")


if __name__ == "__main__":
    main()
