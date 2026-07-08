# Session handoff — 2026-07-08

Self-contained pickup for a fresh session. Read [README.md](README.md) gotchas
1–11, [FINDINGS.md](FINDINGS.md), and [LIVE_ROLLOUT.md](LIVE_ROLLOUT.md) first.
Project memory (`polymarket-resolution-truth`, `polymarket-us-expansion`) has
deeper history; the repo is authoritative.

## Where things stand (all green)

- **Live paper bot**: Fly.io app `wwf-copybot`, region `arn` (Stockholm),
  push mode. Following **Set D** (2026-07-08): LSB1, imwalkinghere, Kruto2027,
  42021, 0xbadaf319, BikesAreTheBikes — 6 moderate-bet volume wallets chosen by
  simulation ($27.8k / +2680% 30d backtest). `flyctl logs --app wwf-copybot`.
- **P&L is honest end-to-end.** The sharps table's All-Time P&L is now the
  realized track record (sum of Polymarket `realizedPnl` per closed position)
  PLUS abandoned-loser fold-in (`_open_split` in validate_timing) — matches PM
  where PM is honest, reads lower where PM hides abandoned losers (oliman2 $181k
  →$20k). 27/31 sharps match PM within a few %, 4 honestly-below, **0
  over-count**. New **Open P&L** column = genuine in-flight exposure.
- **Ledger reconciles** (`ledger_drift` 0.00, self-checked every heartbeat).
- **Daily pipeline** (launchd 08:00) runs the corrected code; exits cache is
  backfilled (86/96 wallets `complete`, incremental from here). Verified wired.
- **Data caches**: `live/cache.duckdb` — `bets` (still windowed/capped, used for
  z-score selection only), `exits` (FULL history, incremental, `realized_pnl`),
  `resolutions` (chain payouts). `closed_exits` is the shared exit/realized
  source for both the backtest and the sharps table.

## THE PROPER FIX (do before real money) — the orphan / cash-debit gap

**Symptom seen 2026-07-08:** after the Set D deploy the ledger self-check flagged
`⚠ LEDGER DRIFT $+36.35`. Root: a position ("Hanyi Liu vs Aoran Wang: Total
Sets", ArbTrader — a now-dropped wallet) sat in `state["my_pos"]` but its cost
was **never debited from `state["cash"]`** and it had **no bet record and no
`conds` entry**. A second market on the same event, sold, was left `status:open`
(a "ghost"). Both were cleaned by hand (`copybot_state.json` surgery, committed);
drift is 0 now. But the *mechanism that creates orphans is guarded, not fixed.*

**Why it happens (as far as traced):** `on_wallet_activity` books cash + creates
the bet record via:
```python
for f in self._drain_fills():          # _drain_fills debits cash for EVERY fill
    if f["token"] == tok:              # but _record_lag (the bet record) fires
        self._record_lag(wallet, t, f) # ONLY for the fill matching THIS trade's token
```
When two markets of one event are processed in a batch (or a fill is left in
`ex.fills` across iterations, or an exception fires between `handle_trade` and
`_drain_fills`), a buy fill can end up **drained-but-not-recorded** (no bet
record), or **added-to-my_pos-but-never-drained** (cost not debited). The
`_record_lag` `if f["token"] == tok` filter is the fragile seam.

**Guards already in place (make recurrence VISIBLE, not silent):**
- `Copybot.ledger_drift()` — checked every heartbeat, logged `⚠ LEDGER DRIFT`,
  published in the feed (`ledger_drift` field). Catches the cash gap in ≤60s.
- `write_feed` synthesizes a bet record for any open `my_pos` position missing
  one (so `open_count` always matches the visible table).
- `my_pos` entries now carry `wallet` (copytrade.py) for attribution.

**The proper fix — two options, recommend A for Phase 1:**

**Option A (surgical, lower risk):** in `on_wallet_activity`, record EVERY
drained buy fill, not just the `tok`-matching one. The tok-match gets
`_record_lag` (real lag/slippage); others get a bet record synthesized from the
fill (lag unknown). AND assert the invariant after each trade: every `my_pos`
token has (cost in cash via a drained fill) + (bet record) + (`conds` entry);
if not, log and self-correct. This closes the "drained-but-not-recorded" and
"added-but-not-drained" seams without touching the cash model.

**Option B (architectural, eliminates the class):** stop *accumulating*
`state["cash"]` per fill; **derive** it each cycle from the book:
`cash = bankroll + Σ(settled bet pnl) − Σ(open my_pos cost+fee) + adjustments`.
Then `ledger_drift` is 0 by construction and an orphan simply (correctly)
reduces available cash. Higher effort — the derivation must reproduce every flow
the accumulation handles (entry fee, sold-leg proceeds, refund/settlement
payoffs, redeems). Worth it long-term; risky to rush before real money.

**To reproduce/investigate:** add a log line in `_handle_their_buy` on every
`my_pos` add with `len(executor.fills)` before/after, and in `_drain_fills` log
each fill booked. Watch a batch where one wallet trades ≥2 markets of the same
event in <10 min (ITF tennis sub-markets, in-play soccer). The orphan appears
when the second market's fill misses `_record_lag`.

## Phase 1 remaining (LIVE_ROLLOUT.md) — 6 of 8 done

Done + verified: 1.1 feed separation, 1.2 env secrets, 1.4 on-chain cash anchor
(`chain_cash_gap`), 1.5 fatal geocheck (refuses from a blocked box), 1.6 exit
retries (`⚠ EXIT STUCK`), 1.7 config sync (`config.live.example.json`).

**1.3 — live deps in the worker image (NOT done).** `fly.Dockerfile` is
stdlib-only. Add `pip install py-clob-client web3` (pin versions) — needed by
`LedgerLiveExecutor` + `redeem.py`. Keep the paper path import-lazy (already is).
Verify: `flyctl deploy --remote-only`, then
`flyctl ssh console -C "python3 -c 'import py_clob_client, web3'"` exits 0, and
the paper bot still boots clean (banner + heartbeat, drift 0.00).

**1.8 — dashboard live-money section (NOT done).** `trading/index.html` (in the
`jaxperro` repo) should read `live/copybot_live_real.json` (the live feed path
from 1.1, set in `config.live.example.json`) into a fourth board chip + tables,
clearly labeled REAL MONEY, reusing the standardized columns (Their/Our Entry,
Stake, Result W/L/R/S, Placed/Settled) and the `ledger_drift`/`adjustments`
fields. Verify against a copy of the paper feed with `"mode":"live"`; then
confirm graceful empty-state when the file 404s (test not started).

After Phase 1: Phase 2 is funding (USER ONLY), then preflight → supervised
first fill → the edge-case matrix (LIVE_ROLLOUT.md Phase 5).

## Standing to-dos (user-only or external)

- **ROTATE THE LEAKED ALCHEMY KEY.** A real key briefly hit the public repo
  (`config.live.example.json`, removed from HEAD but in git history). Generate a
  new key at dashboard.alchemy.com, then update `alchemy_key` in the local
  gitignored `config.json` AND the `ALCHEMY_RPC_URL` Fly secret
  (`flyctl secrets set ALCHEMY_RPC_URL=...`). Blocks nothing but is a live key.
- Delete the stopped Railway project `magnificent-kindness` when comfortable
  (saves the idle spend; the Fly worker fully replaced it).

## Operational quick-reference

- Change the follow set: edit `live/copybot.paper.json` `wallets`, then
  `./live/deploy_bot.sh` (validates → pins floors → syncs Alchemy webhook →
  restarts Fly → confirms banner). `backtest.json` should mirror it.
- Backtest any set: `python3 live/portfolio.py --wallets 0xa,0xb --days 30
  --out /tmp/t.json`.
- Never keep 2 Fly machines (2 book-writers): `flyctl scale count 1`.
- Cache is single-writer; don't run cache-touching scripts while the daily
  pipeline or a regen is running.
- Bot commits its own state/feed every few minutes — always
  `git pull --rebase --autostash` before pushing.
