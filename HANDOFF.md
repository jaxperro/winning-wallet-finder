# Session handoff — 2026-07-10 (rev 5: LIVE PLACEMENT PROVEN — awaiting first organic fill)

Self-contained pickup for a fresh session (human or AI). Read
[README.md](README.md) gotchas 1–16 (16 is the new-stack one), 
[FINDINGS.md](FINDINGS.md), [ETHERSCAN_MIGRATION.md](ETHERSCAN_MIGRATION.md),
[LIVE_ROLLOUT.md](LIVE_ROLLOUT.md). The repo is authoritative.

## THE CRITICAL PATH IS DONE (2026-07-10 05:08Z)

Every link from rev 4 closed, in order, all proven on the live box:

1. **Root cause of the wrap revert found on-chain** (not caller-gating): the
   pUSD CollateralToken accepts BOTH native USDC and USDC.e by contract, but
   the public CollateralOnramp `0x93070a…` has native USDC **paused**
   (`paused(0x3c499c…)=1`). Docs claiming "must be USDC.e" are downstream of
   that pause, and the ts-sdk has NO wrap helper — the UI converts via the
   Bridge.
2. **Bankroll converted via the sanctioned path** (`host/wrap_via_bridge.py`,
   stage-gated $3 test slice first): gasless ERC-20 transfer → per-wallet
   bridge address (`POST bridge.polymarket.com/deposit`) → delivered back
   **already wrapped as pUSD, fee-free** ($24.73 in → $24.73 pUSD out).
   `get_balance_allowance(COLLATERAL)` = 24730000 with max-uint allowances.
3. **Executor ported** (copybot.py `LedgerLiveExecutor`): unified SDK
   `SecureClient.create(private_key)` — deposit wallet auto-resolves, no
   api_key at runtime; `place_market_order` FAK with protected prices
   (quoted ± `live.max_slippage_pct`, default 5%, clamped [0.01,0.99] so the
   SDK's band check can't raise); never raises into the trade loop.
   `chain_cash_gap` repointed at the deposit wallet's pUSD (the +24.73 alarm
   is gone). Old tick-rounding machinery deleted — the SDK owns tick/negrisk.
4. **$5 FAK round trip FILLED on the new image**
   (polymarket-client==0.1.0b16 in fly.Dockerfile): BUY matched 7.35294 sh
   @ 0.68 ($5.00), SELL matched 7.35 sh @ 0.67 ($4.92 gross). Fill-field
   semantics confirmed: BUY making=USD given / taking=shares got; SELL
   reversed. Round trip cost 23.7¢ = 1c spread + ~3.2% taker fee on the
   sell + 0.003 sh dust — the modeled `TAKER_FEE_RATE 0.03` tracks reality.
5. **Armed bot restarted on the ported code**: geo TRADABLE, LIVE banner,
   baseline clean, heartbeat `[1] open 0 · free $25/$25` with NO drift/chain
   alarm. The bot can now actually place.

## THE ONE REMAINING STEP — first organic fill ⇒ Phase 4

When the first real copy fills, walk LIVE_ROLLOUT **Phase 4** and report
every link: order id → fills ledger row → position visible in the Polymarket
UI (the DEPOSIT WALLET `0x455e252e45Ee46d6C4cc1c8fAdD3899d68f245a1`, not the
legacy proxy) → /live feed row → cash math → Polygonscan. Liveness until
then = `flyctl logs -a wwf-copybot-live` + feed freshness (still NO watchdog
on the live app).

## Known small offsets (do NOT fix while the bot runs — gotcha 15)

- Book cash $25.00 vs chain pUSD $24.49: fixed $0.51 offset (config
  bankroll $25 vs $24.73 deposited, + the probe's 23.7¢ round trip which
  correctly bypassed the book). Below the $1 CASH≠CHAIN alarm. Fold into
  the state at the next natural machine-stop surgery if desired.
- The probe's trade is deliberately absent from the fills ledger/feed.

## System state (2026-07-10 05:10Z)

- **Live bot** (`wwf-copybot-live`, poll 60s, arn): ARMED, $24.49 pUSD,
  caps $5/trade · $25/day · $30 exposure — NEVER raise without the user.
  Disarm: `flyctl secrets unset LIVE_CONFIRM` or `flyctl apps stop`.
- **Paper bot** (`wwf-copybot`, push mode): Set E, $1,000 book of
  2026-07-08 — Day 1½ realized +$1,107.57 (36 copies) at rev-4 time; check
  the dashboard for current.
- **Set E** both books: LSB1, imwalkinghere, Kruto2027, 0xbadaf319,
  gkmgkldfmg, AIcAIc, 1kto1m (watch list & demotion candidates in rev 4 /
  FINDINGS).
- **Dashboards**: jaxperro.com/trading (paper), jaxperro.com/live (real).
- **Ops utilities** (all need the box's env): `host/wrap_via_bridge.py`
  (bankroll conversion — done, idempotent), `host/order_probe_v2.py`
  ($5 round-trip probe; now polls the indexer and self-revokes its builder
  key), `host/flatten_positions.py` (market-sell everything — the
  emergency exit).

## New-stack facts a future session must not relearn (also gotcha 16)

- py-clob-client: ARCHIVED. Reads OK, placement dead everywhere.
- polymarket-client (`SecureClient`): placement, gasless
  `execute_transaction`, `get_balance_allowance` (`.balance` int base
  units). `create()` is ready-to-use; `with` only closes transports.
- Protected prices (`max_price`/`min_price`) raise outside [tick, 1−tick];
  we clamp to [0.01, 0.99] before calling.
- Builder API keys: revocable ONLY via their own secret (HMAC DELETE) —
  always `atexit`-revoke in-process keys. 9 inert ones from bring-up sit on
  the account; only the UI (settings→builder) can clear them. Runtime bot
  needs NO key (approvals persist on-chain).
- Bridge: `POST /deposit {address}` → per-wallet EVM address (ours:
  `0x66C1A4b43824CB0DDa54c94F118fc868A6270b91`, pinned in
  wrap_via_bridge.py); Polygon native USDC accepted ($2 min); delivery is
  pUSD directly; `/status/{bridge_addr}` tracks; `/quote` previews.

## Queued work (unchanged priorities)

1. ETHERSCAN_MIGRATION.md phases 0→5.
2. Empirical fill model from `book` snapshots; depth gate before sizing up.
3. Funding-cluster tracer port to Etherscan logs.
4. Live-app watchdog (poll mode has no /health).
5. Decide the leftover $32 on polymarket.us (withdraw or abandon).
6. `preflight_live.py` still validates the ARCHIVED stack — port or retire.

## User to-dos (security/hygiene)

- Revoke the polymarket.us API key `f03d9eb5…` (leaked in a screenshot) if
  not already done.
- Rotate the Discord webhook committed pre-2026-07-09 (spam risk only).
- Optional: clear the 9 inert builder keys at
  polymarket.com/settings?tab=builder.

## Operational quick-reference

- Follow-set change: edit live/copybot.paper.json → `./live/deploy_bot.sh`;
  keep backtest.json mirrored.
- State surgery: `flyctl machine stop` FIRST → edit → push → restart →
  VERIFY first heartbeat (gotcha 15).
- Never 2 Fly machines per app (`flyctl scale count 1`).
- Bot commits its own state — `git pull --rebase --autostash` before pushing.
- Image changes (fly.Dockerfile, host/) → `flyctl deploy --remote-only -c
  fly.live.toml -a wwf-copybot-live`; code/config → push + `flyctl apps
  restart` (a deploy also wipes /tmp clones on the box).
- Box one-offs: `flyctl ssh console -a wwf-copybot-live -C "bash -c '…'"`;
  clone to /tmp with the box's GITHUB_TOKEN; run python with `-u`.
