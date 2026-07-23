# ── POST-MORTEM (2026-07-19): EXPERIMENT CLOSED — HYPOTHESIS REFUTED ──
Killed by the pre-registered criterion, decisively: 994 resolved tickets,
**1W / 993L, 0.075x realized multiple** (paper bank $1,000 spent; 32 tickets
/ ~$32 still open at shutdown — even if ALL win, the multiple caps ~0.1x).
The v0 sub-2¢ mix AND the v0.1 1-2c band both failed live-paper despite the
cache showing 1.24-1.28x historically. Expected wins at the historical rate:
~12; observed: 1 (p ≈ 0 — not variance).

VERDICT: today's sub-2¢ asks are near-perfectly informed. The 2025-era
calibration edge no longer exists in the current market — the visible cheap
tail is pure adverse selection now. The honest FAK fill model + $1 stakes
made this a ~$1,000-paper-dollar lesson instead of a real one; the fill
model, chain settles, and silo pattern all worked exactly as designed and
are reusable for the next hypothesis (tape-derived flow signals).
Fly app destroyed 2026-07-19; feed/state/dashboard remain as the record.

# VALUE — undervalued-market bot (silo'd from the copy trader)

Status: RESEARCH + PLAN (2026-07-17). No bot code yet. USER directives:
copy-bot playbook (paper first → real money), paper must mimic reality as
closely as possible, and a HARD SILO — nothing here may touch or impact the
copy trader. Own dashboard page eventually (jaxperro.com/value).

## Phase-1 research findings (calibration study, 2026-07-17)

Data: 13.5M TRUSTED resolved bet rows (trust.py CTE — the res_t=ts poison and
stale marks excluded), 26.2M raw, read-only against live/cache.duckdb.

1. **The market is well-calibrated where the copybot lives.** 60¢–98¢
   realized-vs-price gaps are ±0.3% and NEGATIVE net of the 4% fee model.
   There is NO simple "buy favorites" edge. (Also independently validates the
   copybot's price band: no systematic juice was left on the table.)
2. **Longshots 2¢–60¢ are systematically OVERPRICED** (gap −1.7% to −3.5%,
   cluster-conservative z −9 to −42). Blanket longshot buying is a donation.
3. **THE ANOMALY: sub-2¢ contracts are UNDERPRICED.**
   - Full set: n=696k bets / 291k markets, avg entry 0.82¢, realized 1.02%
     → 1.24x gross, ≈ +20%/$ net of entry fee, z≈+12 (market-clustered).
   - Survives WITHOUT our skilled wallets: rows from wallets outside the
     94-wallet skilled pool, on market-sides NO skilled wallet touched:
     1.27x, +23%/$ net (n=648k / 286k markets).
   - NOT refunds: on the payout-covered subset only 41/153k rows were 50/50
     refunds (each does mark won=TRUE — the boolean lies exactly as feared,
     but the frequency is negligible). Refund-aware TRUE EV on that subset:
     entry 0.85¢ → EV 1.31¢/share = 1.55x gross, ~+50%/$ net. Covered
     subset skews rich (it's markets our pipeline settled); the honest
     global estimate stays ~1.24–1.27x.
   - Horizon: the effect is strongest at SHORT time-to-resolution (<1h and
     1h–1d), i.e. in-play/near-expiry dust.
   - Skilled wallets at sub-2¢ run 2.85x (n=5.5k) — the informed-niche
     effect on top of the structural one; that part is signal A's business
     (smart-money scanner), not this bot's.

**Interpretation**: below ~2¢ the favorite-longshot bias INVERTS — consistent
with sellers harvesting "sure" pennies (selling 99¢ completes = supplying the
1¢ side) pushing tails below fair. Per-bet payoff is 50–120x with ~1% hit
rate: massive variance, only harvestable as a LAW-OF-LARGE-NUMBERS portfolio
(hundreds of independent markets), tiny per-market stakes.

**What the data CANNOT tell us (= why the paper bot exists):**
- EXECUTABILITY: cached fills are prices people actually got, but dust books
  are thin (our own depth gate: ask5c<$50 = "mispriced every fill"). Whether
  $1–5/market is fillable at ≤2¢ TODAY, at FAK, without moving the book, is
  unknowable from history. This is the #1 risk and the paper bot's #1 job.
- Era drift: the cache spans months; the bias may have closed.
- Correlation: same-event dust tickets resolve together (event cap needed).

## Strategy V0 (paper only): systematic sub-2¢ portfolio

- Universe: every active market with an ask ≤ 2¢ (gamma/CLOB scan; exclude
  markets resolving <X min out if the book is one-sided — parameter, not
  dogma; the horizon data says near-expiry is where the edge LIVES, so no
  blanket exclusion).
- Entry: FAK-modeled buy at the ask, protected band exactly like the live
  executor would send. Stake: flat $1–2/market (venue min), hard event cap 1
  (correlated dust resolves together), portfolio cap N open.
- Exit: hold to resolution (redeem is fee-free; the whole edge is terminal).
  Settle at CHAIN truth via payouts.py vectors (0.5 refunds are real).
- Measurement: edge-vs-hurdle daily from day 1 (the copybot's edge.py
  pattern). Break-even hit rate at 1¢ ≈ price×(1+fee drag) ≈ 1.05% — the
  book needs ~2,000+ resolved tickets for a significant verdict; at ~1–3k
  qualifying markets/day observed in cache era, ~2–6 weeks of paper.

Signal A (smart-money consensus scanner) is a SEPARATE later deliverable —
dashboard watchlist first, never auto-traded from this silo.

## Silo architecture (mirrors the copybot playbook, shares nothing live)

- **Code**: new files under `value/` ONLY. May import the read-only research
  libs (`trust`, `payouts`, `smart_money` GETs). MUST NOT import copybot.py
  or copytrade.py (book/fee helpers get duplicated into `value/` — ~40 lines
  bought for total blast-radius isolation). No shared state, no shared feed,
  no shared webhook, no shared Fly app.
- **Runtime**: own Fly app (`wwf-valuebot`, arn) cloning this repo at boot,
  same clone-guard pattern. Poll-driven (gamma universe scan + book reads);
  needs NO Alchemy/RTDS — detection latency is irrelevant to a standing
  limit-universe strategy.
- **State/feed**: `value/valuebot_state.json`, feed `value/valuebot.json`,
  fills `value/valuebot_fills.jsonl` — committed by ITS OWN pushes, same
  pull-rebase-push discipline. Paper realism from day 1: FAK-vs-book fill
  model (no ask inside band = MISS, the 2026-07-16 parity lesson), category
  fee rates, depth-gated stakes, refund-aware chain settles, honest missed
  ledger, daily edge row.
- **Dashboard**: later, jaxperro.com/value reading the feed — separate page,
  separate feed file; the trading/live pages are untouched.
- **Ops**: HANDOFF.md stays the shared session log; this file is the value
  bot's own state-of-the-world.

## Rollout gates (copybot playbook)

1. PAPER (build next): 2–6 weeks; verdict = realized portfolio ROI vs the
   ~1.05% break-even hit rate with real fill misses counted.
2. KILL criteria: fill rate <30% of attempts (the edge exists but isn't
   buyable), or realized multiple <1.1x after 2k resolved tickets, or the
   paper book can't deploy >$50/week at $1–2 stakes (too thin to matter).
3. REAL MONEY: only after paper verdict + user go; separate wallet, separate
   caps conversation. Never funded from or through the copy-trader wallet.

## Open research (phase 2, before/alongside paper build)

- Maker-vs-taker split: can resting 1¢ bids harvest the same edge with
  NEGATIVE fee (maker) instead of paying the taker rate? Adverse-selection
  test needed (the copybot's resting-limit analysis says beware).
- Era stability: recompute the bucket table on trailing 30/60/90d windows.
- Category cut once slugs are joined (slug_cache/gamma) — is it all esports
  comebacks, or broad?
