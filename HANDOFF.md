# Session handoff — 2026-07-08 (rev 3: books aligned, Set E live, fresh $1k)

Self-contained pickup for a fresh session (human or AI). Read
[README.md](README.md) gotchas 1–15, [FINDINGS.md](FINDINGS.md) (especially
the three 2026-07-08 sections at the end), and [LIVE_ROLLOUT.md](LIVE_ROLLOUT.md).
The repo is authoritative; project memory has deeper history.

## Where things stand (all green)

- **Live paper bot**: Fly.io `wwf-copybot`, region `arn` (Stockholm), push
  mode, ONE machine. Following **Set E** (7 volume wallets): LSB1,
  imwalkinghere, Kruto2027, 0xbadaf319, gkmgkldfmg, AIcAIc, 1kto1m.
  `flyctl logs --app wwf-copybot`.
- **The book is a fresh $1,000, reset 2026-07-08 ~20:45 UTC** — this is the
  CALIBRATION EXPERIMENT: measure the live-to-backtest ratio for Set E with
  every fix live from day one; that ratio (not the replay %) sizes real money.
  Old book (Jun 25 → Jul 8, +$229.79) is in git history +
  `archive/copybot_fills.pre-reset-2026-07-08.jsonl`.
- **The three books agree by construction** (2026-07-08 alignment work):
  same W/L/R/S taxonomy (win% = held-to-resolution only), same
  sold-vs-redeem price discriminator (redeem prints exactly the payout), same
  pinned floors, same follow set. Row-level cross-check bot↔backtest: 7/9
  exact, 0 absent, 2 legit execution diffs. Sharps table reproduces PM's
  per-position books to the dollar; PM's LEADERBOARD is the biased number.
- **Dashboards**: [jaxperro.com/trading](https://jaxperro.com/trading) =
  paper book + backtest + sharps; [jaxperro.com/live](https://jaxperro.com/live)
  = REAL MONEY page (NOT STARTED until Phase 2). Shared renderer:
  `jaxperro/trading/copybot-section.js`. `?botFeed=`/`?rmFeed=` query params
  override feeds for local testing.
- **Watchdog live (2026-07-08)**: Fly `/health` check auto-restarts a dark
  machine; GH Actions `watchdog.yml` probes from GitHub's infra and pings
  Discord after 3 failed probes. `copytrade.py` now lives at repo root
  (archive/ is 100% retired code again).
- **Daily pipeline** (Mac launchd 08:00, fires on wake — typically ~10:00,
  caffeinate-wrapped)
  appends one live-vs-model row/day to `live/history/calibration.csv` and
  re-derives everything under the new rules: entries cursors now invalidate
  daily, `_open_split` folds by the `redeemable` flag, `rtally` splits SOLD.
- **Backtest**: `portfolio.py` takes `--bank` (backtest.json `"bank"`).
  Published feed stays at $1,000 to match the paper book. $500 run: +2728%
  (small banks compound faster — their-bet ceilings bind later).

## Queued next-session work (in value order)

1. **Exact-res_t migration**: plan agreed 2026-07-08 — execute
   [ETHERSCAN_MIGRATION.md](ETHERSCAN_MIGRATION.md) phase by phase
   (0: chain sweep backfill → 1: shadow audit → 2: validate_timing →
   3: portfolio → 4: selection+floors → 5: trust simplification LAST).
   One phase per session, before/after diffs, never coupled with
   follow-set changes.
2. **Empirical fill model**: every copy now logs a `book` snapshot (spread +
   5c depth) to the fills ledger; once a few weeks accumulate, fit slippage
   vs stake/depth and replace portfolio.py's flat SLIP haircut; add a depth
   gate before real-money sizing.
3. **Funding-cluster tracer port** to Etherscan logs (Alchemy free tier now
   caps getLogs at 10 blocks — insider.py's ring detection is degraded
   until then).

## Watch list (the standing question: does Set E hold up?)

- **0xbadaf319** — 2026-07-08 evening revealed his structure: PAIRED YES+NO
  clips ($6–37 each, sums ≈ $1.00) = two-sided arb/merge flow, not
  directional conviction. Copying one leg of a hedge is a bet HE never took;
  his replay P&L rode the larger leg of pairs. Moves to the TOP of the
  demotion watch list — judge on forward copies, not his account P&L.
- **AIcAIc** — held-win only 42%; his edge is sell-timing (most lag-fragile
  class). Second demotion candidate.
- **1kto1m** (z=2.4) and **gkmgkldfmg** (z=2.05) — near the gate floor;
  strong month, shallow statistical edge.
- Re-ranking is cheap: the per-wallet replay harness pattern is in FINDINGS
  ("Aligning the three books"); `python3 live/portfolio.py --wallets 0x…
  --days 30 --out /tmp/w.json` per candidate, then combined-set runs.
- Rejected-with-evidence (don't re-add without new facts): oliman2 (true
  lifetime ~$19k), leegunner (negative to copy), 0xb0E43B/ArbTraderRookie
  (refund harvesters — real edge, uncopyable margins), lma0o0o0o (negative in
  the shared book), Winnertraders (6.6-day capital locks).

## Real-money worker status (built 2026-07-09)

`wwf-copybot-live` exists on Fly (arn, ONE machine, geo TRADABLE), idling
UNARMED: no keys, no book, no orders until three user-set secrets exist
(`LIVE_PRIVATE_KEY`, `LIVE_FUNDER_ADDRESS`, `LIVE_CONFIRM` = the typed
confirmation phrase; plus `GITHUB_TOKEN` — mint a FRESH fine-grained PAT so
live and paper credentials stay separable, and `ALCHEMY_RPC_URL` for
settles/redeems). Armed boot: config.live.example.json (Set E, $50 @ 10% =
$5 stakes, rule-0.6 caps) + own state `copybot_state.live.json` + poll mode
— the push webhook stays on the paper app. Disarm any time:
`flyctl secrets unset LIVE_CONFIRM` (next boot idles) or
`flyctl apps stop wwf-copybot-live`. ROTATE the Discord webhook that was
committed in config.live.example.json history (removed from HEAD
2026-07-09; spam-risk only).

## Next: Phase 2 — funding (USER ONLY)

Phase 1 is 8/8 complete (LIVE_ROLLOUT.md). Before funding, the user should
decide the stranding tolerance explicitly: real money turns the primary risk
from P&L into the ACCOUNT (US person routing orders via Stockholm through the
geoblock — a frozen wallet is a different loss category than a losing bet).
Caps stay $5/trade · $25/day · $30 exposure until Phase 6. Then Phase 3
preflight (`preflight_live.py`) → Phase 4 supervised first fill → Phase 5
edge-case matrix. The typed-phrase interlock is the human checkpoint — never
automated.

## Standing to-dos (user-only or external)

- **None open.** Alchemy key ROTATED 2026-07-08: old app "Jax's First App"
  (key `…OdWgOi`, the one leaked to public git history) deleted; new app
  `wwf-rpc-2026-07` (key `…w0BxV5`) wired into gitignored `config.json` +
  the `ALCHEMY_RPC_URL` Fly secret. Verified: old key → HTTP 401 (the key
  in git history is now dead, so no history rewrite needed), new key serves
  Polygon, bot rebooted with settle-fallback ON and made a live copy, Notify
  webhook `wh_blf4qjjvfdbqs9mc` survived (app-delete doesn't touch Notify
  resources) — 7 addresses in sync. Railway project deleted (purges 07-10).

## Operational quick-reference

- **Change the follow set**: edit `live/copybot.paper.json` wallets →
  `./live/deploy_bot.sh` (validates → pins floors → syncs Alchemy webhook →
  restarts Fly → confirms banner). Keep `backtest.json` mirrored.
- **State surgery (reset/repair the book)**: `flyctl machine stop` FIRST,
  push the new state, `flyctl apps restart`, then VERIFY the first heartbeat
  shows the book you wrote (gotcha 15: the bot's memory wins conflicts by
  design, and a boot clone can read a stale GitHub replica for a few seconds).
- Never 2 Fly machines (`flyctl scale count 1`) — one book, one writer.
- Cache is single-writer; don't run cache-touching scripts during the daily
  pipeline or a regen. Three cursors per wallet (gotcha 12).
- Bot commits its own state/feed — `git pull --rebase --autostash` before
  pushing. Image changes (fly.Dockerfile, host/) need
  `flyctl deploy --remote-only`; code/config changes just push + restart.
