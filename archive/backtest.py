#!/usr/bin/env python3
"""Backtest the copy-trade strategy over a recent window.

Replays each watched wallet's real trades through the same copy logic the live
bot uses — % -of-bankroll sizing, no-backfill, proportional adds/exits, risk
caps — but fills at the wallet's actual historical trade price. Outcomes are
marked from how each market resolved (curPrice 1/0 from closed-positions) or,
for still-open positions, the current market price.

    python3 backtest.py                 # last 7 days, config.json watchlist
    python3 backtest.py --days 7

This is an approximation. Notably the price guard is a near no-op in backtest
(we fill at their price, with no 12s real-time lag), so results are slightly
optimistic. Wallets whose history doesn't reach before the window are flagged.
"""

import argparse
import json
import time
from collections import defaultdict

import smart_money as sm
from copytrade import clob_price, DEFAULT_CONFIG, load_json

LOOKBACK_DAYS = 21          # how far before the window we try to read, for seed
MAX_TRADES = 4000           # pagination cap per wallet


def fetch_trades(wallet, since_ts):
    """Newest-first TRADE activity back to ~since_ts (capped)."""
    out, off = [], 0
    while off < MAX_TRADES:
        page = sm.get_json("/activity",
                           {"user": wallet, "type": "TRADE",
                            "limit": 500, "offset": off})
        if not page:
            break
        out += page
        off += 500
        if len(page) < 500 or page[-1].get("timestamp", 0) < since_ts:
            break
    return out


def mark_map(wallets):
    """asset(token) -> current/resolved price (curPrice).

    Merges each wallet's open /positions (curPrice = live price, or 0/1 if it
    resolved but isn't redeemed yet) and /closed-positions (resolved 1/0). This
    is what lets us mark a position we still hold at its true value rather than
    falling back to entry price.
    """
    res = {}
    for w in wallets:
        for endpoint in ("/positions", "/closed-positions"):
            off = 0
            while off < 1000:
                params = {"user": w, "limit": 50, "offset": off}
                if endpoint == "/closed-positions":
                    params.update(sortBy="TIMESTAMP", sortDirection="DESC")
                else:
                    params["sizeThreshold"] = 0.0
                page = sm.get_json(endpoint, params)
                if not page:
                    break
                for p in page:
                    if p.get("asset") is not None:
                        # closed-positions wins ties (definitively resolved)
                        if endpoint == "/closed-positions" or p["asset"] not in res:
                            res[p["asset"]] = p.get("curPrice", 0)
                off += 50
                if len(page) < 50:
                    break
    return res


def backtest(cfg, days):
    wallets = cfg["watchlist"]
    now = time.time()
    window_start = now - days * 86400
    lookback_start = window_start - LOOKBACK_DAYS * 86400
    stake = cfg["bankroll_usd"] * cfg["bankroll_pct"]
    risk = cfg["risk"]

    print(f"Backtesting {len(wallets)} wallets over the last {days} days "
          f"· ${stake:.0f}/entry · caps: ${risk['max_trade_usd']:.0f}/trade, "
          f"${risk['daily_spend_cap_usd']:.0f}/day, "
          f"${risk['max_total_exposure_usd']:.0f} exposure\n")

    # gather every wallet's trades + per-wallet data reach
    all_trades, reach = [], {}
    for w in wallets:
        ts = fetch_trades(w, lookback_start)
        for t in ts:
            t["_wallet"] = w
        all_trades += ts
        oldest = min((t["timestamp"] for t in ts), default=now)
        reach[w] = (now - oldest) / 86400
    all_trades.sort(key=lambda t: t["timestamp"])

    res = mark_map(wallets)

    # replay state
    their_pos = defaultdict(float)         # (wallet, token) -> shares
    seed_tokens = set()                    # (wallet, token) held before window
    my = {}                                # token -> {shares, cost, title, outcome, wallet}
    daily_spend = defaultdict(float)       # 'YYYY-MM-DD' -> usd
    deployed = 0.0
    realized = 0.0
    n_open = n_add = n_exit = n_skip_guard = n_skip_cap = n_skip_backfill = 0
    price_cache = {}

    def cur_price(token, side):
        key = (token, side)
        if key not in price_cache:
            price_cache[key] = clob_price(token, side)
        return price_cache[key]

    def exposure():
        return sum(p["cost"] for p in my.values())

    for t in all_trades:
        w, token = t["_wallet"], t.get("asset")
        side, size, price = t.get("side"), t.get("size", 0), t.get("price", 0)
        key = (w, token)
        prev = their_pos[key]

        # pre-window trades only build their position (establish the seed)
        if t["timestamp"] < window_start:
            seed_tokens.add(key)
            their_pos[key] = prev + size if side == "BUY" else max(0.0, prev - size)
            continue

        label = f"{t.get('outcome','?')} · {t.get('title','?')[:44]}"
        if side == "BUY":
            mine = my.get(token)
            if mine is None and key in seed_tokens:
                n_skip_backfill += 1
            elif mine is None:
                # fresh OPEN
                if not (risk["min_price"] <= price <= risk["max_price"]):
                    n_skip_guard += 1
                else:
                    day = time.strftime("%Y-%m-%d", time.gmtime(t["timestamp"]))
                    cap = min(stake, risk["max_trade_usd"],
                              risk.get("max_position_usd", float("inf")),
                              risk["daily_spend_cap_usd"] - daily_spend[day],
                              risk["max_total_exposure_usd"] - exposure())
                    if cap < risk["min_order_usd"] or len(my) >= risk["max_open_positions"]:
                        n_skip_cap += 1
                    else:
                        sh = cap / price
                        my[token] = {"shares": sh, "cost": cap,
                                     "title": t.get("title", "?"),
                                     "outcome": t.get("outcome", "?"), "wallet": w}
                        deployed += cap
                        daily_spend[day] += cap
                        n_open += 1
            else:
                # proportional ADD
                frac = size / prev if prev > 0 else 0
                add_sh = mine["shares"] * frac
                add_usd = add_sh * price
                day = time.strftime("%Y-%m-%d", time.gmtime(t["timestamp"]))
                cap = min(add_usd, risk["max_trade_usd"],
                          risk.get("max_position_usd", float("inf")) - mine["cost"],
                          risk["daily_spend_cap_usd"] - daily_spend[day],
                          risk["max_total_exposure_usd"] - exposure())
                if cap >= risk["min_order_usd"]:
                    sh = cap / price
                    mine["shares"] += sh
                    mine["cost"] += cap
                    deployed += cap
                    daily_spend[day] += cap
                    n_add += 1
            their_pos[key] = prev + size
        elif side == "SELL":
            mine = my.get(token)
            if mine and mine["shares"] > 0:
                frac = 1.0 if prev <= 0 else min(1.0, size / prev)
                sell_sh = min(mine["shares"], mine["shares"] * frac)
                if sell_sh > 0:
                    sold_frac = sell_sh / mine["shares"]
                    cost_out = mine["cost"] * sold_frac
                    proceeds = sell_sh * price
                    realized += proceeds - cost_out
                    mine["shares"] -= sell_sh
                    mine["cost"] -= cost_out
                    n_exit += 1
                    if mine["shares"] <= 0.01:
                        del my[token]
            their_pos[key] = max(0.0, prev - size)

    # mark remaining open positions to resolution or current price
    unrealized = 0.0
    open_rows = []
    for token, p in my.items():
        mark = res.get(token)
        if mark is None:
            mark = cur_price(token, "sell")
        if mark is None:
            mark = p["cost"] / p["shares"]   # last resort: flat
        # curPrice at the extremes means the market has resolved
        if mark <= 0.02:
            status = "LOST"
        elif mark >= 0.98:
            status = "WON"
        else:
            status = "open"
        val = p["shares"] * mark
        pnl = val - p["cost"]
        unrealized += pnl
        open_rows.append((p, mark, pnl, status))

    total_pnl = realized + unrealized
    print(f"{'─'*74}")
    print("  Per-wallet data reach (how far history extended before today):")
    for w in wallets:
        flag = "" if reach[w] >= days + 3 else "  ⚠ short history — low confidence"
        print(f"    {w[:12]}…  {reach[w]:5.1f} days{flag}")
    print(f"{'─'*74}")
    print(f"  Copies it would have made:")
    print(f"    {n_open} fresh entries · {n_add} adds · {n_exit} exits/trims")
    print(f"    skipped: {n_skip_backfill} held-before-start, "
          f"{n_skip_guard} price/range, {n_skip_cap} risk-cap")
    print(f"{'─'*74}")
    print(f"  Total deployed (bought):     ${deployed:>12,.2f}")
    print(f"  Realized P&L (closed legs):  ${realized:>+12,.2f}")
    print(f"  Unrealized P&L (still held): ${unrealized:>+12,.2f}")
    print(f"  ── Net P&L:                  ${total_pnl:>+12,.2f}"
          f"   ({(total_pnl/deployed*100) if deployed else 0:+.1f}% on deployed)")
    print(f"{'─'*74}")
    if open_rows:
        won = sum(1 for _, _, _, s in open_rows if s == "WON")
        lost = sum(1 for _, _, _, s in open_rows if s == "LOST")
        opn = sum(1 for _, _, _, s in open_rows if s == "open")
        print(f"  Positions still on the book at window end: {len(open_rows)} "
              f"({won} won, {lost} lost, {opn} open & marked-to-market)")
        for p, mark, pnl, status in sorted(open_rows, key=lambda x: x[2]):
            print(f"    {status:>5}  {pnl:>+9,.2f}  {p['outcome']} · {p['title'][:40]}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    cfg = {**DEFAULT_CONFIG, **load_json(args.config, {})}
    cfg["risk"] = {**DEFAULT_CONFIG["risk"], **cfg.get("risk", {})}
    backtest(cfg, args.days)


if __name__ == "__main__":
    main()
