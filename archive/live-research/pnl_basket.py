#!/usr/bin/env python3
"""Capital-constrained copy backtest of the 10-wallet basket, June 1 -> now.

$1,000 bankroll. Replay the wallets' June-1+ entries in time order (using cached
entry timestamps). At each entry: first settle any held bets that have resolved
(free the cash + realize P&L), then enter IF we can afford the stake — otherwise
it's a MISSED trade (counted, with its hypothetical outcome). Capital stays tied
in still-open positions, which is what forces the misses. Realized P&L only.

One position per market (earliest of the 10 wallets to enter it). Shown across a
few flat stake sizes since that's the knob that trades off coverage vs misses.
"""

import time
import cache

JUN1 = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))
NOW = time.time()
BANK = 1000.0
WALLETS = [
    "0xe8ca3f758c93f44f3ec210542ab78afb7c0bcccb", "0x0a7aaf83341b52df34e8ffef52aa295538d6df1b",
    "0xfd4263b3ad08226034fe1b1ea678a46d80b58895", "0x13464aabec792c36b062316f474713e681330448",
    "0x36bfcd8ab96dce2ddea30145ab749b59c6362864", "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a",
    "0x1cff72c8dddc30a64486fda6eab71ab5f9243984", "0xfc81760d44a21acc9fd4b749a5bf9a9b2eeae072",
    "0x86c878cde72660ec52f5e6f0f0438b76de8fc867", "0x6fdddf25b92251ed1515703cda43bf8ff5f5d385",
]


def gather():
    """One copy signal per market: (entry_ts, p, won, res_t|None). res_t None =
    still open (ties up capital, no realized P&L)."""
    pos = {}
    for w in WALLETS:
        ent = cache.get_entries(w)                    # {cond: first_buy_ts}
        resolved = {b["cond"]: b for b in cache.get_bets(w)}
        for cond, ets in ent.items():
            if ets < JUN1:
                continue
            b = resolved.get(cond)
            p = max(0.001, min(0.999, b["p"])) if b else None
            rec = dict(ets=ets, p=p, won=b["won"] if b else None,
                       res_t=(b["res_t"] if b else None))
            if cond not in pos or ets < pos[cond]["ets"]:
                pos[cond] = rec
    return sorted(pos.values(), key=lambda r: r["ets"])


def sim(events, stake):
    cash, realized = BANK, 0.0
    held = []                       # (res_t, p, won, stake)
    entered = missed = openn = 0
    missed_pnl = 0.0

    def settle2(upto):
        nonlocal cash, realized
        keep = []
        for res_t, p, won, s in held:
            if res_t is not None and res_t <= upto:
                payout = (s / p) if won else 0.0
                cash += payout
                realized += payout - s
            else:
                keep.append((res_t, p, won, s))
        held[:] = keep

    for e in events:
        settle2(e["ets"])
        if cash >= stake:
            cash -= stake
            held.append((e["res_t"], e["p"], e["won"], stake))
            entered += 1
            if e["res_t"] is None:
                openn += 1
        else:
            missed += 1
            if e["res_t"] is not None:               # hypothetical realized miss
                missed_pnl += (stake / e["p"] - stake) if e["won"] else -stake
    settle2(NOW)
    open_left = sum(1 for h in held if h[0] is None or h[0] > NOW)
    equity = BANK + realized                          # open held at cost
    return dict(stake=stake, entered=entered, missed=missed, open_left=open_left,
                realized=realized, equity=equity, missed_pnl=missed_pnl)


def main():
    ev = gather()
    res = sum(1 for e in ev if e["res_t"] is not None)
    print(f"10-wallet basket · {len(ev)} unique June1+ markets entered "
          f"({res} resolved, {len(ev)-res} still open) · $1000 bankroll, miss when broke\n")
    h = f"{'stake':>6}{'entered':>8}{'missed':>7}{'open':>5}{'realized P&L':>14}{'equity':>10}{'missed P&L':>12}"
    print(h); print("-" * len(h))
    for s in (20, 50, 100, 200):
        r = sim(ev, s)
        print(f"${r['stake']:>4}{r['entered']:>8}{r['missed']:>7}{r['open_left']:>5}"
              f"{r['realized']:>+13,.0f}{r['equity']:>10,.0f}{r['missed_pnl']:>+12,.0f}")
    print("\nrealized P&L = settled bets only · equity = $1000 + realized (open held at cost)")
    print("missed P&L = hypothetical resolved P&L of trades skipped for lack of cash")


if __name__ == "__main__":
    main()
