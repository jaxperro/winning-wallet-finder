# Real-money rollout plan — executable by a fresh agent session

> **STATUS 2026-07-10: executed through Phase 4** (bot ARMED, push mode,
> first fills verified end-to-end — see [HANDOFF.md](HANDOFF.md) for current
> state). **Rule 0.6's hard caps were RETIRED 2026-07-10 by user decision**:
> sizing is paper-parity 4%-of-equity, floored at the venue $1 minimum. The
> new-stack realities discovered during execution live in README gotchas
> 16–17 (pUSD collateral / bridge conversion; in-play `delayed` acceptances
> and the pending-order registry). This document stays as the historical
> plan + the Phase 5 edge-case matrix, which is still worth running.

Written 2026-07-07 after the pre-money audit. This document is **self-contained**:
a session with no prior context (Sonnet/Opus) should be able to execute every
phase from here + the repo. Read [README.md](README.md) gotchas 1–15 and
[LIVE_TEST.md](LIVE_TEST.md) first. Project memory (`polymarket-resolution-truth`,
`polymarket-us-expansion`) has deeper background; the repo is authoritative.

**The goal:** a supervised minimum-size live test — deposit → copy a followed
bet → mirror a sell before resolution → hold to resolution (win, loss, AND
50/50 refund) → withdraw — with worst-case loss bounded by the ~$30 exposure
cap. Only after every edge case in Phase 5 passes does sizing-up get discussed.

---

## Phase 0 — invariants (read, don't skip)

These were each earned the hard way. Violating any of them voids the test.

| # | invariant | why |
|---|-----------|-----|
| 0.1 | **Live orders only from a geo-TRADABLE box.** `python3 host/geocheck.py` must print `VERDICT: TRADABLE`. The Fly worker (`wwf-copybot`, Stockholm) passes; a Mac in the US gets 403-geo-blocked on the order POST. | Polymarket IP-blocks order placement (README gotcha 10). |
| 0.2 | **The user decides WHEN to arm** (jurisdictional posture: they trade live from Colombia months, paper from US months). The agent prepares everything; the arming step and its timing are user-only. | The machine's location doesn't relocate the person. |
| 0.3 | **One writer per book.** The live book uses its own state (`copybot_state.live.json`) and must use its own feed file (Phase 1.1 — today both modes write `live/copybot_live.json`, which would clobber the July paper test's dashboard). The paper worker keeps running untouched throughout. | The paper July test is the control group; don't contaminate it. |
| 0.4 | **Settlement truth is the chain.** CLOB `winner` flags never populate for operator-resolved in-play markets, and 50/50 refunds (28% of follow-set markets!) pay $0.50/share. `resolution_price()` in copybot.py already does CLOB→chain-payout tiering; live mode requires `ALCHEMY_RPC_URL` set or refunded/operator-resolved positions never settle. | 2026-07-06 audit: 4 positions sat locked for hours. |
| 0.5 | **The ledger must reconcile to the cent.** `ledger_drift` in the feed must stay 0.00; in live mode it additionally must match the on-chain USDC balance (Phase 1.4). Any drift >1¢ = stop, diagnose, fix before the next order. | Real money makes bookkeeping bugs unrecoverable. |
| 0.6 | **Hard caps stay at test size until Phase 6.** `config.live.json` risk block: $5/trade, $5/position, $25/day, $30 exposure, 6 open, prices 0.05–0.95, FAK orders. Never raise mid-test. | Worst case ≈ the $30 exposure cap. |
| 0.7 | **Paper-mode safety interlocks stay intact**: live needs `mode:"live"` + `--live` + typed phrase. Never bypass, never automate the phrase. | The phrase is the human checkpoint. |

---

## Phase 1 — code prep (agent; all verifiable in paper mode, no money)

Each item is a small PR-sized change with its own verification. Do them in order.

### 1.1 Feed separation
`copybot.py` `FEED` is a module constant (`live/copybot_live.json`) — a live run
overwrites the paper feed. Make it config-driven: `cfg.get("feed_path",
"live/copybot_live.json")`, and set `"feed_path": "live/copybot_live_real.json"`
in `config.live.json`. Same for `FILL_LOG` → `copybot_fills.live.jsonl`.
**Verify:** run paper poll-once with a scratch config setting `feed_path`; confirm
the alternate file is written and `live/copybot_live.json` is untouched.

### 1.2 Secrets via env for the worker path
`config.live.json` is gitignored — the Fly worker clones the repo and won't have
it. Extend `load_cfg`/`normalize_follow_config` so `LIVE_PRIVATE_KEY`,
`LIVE_FUNDER_ADDRESS`, `LIVE_SIGNATURE_TYPE` env vars override `cfg["live"]`.
**Verify:** unit-style: `LIVE_PRIVATE_KEY=0xdead python3 -c "…load_cfg…"` shows
the override; no secret ever printed or committed.

### 1.3 Live deps in the worker image
`fly.Dockerfile` is stdlib-only. Add `pip install py-clob-client web3` (pin
versions) — needed for `LedgerLiveExecutor` + `redeem.py`. Keep the paper path
import-lazy (it already is: LiveExecutor imports inside `__init__`).
**Verify:** `flyctl deploy --remote-only`, then
`flyctl ssh console -C "python3 -c 'import py_clob_client, web3'"` exits 0, and
the paper bot still boots clean (banner + heartbeat, drift 0.00).

### 1.4 Live-mode ledger anchor: on-chain balance check
In live mode, `summary()` should each heartbeat compare `state["cash"]` against
the CLOB collateral balance (`client.get_balance_allowance`, cached ≥60s) and
log `⚠ CASH≠CHAIN` when they diverge by more than pending-order float. This is
the real-money version of `ledger_drift`.
**Verify:** code-review level only until Phase 4 (needs a funded account);
assert the check is skipped cleanly in paper mode.

### 1.5 Geocheck fatal in live mode
`host/start.sh` warns on geo-block but continues (fine for paper). In
`copybot.py` main(): if `want_live` and a fresh `geocheck` probe (import the
3-probe logic or shell out) is not TRADABLE → `sys.exit`. Never place a live
order from a blocked box.
**Verify:** from the (US) Mac, `--live` with a filled dummy key must refuse with
the geo message before the confirmation phrase.

### 1.6 Exit-order resilience
Mirror-sells use marketable FAK. On a thin in-play book a sell may fill 0 and
the engine just logs ORDER FAILED — the position then silently rides to
resolution. Add bounded retry: on a failed/partial SELL, retry next heartbeat
(up to N=10 attempts, logged each time), then alert `⚠ EXIT STUCK` to Discord.
**Verify:** paper-mode unit: monkeypatch executor.sell to fail twice then
succeed; position exits on third heartbeat, sold-leg accounting intact.

### 1.7 Sync `config.live.json` follow set
It still lists `shisan888` (long gone). Copy the current `wallets` list +
pinned floors from `live/copybot.paper.json` (post-demotion: all volume class,
Stavenson/0x4bFb/ArbTrader pinned at $4,760/$1,090/$168), keep the $5 caps and
`bankroll_pct` small (0.02 ≈ $5 stakes on a $250 book — recompute so
`bankroll_pct × bankroll_usd ≈ $5`).
**Verify:** `./live/deploy_bot.sh`-style preview: `python3 -c` validation prints
8 wallets and the caps unchanged.

### 1.8 Dashboard: live-money section
The dashboard reads `live/copybot_live_real.json` (from 1.1) into a fourth
board chip + tables, clearly labeled REAL MONEY, reusing the standardized
columns (Their/Our Entry, Stake, Result W/L/R/S, Placed/Settled). Statuses and
the `ledger_drift`/`adjustments` fields render like the paper section.
**Verify:** point it at a copy of the paper feed with `"mode":"live"`; preview
renders; then empty-state renders when the file 404s (live test not started).

**Phase-1 exit criteria:** all verifications green; paper bot untouched and
heartbeating (`flyctl logs`); `git push` after each item; the July paper feed
never skipped a beat.

---

## Phase 2 — account + funding (USER ONLY — agent stops here until told)

Follow [LIVE_TEST.md](LIVE_TEST.md) §1–2, with these updates:

1. Dedicated Polymarket account (email login recommended → `signature_type: 1`).
   **Deposit through the Polymarket UI** (~$50 USDC) so the collateral token is
   whatever the venue expects — do not send tokens directly on-chain (USDC vs
   USDC.e confusion is a real loss mode).
2. Export the private key (Settings → Export private key). Fill
   `config.live.json` `live.private_key` + `live.funder_address` (profile
   deposit address). The file is gitignored; keep it that way.
3. Send ~1 POL to the **signing EOA address** (preflight prints it) for redeem
   gas. Skipping this = set `live.auto_redeem: false` and redeem manually.
4. Decide the venue box (0.1/0.2): **recommended = the Fly worker** (geo-clean,
   push-speed). Running on the Mac requires being in an unrestricted country.

---

## Phase 3 — preflight + dry runs (agent, with user watching)

1. `python3 preflight_live.py` — all green (auth, USDC balance, book access,
   POL gas). Fix reds before proceeding.
2. `python3 copybot.py --config config.live.json --state /tmp/dry.json
   --test-wallet <an active follow-set wallet>` — paper-mode routing of a real
   trade through filter+engine. Confirms config/filters without orders.
3. If running on Fly: `flyctl secrets set LIVE_PRIVATE_KEY=… LIVE_FUNDER_ADDRESS=…`
   (values piped, never echoed), plus a **separate Fly app or process group**
   decision: do NOT convert the paper worker — run a second app `wwf-copybot-live`
   from the same image with `mode:"live"` env config, so paper and live books
   never share a process. Update the Alchemy webhook? No — live app runs
   **poll mode (60s)** for the test; push webhooks stay pointed at the paper app.
   Poll lag ≈ 39s avg was measured fine.
4. Baseline sanity: live app boots, logs `on-chain settle fallback: ON`,
   `geocheck TRADABLE`, prints caps, **waits at the confirmation phrase** —
   which only the user types (via `flyctl ssh console` attach or Mac run).

**Abort at any point:** Ctrl-C / `flyctl apps stop wwf-copybot-live`. Nothing
is armed until the phrase is typed; nothing is at risk beyond the deposit.

---

## Phase 4 — first fill (supervised)

Arm during a liquid window (evening EU/US — ITF + esports flow). Then wait for
the first conviction signal. For the FIRST fill, verify every link end to end:

- [ ] console `FOLLOW …` line → `OPEN … [LIVE] buy` with order id
- [ ] fill appears in `copybot_fills.live.jsonl` with lag + slippage
- [ ] position visible in the Polymarket UI (the account actually holds it)
- [ ] feed `live/copybot_live_real.json` row status `open`; dashboard renders it
- [ ] `state.cash` dropped by cost+fee; 1.4's CASH≠CHAIN check silent
- [ ] Polygonscan: the fill's on-chain transfer matches shares/price ±rounding

If any link fails: stop (Ctrl-C), snapshot state + logs, diagnose before re-arming.

---

## Phase 5 — edge-case matrix (the actual test)

Run days-to-weeks until every row has a ✓ with evidence (log line + feed row +
chain tx where applicable). Most trigger naturally from the follow set's flow;
patience beats forcing.

| case | trigger | acceptance evidence |
|------|---------|---------------------|
| BUY copy | any conviction signal | Phase-4 checklist |
| ADD (signal scales in) | whale re-buys same market | position grows ≤ stake rule; feed cost/shares aggregate |
| **SELL before resolution** | signal fully exits (they exit most positions — hours, not days) | `EXIT … [LIVE] sell` + proceeds land in balance + status `closed`/SOLD + 1.6 retry logs if book thin |
| hold → WIN | resolution | `SETTLE … WON` + **auto-redeem tx hash on Polygonscan** + USDC balance up by shares×$1 |
| hold → LOSS | resolution | `SETTLE … LOST`, no redeem attempted, book realizes −cost |
| hold → **REFUND 50/50** | ITF walkover/abandonment (≈28% of markets — will happen) | `SETTLE … REFUND ↩` + redeem tx at $0.50/share + cash up by shares×0.5 |
| missed (price guard) | price runs >5% in the copy lag | `skip … price moved` + missed row on dashboard, later settled hypothetically |
| missed (cash gate) | exposure cap hit | `capital fully deployed` skip + missed row |
| downtime reconcile | stop the live app 30+ min during flow, restart | boot logs `reconcile:` lines; downtime entries → missed("bot offline"); exits mirrored or settled |
| FAK no-fill | thin book (observe, don't force) | order killed cleanly, no resting order in the UI, retry per 1.6 |
| neg-risk market | if one gets copied | `⚠ NEG-RISK … redeem manually` warning; manual UI redeem works |
| **WITHDRAW** | user, at the end | USDC withdrawn via UI back to source; final book cash ≈ withdrawal ± dust; screenshot + final feed snapshot committed |

Throughout: `ledger_drift` 0.00 every heartbeat, CASH≠CHAIN silent, and the
paper book running in parallel as control.

---

## Phase 6 — wind-down + verdict (user + agent)

1. Stop the live app; final feed + state snapshot committed (tagged `live-test-1`).
2. Write `FINDINGS.md` addendum: measured live slippage vs paper's −3.6%/65s,
   fee accuracy, fill rate (FAK kill rate), any stuck exits, refund handling.
3. **Graduation gate (all required):** every Phase-5 row ✓ · ledger drift 0
   throughout · live slippage within 2× of paper model · no manual state
   surgery needed. Then — and only then — a sizing discussion (separate
   decision, separate doc; the their-bet ceiling and depth-aware fills from
   README "Scale carefully" apply).

## Standing notes for the executing agent

- Never print/commit the private key; `git status` before every commit.
- The daily 08:00 Mac pipeline and the bot's self-commits keep pushing to this
  repo — always `git pull --rebase --autostash` before pushing.
- The paper worker's webhook/feed/state are load-bearing for the July test:
  if a change might touch them, it's a Phase-1-style isolated change with its
  own verification first.
- When something looks wrong, the debugging order that worked in the audit:
  feed → state → fills ledger → data-api → CLOB → **chain payouts** (the chain
  is always right).
