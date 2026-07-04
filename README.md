# 🏆 Winning Wallet Finder

Find Polymarket wallets with a **real, statistically-verifiable edge**, test
whether copying them actually makes money — **with real fees, lag, and
slippage** — and copy-trade them with a 24/7 bot (paper today, live-capable).

This started as "copy the smart money." Along the way we tested — and ruled out
— six systematic public-data strategies, and found that the *only* signal that
holds up is **statistical improbability**: wallets that win far more than the
prices they paid imply. This repo is the tooling for finding, validating, and
copying those wallets, plus an honest record of everything that didn't work.

> **Read [`FINDINGS.md`](FINDINGS.md) for the research story.** TL;DR: detection
> works; profitable copying is *plausible but unproven* — every backtest here is
> in-sample by construction, and **the July 2026 forward test (live now) is the
> arbiter.** Treat headline returns as ceilings, not forecasts.

---

## The system today (July 2026)

Three deployed pieces + one static dashboard:

| piece | where it runs | what it does |
|-------|--------------|--------------|
| **daily pipeline** (`live/daily.sh`) | this Mac, launchd 10:00 | refresh the bet cache → 5-gate skill scan → fee-aware sharp selection → conviction floors → backtest book → publish JSON feeds to GitHub |
| **copybot worker** (`copybot.py` via `host/start.sh`) | Railway, 24/7 | polls the 4 followed wallets every 60s, paper-copies their conviction bets with real fees/lag/slippage accounting, settles at CLOB resolution, commits its book back to the repo |
| **Discord watcher** (`webhook_receiver.py`) | Railway `web` service | Alchemy address-activity webhook → instant trade pings |
| **dashboard** | [jaxperro.com/trading](https://jaxperro.com/trading) (static, in the `jaxperro` repo) | renders the three JSON feeds: live bot book, backtest book, sharp table |

**The July 2026 live test:** a fresh $1,000 paper book (started 2026-07-02, on
Railway) following **Kruto2027, shisan888, fortuneking, LSB1** — the top
fee-adjusted copy-positive sharps + one curated pick. Every fill records
detection lag, price slippage, and the taker fee; missed bets are recorded and
settled hypothetically. If this month's *measured* numbers hold up, real money
follows (see [`LIVE_TEST.md`](LIVE_TEST.md)).

```
 data layer          selection                        execution              display
 ──────────          ─────────                        ─────────              ───────
 live/cache.duckdb ─▶ skill.py (5-gate funnel)        copybot.py (Railway) ─▶ jaxperro.com/trading
 (schema v2:          conviction_scan.py (p80 bets)   · 4%-of-equity stakes   · copybot_live.json
  33k wallets,        validate_timing.py (fee-aware   · taker fees modeled    · portfolio.json
  19M resolved bets,   copy replay → watch_sharps)    · lag/slip per fill     · watch_sharps.json
  token-keyed,        portfolio.py (backtest book)    · missed-bet ledger
  archival)           sync_floors.py (bot parity)     · CLOB settle + redeem
```

---

## File map

| path | role |
|------|------|
| `live/` | **the current system**: cache, scanners, sharp selection, backtest, daily pipeline ([live/README](live/README.md)) |
| `copybot.py` | the copy-trading bot: push/poll trigger → follow filter → execution engine (paper + live) |
| `archive/copytrade.py` | the execution engine the bot reuses: sizing, risk gates, price guard, paper/live executors |
| `host/start.sh` | 24/7 worker bootstrap for Railway/Fly/VPS (clones repo, resumes committed state) |
| `LIVE_TEST.md` · `preflight_live.py` · `redeem.py` | real-money runbook, read-only credential preflight, on-chain redemption |
| `insider.py` | the original detector: z-score, pre-resolution timing, fresh-wallet flags, funding-cluster rings |
| `smart_money.py` | shared HTTP helper + survivorship-corrected win-rate dashboard (`:8899`) |
| `webhook_receiver.py` | Alchemy webhook → Discord trade pings (Railway `web` service) |
| `wide/` | frozen-subgraph bulk scanner (1.76M wallets, historical only — subgraph froze Jan 2026) |
| `archive/` | the six strategies that didn't work, kept honest ([archive/README](archive/README.md)) |
| `hunt.py` · `huntwide.py` · `oos.py` · `copyback.py` | earlier research sweeps/backtests (superseded by `live/`) |

---

## The core idea: z-score, not win rate

Every Polymarket bet has an entry price that *is* the market's estimate of its
odds (a YES at 30¢ ⇒ market thinks 30%). If you have no edge, over many bets
you win about the **sum of your entry prices** — call it *expected wins*.

```
z = (actual wins − expected wins) / standard deviation
```

- **z = 0** → you won exactly what your prices implied → no edge.
- **z = 3** → ~1-in-740 by luck. **z = 5** → ~1-in-3.5M.

Why this beats win rate: a wallet that bets longshots and wins 14% when the odds
implied 8% has a huge edge (high z) despite a low win rate; a wallet buying 90¢
favorites and winning 90% has z≈0. Win rate is also **survivorship-biased** on
Polymarket (losing shares sit unredeemed and invisible — see FINDINGS). And even
true win rate **over-counts scalpers** — so the final selection metric is
**fee-adjusted Copy P&L**: what a flat-$50 copy of the wallet's conviction bets
*actually* returns after taker fees (replay entries, mirror exits, settle at
CLOB resolution). Judge by Copy P&L, never win rate.

---

## Quickstart for a new developer

```bash
git clone https://github.com/jaxperro/winning-wallet-finder
cd winning-wallet-finder
pip3 install duckdb                    # the only dep for research/selection
cp config.example.json config.json     # secrets live here (gitignored)

# selection layer (live/) — everything reads the local bet cache
cd live
python3 enumerate.py 30                # build a candidate pool (last 30d markets)
python3 collect.py                     # pull their bets into cache.duckdb (resumable)
python3 skill.py                       # 5-gate skill funnel -> watch_skilled.json
python3 conviction_scan.py             # conviction-bet profile scan
python3 validate_timing.py             # fee-aware copy replay -> watch_sharps.json
python3 portfolio.py                   # the backtest book -> portfolio.json
./daily.sh                             # or: the whole thing, end to end

# copy bot (repo root) — paper by default, no orders ever without --live
python3 copybot.py --config live/copybot.paper.json --state /tmp/s.json --poll 60
python3 copybot.py --test-wallet 0x…   # dry-run one wallet's latest trade

# live trading (real money): read LIVE_TEST.md, then
pip3 install py-clob-client web3
python3 preflight_live.py              # read-only credential/balance check
```

The cache is the point: **every score re-runs in seconds from
`live/cache.duckdb`** (~33k wallets / 19M+ resolved bets) instead of hours of
API pulls. Schema v2 is token-keyed, provenance-tagged, and archival (refreshes
upsert instead of wiping; failed pulls are never cached as "no bets") — details
in [`live/README.md`](live/README.md).

### Config & secrets (all gitignored)

| file | holds |
|------|-------|
| `config.json` | Discord webhooks, Alchemy key, the followed-wallet list + per-wallet conviction floors (auto-refreshed daily by `sync_floors.py`) |
| `config.live.json` | live-trading credentials (`private_key`, `funder_address`) + tiny test caps — see `LIVE_TEST.md` |
| Railway `copybot` service | `GITHUB_TOKEN` (fine-grained PAT, contents-RW on this repo — the bot commits its state/feed back), optional `DISCORD_WEBHOOK` |

---

## How the copy bot models reality

The whole point of the July test is that paper ≈ live. Every mechanism the
backtest and bot share:

- **Taker fees** (Polymarket V2, since 2026-03-30): `fee = shares × rate × p(1−p)`,
  sports rate 0.03 — charged on every marketable entry and exit; redeeming at
  resolution is free. Fee-adjusted Copy P&L also drives *selection*.
- **Lag + slippage**: the bot fills at the live CLOB ask at detection (~60s poll),
  logging per-fill `detect_lag_s` and `slippage_pct`; the backtest applies a
  +0.5%/~90s haircut. Measured so far: ~48s avg / +0.8% avg slip.
- **Dynamic sizing**: each bet stakes **4% of current working equity** (compounds
  both ways), halved while equity is below 80% of its high-water mark. The rule
  binds **per market** — adds that mirror a sharp scaling in can only top a
  position up to the current stake size, never past it.
- **Profit ratchet** (`stake_cap_usd: 250`): stakes pin at $250; once the book
  outgrows that level, surplus cash sweeps to a **banked reserve** that never
  bets — locked-in profit, and fills stay inside realistic book depth. A
  per-event correlation cap exists (`risk.max_per_event`) but is **off**.
- **Entry cap 0.95**: entries above 95¢ are skipped (`follow.max_entry`) — the
  June sweep showed >95¢ favorites *lower* final equity even while winning
  (slip + fee eat the 1–3% payouts; the capital compounds better elsewhere).
- **Asymmetric price guard**: a price *below* the sharp's fill is never blocked
  (better odds, by rule); only adverse drift >5% is skipped.
- **Conviction filter**: only copy a wallet's top-20%-by-stake bets (per-wallet
  p80 floor, kept in sync with the dashboard by `sync_floors.py`).
- **Missed-bet ledger**: every bet the bot *couldn't* take (cash deployed, price
  ran up) is recorded and settled hypothetically — capacity costs are measured,
  not invisible.
- **Settlement**: winners settle at authoritative CLOB winner flags (see the
  gotcha below — it caused the project's worst bug); live mode auto-redeems
  on-chain (`redeem.py`; neg-risk markets need manual redeem).

Safety: paper is the default; live requires `mode:"live"` **and** `--live`
**and** a typed confirmation phrase, under hard caps. The GH-Actions cron
runner is retired (GitHub throttled `*/5` to ~2h in practice — it copied 1 of
~104 qualifying trades in June; the always-on Railway poller replaced it).

---

## Data sources

| Source | Used for |
|--------|----------|
| `data-api.polymarket.com` | positions, trades, activity (+`eventSlug`), leaderboard |
| `gamma-api.polymarket.com` | market metadata (NB: `condition_ids` filter returns nothing for resolved markets) |
| `clob.polymarket.com` | order books, prices, **authoritative resolution** (`winner` flags), market slugs |
| Alchemy (Polygon) | funding-cluster traces + the live trade webhook |

## Gotchas a maintainer must know

1. **CLOB `winner` flags: `false` means "not yet", not "lost".** Every token of
   an *unresolved* market reports `winner: false`; resolution flips exactly one
   to `true`. Any settle/replay logic must gate on `any(winner is True)` first.
   Treating `false` as lost made the bot settle live in-play positions as
   instant losses (four winning bets booked −$180 on 2026-07-02). Slow pollers
   never see this — markets genuinely resolve between checks — which is why it
   survived June.
2. **`eventSlug` sub-splits one game** (`…-2026-07-01-more-markets`,
   `…-second-half-result`): group by the `…-YYYY-MM-DD` prefix
   (`event_key()` in `copytrade.py` / `portfolio.py`) for anything per-event.
3. **Win rate over-counts scalpers and is survivorship-biased** — never select
   on it; the fee-adjusted Copy P&L replay is the selection metric.
4. **`/closed-positions` sorts by `realizedPnl` by default** — always pass
   `sortBy=TIMESTAMP` or you sample only the biggest wins.
5. **`cache.duckdb` is single-writer** — a running `collect.py` blocks even
   read-only connections; the daily pipeline serializes for this reason (and
   caps stale refreshes at `STALE_CAP=2500`/run so it stays daily).
6. **GitHub Actions `*/5` cron actually fires ~every 1.5–2.5h** — never use it
   for anything latency-sensitive (it copied 1 of ~104 trades in June).
7. **GitHub Pages soft-limits ~10 deploys/hour** on the `jaxperro` repo —
   batch dashboard pushes (see that repo's README).

---

## The research (how we got here)

**The 5-gate funnel** (`live/skill.py`): a wallet is "skilled" only if it clears
`n ≥ 15 resolved bets` → `z > 0` → `Benjamini–Hochberg FDR @5%` → `split-half
out-of-sample persistence` → `not a market-maker` (all resolved-only: early-sold
positions in unended markets are marks, not outcomes, and never score).

**The clean test (June 2026):** high-win-rate "favorite-rider" wallets looked
+23.6% in-sample and lost −7.4% once selected without look-ahead — exactly the
LBS/Yale "~60% of lucky winners become losers" result. **Don't copy win rates.**

**The repeatable find:** score wallets on their **conviction bets** (top 20% by
stake, per-wallet p80) — the edge is wallets that win 70–80% on genuinely
uncertain (~0.4–0.6) markets. Trained pre-June, validated June: 62/83 stayed
profitable forward (p≈0). A **fee-aware flat-$50 copy replay** then keeps only
wallets that are actually profitable to *copy* (scalpers with ~100% shown win
rates lose money when copied) → currently **12 copy-positive holders** in
`watch_sharps.json`, refreshed daily.

**The backtest** (`live/portfolio.py`): the followed four, June 1 → now, with
fees/lag/dynamic sizing: **+531%** — but June is the month these wallets were
*selected on*, so that's an in-sample ceiling. July, live, is the test.

**What didn't work** (see `FINDINGS.md` + `archive/`): copy-trading raw,
win-rate ranking, LP reward farming, binary & multi-outcome arb, cross-venue
PM↔Kalshi arb — all efficient or illusory.

---

## The honest verdict

- **Detection works.** z + timing + funding clusters reliably surface anomalous
  wallets.
- **Copying is promising but unproven.** Selection is fee-aware and
  execution-realistic now, but every historical return in this repo is
  in-sample. The running July book — real lag, real fees, measured slippage,
  missed bets counted — is the first number that deserves trust.
- **Scale carefully.** Above ~$250/clip in thin sports books, best-ask fills
  turn optimistic; a depth-aware fill model is the known next step before
  sizing up.
