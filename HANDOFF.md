# Session handoff — 2026-07-10 (rev 4: real money one step from live)

Self-contained pickup for a fresh session (human or AI). Read
[README.md](README.md) gotchas 1–15, [FINDINGS.md](FINDINGS.md) (the
2026-07-08 sections at the end), [ETHERSCAN_MIGRATION.md](ETHERSCAN_MIGRATION.md),
and [LIVE_ROLLOUT.md](LIVE_ROLLOUT.md). The repo is authoritative.

## THE CRITICAL PATH — finish live placement (one wrap + one port)

The real-money bot (`wwf-copybot-live`, armed, $24.73) cannot place yet.
Root-cause chain, every link PROVEN 2026-07-09/10:

1. First two qualifying signals CRASHED the bot inside the unguarded live
   order path (Fly events: exit_code=1 at 21:21:55Z and 21:53:35Z, seconds
   after each signal); boot baseline then marked the fresh trades seen.
   ALL FIXED: executor never raises, per-wallet poll guard, baseline
   exempts trades younger than the stale window.
2. **py-clob-client is ARCHIVED** (May 2026) — the CLOB rejects its orders
   ('invalid order version') globally. Reads/auth still work; placement is
   dead for everyone on it. No newer PyPI release exists.
3. Successor: **Polymarket/py-sdk** (`pip install --pre polymarket-client`,
   `SecureClient`). Proven working for us tonight: auth ✓, the signer's
   **Deposit Wallet deployed: `0x455e252e45Ee46d6C4cc1c8fAdD3899d68f245a1`** ✓,
   gasless relayer transactions execute ✓ (real txs: 0x935be727…,
   0x1044d3c7…), `setup_trading_approvals()` ✓ (max-uint on all spenders),
   order FORMAT accepted by the exchange ✓.
4. **Sole remaining gap: the wrap.** The user's $24.73 sits SAFE as native
   USDC at the deposit wallet (his key controls it). The 2026 exchange
   trades a wrapped-collateral token (`0xC011a7…`; wraps native USDC; UI
   flows wrap on deposit — our raw transfer bypassed it), and the CLOB's
   balance view counts ONLY that token → orders reject with 'balance: 0'.
   Attempted: periphery `0x93070a…`.wrap(asset,to,amount) via gasless batch
   → 'batch would revert' (likely caller-gated). Neg-risk adapter ruled out.

**Next session, in order (do NOT guess with live funds):**
1. Read **Polymarket/ts-sdk** (same unified family) for the deposit-wallet
   wrap/deposit implementation — it contains the exact supported call.
   Also check docs.polymarket.com's new wallet/collateral pages.
2. Replicate that one call via `client.execute_transaction` (the gasless
   path is proven). Success = `get_balance_allowance` shows ~$24.73.
3. Port `LedgerLiveExecutor` to the new SDK: `SecureClient.create(
   private_key=…)` (wallet auto-resolves; NO api_key needed at runtime once
   approvals exist), BUY = `place_market_order(token_id, side="BUY",
   amount=<USD>, order_type="FAK")`, SELL = `shares=<n>`; parse the result
   for filled shares/price; keep py-clob-client temporarily for reads.
   Repoint the 1.4 cash anchor at the deposit wallet's collateral view —
   the live bot currently alarms `CASH≠CHAIN +24.73` because the anchor
   still reads the emptied legacy proxy (expected, harmless).
4. `pip` add `polymarket-client` (--pre) to fly.Dockerfile, deploy, re-run
   `host/order_probe_v2.py` (contains the ENTIRE working onboarding
   sequence) until a $5 round trip fills, then run LIVE_ROLLOUT Phase 4 on
   the first organic fill.
5. Cleanup: probe runs minted several Builder API keys —
   `fetch_builder_api_keys` / revoke extras.

Meanwhile the armed live bot is harmless-by-construction: every attempt
returns ok:False and records an honest missed row.

## System state (all healthy, 2026-07-10 00:47Z)

- **Paper bot** (`wwf-copybot`, push mode): Set E, fresh $1,000 book reset
  2026-07-08 20:45Z. **Day 1½: realized +$1,107.57, 36 copies, 6 open,
  drift CLEAN** (the −11.32 forensics closed at 0.00 — see below).
  Biggest single win: mirrored Kruto's 3¢→48¢ Hive scalp for +$722.
- **Live bot** (`wwf-copybot-live`, poll 60s): armed via LIVE_CONFIRM,
  $24.73 book, 0 trades (see critical path), Discord pings wired
  (DISCORD_WEBHOOK secret), auto_redeem off. Disarm:
  `flyctl secrets unset LIVE_CONFIRM` or `flyctl apps stop`.
- **Set E** (both books + backtest): LSB1, imwalkinghere, Kruto2027,
  0xbadaf319, gkmgkldfmg, AIcAIc, 1kto1m — pinned floors, identical rules
  (min_price 0.01 everywhere since 2026-07-09 18:05Z, user-approved).
- **Dashboards**: jaxperro.com/trading (paper+backtest+sharps),
  jaxperro.com/live (real money). Shared renderer
  jaxperro/trading/copybot-section.js; `?botFeed=`/`?rmFeed=` overrides.
- **Watchdog**: Fly /health check (paper app) + GH Actions watchdog.yml →
  Discord. NOTE: the live app has NO watchdog (poll mode, no HTTP) —
  liveness = flyctl logs + feed freshness; wire one before raising caps.
- **Daily pipeline**: Mac launchd 08:00 + pmset RTC wake 07:58 +
  caffeinate; appends live-vs-model calibration.csv row daily.

## Completed 2026-07-09 (the honesty & detection sprint)

- **Fill-split merging**: same-token BUY clips ≤120s apart merge into the
  bet the sharp actually made before the floor gate (gkmg's $612 MOUZ entry
  read as 3×$204 sub-floor clips; he sold +60% in 5 min). Both books.
- **Missed-ledger truth**: missed bets settle at the sharp's EXIT when they
  sold pre-resolution (Kruto Hive miss = honest +$75 counterfactual on $5);
  settled won/lost rows get one exit-print re-check (fast-resolve race);
  every row relabeled with its diagnosed cause; the two silent follow-path
  exits (no-ask book, order rejection) now record misses.
- **Clobber fix + drift forensics CLOSED**: re-entry on a settled token
  archives (token#ts keys) instead of overwriting; three vanished records
  reconstructed from fills-ledger/state-history; paper drift 0.00.
- **Order-book snapshots** (`book` field) logged per copy → empirical fill
  model later. **payouts.resolution_time(cond)** wired (Etherscan V2,
  exact on-chain res_t; metadata was 29h wrong on the test market).
- **min_price parity** 0.05→0.01 on the live book (user-approved; the band
  — not detection — blocked the 16x Hive copy).

## Queued work (after the critical path)

1. ETHERSCAN_MIGRATION.md phases 0→5 (chain-sweep res_t backfill → shadow
   audit → consumer flips → trust.py simplification LAST).
2. Empirical fill model from the accumulating book snapshots; depth gate
   before real-money sizing.
3. Funding-cluster tracer port to Etherscan logs (Alchemy free tier caps
   getLogs at 10 blocks).
4. Live-app watchdog (no /health in poll mode).
5. Decide the leftover **$32 on polymarket.us** (user's first deposit,
   wrong venue — withdraw or abandon).

## Watch list (does Set E hold up?)

- **0xbadaf319** — two-sided arb/merge clips, not directional conviction;
  TOP demotion candidate; judge on forward copies.
- **AIcAIc** — 42% held-win; sell-timing edge (lag-fragile). Second.
- **1kto1m** (z=2.4), **gkmgkldfmg** (z=2.05) — near the gate floor.
- Rejected-with-evidence (don't re-add): oliman2 (true lifetime ~$19k, not
  PM's $112k), leegunner (negative to copy), 0xb0E43B/ArbTraderRookie
  (refund harvesters), lma0o0o0o (negative in shared book), Winnertraders
  (6.6-day locks).

## User to-dos (security)

- Revoke the polymarket.us API key `f03d9eb5…` (secret appeared in a chat
  screenshot) — if not already done.
- Rotate the Discord webhook that was committed to this repo's history
  pre-2026-07-09 (spam-risk only).

## Operational quick-reference

- Follow-set change: edit live/copybot.paper.json → `./live/deploy_bot.sh`;
  keep backtest.json mirrored.
- State surgery: `flyctl machine stop` FIRST → edit → push →
  `flyctl apps restart` → VERIFY the first heartbeat (gotcha 15; bot memory
  wins conflicts; boot clones can read a stale GitHub replica briefly).
- Never 2 Fly machines per app (`flyctl scale count 1`).
- Cache single-writer; three cursors per wallet (gotcha 12).
- Bot commits its own state/feed — `git pull --rebase --autostash` first.
- Image changes (fly.Dockerfile, host/) → `flyctl deploy --remote-only`;
  code/config → push + restart. Live app config = fly.live.toml.
