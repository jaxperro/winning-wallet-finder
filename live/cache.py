#!/usr/bin/env python3
"""Local cache of per-wallet resolved bets, so we stop re-pulling the data-api.

Each wallet's resolved bets (won, entry price p, conditionId, resolution time,
size) are stored once in cache.duckdb. Because we keep res_t per bet, ANY date
cutoff — pre-June-1, full window, future experiments — reads the same cached
rows and filters locally. A pull only happens for wallets not seen, or older
than MAX_AGE_DAYS.

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
    wallet TEXT, cond TEXT, won BOOLEAN, p DOUBLE, res_t BIGINT, size DOUBLE)""")
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


def get_bets(wallet):
    """Resolved bets for a wallet — from cache if fresh, else pull and store."""
    now = time.time()
    with _lock:
        r = _con.execute("SELECT pulled_at FROM pulled WHERE wallet=?", [wallet]).fetchone()
        if r and now - r[0] < MAX_AGE_DAYS * 86400:
            rows = _con.execute(
                "SELECT won,p,cond,res_t,size FROM bets WHERE wallet=?", [wallet]).fetchall()
            return [{"won": w, "p": p, "cond": c, "res_t": rt, "size": s}
                    for w, p, c, rt, s in rows]
    # cache miss / stale -> pull (slow, outside the lock so workers stay parallel)
    try:
        bets = insider.resolved_bets(wallet, now - WINDOW_DAYS * 86400)
    except Exception:
        bets = []
    with _lock:
        _con.execute("DELETE FROM bets WHERE wallet=?", [wallet])
        if bets:
            _con.executemany(
                "INSERT INTO bets(wallet,cond,won,p,res_t,size) VALUES (?,?,?,?,?,?)",
                [(wallet, b["cond"], b["won"], b["p"], b.get("res_t"), b.get("size"))
                 for b in bets])
        _con.execute("INSERT OR REPLACE INTO pulled VALUES (?,?)", [wallet, int(now)])
    return bets


def invalidate(wallets):
    """Force a re-pull of these wallets on next get_bets (for daily watchlist
    forward-refresh)."""
    with _lock:
        for w in wallets:
            _con.execute("DELETE FROM pulled WHERE wallet=?", [w])


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
