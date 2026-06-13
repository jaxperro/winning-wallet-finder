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
2. **True win rate** — over resolved bets in the last 90 days. This is the
   subtle part: Polymarket only redeems *winning* shares, so `/closed-positions`
   (redeemed or sold) is heavily **survivorship-biased toward winners** —
   losing shares are worth $0 and sit unredeemed in `/positions` at
   `curPrice 0` forever. A correct win rate has to **union both** endpoints
   over the same window. Counting only `/closed-positions` (the naive approach)
   reports ~90% for wallets whose real hit rate is ~50%.
3. **Frequency** — counts trades from `/activity` over the last 4 weeks;
   *bets/week* is the number of **distinct markets** traded per week, so 50
   fills on one order don't count as 50 bets.
4. **Filter** — keeps wallets with win rate ≥ 75%, ≥ 2 bets/week, and ≥ 10
   resolved bets.

> **Reality check:** with the unbiased metric, essentially **no** top wallet
> wins 75% of its bets — true rates cluster around **49% (a coin flip)**, max
> ~60%. The profitable ones make money through position sizing and entry
> prices, *not* hit rate. Treat a high win rate as a red flag for a bias bug,
> not a green light. See the backtest below.

## Copy-trading (`copytrade.py`)

Once you've found wallets worth following, `copytrade.py` watches them and
mirrors their trades onto your own account.

- **Sizing** — each fresh entry stakes a fixed **% of your bankroll** (default 2%).
- **Mirror** — copies **entries and exits**: when they add, it adds
  proportionally; when they sell part of a position, it sells the same
  fraction of yours.
- **Price guard** — skips a copy if the market has moved **>5%** from their
  fill price, so you don't chase.
- **No backfill** — only copies positions they open *after* you start
  watching; positions they already held are tracked (so exits still mirror)
  but never opened.
- **Per-position cap** — `max_position_usd` caps total cost in any one market.
  Without it, proportional adds let a single position balloon toward your whole
  exposure limit as the whale piles in (a backtest caught exactly this).
- **Discord alerts** — set `discord_webhook` in the config to get a ping on
  every trade it would place (entries green, exits red).

## Backtesting (`backtest.py`)

Replays a watchlist's real trades over a recent window through the same copy
logic, filling at each wallet's actual historical price and marking outcomes
from how markets resolved.

```bash
python3 backtest.py --days 7
```

A 7-day backtest of four top wallets returned **−48% on deployed capital** —
not because the engine is broken, but because the wallets' true entry hit rate
is ~50% and flat-size copying pays the spread on every coin flip. Copying a
profitable wallet's *entries* does not reproduce its edge, which lives in
sizing and entry prices. Backtest before you fund anything.

### ⚠️ Real money — read this

It runs in **PAPER mode by default** and places nothing — it just logs what it
*would* do. Live trading requires **all** of: `"mode": "live"` in the config,
the `--live` flag, typing a confirmation phrase, and `py-clob-client` with
valid credentials. Hard caps (per-trade, daily spend, total exposure, open
positions, price bounds) apply in both modes. In live mode this places real
orders with real money on your account — you own the config and the outcomes.

```bash
python3 copytrade.py --init      # write config.example.json
cp config.example.json config.json
#  ... edit config.json: add wallets to "watchlist", set bankroll & caps ...
python3 copytrade.py             # PAPER mode — safe, logs only
python3 copytrade.py --once      # single polling pass, then exit
```

Going live (only after you trust the paper output):

```bash
pip install py-clob-client
#  set "mode": "live" and fill in the "live" block (private_key, funder_address)
python3 copytrade.py --live      # prompts for a typed confirmation
```

`config.json` and `copytrade_state.json` are gitignored so your credentials
and runtime state never get committed.

## Caveats

- Candidates come from the leaderboards, so this surfaces *profitable* sharps.
  A high-win-rate wallet that has never cracked any leaderboard window won't
  appear — scanning every wallet on the platform isn't feasible via the
  public API.
- Win rate is measured over resolved bets in the last 90 days, not all history.
- **Win rate ≠ EV.** Wallets with positive all-time leaderboard PnL routinely
  show ~50% true win rates and even negative 90-day realized PnL. Following a
  wallet profitably is about *how* it sizes and prices entries, not how often
  it's right. The `realized_pnl` column is position-level over 90 days and is
  **not** comparable to the all-time leaderboard figure.
- Very high-volume / market-maker wallets (thousands of fills) can't be cleanly
  backtested via the public API — too many fills, no historical position
  snapshot.
