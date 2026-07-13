# Session handoff — 2026-07-13 (rev 9: RTDS on BOTH books; oversell + H3 fixed)

## Since rev 8 (2026-07-11 → 07-13)

- **RTDS shadow verdict: GO** — 916 detections, p50 0.8s / p99 6.4s / max
  9.9s (in-play emits within 10s). **RTDS_DETECT=1 is live on the
  real-money bot**; detections stream on both books; Alchemy + 300s poll
  remain backstops. Silent-stale guard force-reconnects a 120s-quiet
  socket (the stream once sat "up" delivering nothing for 35 min).
- **Phantom-cash incident (+$7.86, book-only, alarms caught it)**:
  overlapping pendings on one token each read the same balance move —
  one real 1.48-share sell booked 3×. FIXED: order-size caps every
  adoption, balance-diff only when the exchange no longer answers, SELLs
  cap at book holdings, ONE pending per token (engine + retries refuse
  while the resolver owns a token). Book rebuilt from chain ($11.21,
  drift 0, poisoned bets corrected from real fills, residual −$0.08).
- **No price floor** (user call after a 0.001 longshot was blocked):
  risk.min_price 0 on both books, cap stays 0.95; executor protected
  prices now 4dp and price-proportional (2dp zeroed sub-penny bounds).
- **H3 fixed**: per-wallet trade cursor + paginated activity fetch (5×100,
  walks to the last processed ts) — bursts can't scroll past a page.
- **clone-guard works now**: verifies over `git ls-remote` (the REST API
  is unreachable from Fly boxes — Bearer 401 + anon rate-limit; it failed
  open on every boot until 07-13). Both boots log `clone verified @ sha`.
- preflight_live.py rewritten for the unified SDK (deposit wallet, pUSD,
  book access, RTDS stream, geo). Drift ALARM floor 1c→5c (penny rounding
  isn't a bug; the self-heal path keeps its threshold).
- Live book truth: 31 copies, realized −$5.32 lifetime (day-one incident
  dominates), ~$11.2 cash + LAB + 1 small open. User to-dos: polymarket.us
  key REVOKED, $32 RESOLVED; Discord webhook rotation still open.

## RTDS shadow run (started 2026-07-10 22:26Z)

**T0 detection is deployed**: `RtdsListener` in copybot.py streams
Polymarket's real-time trade socket (`wss://ws-live-data.polymarket.com`,
topic `activity`/type `trades` — undocumented but official; probe measured
**median 0.8s delivery**, ~45 msg/s peak firehose, zero drops in 45 min).
Client-side Set E filter → the same `on_wallet_activity` funnel as the
Alchemy push. Heartbeat shows `rtds up Ns` / `⚠ rtds down`.

- **Paper: ON by default** — this IS the 24h shadow run. Compare `rtds:`
  detection lines vs Alchemy-path lag; watch the in-play tail (does RTDS
  emit held esports matches at match time or settlement?).
- **Live: OFF until the shadow validates.** Enable with
  `flyctl secrets set RTDS_DETECT=1 -a wwf-copybot-live` (restarts armed).
- Expected outcome: 24h lag stat drops toward ~1-2s and the
  "not copied in the detection window" misses go near-zero on paper.
- Phase 2 (researched, not built): RTDS `clob_user` topic streams OUR order
  lifecycle (auth: CLOB api creds) — would make the pending-order registry
  event-driven instead of 60s polling.

## Retune (2026-07-10 ~21:20Z, USER-directed — supersedes the rule-0.6 caps)

- **Hard caps RETIRED** (user decision): risk block mirrors the paper
  sentinels. Sizing = paper-parity **4% of equity** (class_pct 0.04/0.12),
  floored at the venue's $1 min order. Bankroll REBASED to the real
  **$22.28** equity (heartbeat reads `free $17/$22`); spend tracker reset.
- **Price guard is ABSOLUTE +0.05** (both books, identical rules):
  0.14→0.15 follows, 0.14→0.20 skips. Key renamed `price_guard_abs`.
- **Pending-order registry**: in-play `delayed` holds are handed to
  `state.pending_orders` (full copy context) instead of a 20s cancel; the
  heartbeat resolver adopts fills whenever they land or cancels at TTL 600s
  → honest miss. Stub-tested (adopt + expire). Watch the first
  `PENDING FILLED` line in production.
- Both bots restarted on the new rules (paper +$1,378, 60 copies at reboot).
- GOTCHA REMINDER proven again today: a boot can clone a stale GitHub
  replica — the 17:18 boot ran pre-push config for 4h (harmless here);
  ALWAYS verify the first heartbeat shows the numbers you shipped.

## INCIDENT 2026-07-10 (resolved in code; bot left DISARMED)

Push mode worked (~6s detection) — and exposed an executor bug: **in-play
markets accept orders with status `delayed` and ZERO matched at response
time**. The port read matched==0 as a rejection, logged an honest miss, and
forgot the order — six such orders then **filled at the exchange untracked**
(09:10–13:14Z, $30 real spend vs $5 booked; caps stopped binding; exits never
mirrored; CASH≠CHAIN alarmed exactly as designed). Damage net **≈ −$2.7**
(G2 g2/g3 and PlayTime g1 lost; Rune Eaters +$7.50 and Aurora g1 +$3.77 won,
platform auto-redeemed; Aurora g2 won, redeem pending). First diagnosis
wrongly blamed a second bot instance — the missed ledger's
"order rejected: {'order_id': …}" rows matching on-chain fills to the second
settled it: it was THIS bot.

**Fix (copybot.py, unit-tested with a stub client, 5 paths):** the executor
invariant is now *no order outlives `_order()` untracked* — zero-matched
acceptances poll `get_order`, cancel the remainder, and measure truth via the
exchange's CONDITIONAL balance diff; exception paths sweep all open orders on
the token the same way. State surgery done disarmed: cash = chain ($10.03),
six fills adopted as bets, spend tracker = the real $35 today (over cap → a
re-arm today places nothing new), false miss rows purged, −$0.59 adjustment
documented.

**DISARMED via `flyctl secrets unset LIVE_CONFIRM` (survives restarts —
note `machine stop` does NOT: http_service auto-restarts the machine).
Re-arm = user types `flyctl secrets set LIVE_CONFIRM="TRADE LIVE"` after
reviewing the fix. Recommended: re-run a probe on an IN-PLAY market first.**

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
legacy proxy) → /live feed row → cash math → Polygonscan.

## PUSH MODE (added 2026-07-10 05:41Z — user wanted push-speed detection)

The live app now runs its OWN Alchemy address-activity webhook
(`wh_98s36kdtcaf9t9xx` → https://wwf-copybot-live.fly.dev/alchemy, 7 Set E
addresses, id also in gitignored config.json as `alchemy_webhook_id_live`)
— fully separate from the paper app's webhook; the two books still share
nothing. Detection ~3s vs the poll era's ~39s avg. Verified: boot banner
`push mode · signature-verify ON · heartbeat 60s · backstop poll 300s`,
public /health "alive", Fly health check passing, unsigned POST → 401.
Remaining soft link: the first REAL Alchemy push accepted (a key mismatch
would print `⚠ bad signature — rejected` on every Set E trade — watch for
that; the 5-min backstop poll covers detection meanwhile).

- `live/sync_webhook.py` now syncs BOTH webhooks on every deploy_bot.sh run.
- **The live app has its watchdog now**: Fly /health check (self-heal) +
  watchdog.yml probes both apps → Discord (notify). A DISARMED live app
  idles WITHOUT HTTP — its watchdog page is expected, not a fault.
- start.sh live role: ALCHEMY_SIGNING_KEY present → push, absent → 60s poll
  fallback (so `flyctl secrets unset ALCHEMY_SIGNING_KEY` is the way back).
- Cosmetic: on restarts the push server exits with a KeyboardInterrupt
  traceback in the logs (SIGINT path) — harmless, not a crash.

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

- ~~Revoke the polymarket.us API key~~ DONE (user, 2026-07-10).
- ~~Decide the leftover $32 on polymarket.us~~ RESOLVED (user moved it,
  2026-07-10).
- Rotate the Discord webhook committed pre-2026-07-09 (spam risk only) —
  STILL OPEN.
- Optional: clear the 9 inert builder keys at
  polymarket.com/settings?tab=builder.

## Hardening trio (2026-07-10 late — all stub-tested)

- **Exit-retry (LIVE_ROLLOUT 1.6 built)**: failed mirror-exits queue in
  `state.exit_retries`; the heartbeat re-attempts up to 10 ticks (recovered
  exits ping Discord), then pages **⚠ EXIT STUCK** and lets the position
  ride knowingly. In-play holds hand off to the pending registry.
- **RTDS shadow ledger**: every Set E detection appends to
  `rtds_shadow.jsonl` (live: `rtds_shadow.live.jsonl`) — ts, lat_s, and
  whether another trigger saw the tx first. Rides the publish commit;
  capped ~2000 lines. This file is the 24h go/no-go data for RTDS_DETECT=1.
- **Boot stale-clone guard (start.sh)**: clone HEAD verified against the
  GitHub API (re-clone up to 4×) — kills the stale-replica boots that hit
  twice on 2026-07-10.

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
