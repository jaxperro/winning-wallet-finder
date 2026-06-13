# Polymarket Smart Money

Tools to find Polymarket wallets worth following, copy-trade them (paper or
live), and backtest the strategy. Zero dependencies — Python 3 stdlib only
(except live trading, which needs `py-clob-client`).

> **Start here — read [What we learned](#what-we-learned-research-log).** The
> project began as "find wallets winning >75% of their bets." That metric turned
> out to be an artifact, and the research below changed what we actually measure.
> Don't fund anything before reading it.

## Tools

| File | What it does |
|------|--------------|
| `smart_money.py` | Dashboard + scanner. Ranks leaderboard wallets by **true** win rate. |
| `edge_research.py` | Scans up to ~2000 wallets for a reliable, copyable weekly edge (consistency metrics + copyability). |
| `lookback.py` | Deep-dive a short list over a long window, split into halves for out-of-sample reads. |
| `table_77.py` | Aggregate a filtered wallet set into one CSV (ROI, total staked, consistency). |
| `copytrade.py` | Copy-trade engine — mirror a watchlist (paper by default, live gated). |
| `backtest.py` | Replay a watchlist over a recent window and mark outcomes. |
| `lp_screener.py` | Rank reward-eligible markets by risk-adjusted LP yield (pool ÷ competition, penalized by volatility). |

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

## What we learned (research log)

The honest story of what the data showed, in order. Each finding killed an
assumption the previous step relied on.

**1. Win rate was an illusion (survivorship bias).**
Polymarket only redeems *winning* shares; losing shares are worth $0 and sit
unredeemed in `/positions` at `curPrice 0` forever, never entering
`/closed-positions`. Measuring win rate over `/closed-positions` alone counts
almost only winners. Examples: NiNo999 read 90.6%, true rate **48.3%**; Boggs
read 73.4%, true **50.3%**. Fixed by unioning both endpoints over a window.
**Takeaway: a high reported win rate is a red flag for a bias bug, not a sharp.**

**2. With the honest metric, nobody wins 75%.**
Across 25 top wallets, true win rates clustered at a **median 49%** (coin flip),
max ~60%. Zero passed a 75% bar. Win rate is the wrong thing to rank on.

**3. Win rate ≠ profit; raw PnL ≠ reliability.**
`surfandturf` won 54.8% and made millions; `Latina` (leaderboard #1 all-time)
won 43% and was **−$3.8M over 90 days**. And wallets with big total PnL often
got there on one or two outlier weeks (38% green weeks) — a lottery, not an
edge. The signal that finds reliable money is **weekly consistency**: % of weeks
green, profit factor, weekly Sharpe — measured per week, with enough weeks.

**4. Flat-size copy-trading is −EV.**
A 7-day backtest of four "top" wallets returned **−48%**. At ~50% entry hit
rates, mirroring entries at flat size just pays the spread on coin flips. A
profitable wallet's edge lives in *sizing and entry prices*, which copying
entries does not reproduce. (The backtest also exposed a missing per-position
cap — proportional adds could balloon one market to the whole exposure limit.)

**5. A reliable edge looks real but rare — and skews young.**
Scanning 1,500 wallets over 120 days: 1,017 had history, 199 passed a
consistency screen, **77 were copyable** (hold-to-resolution ≥70%). But ~7.5%
of wallets passing by chance is exactly what randomness produces over 1,017
coin-flippers — so some of the 77 are luck. Worse, a 240-day lookback showed
the "best" wallets are **young accounts** (surfandturf's oldest bet: 72 days;
joblessfinalboss: 79). New accounts that get hot rise to the leaderboard and
pass the screen; the ones that flamed out are delisted. **The most impressive
short-term performers are the least trustworthy.**

**6. ROI and size are inversely related.**
Among the 77 copyable wallets, the highest-ROI ones bet small (dnte: 57% ROI on
$109K), while the biggest bettors scalp thin edges (elmcap2: $114M staked,
**0.4%** ROI). `surfandturf` was the lone anomaly — big *and* high-ROI ($27.9M
staked at 16%) — which makes it either the best find or the biggest variance
story. At 72 days old, we can't yet tell.

### Where this leaves the strategy

- **Rank on risk-adjusted consistency** (% green weeks × profit factor ×
  Sharpe), never win rate or raw PnL.
- **Require account longevity** — distrust anything under ~4–6 months.
- **Validate out-of-sample** (walk-forward: select on an early window, measure a
  later one) before trusting any wallet list. This is the decisive open step.
- **Copying entries ≠ copying edge.** A working strategy likely needs to model
  sizing/pricing, or pivot to a consensus signal (bet where many vetted wallets
  agree) rather than blind mirroring.

## Liquidity rewards (the market-making pivot)

After copy-trading proved unreliable, the research pointed to **liquidity
rewards** as the lowest-risk edge. Polymarket pays makers a daily USDC pool for
resting limit orders near a market's midpoint — your share = your score ÷ total
score, where score rewards size and closeness to mid (quadratic:
`((max_spread − your_spread) / max_spread)²`). ~$200K/day is distributed across
~8,000 eligible markets (queryable via the CLOB `sampling-markets` endpoint;
each market exposes `rewards.rates[].rewards_daily_rate`, `min_size`,
`max_spread`).

`lp_screener.py` ranks those markets by **risk-adjusted** yield — reward pool ÷
order-book competition near mid (gross APR for a $1000 two-sided position),
penalized by 24h midpoint volatility (the adverse-selection proxy) and by
time-to-resolution (imminent = live = toxic).

```bash
python3 lp_screener.py --min-rate 50 --capital 1000   # one-shot snapshot → lp_markets.csv
```

**It's a one-shot snapshot, not a daemon** — reward pools, books, and the
markets themselves churn daily, so re-run before each session.

**What it found:** the sweet spot is **long-dated, low-volatility prop markets**
(World Cup player props, eliminations) — thin books, decent pools, vol ~0, days
to resolution. Live esports markets show astronomical gross APR but get
correctly de-ranked: that's where you get picked off.

**Caveats that still gate real money:** headline APRs are a *snapshot* — thin
pools attract competitors and yield mean-reverts down; they're *gross*, ignoring
inventory losses when you get filled; and we have not yet confirmed near-empty
books actually pay the full pool. The paper LP loop (post near mid, requote on
moves, track **net** = rewards − pick-off) is the next and decisive test.
