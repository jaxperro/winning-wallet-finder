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
| dashboard | `dashboard.py` | self-contained `dashboard.html` — sortable, archetype-tagged, live recent-trade lookup |
| backtest | `backtest_june.py [arch]` | copy an archetype's June-1+ entries, $1000, no lag → P&L |
| clean test | `clean_test.sh` | **the honest test**: re-select on pre-June-1 data only, then backtest June-1+ forward |

## The cache is the point

`cache.duckdb` holds ~26k wallets / 12.5M+ bets, pulled once. Every score —
any archetype, any cutoff, the clean OOS test — now runs in **seconds** instead
of hours of API pulls. `MAX_AGE_DAYS=14`: the broad pool refreshes biweekly; the
watchlist is force-refreshed daily (`cache.invalidate`) for forward tracking.

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
- `validate_timing.py` — the copyability gate: entry→resolution **lead time** on
  winning conviction bets separates copyable sharps (multi-day lead) from
  "last-minute" wallets (median lead <24h) we can't mirror in time — could be
  genuine insiders or just fast-resolving-market specialists, can't tell which.
  → **50 validated copyable sharps** in `watch_sharps.json` (shown live on
  jaxperro.com/trading).

Identifiers for a follow-worthy wallet: on its **≥$200 bets** — win ≥65%, avg
entry 0.35–0.70 (edge, not favorites), +copy-ROI, FDR-significant, **median lead
≥24h** (copyable), and it **holds out-of-sample**.

## Daily (`daily.sh`)

1. discover (enumerate last 14d) → 2. freshen cache (force-refresh watchlist +
top up new wallets) → 3. re-score (instant from cache) → 4. regenerate dashboard
+ snapshot to `history/`. Schedule via launchd/cron (Mac must be awake).

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
