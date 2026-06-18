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

_lock = threading.Lock()
_con = duckdb.connect(DB)
_con.execute("""CREATE TABLE IF NOT EXISTS bets(
    wallet TEXT, cond TEXT, won BOOLEAN, p DOUBLE, res_t BIGINT, size DOUBLE)""")
_con.execute("CREATE INDEX IF NOT EXISTS bets_w ON bets(wallet)")
_con.execute("CREATE TABLE IF NOT EXISTS pulled(wallet TEXT PRIMARY KEY, pulled_at BIGINT)")


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


def stats():
    with _lock:
        w = _con.execute("SELECT count(*) FROM pulled").fetchone()[0]
        b = _con.execute("SELECT count(*) FROM bets").fetchone()[0]
    return w, b


if __name__ == "__main__":
    w, b = stats()
    print(f"cache: {w:,} wallets, {b:,} bets in {DB}")
