#!/usr/bin/env python3
"""Trusted-row filtering for cache.duckdb — the 2026-07-03 data-integrity fix.

Two ways a cached bet row can lie (see FINDINGS.md "The holder blind spot"):

  1. res_t = ts fallback: when the data-api omits `endDate` on a closed
     position, insider.resolved_bets stores the wallet's SELL time as res_t and
     `won = curPrice >= 0.5` at pull time. A scalper's sold-at-profit position
     then masquerades as a resolved win at a fake resolution time
     (ArbTraderRookie's 1,997 legacy rows were 100% this).
  2. stale marks: `won` is only authoritative if the wallet was pulled AFTER
     the market resolved; rows pulled earlier carry a price mark, and v2 rows
     say so via resolved=False, but legacy rows can't.

A row is TRUSTED iff res_t <= now AND either:

  * **v2 self-certifying**: resolved = TRUE. resolved is only True when the
    pull saw a real endDate AND the market had ended, so res_t is endDate-based
    (never the ts fallback) and won was observed post-end. No consensus needed —
    important because Polymarket sometimes REWRITES endDate after resolution,
    so a freshly-pulled wallet's corrected res_t stops matching the stale
    cross-wallet mode (2026-07-04: this wrongly distrusted 374/454 of iohihoo's
    rows the morning after his re-pull).
  * **legacy consensus** (resolved IS NULL, pre-v2 rows): res_t equals the
    market's modal res_t across >= 2 distinct wallets (endDate rows agree;
    ts-fallback sell times scatter), and the wallet's pulled_at >= E so won was
    observed after resolution. If the wallet is missing from `pulled` (daily.sh
    invalidates watchlist wallets by DELETING their pulled row; a transiently
    failed re-pull then leaves them absent), fall back to trusting markets
    resolved >= 14 days ago — every wallet gets re-pulled at least each
    MAX_AGE_DAYS cycle, so its rows' true pull time is at most ~2 weeks old.

  (resolved = FALSE rows — price marks on unended markets — are never trusted.)

~13.5M of 19.2M rows pass; what's dropped is exactly the poison that made
scalpers look like 99%-win holders. Selection must only ever score trusted rows.

This module has NO cache.py import (so read-only scripts that open their own
connection can use it without a second in-process connection fighting the
single-writer lock). Callers pass a `runq(sql, params) -> rows` callable.
"""
import time

# CTE fragments: prepend inside `WITH ...` and select from `trusted`.
# {now} must be substituted with an int epoch.
TRUSTED_CTE = """
tr_r AS (SELECT DISTINCT wallet, cond, asset, won, p, res_t, size, src, ts, resolved
         FROM bets WHERE res_t > 0 AND size > 0),
tr_cons AS (SELECT cond, res_t AS E FROM (
    SELECT cond, res_t, count(DISTINCT wallet) nw,
           row_number() OVER (PARTITION BY cond
               ORDER BY count(DISTINCT wallet) DESC, count(*) DESC) rn
    FROM tr_r GROUP BY cond, res_t) WHERE rn = 1 AND nw >= 2),
trusted AS (
  SELECT tr_r.* FROM tr_r
  LEFT JOIN tr_cons ON tr_r.cond = tr_cons.cond AND tr_r.res_t = tr_cons.E
  LEFT JOIN pulled pl ON pl.wallet = tr_r.wallet
  WHERE tr_r.res_t <= {now} AND (
        tr_r.resolved = TRUE
     OR (tr_r.resolved IS NULL AND tr_cons.E IS NOT NULL
         AND tr_cons.E <= COALESCE(pl.pulled_at, {now} - 1209600))))
"""


def cte(now=None):
    """The trusted-rows CTE body with {now} filled in."""
    return TRUSTED_CTE.format(now=int(now or time.time()))


def ensure_cons(runq, now=None):
    """Materialize the consensus map once per connection as TEMP TABLE t_cons
    (cond, E) so repeated per-wallet queries don't re-scan 19M rows. Temp
    tables are allowed on read-only connections."""
    have = runq("SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 't_cons'", [])
    if have and have[0][0]:
        return
    runq(f"""CREATE TEMP TABLE t_cons AS
        WITH r AS (SELECT DISTINCT wallet, cond, res_t
                   FROM bets WHERE res_t > 0 AND size > 0)
        SELECT cond, res_t AS E FROM (
            SELECT cond, res_t, count(DISTINCT wallet) nw,
                   row_number() OVER (PARTITION BY cond
                       ORDER BY count(DISTINCT wallet) DESC, count(*) DESC) rn
            FROM r GROUP BY cond, res_t) WHERE rn = 1 AND nw >= 2
        AND res_t <= {int(now or time.time())}""", [])


def trusted_wallet_rows(runq, wallet, now=None):
    """This wallet's trusted resolved bets as (cond, won, p, res_t, size),
    deduped per token. Requires ensure_cons() first."""
    now = int(now or time.time())
    return runq("""
        SELECT DISTINCT b.cond, b.asset, b.won,
               least(0.999, greatest(0.001, b.p)) p, b.res_t, b.size
        FROM bets b
        LEFT JOIN t_cons c ON b.cond = c.cond AND b.res_t = c.E
        LEFT JOIN pulled pl ON pl.wallet = b.wallet
        WHERE b.wallet = ? AND b.size > 0 AND b.res_t > 0 AND b.res_t <= ?
          AND (b.resolved = TRUE
               OR (b.resolved IS NULL AND c.E IS NOT NULL
                   AND c.E <= COALESCE(pl.pulled_at, ? - 1209600)))""",
        [wallet, now, now])


def conviction_record(runq, wallet, days=90, pctile=0.80, now=None):
    """Trailing trusted CONVICTION record for the held-edge gate:
    {n, wr, roi} over the wallet's top-(1-pctile) stake bets resolved in the
    last `days`. The conviction cutoff is that wallet's stake p80 over its FULL
    trusted history (matching cache.conv_cutoff semantics). roi is the flat-
    stake hold-to-resolution copy ROI per bet, fee/slip-free (gates compare it
    to 0, and fees are already charged in copy_pnl, the other selection leg)."""
    now = int(now or time.time())
    rows = trusted_wallet_rows(runq, wallet, now)
    if not rows:
        return dict(n=0, wr=0.0, roi=0.0)
    sizes = sorted(r[5] for r in rows)
    k = (len(sizes) - 1) * pctile
    f = int(k)
    thr = sizes[f] if f + 1 >= len(sizes) else sizes[f] + (sizes[f + 1] - sizes[f]) * (k - f)
    cut = now - days * 86400
    # one bet per market: keep the largest-stake token row
    best = {}
    for cond, asset, won, p, res_t, size in rows:
        if size >= thr and res_t >= cut:
            if cond not in best or size > best[cond][2]:
                best[cond] = (won, p, size)
    conv = list(best.values())
    if not conv:
        return dict(n=0, wr=0.0, roi=0.0)
    wins = sum(1 for won, _, _ in conv if won)
    roi = sum(((1 - p) / p if won else -1.0) for won, p, _ in conv) / len(conv)
    return dict(n=len(conv), wr=wins / len(conv), roi=roi)
