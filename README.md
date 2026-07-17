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
| **daily pipeline** (`live/daily.sh`) | this Mac, launchd **08:00** (a `pmset repeat wakeorpoweron` RTC wake at **07:58** makes this run on time even from sleep — set 2026-07-08, undo with `sudo pmset repeat cancel`; job wrapped in `caffeinate -i` so mid-run idle-sleep can't kill it. Heads-up: the wake fires even lid-closed-in-a-bag — cancel it if the Mac travels) | refresh the bet cache → 5-gate skill scan → fee-aware sharp selection → conviction floors → backtest book → publish JSON feeds to GitHub |
| **copybot worker** (`copybot.py` via `host/start.sh`) | **Fly.io app `wwf-copybot`, region `arn` (Stockholm), 24/7** — migrated off Railway 2026-07-06: Railway ran it in a US region, which Polymarket's IP geoblock would 403 the moment orders got real; Stockholm is unrestricted AND ~25ms from the CLOB's eu-west-2 primaries. Every boot self-checks the geo-gate (`host/geocheck.py`) and verifies the clone against `git ls-remote` (stale-replica guard) | **T0 = the RTDS trade stream** (gotcha 18): every platform trade, wallet-attributed, **~1s detection** (measured p50 0.8s over 22k msgs), silent-stale guard force-reconnects a quiet socket. Backstops: the Alchemy address-activity webhook (~3s, `POST /alchemy`, signature-verified — **T0b chain seed** 2026-07-14: its tx hashes decode straight from the receipt's OrderFilled logs when the data-api indexer lags, so RTDS-silent markets still copy at push speed), a 5-min backstop poll, and the reconcile janitor. Per-wallet **trade cursor** paginates the activity feed so clip bursts can't scroll past a fetch (H3). 60s heartbeat settles/publishes; paper-copies with real fees/lag/slippage AND live-FAK thin-book reality (**paper FAK parity** 2026-07-16: a quote with no ask inside the protected band books a MISS, not a fill — keeps the live-vs-paper per-signal ratio honest; `live/edge.py` turns it into the daily bankroll-decision verdict in `history/edge.csv` + the Discord digest footer); commits its book back to the repo |
| **REAL-MONEY worker** (same code, `COPYBOT_ROLE=live`) | **Fly.io app `wwf-copybot-live`, `arn`, 24/7, ARMED 2026-07-10** — separate app so the two books never share a process, state file, or failure mode. Trades the signer's **Deposit Wallet** `0x455e…45a1` (pUSD collateral) via the unified SDK (`polymarket-client`) | Same T0 RTDS detection (`RTDS_DETECT=1`) + Alchemy/poll backstops; **own-fill push** via the CLOB user channel (`userws` in the heartbeat) makes in-play pending holds adopt the moment they match — ws events only *trigger* the resolver, `get_order` stays the arbiter (gotcha 17). Paper-parity sizing (4% of equity, $1 venue floor, no price floor, hard caps retired — the **depth gate** is the scaling rail: skip spread>0.08 or ask5c<$50, stake ≤10% of 5c depth). Every placement/exit/settle pings Discord. Disarm: `flyctl secrets unset LIVE_CONFIRM -a wwf-copybot-live` |
| **VALUE bot** (separate experiment, [`value/README.md`](value/README.md)) | **Fly.io app `wwf-valuebot`, `arn`, 24/7, PAPER** — a hard SILO: own code (`value/valuebot.py`, zero copybot imports), own state/feed, own image, no shared wallet ever | Systematically buys sub-2¢ contracts (the calibration study's one underpriced bucket — [`value/PLAN.md`](value/PLAN.md)); honest FAK fill model, chain-truth settles, $1 tickets, event cap 1. Verdict at ~2k resolved tickets vs a ~1.05% break-even hit rate |
| **Discord digest** (`live/discord_daily.py`) | end of the daily pipeline | one message/day: the sharp list with profile links + 30-day conviction stats (per-trade pings retired 2026-07-04 for PAPER; the LIVE book pings every real placement/exit/settle) |
| **dashboard** | [jaxperro.com/trading](https://jaxperro.com/trading) + [jaxperro.com/live](https://jaxperro.com/live) (static, in the `jaxperro` repo) | `/trading` renders the paper book, backtest book and sharp table; `/live` is the REAL MONEY page (reads `live/copybot_live_real.json` — live since 2026-07-10). Both pages share `trading/copybot-section.js` — one renderer, no drift |

**The calibration experiment (running now):** a fresh $1,000 paper book,
**reset 2026-07-08** so it measures exactly one thing — the follow set in
**`live/copybot.paper.json`** (the single source of truth) from a clean start,
with every bookkeeping fix live from day one. The follow set is **Set E rev 4**
(2026-07-16, weekly bench review — HANDOFF logs each revision's evidence):
six volume wallets — **Kruto2027 (floor 80 PINNED — `floor_pin` survives the
nightly p80 sync), 0xbadaf319, gkmgkldfmg, 1kto1m, BikesAreTheBikes
(promoted 07-13), AIcAIc (dropped 07-13, re-added 07-16 on +22%-since-drop
forward + the Esports World Cup catalyst)**. Out: imwalkinghere (his flow is
5-min BTC Up/Down markets that resolve faster than any copy), LSB1 (−$438
all-time paper). The backtest additionally proves the bench: Vahan88,
EdwardIN (both negative forward at the 07-16 review — not promoted). All
followed wallets are copied on their
**conviction bets only** (top-20%-by-stake, floor pinned daily from the
trusted cache p80 via `sync_floors.py`), 4% of equity/bet, **capped at the
signal's own bet size** and by the **depth gate** (≤10% of visible 5c ask
depth). The whale class (12%/bet, follow-all) is retired.
Every fill records detection lag, price slippage, and the taker fee; missed
bets are recorded and settled hypothetically. **The point of the fresh book:
the backtest of the same set says +2103%/30d — an in-sample ceiling. The
measured ratio between this live book and that model over the next weeks is
the number that decides real-money sizing** (see [`LIVE_ROLLOUT.md`](LIVE_ROLLOUT.md)
for the phased path; Phase 1 code prep is complete, Phase 2 is funding).

```
 data layer            selection                          execution              display
 ──────────            ─────────                          ─────────              ───────
 live/cache.duckdb ──▶ trust.py (trusted-row filter)      copybot.py (Fly arn) ─▶ jaxperro.com/trading
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
| **add / remove / reclass a LIVE wallet** | edit the `wallets` list in `live/copybot.paper.json` (`{"wallet","name","class":"volume"\|"whale","floor":123?}` — floor optional, auto p80 at boot; whales ignore floors) then run **`./live/deploy_bot.sh`** — it validates, previews, **syncs the Alchemy webhook's address list** (`live/sync_webhook.py`, Notify API), commits, pushes, restarts the Fly machine, and confirms the boot banner. Fully self-contained — nothing to click in any dashboard |
| **backtest any wallet set** | edit `live/backtest.json` (same entry shape) → `python3 live/portfolio.py`; ad-hoc without touching the dashboard: `python3 portfolio.py --wallets 0xabc,0xdef:whale --days 14 --bank 500 --out /tmp/t.json` (`--bank`/backtest.json `"bank"` sets the starting bankroll; smaller books compound at a *higher* rate because 4%-of-equity stakes hit the their-bet ceilings later) |
| **promote a wallet to live** | prove it in `backtest.json` first, copy the same entry into `copybot.paper.json`, run `deploy_bot.sh` |
| **watch the bots** | paper: `flyctl logs --app wwf-copybot` · real money: `flyctl logs --app wwf-copybot-live` (one summary line per 60s heartbeat); books are committed as `live/copybot_live.json` / `live/copybot_live_real.json` and rendered on the dashboard |
| **restart / redeploy the bot** | `flyctl apps restart wwf-copybot` (or `wwf-copybot-live`; the worker clones the repo fresh at boot, so a restart IS the deploy — but a boot can clone a STALE GitHub replica: always verify the first heartbeat shows the numbers you shipped, gotcha 15). **Changes to `host/` or the toml need an image rebuild: `flyctl deploy --remote-only` (live app: add `-c fly.live.toml -a wwf-copybot-live`).** Secrets: `flyctl secrets set K=V` (applies with an automatic restart). Keep it ONE machine per app (`flyctl scale count 1`) |
| **disarm / re-arm real money** | disarm: `flyctl secrets unset LIVE_CONFIRM -a wwf-copybot-live` (survives restarts; `machine stop` does NOT — the http_service auto-restarts it). Re-arm: set `LIVE_CONFIRM` to the confirmation phrase (see `CONFIRM_PHRASE` in copytrade.py). A disarmed live app idles without HTTP, so its watchdog page is expected |
| **reset the paper book** | **stop the machine FIRST** (`flyctl machine stop <id> --app wwf-copybot`), then write a fresh `new_state()` into `copybot_state.json`, archive `copybot_fills.jsonl`, commit+push, then `flyctl apps restart`. Order matters: the running bot's memory is the book's single writer and its publish flow will re-commit its own book over yours (by design — see gotcha 15). Verify the first heartbeat reads `free $1,000/$1,000 · realized $+0` |
| **watchdog** | two layers, both automatic: fly.toml's http `/health` check (Fly restarts a dark machine) + `.github/workflows/watchdog.yml` (GitHub-hosted probe every ~30-150 min → Discord alert via the `DISCORD_WEBHOOK_URL` repo secret when 3 probes fail). Test: `gh workflow run watchdog` |
| **run the daily pipeline manually** | `cd live && bash daily.sh` (launchd runs it 08:00; ~40 min, mostly collect). Never run two at once — the cache is single-writer |
| **refresh just the sharp list** | `cd live && python3 conviction_scan.py && python3 validate_timing.py` |
| **daily Discord digest** | sent by `live/discord_daily.py` at the end of `daily.sh`; webhook = `daily_webhook` in gitignored `config.json` |
| **go real-money** | **DONE 2026-07-10** — [`LIVE_ROLLOUT.md`](LIVE_ROLLOUT.md) executed through Phase 4 (first fills verified end-to-end); the armed bot runs paper-parity rules (caps retired by user decision, superseding rule 0.6). [`HANDOFF.md`](HANDOFF.md) is the current-state entry point |

Three moving parts talk to each other: this repo (research + bot + feeds), the
`jaxperro` repo (static dashboard reading this repo's raw JSON feeds), and the
Fly.io app `wwf-copybot` (Stockholm; the old Railway project
`magnificent-kindness` is stopped and can be deleted). The bot **commits its
own state/feed back to this repo** every few minutes — always
`git pull --rebase --autostash` before you push (every script here already
does).

---

## File map

| path | role |
|------|------|
| `live/` | **the current system**: cache, scanners, sharp selection, backtest, daily pipeline ([live/README](live/README.md)) |
| `live/trust.py` | the trusted-row filter every selector must read through (see gotchas 8–9) |
| `live/backtest.json` · `live/copybot.paper.json` | the two wallet-set configs: backtest experiments vs. the live paper bot (same entry shape) |
| `live/deploy_bot.sh` | one-command live-bot deploy: validate → preview → Alchemy address sync → commit → push → Fly restart → confirm boot |
| `live/sync_webhook.py` | diffs the follow set against the push-mode Alchemy webhook and patches add/remove (token in gitignored `config.json`) |
| `fly.toml` · `fly.live.toml` · `fly.Dockerfile` | Fly.io app configs (paper `wwf-copybot` / real-money `wwf-copybot-live`, both `arn`, single machine each, `:8080` webhook ingress + /health) + the shared worker image (python + git + `polymarket-client`; the bot code itself is cloned fresh at boot) |
| `host/wrap_via_bridge.py` | one-time bankroll conversion native USDC → pUSD via the Polymarket Bridge (gotcha 16); idempotent, stage-gated |
| `host/order_probe_v2.py` · `host/flatten_positions.py` | live-venue probes on the unified SDK: $5 FAK round-trip validation · market-sell-everything (the emergency exit) |
| `host/geocheck.py` | 3-probe geo-gate verdict (ipinfo → Polymarket's `/api/geoblock` → unauth CLOB order POST); runs at every boot, `GEOCHECK_ONLY=1` = probe-and-idle for testing a new host/region without a second book-writer |
| `live/discord_daily.py` | the daily Discord digest (the only Discord output) |
| `copybot.py` | the copy-trading bot: push/poll trigger → follow filter → execution engine (paper + live) |
| `copytrade.py` | the execution engine the bot reuses: sizing, risk gates, price guard, paper/live executors (moved out of archive/ 2026-07-08 — nothing load-bearing lives in archive) |
| `host/start.sh` | 24/7 worker bootstrap for Fly/any VPS (geo-gate check, clones repo, resumes committed state) |
| `LIVE_TEST.md` · `preflight_live.py` · `redeem.py` | real-money runbook, read-only credential preflight, on-chain redemption |
| `insider.py` | the original detector: z-score, pre-resolution timing, fresh-wallet flags, funding-cluster rings |
| `smart_money.py` | shared HTTP helper + survivorship-corrected win-rate dashboard (`:8899`) |
| `archive/webhook_receiver.py` | retired 2026-07-04: Alchemy webhook → per-trade Discord pings (replaced by `live/discord_daily.py`'s daily digest) |
| `wide/` | frozen-subgraph bulk scanner (1.76M wallets, historical only — subgraph froze Jan 2026) |
| `archive/` | everything retired, kept honest ([archive/README](archive/README.md)): the six failed strategies, earlier research sweeps (`hunt/huntwide/oos/copyback`), the superseded live selection layer (`live-research/`), the scrapped Polymarket-US venue probe (`us-venue/`), and retired infra (`retired-infra/`: Railway config, Mac launchd runner, GH-Actions cron) |

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
| `config.json` | `daily_webhook` (Discord digest) · `alchemy_notify_token` + `alchemy_webhook_id` (webhook address sync) · `alchemy_signing_key` (local push-mode runs) · Alchemy RPC key · a legacy curated watchlist + floors (`sync_floors.py`) |
| `config.live.json` | live-trading credentials (`private_key`, `funder_address`); the Fly worker instead reads committed `config.live.example.json` + env secrets. Risk block is **paper-parity** (caps retired 2026-07-10 by user decision; bands/floors mirror `live/copybot.paper.json` — kept in sync MANUALLY, nothing auto-writes it) |
| Fly `wwf-copybot` secrets | `GITHUB_TOKEN` (fine-grained PAT, contents-RW on this repo — the bot commits its state/feed back) · `ALCHEMY_SIGNING_KEY` (presence = push mode; remove it to fall back to 60s polling) · `DISCORD_WEBHOOK`. The webhook ingress is `https://wwf-copybot.fly.dev/alchemy` (Alchemy webhook `wh_blf4qjjvfdbqs9mc`, address list auto-synced by `deploy_bot.sh`) |

---

## How the copy bot models reality

The whole point of the July test is that paper ≈ live. Every mechanism the
backtest and bot share:

- **Taker fees** (Polymarket V2, since 2026-03-30): `fee = shares × rate × p(1−p)`,
  sports rate 0.03 — charged on every marketable entry and exit; redeeming at
  resolution is free. Fee-adjusted Copy P&L also drives *selection*.
- **Lag + slippage**: the bot fills at the live CLOB ask at detection — since
  2026-07-06 that's **push mode** (Alchemy address-activity webhook →
  `POST /alchemy`, ~2–5s) rather than the 60s poll; per-fill `detect_lag_s`
  and `slippage_pct` are logged, and the backtest applies a +0.5%/~90s
  haircut. Poll-era measurements: ~39s avg lag, **−4.0% avg slip** (the
  asymmetric price guard means better-than-their-price fills are common).
- **Dynamic sizing, their-bet ceiling**: each bet stakes a fraction of current
  working equity — **4% for `volume`** wallets (conviction bets only), the
  class the whole Set-D follow list now uses; `follow.class_pct` still carries
  a `whale` fraction (12%, every trade) for the retired follow-all mode — **and
  is never larger than the signal's own position size**: when the percentage
  works out to more than the
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
  (better odds, by rule); only adverse drift of **>5 points absolute**
  (`price_guard_abs`, 2026-07-10) is skipped — 0.14→0.15 follows, 0.14→0.20
  skips. (The old relative 5% blocked one-tick moves on cheap in-play books.)
  **Validated 2026-07-13 by missed-ledger counterfactuals**: 0.05 sits at the
  EV knee — refused moves of 0.05–0.10 were breakeven, >0.10 ran −20% ROI.
- **Depth gate** (`config.depth_gate`, 2026-07-13, fitted on 131
  book-annotated fills): before ordering, skip if spread > 0.08 (market
  mid-move — median |slippage| ~14% out there) or ask-side 5c depth < $50
  (dust books mispriced every observed fill), else cap the stake at **10% of
  visible 5c ask depth** (keeps impact <~2%). Fails open on a book-fetch
  error. This is the rail that lets stakes scale.
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
**and** a typed confirmation phrase, plus a fatal geo-gate. Hard caps were
retired 2026-07-10 (user decision) in favor of paper-parity 4%-of-equity
sizing floored at the venue's $1 minimum; the structural bounds that remain
are the their-bet ceiling, the conviction floors, the entry band, and the
all-or-nothing free-cash gate. The GH-Actions cron
runner is retired (GitHub throttled `*/5` to ~2h in practice — it copied 1 of
~104 qualifying trades in June; the always-on worker replaced it).

---

## Data sources

| Source | Used for |
|--------|----------|
| `data-api.polymarket.com` | positions, trades, activity (+`eventSlug`), leaderboard |
| `gamma-api.polymarket.com` | market metadata (NB: `condition_ids` filter returns nothing for resolved markets) |
| `clob.polymarket.com` | order books, prices, **authoritative resolution** (`winner` flags), market slugs |
| `lb-api.polymarket.com` | the wallet's OWN all-time P&L/volume (`/profit`, `/volume`) — the sharps table's **PM P&L** sanity anchor (see gotcha 10) |
| Alchemy (Polygon) | the push-mode address-activity webhook (instant trade detection) + funding-cluster traces; Notify API (`dashboard.alchemy.com/api`) drives the automatic address-list sync |
| `bridge.polymarket.com` | deposits/withdrawals for the 2026 pUSD stack — the ONLY sanctioned native-USDC→pUSD conversion (gotcha 16); `/deposit` registers a per-wallet address, `/status/{addr}` tracks delivery |
| `relayer-v2.polymarket.com` | gasless transactions from the deposit wallet (via `SecureClient.execute_transaction`; needs an in-process Builder API key) |
| `wss://ws-live-data.polymarket.com` (RTDS) | **T0 detection**: `activity/trades` streams EVERY platform trade, wallet-attributed, ~1s (gotcha 18). Undocumented-but-official (powers polymarket.com's live feed); server-side filters broken → subscribe unfiltered, filter client-side |
| `wss://ws-subscriptions-clob.polymarket.com/ws/user` | **own-fill push** (live bot): order/trade lifecycle for OUR account (auth = the SDK's derived L2 creds via `client.credentials`); triggers immediate pending-adoption |

**Candidate next sources** (researched 2026-07, not yet wired in):

| Source | Would unlock |
|--------|--------------|
| [Goldsky Turbo Pipelines](https://docs.goldsky.com/chains/polymarket) | per-fill order events with timestamps for *every* wallet (Polymarket killed subgraphs with the 2026-04-28 v2 migration) — fixes the cache's two blind spots: no entry times, and position-level aggregation hiding scalps. See also [warproxxx/poly_data](https://github.com/warproxxx/poly_data), [Bitquery](https://docs.bitquery.io/docs/examples/polymarket-api/) |
| [PolymarketData.co](https://www.polymarketdata.co/) | HISTORICAL order-book snapshots (Aug 2025+) → depth-aware backtest fills. Forward-going depth is now captured in-house: every copy logs `book` {bb, ba, spread, bid5c, ask5c} to the fills ledger (2026-07-08) |
| ~~free Etherscan-V2 key~~ **WIRED 2026-07-08**: `payouts.resolution_time(cond)` returns the exact on-chain `ConditionResolution` timestamp (cached forever in `resolution_times`; key = `etherscan_key` in gitignored config.json, 5 req/s free tier). Verified: the Jul-7 Brewers market truly resolved 2026-07-08 04:51 — cached metadata res_t was 29h wrong. NEXT: migrate trust/validate/portfolio to consume it (alignment-grade change, do carefully), and port the funding-cluster tracer off Alchemy getLogs (free tier caps at 10 blocks) |
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

10. **Polymarket geoblocks ORDER PLACEMENT by IP** (reads are open everywhere —
    which is why the paper bot never noticed Railway was in a US region).
    Restricted list has surprises: US, UK, France, Germany, Italy, Netherlands,
    Poland, Singapore, Australia, Ontario, **Brazil**
    (docs.polymarket.com/developers/CLOB/geoblock). `host/geocheck.py` gives a
    3-probe verdict from any box and runs at every worker boot; **never go live
    from a box that doesn't print `VERDICT: TRADABLE`** — and the machine's
    location doesn't relocate *you* (trade live from Colombia months, paper
    from US months).

11. **All-Time / Conv P&L is the wallet's ANTI-SURVIVORSHIP realized track
    record.** Every DECIDED position at its actual result: redeemed/sold closed
    positions (Polymarket's own `realizedPnl`, `cache.closed_exits`) PLUS
    resolved-but-unredeemed positions still sitting in `/positions` — bets that
    already won/lost and the wallet never redeemed (mostly **abandoned losers**
    at $0). `_open_split` in validate_timing pulls current positions and routes
    those decided ones into the realized total (`cashPnl` = their outcome),
    leaving only genuinely in-flight positions in the **Open P&L** column. This
    is the whole point of the project surfacing: PM `/profit` **under-counts
    abandoned losers**, unevenly — Coteykens' PM subtracts them (realized $66k −
    $52k walked-away = $14k = PM), oliman2's does not ($181k − $161k = $20k
    true, yet PM says $112k). So where our All-Time reads **lower** than PM, PM
    is the survivorship-biased number and **ours is the truth** (oliman2 and
    JuiceFarm look elite on redeemed-only P&L, ~$20k/$32k once the losers they
    walked from are counted). This *replaced* the old hold-to-resolution
    reconstruction (`won × entry × size`), which diverged by up to 10× and
    flipped signs — four bugs killed with it: the 2,000-row pull cap, both-sides
    double-drop, `initialValue = 0` mis-sizing, and corrupt near-epoch `res_t`.
    **Win % is held-outcomes only** (see gotcha 13); sold P&L still counts in
    the P&L columns. And when our number disagrees with PM's **leaderboard**
    (`lb-api /profit`), check PM against itself first: for mega-abandoners the
    leaderboard doesn't foot against PM's own per-position books (oliman2:
    per-position sums say ~$19k true; leaderboard says $112k). The per-position
    data is the truth source, never the leaderboard.

12. **The cache keeps THREE per-wallet cursors** — `pulled` (bets),
    `pulled_exits` (incremental), `pulled_entries` (14-day-TTL full snapshot) —
    and **anything not re-pulled goes silently stale, not visibly wrong.**
    `cache.invalidate()` must clear ALL of them (it does, since 2026-07-08):
    when it only cleared `pulled`, watchlist wallets' entry maps went up to 14
    days stale and `portfolio.py` silently dropped every bet on a market first
    entered since the last pull (`if not et: continue`) — Kruto's Brewers sells
    were visible in the live bot and absent from the backtest for exactly this.

13. **`res_t` is endDate METADATA, not resolution time** — for in-play markets
    it's game-day midnight (it can pre-date the wallet's own entry) or even a
    date in the future (a Jul-5 tennis match carried res_t Jul-14). Never use
    it to detect a pre-resolution sell. The reliable discriminator is PRICE: a
    redeem prints **exactly** the payout (1 / 0 / 0.5 to float precision — the
    exit_p reconstruction is exact for redeems), a real sell prints a mid
    price. `portfolio._sold_pre_resolution` and validate_timing's `rtally`
    both use it; that's what makes W/L (held to resolution) vs S (sold) vs R
    (refund) mean the same thing in the sharps table, the backtest, and the
    bot. In `/positions`, the **`redeemable` flag is the resolution truth**
    (set on-chain-resolution for winners AND losers) — `_open_split` folds on
    it, not on price-pinning.

14. **A high z-score can be a refund-harvesting machine, not a picker.** The
    exact-0.5 exit signature at scale = buying ITF totals just under 50¢ and
    banking the chronic ITF cancellation rate (0xb0E43B: $148k of a $218k
    lifetime P&L is 797 refund redeems; ArbTraderRookie is the same species).
    The edge is real but pays 1–2¢/share — taker fees + slippage consume it, so
    it never survives the copy replay. General rule: **z finds skill; only the
    honest replay decides whether the skill is harvestable by a follower.**

15. **The running bot is the book's single writer — remote state surgery must
    stop the machine first.** The publish flow recovers from conflicted
    rebases by resyncing to origin and re-committing ITS OWN in-memory book on
    top (so a wedge can't silently kill publishing — the 2026-07-08 BOOK RESET
    race left the repo in UU and every publish dead until reboot). Two
    corollaries: a state push while the bot runs WILL be overwritten, and a
    boot clone seconds after a push can read a stale GitHub replica — after
    any surgery, verify the first heartbeat shows the book you wrote.
    THIRD corollary (2026-07-17 bankroll rebase): `flyctl machine stop` can
    report "stopped" while the process keeps running for many more minutes
    (heartbeat cycle numbers kept incrementing 25 min past the "stop") — the
    machine then boots on whatever it cloned at ITS OWN restart moment, not
    yours. Before editing state, WATCH the heartbeats actually cease; if a
    surgery boot shows stale numbers, just restart again once your push is
    on origin (second boot picked it up cleanly).
16. **The 2026 exchange stack: pUSD collateral + the unified SDK.**
    py-clob-client is ARCHIVED (May 2026) — reads/auth still work but the
    CLOB rejects its orders globally ('invalid order version'). Placement
    lives in `polymarket-client` (`SecureClient`; pinned 0.1.0b16 in
    fly.Dockerfile). The exchange's balance view counts ONLY the pUSD
    CollateralToken (`0xC011a7…`) — raw USDC at the wallet reads as
    `balance: 0`. pUSD accepts native USDC *and* USDC.e by contract, but the
    public CollateralOnramp (`0x93070a…`) keeps native USDC **paused**
    (`paused(0x3c499c…)=1`, verified on-chain 2026-07-10), so a direct
    `wrap()` of native USDC reverts. The sanctioned conversion is
    **bridge.polymarket.com** (the UI deposit path): register the wallet via
    `POST /deposit`, gasless-transfer the USDC to the returned per-wallet
    address ($2 min from Polygon), and it lands back *already wrapped* as
    pUSD, fee-free (`host/wrap_via_bridge.py` — $24.73 in → $24.73 pUSD out).
    Corollary: Builder API keys revoke only via their OWN secret (HMAC DELETE)
    — revoke in-process before exit (`atexit` in the probe) or they pile up
    unrevocable-but-inert on the account (9 accumulated during bring-up;
    the UI at polymarket.com/settings?tab=builder can clean those).
17. **In-play order acceptance reports ZERO matched — it is NOT a rejection.**
    In-play (esports/live-game) markets return `AcceptedOrder` with status
    `delayed` and `making/taking = 0`; the match happens seconds-to-minutes
    later at the exchange. Treating zero-matched as a miss caused the
    2026-07-10 incident: six $5 copies were logged as misses and filled
    untracked — the book went blind, caps stopped binding, exits were never
    mirrored (net −$2.7; the CASH≠CHAIN alarm caught it). The executor's
    invariant since: **no order outlives the placing call untracked** —
    poll `get_order`, cancel the remainder, then measure the fill as the
    exchange's CONDITIONAL-balance diff (chain truth beats response parsing).
    Same sweep runs on exception paths (a timed-out POST may still fill).
    **Corollary (the 2026-07-12 +$7.86 phantom-cash sequel): the balance
    diff is a FALLBACK, never the primary.** With several pendings on one
    token, each diff window saw the same balance move and one real
    1.48-share sell booked 3×. Since then: the order's own `size_matched`
    is authoritative, every adoption caps at the order's own size and at
    book holdings, the diff is consulted only when the exchange no longer
    answers for the order, and only ONE pending may rest per token.
18. **The RTDS trade stream is the real detection layer** — and it lies in
    two small ways. `wss://ws-live-data.polymarket.com`, topic
    `activity`/type `trades`: every platform trade with `proxyWallet`
    attribution at ~1s (probe: p50 0.8s, p99 6.4s over 22k msgs). It is
    undocumented but official (spec'd in Polymarket/real-time-data-client;
    powers the site's live feed). Quirk 1: server-side `filters` silently
    return nothing (RTDC issue #34) — subscribe unfiltered and filter
    client-side (~45 msg/s peak, trivial). Quirk 2: the socket can go
    SILENT while staying connected (35 min once) — no close event, so an
    on-close reconnect never fires; the listener force-closes after 120s
    without messages. Both bots keep Alchemy push + 300s poll as backstops:
    losing RTDS degrades detection, never blinds it. The live bot also
    streams its OWN fills from the CLOB user channel (auth =
    `client.credentials`) purely as a resolver TRIGGER — heartbeat fields
    `rtds up Ns · userws up` are the liveness truth, since boot log lines
    scroll out of Fly's buffer.

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
