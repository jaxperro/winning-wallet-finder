# Polymarket Smart Money

Finds Polymarket wallets that **win more than 75% of their resolved bets** and
**bet multiple times per week** — the "smart money" worth watching.

Zero dependencies. One file, Python 3 stdlib only.

## Run the dashboard

```bash
python3 smart_money.py
```

Open **http://localhost:8899**, hit **Scan**, and wait a minute or two.
Adjust the filters (win rate, bets/week, minimum resolved bets, candidate
pool size) and the table updates live while the scan runs. Click any trader
to see their recent resolved bets and a link to their Polymarket profile.

## Run in the terminal

```bash
python3 smart_money.py --scan            # default 150-wallet pool
python3 smart_money.py --scan --pool 300 # broader sweep
```

## How it works

1. **Candidates** — pulls the 7d, 30d, and all-time leaderboards from
   `data-api.polymarket.com/v1/leaderboard` and dedupes into a candidate pool
   (default 150 wallets).
2. **Win rate** — for each wallet, pages through `/closed-positions` (up to
   300 most recent resolved positions). A *win* is a resolved position with
   `realizedPnl > 0`.
3. **Frequency** — counts trades from `/activity` over the last 4 weeks;
   *bets/week* is the number of **distinct markets** traded per week, so 50
   fills on one order don't count as 50 bets.
4. **Filter** — keeps wallets with win rate ≥ 75%, ≥ 2 bets/week, and ≥ 10
   resolved bets (so a 3-for-3 fluke doesn't rank as a 100% winner).

## Caveats

- Candidates come from the leaderboards, so this surfaces *profitable* sharps.
  A high-win-rate wallet that has never cracked any leaderboard window won't
  appear — scanning every wallet on the platform isn't feasible via the
  public API.
- Win rate is measured over each wallet's most recent ~300 resolved
  positions, not their entire history.
- High win rate ≠ high EV: someone selling early for +$1 on every position
  counts as winning. Check the realized PnL column alongside the win rate.
