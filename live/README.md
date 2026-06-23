# live/ — find & track the genuinely-skilled ~3%

Finds the small fraction of Polymarket wallets with a *real, repeatable* edge —
the ~3% the [LBS/Yale study](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5910522)
identifies — from the **live data-api**, caches everything locally, and tracks
them forward. This is the going-forward system; the frozen-subgraph bulk approach
lives in `../wide/`.

## Why this and not win rate

Win rate is survivorship-biased and decoupled from edge (see `../FINDINGS.md`).
A wallet is "skilled" only if it **beats the prices it paid** and that edge
**persists out-of-sample**. We reproduced the research's own finding on live
June data: favorite-rider wallets that looked +23.6% in-sample lost **−7.4%**
once selected without look-ahead (see "The clean test" below).

## The 5-gate funnel (`skill.py`)

A wallet counts as skilled only if it clears all five:

1. **n ≥ 15** resolved bets (assessability; the paper's skilled avg ~79).
2. **z = (wins − Σp)/√Σp(1−p)** clearly > 0 — wins above what entry odds implied.
   This is the closed form of the paper's "randomize direction 10k×" benchmark.
3. **Benjamini–Hochberg FDR @ 5%** — at scale, thousands clear z>3 by chance.
4. **Split-half out-of-sample** — skill in the earlier half persists in the
   recent half (`z_oos > 0`). The gate that separates the real 3% from the lucky.
5. **MM/bot cap** (`n ≤ 2500`) — a thousands-of-bets grinder isn't info-edge.

Win rate is never a gate — only displayed. Wallets are tagged `value` (beats
underdog/longshot prices — the copyable alpha), `balanced`, or `favorite`
(high win% riding near-certain favorites — real but thin/uncopyable).

## Pipeline

| step | script | what |
|------|--------|------|
| enumerate | `enumerate.py [days]` | recent liquid markets (Gamma `end_date_min`) → top traders → candidate pool (`candidates.json`), accumulates across runs |
| cache | `cache.py` / `collect.py` | pull each wallet's resolved bets **once** into `cache.duckdb` (24s → 0.003s on re-read). Stores `res_t` per bet, so any date cutoff reads the same cache |
| score | `skill.py [N]` | the 5-gate funnel over cached candidates → `watch_skilled.json` (webhook-compatible) |
| sharps | `conviction_scan.py` + `validate_timing.py` | conviction-profile scan → copy-positive-holder selection → `watch_sharps.json` (see "The repeatable find") |
| portfolio | `portfolio.py` | $1k paper book off the cache → `portfolio.json` (see "Paper portfolio") |
| dashboard | `dashboard.py` | self-contained `dashboard.html` — sortable, archetype-tagged, live recent-trade lookup |
| backtest | `backtest_june.py [arch]` | copy an archetype's June-1+ entries, $1000, no lag → P&L |
| clean test | `clean_test.sh` | **the honest test**: re-select on pre-June-1 data only, then backtest June-1+ forward |

## The cache is the point

`cache.duckdb` holds ~30k wallets / 14M+ bets, pulled once. Every score —
any archetype, any cutoff, the clean OOS test — now runs in **seconds** instead
of hours of API pulls. `MAX_AGE_DAYS=14`: the broad pool refreshes biweekly; the
watchlist is force-refreshed daily (`cache.invalidate`) for forward tracking.

**Retention gotcha — the cache is NOT append-only.** Each wallet's refresh does
`DELETE FROM bets WHERE wallet=?` then re-inserts a fresh pull, and that pull is a
**rolling 180-day window** (`WINDOW_DAYS`) capped at ~2k bets/endpoint
(`max_pages`). So wallet *coverage* grows (new wallets are kept), but any single
wallet's history is a capped, rolling, overwrite-on-refresh snapshot — bets older
than ~180d are dropped on the next re-pull. For a permanent long-horizon archive,
use the append-style `../wide/pmkt.duckdb` subgraph dataset instead, or change the
pull to upsert + drop the cutoff.

## The clean test (why the favorites are a mirage)

`clean_test.sh` selects favorites using **only bets resolved before June 1**,
then copies their June-1+ entries:

- **In-sample (contaminated):** 21 favorites, 99% win rate, **+23.6%**.
- **Clean (pre-June-1 selection):** 15 favorites, 68% win rate, **−7.4%**
  (−19% on the settled portion).

The +23.6% was selection bias. This matches the paper: ~60% of "lucky winners"
turn into losers out-of-sample. **Don't copy favorite-riders.** The `value`
archetype (beats underdog prices) is where real alpha may live — test it with
`backtest_june.py value`.

## Strategy backtests

- `strategy.py` — train (pre-May-30) / test (June1+) wallet selection on copy-ROI
  + z + monthly consistency + diversification. → `selection.json`.
- `followability.py` — pull entry timestamps (cached), drop wallets whose edge is
  in un-followable fast/live markets, re-rank on followable forward bets. →
  `watch_final.json` (the execution-realistic list).
- `pnl_basket.py` / `pnl_focused.py` — $1,000 capital-constrained copy sims with
  **missed-trade accounting**. Key result: the broad basket loses on $1k (can't
  follow 1,200 trades), but **1–2 wallets + a conviction (bet-size) filter clears**
  out-of-sample. See `../FINDINGS.md`.

## The repeatable find (`conviction_scan.py` + `validate_timing.py`)

The best result. Score wallets on their **high-conviction bets only — the top 20%
by stake size (per-wallet p80, not a flat $200)**: the edge is wallets that win
70–80% on genuinely-uncertain (~0.4–0.6) markets — real skill, not favorite-riding.
The per-wallet cutoff reproduces flat-$200's win-rate lift while adapting to each
wallet's scale (a whale's $200 bet isn't conviction; a minnow's is).

- `conviction_scan.py` — train pre-June / validate June on conviction bets →
  218 matches, **62/83 profitable forward (p≈0)**. → `conviction_wallets.json`.
- `validate_timing.py` — the **copyability selection** (rewritten 2026-06-23).
  For every conviction wallet it runs a flat-$50 copy replay and keeps only the
  ones genuinely profitable to copy: `copy_pnl > 0` **and** a real hold-to-
  resolution edge (`held_pnl > 0`, held win-rate ≥55% over ≥8 resolved held bets),
  active in 30d, median lead ≥1h (drops sub-hour snipers). It also precomputes
  every stat the dashboard renders — including **`copy_pnl`**, the authoritative
  flat-$50 copy P&L (replay their entries, mirror their exits, settle held bets at
  CLOB resolution by `token_id`). → **~14 copy-positive holders** in
  `watch_sharps.json` (read live by jaxperro.com/trading; the table defaults to
  sorting by Copy P&L).

**Copy P&L is the one number that matters for picking copy targets.** It replaced
an earlier lead-time gate. The lesson (see FINDINGS "the scalper trap"): position
**win% over-counts scalpers** — `cache.won` (curPrice ≥ 0.5) scores a sell-at-
profit as a "win", so a wallet can show ~100% conviction win% yet *lose money when
copied* (ArbTrader: 99.5% win, **−$790** copy P&L). Conviction itself = the top 20%
of a wallet's **own** stake sizes, measured at the **position** level (a wallet's
total stake in a market, not per-trade — a scalper splits one position over many
small buys). Judge by Copy P&L, never win%.

## Paper portfolio (`portfolio.py` → `portfolio.json`)

A $1,000 paper book that mirrors a chosen set of wallets' conviction bets at a
flat $50/trade, **computed off the cache** (not client-side). The cache stores
each bet's resolution time (`res_t`), so capital **recycles at the true resolution
moment** — fixing the phantom capital-lock the old browser replay suffered (it
missed resolution dates for high-volume wallets and skipped bets it could afford).
Edit the `WALLETS` list at the top of `portfolio.py` to change who's followed.
Output `portfolio.json` (equity, splits, current/resolved/missed tables, per-
wallet) is read by the dashboard in one request. Hold-to-resolution model (the
cache has no sell events — the right model for the holders we select); a small
live `/positions` pull supplies the "current open bets" panel.

> **Caveat carried through the whole stack:** every P&L here is *idealized* — no
> slippage, fees, or copy lag, and the wallets were selected in-sample. Treat the
> headline % as a ceiling, not a forecast (the project's own history: an in-sample
> copy backtest hit +168% then collapsed out-of-sample).

## Dashboard feeds (jaxperro.com/trading)

The dashboard (in the `jaxperro` repo, `trading/index.html`) is static and reads
two precomputed JSONs from this repo via raw.githubusercontent:

- **`watch_sharps.json`** — the sharps table (copy-positive holders + per-wallet
  Copy P&L, win%/record, avg bet, leads).
- **`portfolio.json`** — the $1k paper book up top.

Both are committed + pushed by `daily.sh`, so the page is just a renderer (no
per-wallet API calls). It keeps a client-side replay as a fallback if the feed is
unreachable.

## Copy execution (separate, in progress)

Actually *placing* the copied trades lives outside this finder — `copybot.py` +
`sync_floors.py` (a teammate's work). `sync_floors.py` recomputes each followed
wallet's p80 conviction floor from the fresh cache into `../config.json` so the
bot's entry gate stays in parity with the dashboard's top-20%-by-stake definition.
This finder's job is selection + tracking; execution is downstream.

## Daily (`daily.sh`, launchd 10:00)

1. **discover** (`enumerate.py 14`) — recent liquid markets → candidate pool.
2. **freshen** — force-refresh the watchlists, then `collect.py` tops up new/stale.
3. **re-score** (`skill.py`) → `watch_skilled.json`.
4. **sharps** (`conviction_scan.py` + `validate_timing.py`) → `conviction_wallets.json`
   + `watch_sharps.json` (the copy-positive holders).
5. **floors** (`sync_floors.py`) — copy-bot conviction floors → `../config.json` (local).
6. **portfolio** (`portfolio.py`) → `portfolio.json` (the $1k paper book).
7. **dashboard** (`dashboard.py`) + snapshot to `history/`.
8. **publish** — commit + push the JSON feeds, then ping Discord (`daily_webhook`
   in the gitignored `../config.json`).

Schedule via launchd/cron (Mac must be awake).

## Usage

```bash
pip install duckdb
python3 enumerate.py 180        # build candidate pool (last 6 months)
python3 collect.py              # cache all candidates (one-time, slow; resumable)
python3 skill.py                # -> watch_skilled.json (seconds, from cache)
python3 dashboard.py            # -> dashboard.html
./clean_test.sh                 # the out-of-sample verdict
```

Local data (`*.duckdb`, `candidates.json`, `*_scored.json`, `history/`) is
gitignored — regenerate via the steps above.
