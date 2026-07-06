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
| **daily pipeline** (`live/daily.sh`) | this Mac, launchd **08:00** (runs on wake if the Mac was asleep) | refresh the bet cache → 5-gate skill scan → fee-aware sharp selection → conviction floors → backtest book → publish JSON feeds to GitHub |
| **copybot worker** (`copybot.py` via `host/start.sh`) | Railway, 24/7 | **push mode**: an Alchemy address-activity webhook (`copybot follow set`, Polygon) pings `POST /alchemy` the moment a followed wallet trades (~2–5s detection), signature-verified; a 60s heartbeat settles/publishes and a 5-min backstop poll catches dropped pushes. Paper-copies with real fees/lag/slippage, settles at CLOB resolution, commits its book back to the repo |
| **Discord digest** (`live/discord_daily.py`) | end of the daily pipeline | one message/day: the sharp list with profile links + 30-day conviction stats (per-trade pings retired 2026-07-04; the old Alchemy watcher lives in `archive/webhook_receiver.py`) |
| **dashboard** | [jaxperro.com/trading](https://jaxperro.com/trading) (static, in the `jaxperro` repo) | renders the three JSON feeds: live bot book, backtest book, sharp table |

**The July 2026 live test:** a fresh $1,000 paper book (started 2026-07-02, on
Railway) following the wallet set in **`live/copybot.paper.json`** — the single
source of truth (currently 5 volume + 3 whale wallets; the dashboard hero lists
them live). Two stake classes: **volume** wallets (4% of equity/bet) are copied
on their **conviction bets only** (auto p80 floor derived at boot); **whale**
wallets (12%/bet) are copied on **every trade** — they're the big-clip informed
holders the trusted-row re-validation surfaced (see FINDINGS "The holder blind
spot"). Every stake is **capped at the signal's own bet size**. Every fill
records detection lag, price slippage, and the taker fee; missed bets are
recorded and settled hypothetically. If this month's *measured* numbers hold
up, real money follows (see [`LIVE_TEST.md`](LIVE_TEST.md)).

```
 data layer            selection                          execution              display
 ──────────            ─────────                          ─────────              ───────
 live/cache.duckdb ──▶ trust.py (trusted-row filter)      copybot.py (Railway) ─▶ jaxperro.com/trading
 (schema v2:           skill.py (5-gate funnel)           · class % of equity,    · copybot_live.json
  35k wallets,         conviction_scan.py (p80 + z_all)     capped at the           (live bot book)
  20M+ resolved bets,  validate_timing.py (fee-aware        signal's own bet     · portfolio.json
  token-keyed,          copy replay → watch_sharps)       · taker fees modeled     (rolling backtest)
  archival;            portfolio.py (rolling replay of    · lag/slip per fill   · watch_sharps.json
  TRUSTED rows only     backtest.json's wallet set)       · missed-bet ledger      (sharp table)
  may score)           sync_floors.py (bot parity)        · CLOB settle + redeem
```

---

## Operating the system (the cheat-sheet a new maintainer needs)

| task | how |
|------|-----|
| **add / remove / reclass a LIVE wallet** | edit the `wallets` list in `live/copybot.paper.json` (`{"wallet","name","class":"volume"\|"whale","floor":123?}` — floor optional, auto p80 at boot; whales ignore floors) then run **`./live/deploy_bot.sh`** — it validates, previews, commits, pushes, redeploys Railway, and confirms the boot banner. **Push mode:** also update the Alchemy webhook's address list (dashboard.alchemy.com → Webhooks → `copybot follow set`) — the 5-min backstop poll covers a forgotten update, at poll-speed lag |
| **backtest any wallet set** | edit `live/backtest.json` (same entry shape) → `python3 live/portfolio.py`; ad-hoc without touching the dashboard: `python3 portfolio.py --wallets 0xabc,0xdef:whale --days 14 --out /tmp/t.json` |
| **promote a wallet to live** | prove it in `backtest.json` first, copy the same entry into `copybot.paper.json`, run `deploy_bot.sh` |
| **watch the live bot** | `railway logs --service copybot` (one summary line per 60s poll); the book is also committed as `live/copybot_live.json` and rendered on the dashboard |
| **restart / redeploy the bot** | `railway redeploy --service copybot --yes` (config/code changes: the worker clones the repo fresh at boot). **Changes to `host/start.sh`, `railway.json`, or env vars need a full rebuild: `railway up --service copybot --detach`** — redeploy reuses the old image and env snapshot (this bit us: the image's launcher was frozen for days) |
| **run the daily pipeline manually** | `cd live && bash daily.sh` (launchd runs it 08:00; ~40 min, mostly collect). Never run two at once — the cache is single-writer |
| **refresh just the sharp list** | `cd live && python3 conviction_scan.py && python3 validate_timing.py` |
| **daily Discord digest** | sent by `live/discord_daily.py` at the end of `daily.sh`; webhook = `daily_webhook` in gitignored `config.json` |
| **go real-money** | read `LIVE_TEST.md`; fill `config.live.json`; `python3 preflight_live.py`; arm with `--live` + typed phrase |

Three moving parts talk to each other: this repo (research + bot + feeds), the
`jaxperro` repo (static dashboard reading this repo's raw JSON feeds), and the
Railway project `magnificent-kindness` (service `copybot`; the old `web`
watcher service is retired). The bot **commits its own state/feed back to this
repo** every few minutes — always `git pull --rebase --autostash` before you
push (every script here already does).

---

## File map

| path | role |
|------|------|
| `live/` | **the current system**: cache, scanners, sharp selection, backtest, daily pipeline ([live/README](live/README.md)) |
| `live/trust.py` | the trusted-row filter every selector must read through (see gotchas 8–9) |
| `live/backtest.json` · `live/copybot.paper.json` | the two wallet-set configs: backtest experiments vs. the live paper bot (same entry shape) |
| `live/deploy_bot.sh` | one-command live-bot deploy: validate → preview → commit → push → Railway redeploy → confirm boot |
| `live/discord_daily.py` | the daily Discord digest (the only Discord output) |
| `copybot.py` | the copy-trading bot: push/poll trigger → follow filter → execution engine (paper + live) |
| `archive/copytrade.py` | the execution engine the bot reuses: sizing, risk gates, price guard, paper/live executors |
| `host/start.sh` | 24/7 worker bootstrap for Railway/Fly/VPS (clones repo, resumes committed state) |
| `LIVE_TEST.md` · `preflight_live.py` · `redeem.py` | real-money runbook, read-only credential preflight, on-chain redemption |
| `insider.py` | the original detector: z-score, pre-resolution timing, fresh-wallet flags, funding-cluster rings |
| `smart_money.py` | shared HTTP helper + survivorship-corrected win-rate dashboard (`:8899`) |
| `archive/webhook_receiver.py` | retired 2026-07-04: Alchemy webhook → per-trade Discord pings (replaced by `live/discord_daily.py`'s daily digest) |
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
| `live/copybot.paper.json` | **committed** (no secrets): the live paper bot's wallet set + classes + follow/risk params — deploy with `live/deploy_bot.sh` |
| `config.json` | `daily_webhook` (the Discord digest), Alchemy key, a legacy curated watchlist + floors (`sync_floors.py`) |
| `config.live.json` | live-trading credentials (`private_key`, `funder_address`) + tiny test caps — see `LIVE_TEST.md` |
| Railway `copybot` service | `GITHUB_TOKEN` (fine-grained PAT, contents-RW on this repo — the bot commits its state/feed back), `DISCORD_WEBHOOK` no longer used (per-trade pings retired) |

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
- **Dynamic sizing, two wallet classes, their-bet ceiling**: each bet stakes a
  fraction of current working equity set by the followed wallet's class —
  **`volume`** (default, 4%, conviction bets only) or **`whale`** (12%, every
  trade), fractions in `follow.class_pct` — **and is never larger than the
  signal's own position size**: when the percentage works out to more than the
  wallet actually bet, the copy mirrors their exact amount (you can't
  out-conviction the signal, and fills stay within size the market demonstrably
  absorbed). Stakes compound both ways and halve while equity is below 80% of
  its high-water mark. The rule binds **per market** — adds that mirror a sharp
  scaling in grow with *their* position, never past it. The old $250 stake cap
  and banked-reserve profit ratchet were retired 2026-07-06 (backtest and bot
  together). A per-event correlation cap exists (`risk.max_per_event`), **off**.
- **Entry cap 0.95**: entries above 95¢ are skipped (`follow.max_entry`) — the
  June sweep showed >95¢ favorites *lower* final equity even while winning
  (slip + fee eat the 1–3% payouts; the capital compounds better elsewhere).
- **Asymmetric price guard**: a price *below* the sharp's fill is never blocked
  (better odds, by rule); only adverse drift >5% is skipped.
- **Conviction filter (volume class only)**: copy a wallet's top-20%-by-stake
  bets. Floors are **auto-derived at boot** (p80 of recent position stakes from
  the data-api — the worker has no cache) unless pinned with `"floor":` in the
  config; whale-class wallets bypass floors entirely (follow-all).
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

**Candidate next sources** (researched 2026-07, not yet wired in):

| Source | Would unlock |
|--------|--------------|
| [Goldsky Turbo Pipelines](https://docs.goldsky.com/chains/polymarket) | per-fill order events with timestamps for *every* wallet (Polymarket killed subgraphs with the 2026-04-28 v2 migration) — fixes the cache's two blind spots: no entry times, and position-level aggregation hiding scalps. See also [warproxxx/poly_data](https://github.com/warproxxx/poly_data), [Bitquery](https://docs.bitquery.io/docs/examples/polymarket-api/) |
| [PolymarketData.co](https://www.polymarketdata.co/) | historical order-book snapshots (Aug 2025+) → depth-aware fill model, the known step before sizing up |
| Pinnacle closing lines via [SharpAPI](https://sharpapi.io/sportsbooks/pinnacle-odds-api) / [sportsapis.dev](https://sportsapis.dev/historical-odds) / [BettingIsCool](https://api.bettingiscool.com/) (Pinnacle closed its public API 2025-07) | closing-line-value as an independent "was this bet sharp" ground truth; a Pinnacle *suspension* on an ITF/esports match is itself a fixing signal |
| [Polysights Insider Finder](https://gizmodo.com/tracking-insider-trading-on-polymarket-is-turning-into-a-business-of-its-own-2000709286) | cross-check for flagged insider wallets |

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
8. **Cached `res_t`/`won` can be fake for high-volume wallets.** When the
   data-api omits `endDate`, `res_t` falls back to the wallet's *sell time*
   and `won` is the price direction at pull — a scalper's sold-at-profit
   position masquerades as a resolved win (ArbTraderRookie's rows were 100%
   this). Selection must read **trusted rows only** via `live/trust.py`
   (cross-wallet consensus `res_t` + pulled-after-resolution + `resolved` not
   False). Also: never judge a held edge on a replay window shorter than the
   wallet's entry→resolution lead — that's how the long-lead holders were
   being filtered out (see FINDINGS "The holder blind spot").
9. **Polymarket rewrites `endDate` after resolution**, so exact-match consensus
   on `res_t` breaks for freshly re-pulled wallets (this collapsed the sharp
   list 25→7 on the first scheduled run). v2 rows with `resolved=TRUE` are
   self-certifying in `trust.py`; consensus only gates legacy rows. Related:
   `daily.sh` invalidates watchlist wallets by **deleting** their `pulled` row —
   a transiently failed re-pull then hides the wallet's whole history from
   exact `pulled_at` checks (trust.py has a 14-day fallback for this).

10. **Hold-to-resolution P&L is a copy ceiling, not the wallet's bank
    statement.** The dashboard's Conv/All-Time P&L columns price every entry
    held to resolution at the wallet's own stakes — the right yardstick for a
    copier that holds, and the wrong one for judging the wallet itself.
    Polymarket's own profile P&L (lb-api `/profit`, the **PM P&L** column) is
    their actual cash-flow result. ~1× gap = true holder (LSB1 +$69.7k vs
    +$68.5k); a huge gap = scalper whose entries resolve well but who never
    holds (ArbTraderRookie: **+$8.6k real vs +$462k held, 53×** — a 0.5%
    margin on $1.7M volume). For scalpers, whether a copier can reproduce
    their fills is the open question — judge by the live book's measured
    slippage, never the ceiling.

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
stake, per-wallet p80) over **trusted rows only** (`trust.py`), gated on
`z_all > 2` (whole-book skill — it ~doubled pooled forward copy-ROI in the
May→June tournament) and a $50 median-stake dust floor. Trained ≤May, validated
June: 30/38 stayed profitable forward, **+21.4% pooled**. A **fee-aware
flat-$50 copy replay** plus a trailing-90d trusted-record gate then keeps the
wallets actually profitable to *copy* → currently **~30 copy-positive holders**
in `watch_sharps.json`, refreshed daily. The niche the top holders live in is
**low-tier tennis (ITF/qualifiers) and tier-3 esports** — informed money with
multi-day leads, i.e. copyable, with **ban/regime risk as the real tail**
(one top wallet's API feed went dark for an hour mid-analysis).

**The backtest** (`live/portfolio.py`): a **rolling N-day replay** of whatever
wallet set `live/backtest.json` holds (default: the live follow set), with
fees/lag/class sizing/their-bet ceiling. Three-whale config showed ~+9,800%
over the trailing 30d — but the whales were *selected* partly on that window,
so treat every headline as an in-sample ceiling. July, live, is the test.

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
- **Scale carefully.** The their-bet ceiling keeps every copy within size the
  market demonstrably absorbed from the signal — but you fill *after* the
  signal moved the price, so large late-compounding clips are still optimistic;
  a depth-aware fill model (order-book snapshots) is the known next step.
- **The edge has a landlord.** The strongest wallets look like informed money
  in fixable niches; assume any month could be the last, re-select weekly (the
  daily pipeline does), and take profits out as you go.
