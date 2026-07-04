#!/usr/bin/env python3
"""Local cache of per-wallet resolved bets, so we stop re-pulling the data-api.

Each wallet's resolved bets are stored once in cache.duckdb. Because we keep
res_t per bet, ANY date cutoff — pre-June-1, full window, future experiments —
reads the same cached rows and filters locally. A pull only happens for wallets
not seen, or older than MAX_AGE_DAYS.

Schema v2 (migrated automatically on first open; legacy rows keep NULLs in the
new columns until their wallet refreshes):
  * asset     — token id, the position identity. Dedupes the two-endpoint union
                (the same asset from /closed-positions AND /positions is one
                position seen twice) and disambiguates YES/NO both-sides rows.
  * src/ts    — endpoint provenance ('closed'/'open') + close timestamp.
  * resolved  — False for early-sold positions in markets that hadn't ended at
                pull time (their `won` is a curPrice mark, not an outcome).
  * p         — stored RAW (0 = avgPrice missing); get_bets clamps to
                [0.001, 0.999] on read, so consumers see the same values as
                before while the DB keeps missing-vs-real-longshot separable.
  * upsert    — refresh replaces only the re-pulled tokens instead of wiping
                the wallet, so history beyond the rolling WINDOW_DAYS pull
                accumulates (permanent archive instead of overwrite-on-refresh).
  * failures  — a failed pull is returned empty but NOT cached and NOT marked
                pulled, so it retries next call instead of masquerading as
                "wallet has no bets" for MAX_AGE_DAYS.

Thread-safe: API pulls (the slow part) run outside the lock; only the small
DuckDB reads/writes are serialized, so skill.py's worker pool still parallelizes
the network.
"""

import os
import sys
import threading
import time

import duckdb

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import insider  # noqa: E402

DB = os.path.join(os.path.dirname(__file__), "cache.duckdb")
WINDOW_DAYS = 180
MAX_AGE_DAYS = 14         # broad pool re-pulls only every 2 weeks; watchlist is
                         # force-refreshed daily via invalidate() (see daily.sh)

# CONVICTION = a bet in the top 20% of a wallet's OWN stake sizes (p80), replacing
# the old flat $200. Validated to reproduce flat-$200's win-rate lift (~74% vs ~51%
# on all bets) across the 23 sharps while adapting to each wallet's scale. Keep this
# in sync with trading/index.html's CONV_PCTILE / pctl().
CONV_PCTILE = 0.80


def conv_cutoff(sizes, q=CONV_PCTILE):
    """A wallet's conviction stake threshold: the q-quantile of its own positive
    bet sizes (linear interpolation, matching the dashboard's pctl). Bets with
    size >= this are conviction bets. Returns +inf if the wallet has no sized bets
    (so nothing qualifies)."""
    s = sorted(x for x in sizes if x and x > 0)
    if not s:
        return float("inf")
    k = (len(s) - 1) * q
    f = int(k)
    return s[f] if f + 1 >= len(s) else s[f] + (s[f + 1] - s[f]) * (k - f)

_lock = threading.Lock()
_con = duckdb.connect(DB)
_con.execute("""CREATE TABLE IF NOT EXISTS bets(
    wallet TEXT, cond TEXT, asset TEXT, won BOOLEAN, p DOUBLE, res_t BIGINT,
    size DOUBLE, src TEXT, ts BIGINT, resolved BOOLEAN)""")


def _migrate_v2():
    """One-shot in-place migration of a v1 `bets` table (no asset/src/ts/resolved
    columns). Rebuilds via SELECT DISTINCT — v1 had no position identity, so its
    few thousand byte-identical duplicate rows are unrecoverable noise and are
    merged. Legacy rows keep NULLs in the new columns until their wallet is
    re-pulled; `p` stays clamped for them (raw-p is forward-only)."""
    cols = {r[0] for r in _con.execute("DESCRIBE bets").fetchall()}
    if "asset" in cols:
        return
    n0 = _con.execute("SELECT count(*) FROM bets").fetchone()[0]
    _con.execute("BEGIN")
    _con.execute("""CREATE TABLE bets_v2(
        wallet TEXT, cond TEXT, asset TEXT, won BOOLEAN, p DOUBLE, res_t BIGINT,
        size DOUBLE, src TEXT, ts BIGINT, resolved BOOLEAN)""")
    _con.execute("""INSERT INTO bets_v2(wallet, cond, won, p, res_t, size)
                    SELECT DISTINCT wallet, cond, won, p, res_t, size FROM bets""")
    _con.execute("DROP TABLE bets")
    _con.execute("ALTER TABLE bets_v2 RENAME TO bets")
    _con.execute("COMMIT")
    n1 = _con.execute("SELECT count(*) FROM bets").fetchone()[0]
    print(f"[cache] migrated bets to schema v2: {n0:,} -> {n1:,} rows "
          f"({n0 - n1:,} exact duplicates merged)", flush=True)


_migrate_v2()
_con.execute("CREATE INDEX IF NOT EXISTS bets_w ON bets(wallet)")
_con.execute("CREATE TABLE IF NOT EXISTS pulled(wallet TEXT PRIMARY KEY, pulled_at BIGINT)")
_con.execute("CREATE TABLE IF NOT EXISTS entries(wallet TEXT, cond TEXT, first_buy BIGINT)")
_con.execute("CREATE INDEX IF NOT EXISTS entries_w ON entries(wallet)")
_con.execute("CREATE TABLE IF NOT EXISTS pulled_entries(wallet TEXT PRIMARY KEY, pulled_at BIGINT)")


def get_entries(wallet):
    """{conditionId: earliest BUY timestamp} for a wallet — cached. Lets us
    compute entry->resolution lead time and trade cadence (followability)."""
    now = time.time()
    with _lock:
        r = _con.execute("SELECT pulled_at FROM pulled_entries WHERE wallet=?", [wallet]).fetchone()
        if r and now - r[0] < MAX_AGE_DAYS * 86400:
            rows = _con.execute("SELECT cond,first_buy FROM entries WHERE wallet=?", [wallet]).fetchall()
            return {c: t for c, t in rows}
    try:
        first_buy, _ = insider.entry_times(wallet)
    except Exception:
        first_buy = {}
    with _lock:
        _con.execute("DELETE FROM entries WHERE wallet=?", [wallet])
        if first_buy:
            _con.executemany("INSERT INTO entries(wallet,cond,first_buy) VALUES (?,?,?)",
                             [(wallet, c, t) for c, t in first_buy.items()])
        _con.execute("INSERT OR REPLACE INTO pulled_entries VALUES (?,?)", [wallet, int(now)])
    return first_buy


def _bet_row(won, p, cond, res_t, size, asset, src, ts, resolved):
    """The dict shape get_bets returns — p clamped on read so consumer math is
    unchanged while the DB stores it raw."""
    return {"won": won, "p": max(0.001, min(0.999, p or 0)), "cond": cond,
            "res_t": res_t, "size": size, "asset": asset, "src": src,
            "ts": ts, "resolved": resolved}


def get_bets(wallet):
    """Resolved bets for a wallet — from cache if fresh, else pull and upsert."""
    now = time.time()
    with _lock:
        r = _con.execute("SELECT pulled_at FROM pulled WHERE wallet=?", [wallet]).fetchone()
        if r and now - r[0] < MAX_AGE_DAYS * 86400:
            rows = _con.execute(
                "SELECT won,p,cond,res_t,size,asset,src,ts,resolved "
                "FROM bets WHERE wallet=?", [wallet]).fetchall()
            return [_bet_row(*row) for row in rows]
    # cache miss / stale -> pull (slow, outside the lock so workers stay parallel)
    try:
        bets = insider.resolved_bets(wallet, now - WINDOW_DAYS * 86400, strict=True)
    except Exception:
        return []          # transient API failure — do NOT cache or mark pulled;
                           # the next call retries instead of trusting a bad pull
    # one row per token: the endpoint union returns the same asset twice for a
    # partially-closed position (closed portion + open remainder) — keep the
    # larger-stake row rather than double-counting one position as two bets.
    best = {}
    for b in bets:
        k = (b["cond"], b.get("asset"))
        if k not in best or (b.get("size") or 0) > (best[k].get("size") or 0):
            best[k] = b
    bets = list(best.values())
    with _lock:
        # upsert: replace only what this pull re-observed — re-pulled tokens, plus
        # any legacy (pre-v2, NULL-asset) rows of the re-pulled markets they
        # supersede. Rows older than the rolling pull window survive, so per-wallet
        # history now accumulates instead of being overwritten each refresh.
        assets = [b["asset"] for b in bets if b.get("asset")]
        conds = list({b["cond"] for b in bets if b.get("cond")})
        _con.execute(
            """DELETE FROM bets WHERE wallet = ?
               AND (asset IN (SELECT UNNEST(?::VARCHAR[]))
                    OR (asset IS NULL AND cond IN (SELECT UNNEST(?::VARCHAR[]))))""",
            [wallet, assets, conds])
        if bets:
            _con.executemany(
                "INSERT INTO bets(wallet,cond,asset,won,p,res_t,size,src,ts,resolved) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(wallet, b["cond"], b.get("asset"), b["won"], b.get("p"),
                  b.get("res_t"), b.get("size"), b.get("src"), b.get("ts"),
                  b.get("resolved")) for b in bets])
        _con.execute("INSERT OR REPLACE INTO pulled VALUES (?,?)", [wallet, int(now)])
        rows = _con.execute(
            "SELECT won,p,cond,res_t,size,asset,src,ts,resolved "
            "FROM bets WHERE wallet=?", [wallet]).fetchall()
    return [_bet_row(*row) for row in rows]


def invalidate(wallets):
    """Force a re-pull of these wallets on next get_bets (for daily watchlist
    forward-refresh)."""
    with _lock:
        for w in wallets:
            _con.execute("DELETE FROM pulled WHERE wallet=?", [w])


def query(sql, params=None):
    """Serialized raw read access to the cache DB for sibling modules (trust.py's
    trusted-row queries). A second duckdb.connect in the same process would fight
    this module's read-write connection for the single-writer lock, so everything
    in-process must go through this one connection."""
    with _lock:
        return _con.execute(sql, params or []).fetchall()


def pulled_ages():
    """{wallet: pulled_at} for every wallet ever pulled — lets collect.py bound
    how many stale re-pulls one run takes on."""
    with _lock:
        return dict(_con.execute("SELECT wallet, pulled_at FROM pulled").fetchall())


def stats():
    with _lock:
        w = _con.execute("SELECT count(*) FROM pulled").fetchone()[0]
        b = _con.execute("SELECT count(*) FROM bets").fetchone()[0]
    return w, b


if __name__ == "__main__":
    w, b = stats()
    print(f"cache: {w:,} wallets, {b:,} bets in {DB}")
