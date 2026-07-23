# Etherscan migration — true resolution time through the whole stack

**Goal:** every consumer of `res_t` reads the exact on-chain
`ConditionResolution` timestamp instead of endDate metadata. Measured rot
that motivates this (2026-07-08): the Jul-7 Brewers market carried
`end_date_iso` **2026-05-05** and cache res_t **29h before** true resolution
(04:51 next morning); a Jul-5 tennis match carried res_t **Jul-14** (future);
an entire class of in-play sells could never pass a timestamp test.
`payouts.resolution_time(cond)` (wired, verified) is the source; this plan
is the *consumption* migration. **No code until the plan is agreed.**

## Ground rules (scar tissue from the 2026-07-08 alignment audit)

1. **Overlay, never rewrite.** Chain timestamps live in `resolution_times`
   (own table). `bets.res_t` is never mutated — provenance stays intact,
   every flip is a one-line revert per consumer.
2. **One consumer per phase**, each with a before/after diff reviewed
   *before* publishing. Never two flips in one day.
3. **Never couple a flip with a follow-set change** — attribution dies.
4. **Calibration continuity:** any phase that changes model output adds a
   note (and a `rules_version` marker) to `live/history/calibration.csv`
   that day, so the live-vs-model series stays interpretable.
5. Docs land with the phase, not after: FINDINGS gets the measured deltas,
   README gotchas get amendments, HANDOFF tracks the phase counter.

## Scale facts (measured 2026-07-08)

- 974,155 distinct conds in the bets cache; **783,911 resolved**;
  141,396 in exits; 66,095 in `resolutions` (payout cache); 1 in
  `resolution_times` (the Brewers proof).
- Per-cond Etherscan queries are a non-starter: 784k ÷ 5 req/s ≈ 43h of
  calls against a 100k/day cap ≈ **8 days**.
- **The chain sweep is the enabler:** ONE topic0-only `getLogs` walk over
  the CTF contract returns every ConditionResolution ever, 1,000 events per
  call ≈ **~1,000 calls ≈ minutes**, resumable by block cursor. Nightly
  increment afterward = 1–3 calls in `daily.sh`.
- `res_t` consumer weight (grep): trust.py 26 · portfolio.py 23 ·
  cache.py 11 · conviction_scan.py 5 · validate_timing.py 4 · skill.py 4 ·
  sync_floors.py 1. Flip order below runs shallowest → deepest.

## Phase 0 — full-chain backfill (enabler, no behavior change)

Build `live/resolution_sweep.py`: windowed topic0-only walker over the CTF
contract (chainid=137), resumable `last_block` cursor, throttled ≤5 req/s,
writes `(cond, res_ts)` into `resolution_times`. Wire the incremental call
into `daily.sh` (cache is single-writer — it runs inside the pipeline slot).

Exit criteria / audits before any consumer flips:
- **Coverage:** ≥99% of *resolved* conds that appear in any scoring path
  (trusted rows for scanned wallets, exits, portfolio replay) have a chain
  timestamp; sample the misses by hand.
- **negRisk verification:** confirm ConditionResolution fires for negRisk
  conds (oracle = the adapter) on a handful of known negRisk markets.
- **Uniqueness:** assert one event per cond in the sweep (if re-reports
  exist, keep the LAST and document).
- **The rot histogram:** distribution of `res_ts − metadata res_t` across
  all matched conds → FINDINGS. This quantifies 18 months of silent error
  and predicts which downstream metrics will move.

## Phase 1 — read-only shadow audit (no behavior change)

Recompute both ways, publish nothing:
1. **validate_timing shadow:** med_lead_h and the held/timing gates with
   chain res_ts — does the 35-sharp membership change? Which wallets flip?
2. **portfolio shadow:** replay with (a) settle/capital-recycle at true
   res_ts (today's game-day-midnight settles free capital EARLY — expect an
   equity haircut from honest capital lock), (b) timestamp sold-test
   primary with the price test retained as fallback. Record equity/record
   deltas per wallet.
3. **trust shadow:** agreement rate between consensus res_t and chain
   res_ts — the empirical case for how much of trust.py survives Phase 5.

Deliverable: one FINDINGS section with the three deltas. Flip order below
is confirmed or reshuffled *on this data*.

## Phase 2 — flip validate_timing (lowest stakes)

Lead metrics + timing gates read chain res_ts (fallback: old logic where a
cond has no event, e.g. genuinely unresolved). If sharp-list membership
changes, that's honest — document the entrants/leavers like a follow-set
change review. Sharps table republished; dashboards unchanged structurally.

## Phase 3 — flip portfolio.py (moves published numbers)

Settle times, sold-test primary, threshold windows (`res_t < START`) all
read chain res_ts. Then: full row-audit vs the live bot book (the 7/9-style
check — agreement must not regress), republish, calibration note (rule 4).
Expect and pre-announce the equity change; the honest number wins.

## Phase 4 — flip conviction_scan + skill + floors (selection layer)

Train/test splits and conviction windows on chain res_ts. Floors re-pin via
`sync_floors` and may shift a few % → coordinate a same-day
`deploy_bot.sh` so bot and backtest keep gating identically (rule 3 still
holds: no wallet adds/drops that day).

## Phase 5 — simplify trust.py (LAST, biggest payoff)

With chain res_ts + chain payouts covering all scoring conds, the consensus
res_t machinery, the v2 self-certification split, and the 14-day pulled_at
fallback reduce to: *"resolved on-chain? what payout? when?"* Keep consensus
only as a fallback for conds absent from chain (Phase 0 says ~none).
Regression bar: the sharp list before/after must be identical or every
difference individually explained. This deletes the most complex code in
the repo — do it after the chain data has soaked a week+.

## Parallel track (independent, any time)

Port insider.py's funding-cluster tracer from Alchemy `getLogs` (now capped
at 10 blocks on free tier) to Etherscan V2 logs. No interaction with the
res_t work.

## Risks

| risk | mitigation |
|------|------------|
| Etherscan limits/downtime | cache-forever table; resumable cursor; env `ETHERSCAN_KEY` override; sweep re-runnable idempotently |
| negRisk event semantics differ | Phase 0 verification gate before any flip |
| UMA disputes / duplicate events | uniqueness assert in Phase 0; disputes precede first report, immutable after |
| DuckDB single-writer | sweep runs in the daily pipeline slot only |
| numbers move and surprise | shadow audit first; pre-announced deltas; rules_version in calibration.csv |
| Etherscan V2 API drift | pin chainid=137 param form; smoke test in sweep preamble |

## Sequencing

Phase 0+1 fit one session (~2–3h, mostly sweep runtime + audit reads).
Each flip (2, 3, 4) is its own session with regen + publish + docs.
Phase 5 waits a week+ of soak. Total: ~4 working sessions spread over
~2 weeks, zero downtime, every step revertible.
