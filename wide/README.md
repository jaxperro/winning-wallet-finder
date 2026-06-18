# wide/ — bulk subgraph edge scanner

Find wallets with a real edge across **all** of Polymarket (~1.76M traders,
268k conditions) by bulk-ingesting the on-chain subgraph into a local DuckDB
and ranking with SQL — instead of per-wallet API calls (which cap out at a few
hundred wallets and rate-limit).

## Why a local DB, not the API

The Goldsky orderbook subgraph **times out on any `orderBy`** over a
non-indexed field (`numTrades`, `scaledProfit`, …). You cannot ask it for "top
wallets." The only scalable pattern is: cursor-paginate every row by `id` (the
indexed key), land it locally, and rank in DuckDB. That constraint *is* the
architecture.

## Why the win rate here is honest

The data-api hides losers: Polymarket only redeems winning shares, so losing
positions sit unredeemed and never enter `/closed-positions`. Measuring win
rate there reads ~90% when the truth is ~50% (see ../FINDINGS.md). The subgraph
records a `marketPosition` for **every** buy regardless of redemption, so the
survivorship bias structurally does not exist in this data.

## What "edge" means

A high win rate is not edge — you can hit 90% by only buying 95¢ favorites.
Edge is **beating the prices you paid**:

```
p   = valueBought / quantityBought      (entry price, 0..1)
won = the outcome you held paid out
z   = (wins − Σp) / sqrt(Σ p(1−p))      standard deviations above odds-implied
```

`z` high over enough bets, on a wallet that isn't a market-maker, is the real
signal (the same one ../insider.py computes, here over the entire market).

## Pipeline

| step | script | source | notes |
|------|--------|--------|-------|
| 1 | `ingest.py conditions` | subgraph | resolution + payoutNumerators → winning outcome |
| 2 | `gamma_tokens.py`       | Gamma   | token_id → (condition, outcome_index); subgraph's `outcomeIndex` is null |
| 3 | `ingest.py accounts`    | subgraph | `numTrades` (market-maker filter), `creationTimestamp` (freshness) |
| 4 | `ingest.py market_positions` | subgraph | the heavy table: entry price + win/loss per bet |
| 5 | `score.py`              | DuckDB  | z, true win rate, profit + FDR + out-of-sample |

All ingests are **resumable** (per-table `id` cursor in `_cursor`), so a long
run can be stopped and restarted. Tables join in `edge.sql`.

## Guardrails (so a 1.76M-wallet scan doesn't just surface luck)

- **min-n + market-maker cap.** A 300k-trade grinder posts huge z with no
  information. Require ≥30 resolved bets and cap `numTrades`.
- **Benjamini–Hochberg FDR.** Scan 100k wallets and thousands clear z>3 by
  chance (look-elsewhere effect). `score.py` reports how many survive 5% FDR.
- **Out-of-sample.** `score.py --cutoff YYYY-MM-DD` selects wallets on bets
  resolved before the date, then measures the *same* wallets forward. Edge that
  is real persists; edge that is curve-fit reverts to z≈0 — which is what every
  strategy in ../FINDINGS.md did. **Do not size up on in-sample z.**

## Usage

```bash
pip install duckdb
python3 ingest.py conditions accounts        # small tables
python3 gamma_tokens.py                       # token→outcome map
python3 ingest.py -p market_positions         # heavy (~53M rows); parallel + resumable
python3 score.py --min-n 15 --max-trades 5000 --top 40
python3 score.py --cutoff 2026-04-30 --min-n 15   # in-sample vs forward
```
