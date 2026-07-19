# Session handoff — 2026-07-17 (rev 14: +$44 bankroll, exact paper-parity config; rev 13: FAK parity; rev 12: chain-seed)

## 2026-07-19: spread skip retired (both books)
The depth gate's hard spread>0.08 skip was a lag-era relic: fitted at 39-90s
detection when wide spread meant the book repriced under a late copy. At ~4s
RTDS lag the mechanism is gone (fills ledger: median |slip| 3.7%→1.7% era
over era; Kruto+gkmg in-play mean +0.4%), and the skip fired on the informed
wallets' best moments — 17 resolved skips would have run 12W/5L (+$11 live /
+$674 paper, Tauson tennis +$447 the headline). Overpay stays bounded by the
price guard (+0.05 abs) and FAK protected band; the depth STAKE CAP and dust
skip remain (thin misses were net-protective, 0W/2L). Watch: wide-spread
copies' realized slip on the dashboards — if mean slip degrades past ~+3%,
revisit with a guard-anchored executable-ask check instead of a spread skip.

## 2026-07-17: bankroll top-up + USER DIRECTIVE — live mirrors paper EXACTLY
- **User deposited +$44.14** (Cash App → BTC → bridge, 70241 sats; ~$6 lost
  to Cash App spread + BTC conversion — Polygon USDC is the cheap route).
  Book rebased: cash = chain $50.53, bankroll $22.28 → **$66.42 = capital
  contributed** (the deposit lives in bankroll_usd ONLY — first attempt also
  logged it as an adjustment, double-counting it: realized read −$44, ledger
  drift −$38.91; the invariant is cash = bank + Σadj + Σpnl + flows, so a
  deposit enters EXACTLY ONE term). Realized preserved at lifetime −$5.14;
  sizing is 4% of working equity (cash+cost basis) ≈ $2.45/bet — bankroll_usd
  is baseline/display only.
- **USER DIRECTIVE (2026-07-16): the live bot mirrors the paper test
  EXACTLY.** Caps were already off (retired 07-10). Parity sync applied:
  risk.max_price 0.95 → **0.99** (paper's), wallet floors synced to paper's
  current pins, dead `follow.per_wallet_min_usd` block removed (overwritten
  at load by wallets[] — it had gone stale). DELIBERATE exception:
  min_order_usd stays $1 (venue physical minimum; paper's $5 never binds at
  its equity — copying it would force $2.45 signals up to $5 = 8% of equity,
  DIVERGING from the 4% rule). NOTE: nothing auto-writes the live config —
  floors drift from paper's nightly sync until manually re-synced; check at
  each Friday bench review.
- **Ops lesson (now README gotcha 15, third corollary)**: `flyctl machine
  stop` reported stopped while the process ran 25 more minutes — the rebase
  boot came up stale with ⚠ CASH≠CHAIN −$44.12 (alarm correct, money safe).
  A plain restart after the push landed picked everything up cleanly. Before
  state surgery: watch heartbeats actually CEASE.
- Docs sweep: README follow-set (Set E rev 4), chain-seed + FAK-parity +
  edge.py coverage, stale "tiny test caps" pointer fixed.

## Operating boundary set by the user (2026-07-13, "away for a while")
**Full autonomy on BOTH bots**; real-money bot **stays ARMED**. Never touch
the private key, never raise/loosen caps, never rotate the Discord webhook
(needs their login — STILL OPEN). If something looks genuinely dangerous,
DISARM the live bot (`flyctl secrets unset LIVE_CONFIRM`) rather than push
through it.

## Bench review 2026-07-16 (follow set rev 4: AIcAIc re-added, no bench promotions)
Forward table = portfolio.json resolved rows (model $-weighted), two windows:
since 07-11 and since the 07-13 21:30Z swap. Verdict:
- **NO promotion for Vahan88 (−21.6% / −30.5%) or EdwardIN (−11.1% / −8.9%)**
  — both negative in both windows (EdwardIN ate two ~$1k Norway–England
  halftime props). Both stay benched in backtest.json.
- **AIcAIc RE-ADDED (6th wallet, auto p80 floor)**: +25.6% since 07-11 and
  +21.9% across 11 bets since being dropped — spread across many LoL markets,
  not one hit. The Esports World Cup (its exact niche) runs through August;
  the 07-13 drop was made mid-bad-stretch, right as its season started.
  WHIPSAW RISK acknowledged: the drop and the re-add are both ~2-week-noise
  decisions; the tiebreaker is the seasonal catalyst + the informed-niche
  prior (June replay). If AIcAIc is negative again next Friday, it goes for
  good and the slot rule tightens.
- **Kruto2027 stays** (floor 80 pinned): inert (3 model bets, −12.8%) but an
  idle wallet costs nothing and its 30d +$3.5k says it's worth the seat.
- **BikesAreTheBikes stays**: −3.7%/−6.1% on n≤12 — too early to cut.
- **LSB1 note**: +21.4% post-swap (16 bets) but −7.5% on the longer window
  and −$438 all-time paper — NOT re-added; re-check next Friday.
- Guard/floor counterfactuals: re-validated 07-14 by the missed-bets audit
  (guard skips net-negative would-be on both books) — unchanged.
- **BANKROLL decision instrumented (live/edge.py, in daily.sh + digest
  footer)**: parity-era per-signal paper edge vs the MEASURED live fee
  hurdle (1.9% of stake — real, not the 3-4% guess; extreme-price copies
  keep p(1−p) small) + matched live-paper drag (6.4pp @ $1 stakes).
  Decision rule: size up only when edge > hurdle+2pp on n≥30 parity-era
  resolved signals (~end of July). history/edge.csv accrues one row/day;
  `python3 live/edge.py` any time for the current verdict. RECOMMENDED to
  the user (2026-07-16, no decision yet): ~$30-50 coverage top-up now at
  most (the free-cash gate is skipping signals at $6 free), big sizing
  waits for the measurement; caps bind at ~$60-125 equity anyway and stay
  untouched. Deposits are the user's to make — never move funds.

## Shipped since rev 12 (2026-07-15)
- **PAPER FAK PARITY**: PaperExecutor BUYs now model live FAK reality — if
  the depth gate's book snapshot (threaded through meta["book"], no second
  fetch) has no ask ≤ quote×1.05 (live.max_slippage_pct default), the paper
  "fill" is a rejection and the engine records the same "order rejected: no
  orders found to match" miss live logs. Rationale: post-chain-seed this is
  live's #1 miss class (10/33) and paper pretended to fill them (it "filled"
  the Claude Opus bet live's FAK couldn't) — biasing the live-vs-paper
  per-signal ratio (the top-up number) OPTIMISTIC. Chain-seed verdict after
  41h: detection misses 23→3 live / 35→2 paper; the residual tail is
  no-trigger-at-all. Resting-limit fix for live FAK no-match evaluated and
  REJECTED for now: recent 10-row sample is −$0.97 would-be net + a resting
  bid is adversely selected (fills when price falls through it, not when
  we're right). Revisit only with paper-parity data showing real cost.
  Fail-open on dead book fetch; SELLs stay optimistic (exits need the
  retry/pending machinery to model honestly — out of scope). Live book
  unaffected (meta ignored). Tests: tests/test_paper_fak.py (8 paths);
  LedgerPaperExecutor no longer appends a fill row for a rejection.
  Deploy: paper app restart only. Expect paper misses like "order rejected:
  no orders found … (paper model: best ask 0.900 > cap 0.840)".

## Shipped since rev 11 (2026-07-14)
- **CHAIN SEED (T0b)**: the missed-bets review found the one remaining
  detection leak — RTDS doesn't emit every market (the "Credible public
  sale" sweep was missed on BOTH books at 20:18Z despite the rev-11 fix:
  RTDS silent + data-api indexer past the 600s window; badaf's detection
  misses are the only +EV ones, +$34.60/10 on paper). Now the Alchemy push
  carries its tx hashes into `on_wallet_activity(hint_txs=…)`; any hash the
  data-api fetch didn't return is decoded straight from the receipt
  (`fills_from_tx`: OrderFilled logs, maker==wallet row; gamma lookup for
  title/outcome/cond; block ts). Validated 200/200 against shadow-ledger
  fills and end-to-end on the actual missed Credible txs. Fail-open on any
  RPC/gamma error (backstops unchanged); forged-event-safe (only the two
  new-stack exchange contracts 0xe111…996b / 0xe2222…0f59 are trusted
  emitters — a dust transfer can trigger the webhook, so this matters).
  Tests: tests/test_chainseed.py (6 stub paths). Both boxes have
  ALCHEMY_RPC_URL, so the path is live on deploy. Watch for the first
  `chainseed:` line — expected on badaf niche markets during RTDS flaps.
- **Missed-bets audit verdict** (what prompted this): price guard skips are
  net-NEGATIVE would-be P&L on both books (validated again — leave at 0.05);
  old detection misses were -EV and all from since-dropped wallets; conform/
  sub-$1/sub-penny categories all fixed pre-rev-11. Remaining candidates NOT
  built: thin-book FAK no-match → resting limit via the pending registry
  (small now, scales with stakes), paper thin-book modeling parity (biases the
  live-vs-paper ratio optimistic).

## Shipped since rev 10 (all deployed + verified)
- **RTDS-seed lag fix** (the big one): RTDS detected trades at ~0.3s but the
  copy landed 130-305s later — `on_wallet_activity` threw the RTDS payload
  away and re-fetched the data-api, whose indexer lagged that long on
  badaf/1kto1m crypto+index markets, so the 300s backstop poll did the copy.
  Now the RTDS message SEEDS the funnel directly (deduped by tx; re-fetch +
  fill-split merge still run). Verified live: first RTDS-era copy landed at
  **4s lag** (heartbeat `24h lag 4s`). This was "option B done as augmentation".
- **Tick-conform protected prices**: the sub-penny 4dp bound violated tick
  size on 1c books ('max_price must conform to tick size 0.01' — cost a
  winning SPX copy). Bound now rounds to the QUOTE's own precision (a quote
  is always a tick multiple → its decimals are the finest safe precision).
- **Cursor cold-start**: `_fetch_since_cursor` with no cursor now seeds at
  now-600s (fresh only). The bug walked 500 rows/wallet of history at the
  02:10 boot → ~230 phantom "too slow (20,000m late)" missed rows/book;
  purged both books.
- **Dashboard**: exec-lag tile falls back to lifetime avg when the 24h window
  is empty; per-copy `lag_s` now renders in the Slip cell. `/live` verified.
- **floor_pin + audit** (3-agent clobber audit of every automated writer):
  sync_floors.py was silently reverting manual floors to p80 nightly (Kruto
  80→125.61) — the paper book diverged from the live book. Fixed with a
  `floor_pin` field it honors; Kruto pinned 80 across paper+live+backtest.
  Audit also fixed: pin now really mirrors to backtest.json (comment lied),
  a loud warning when reverting an unpinned hand-edited floor, watch_sharps
  add/drop diff logging + generated-only doc, class_pct paper/backtest parity
  guard in daily.sh. **Audit verdict: otherwise CLEAN** — sync_floors is the
  ONLY pipeline step that mutates a manual-knob file, and it round-trips the
  whole JSON touching only `floor`; config.live.example.json is untouched by
  every automated writer (safe by neglect).

## Known live quirk (not a bug): RTDS stream FLAPS
The undocumented RTDS socket drops a few times a day and hits the 60s
reconnect-backoff cap; heartbeat shows `⚠ rtds down` briefly then `rtds up`.
Detection is covered by the Alchemy webhook (~3s) + 300s poll + the 120s
silent-stale guard throughout. This is the layered-fallback design working.
If it ever flaps CONSTANTLY, that's when to investigate (RTDS deprecation?).

## Current state (2026-07-13 ~20:40Z)
- **Live** (`wwf-copybot-live`, ARMED): ~$16 equity, 32 copies, realized
  −$5.33 (day-1 incident still dominates), `rtds up · userws up`, books
  chain-exact. Follow set: Kruto2027(floor 80), 0xbadaf319, gkmgkldfmg,
  AIcAIc, 1kto1m.
- **Paper** (`wwf-copybot`): +$1,347 realized, 107 copies. Same 5 wallets.
- **Backtest** proves 11 (bench: Vahan88, 42021, BikesAreTheBikes, EdwardIN).
- Two persistent monitors were watching failure signatures this session
  (they end with the session — re-arm in the next one if desired).

## Next-session priorities
1. **Friday bench review**: pull the per-wallet forward table for the 4
   backtest candidates + re-read guard/floor counterfactuals; decide
   promotions and the bankroll top-up size (the live-vs-paper per-signal
   ratio is the number — now clean & comparable on both books).
2. Watch the exec-lag tile fall toward ~1-2s as RTDS-era copies replace the
   pre-RTDS average.
3. The protected-price/amount rounding (5 lines in copybot.py `_order`) has
   been the buggiest corner — 4 fixes now, each for a different quote class.
   Believed done (tests pin every class) but it's the first place to look if
   an `ORDER FAILED: … conform/amount` line appears.
4. User to-do still open: rotate the Discord webhook (their login).

## Items 2-4 shipped (2026-07-13, evidence from a 3-agent analysis workflow)

- **DEPTH GATE** (copytrade.book_depth + gate in _handle_their_buy;
  config.depth_gate, both books). Fitted on 131 book-annotated fills:
  before ordering, SKIP if spread>0.08 (market mid-move, median |slip|
  ~14%) or ask5c<$50 (dust books mispriced every fill), else cap stake at
  10% of 5c ask depth (impact <~2%; >20% of depth → >+2% slip 33-50% of
  the time). Fail-open on a book-fetch error. This is what lets stakes
  SCALE — binds mainly on slow-market wallets (badaf median 23% of depth
  at $40-100). 5 stub paths pass.
- **PRICE GUARD validated, unchanged**: counterfactuals put 0.05 exactly at
  the EV knee (0.05-0.10 moves ≈ breakeven, >0.10 = -20% ROI). Comment
  records it. The +9-14% slip tail is lag-drift in deep esports books —
  a detection problem RTDS ~1s already addresses, not a guard problem.
- **clob_user OWN-FILL PUSH** (UserFillsListener, live only): streams our
  order/trade lifecycle from ws-subscriptions-clob/ws/user (auth =
  client.credentials; all-markets). A matching order id fires an IMMEDIATE
  resolve_pendings() — in-play holds adopt on match, not on the 60s tick.
  Events only TRIGGER; get_order stays the arbiter (2026-07-12 invariant).
  resolve_pendings is now re-entrancy-locked (ws + heartbeat race). 60s
  poll stays as fallback; ws failure degrades to today. Heartbeat shows
  `userws up`. VERIFIED connected on the live box.
- **Sub-$1 BUY fix**: gated $1.00 stakes shaved to $0.99 by share-flooring
  → 7 of 13 live rejections. Now bumped to the $1 venue minimum.
- Follow-set (rev 9→10): 5 wallets live (dropped imwalkinghere, demoted
  LSB1; Kruto floor 80 pinned in backtest too for comparability). Backtest
  proves 11 (bench: Vahan88, 42021, BikesAreTheBikes, EdwardIN).

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
