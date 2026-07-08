#!/usr/bin/env python3
"""Chain-truth resolution overlay for cache.duckdb — the 2026-07-06 audit fix.

The cache's `won` is `curPrice >= 0.5` at pull time, and that lies two ways
(see the audit + [[polymarket-resolution-truth]] in project memory):

  * 50/50 REFUNDS (walkovers/abandonments — 28% of the follow set's resolved
    markets in the 30d sample!) leave curPrice at 0.5, so EVERY holder of
    EITHER side gets won=True and (1-p)/p phantom profit. The whales' 92-100%
    displayed win rates were largely this.
  * operator-resolved in-play markets can leave a stale mark on the losing
    side (one confirmed both-sides-won market), which resolved=TRUE certifies.

The fix keeps the cache AS PULLED and overlays the on-chain truth: the CTF
contract's payout vector per condition, plus the market's token order so a
row's `asset` maps to its payout. Resolved payouts are immutable — fetched
once, cached in a `resolutions` table forever. Unresolved conditions recheck
after RECHECK_S.

Usage:
    import payouts
    payouts.ensure(conds)              # batch-backfill (chain + CLOB, cached)
    wp = payouts.truth(cond, asset)    # 1.0 / 0.0 / 0.5 / None (unknown)

Scoring rule for consumers: truth 1/0 -> real win/loss; 0.5 -> REFUND, count
as neither and P&L = size*(0.5-p)/p; None -> fall back to the cache's `won`
(legacy NULL-asset rows, unresolved markets, RPC gaps).

RPC: config.json `alchemy_key` (or ALCHEMY_RPC_URL env). Batched JSON-RPC
(BATCH per POST); the CLOB market fetch supplies token order. Selection runs
on the Mac, so this module is never needed by the Fly worker.
"""
import json
import os
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import cache

_SSL = ssl._create_unverified_context()
HERE = os.path.dirname(__file__)
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
SEL_DEN = "0xdd34de67"    # payoutDenominator(bytes32)
SEL_NUM = "0x0504c814"    # payoutNumerators(bytes32,uint256)
BATCH = 60                # JSON-RPC calls per POST
PACE_S = float(os.environ.get("PAYOUTS_PACE_S", 5.0))   # sleep between batches:
                          # free-tier Alchemy sustains ~12 eth_calls/s — an
                          # unpaced backfill just trades 429 retries for gaps
RECHECK_S = 6 * 3600      # re-ask the chain about unresolved conds after this

cache.query("""CREATE TABLE IF NOT EXISTS resolutions(
    cond TEXT PRIMARY KEY, p0 DOUBLE, p1 DOUBLE,
    token0 TEXT, token1 TEXT, checked_at BIGINT)""")

_mem = {}                 # cond -> row tuple, warm in-process copy
_lock = threading.Lock()


def _rpc_url():
    url = os.environ.get("ALCHEMY_RPC_URL")
    if url:
        return url
    try:
        key = json.load(open(os.path.join(HERE, "..", "config.json")))["alchemy_key"]
        return f"https://polygon-mainnet.g.alchemy.com/v2/{key}"
    except Exception:
        return None


def _rpc_batch(calls):
    """calls: [hex calldata] -> [int results or None], one batched POST."""
    url = _rpc_url()
    if not url:
        return [None] * len(calls)
    body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                        "params": [{"to": CTF, "data": d}, "latest"]}
                       for i, d in enumerate(calls)]).encode()
    # retry with backoff — Alchemy 429s a fast backfill after ~2 batches, and a
    # failed batch must surface as None (skip-this-run), never "unresolved"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            rs = json.loads(urllib.request.urlopen(req, timeout=30, context=_SSL).read())
            if isinstance(rs, dict):                  # whole-batch error object
                raise OSError(str(rs.get("error"))[:80])
            by_id = {r.get("id"): r for r in rs}
            out = []
            for i in range(len(calls)):
                v = by_id.get(i, {}).get("result")
                try:
                    # "0x" (revert/empty) must not kill the whole batch — one
                    # bad item used to blank every cond in the chunk
                    out.append(int(v, 16) if v and v != "0x" else None)
                except (TypeError, ValueError):
                    out.append(None)
            return out
        except Exception:
            if attempt == 3:
                return [None] * len(calls)
            time.sleep(2 ** attempt)


def _clob_tokens(cond):
    """(token0, token1) in the CLOB market's outcome order — payout index order."""
    try:
        req = urllib.request.Request("https://clob.polymarket.com/markets/" + cond,
                                     headers={"User-Agent": "Mozilla/5.0"})
        m = json.loads(urllib.request.urlopen(req, timeout=15, context=_SSL).read())
        toks = [str(t.get("token_id")) for t in (m.get("tokens") or [])[:2]]
        return (toks + [None, None])[:2]
    except Exception:
        return None, None


def _load(conds):
    with _lock:
        missing = [c for c in conds if c and c not in _mem]
    if missing:
        rows = cache.query(
            "SELECT cond, p0, p1, token0, token1, checked_at FROM resolutions "
            "WHERE cond IN (SELECT UNNEST(?::VARCHAR[]))", [missing])
        with _lock:
            for r in rows:
                _mem[r[0]] = r


def ensure(conds, workers=8):
    """Make sure every cond has a resolutions row (fetching chain + CLOB for the
    unknown ones). Resolved rows are permanent; unresolved recheck after
    RECHECK_S. Safe to call with thousands of conds — everything is cached."""
    conds = [c for c in {c for c in conds if c}]
    _load(conds)
    now = int(time.time())
    with _lock:
        todo = [c for c in conds
                if c not in _mem
                or (_mem[c][1] is None and now - (_mem[c][5] or 0) > RECHECK_S)]
    if not todo:
        return
    # chain payouts, batched: den + num0 per cond (binary: num1 = den - num0)
    payout = {}
    for i in range(0, len(todo), BATCH // 2):
        chunk = todo[i:i + BATCH // 2]
        calls = []
        for c in chunk:
            h = c[2:].rjust(64, "0")
            calls += [SEL_DEN + h,
                      SEL_NUM + h + "0".rjust(64, "0")]
        res = _rpc_batch(calls)
        for j, c in enumerate(chunk):
            den, n0 = res[2 * j], res[2 * j + 1]
            if den is None:
                payout[c] = "rpc-fail"      # do NOT cache — retry next run
            elif den and n0 is not None:
                payout[c] = (n0 / den, (den - n0) / den)
            else:
                payout[c] = (None, None)    # chain says: not resolved yet
        if i + BATCH // 2 < len(todo):
            time.sleep(PACE_S)
    # token order for the RESOLVED ones (index -> asset mapping); cached forever
    resolved = [c for c in todo
                if isinstance(payout.get(c), tuple) and payout[c][0] is not None]
    tokens = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for c, tk in zip(resolved, ex.map(_clob_tokens, resolved)):
            tokens[c] = tk
    with _lock:
        for c in todo:
            p = payout.get(c)
            if p == "rpc-fail" or p is None:
                continue                    # transient — leave uncached, retry next run
            t0, t1 = tokens.get(c, (None, None))
            row = (c, p[0], p[1], t0, t1, now)
            _mem[c] = row
            cache.query("INSERT OR REPLACE INTO resolutions VALUES (?,?,?,?,?,?)", list(row))


def truth(cond, asset=None):
    """Chain-truth payout of this position: 1.0 / 0.0 / 0.5 / None (unknown).
    Refunds ([0.5,0.5]) need no asset; decided markets map asset -> index."""
    _load([cond])
    r = _mem.get(cond)
    if not r or r[1] is None:
        return None
    p0, p1, t0, t1 = r[1], r[2], r[3], r[4]
    if p0 == p1:                       # refund (or exotic even split)
        return p0
    if asset is not None:
        if str(asset) == str(t0):
            return p0
        if str(asset) == str(t1):
            return p1
    return None                        # decided, but we can't map the side


def stats():
    n = cache.query("SELECT count(*), count(p0) FROM resolutions")[0]
    return {"rows": n[0], "resolved": n[1]}


if __name__ == "__main__":
    print(stats())


# ---- exact resolution TIME (Etherscan V2 logs; wired 2026-07-08) ------------
# CLOB/gamma carry NO true resolution moment (end_date_iso can be months off —
# the Jul-7 Brewers game carried 2026-05-05 — and cached res_t is endDate
# metadata, 29h wrong on that market). The only truth is the CTF
# ConditionResolution event. Alchemy's FREE tier caps eth_getLogs at 10
# blocks (discovered 2026-07-08), so this goes through Etherscan V2
# (chainid=137, wide-range topic-filtered logs, free `etherscan_key` in the
# gitignored config.json). Cached forever — resolution events are immutable.
# Free-tier limits: 5 req/s, 100k/day — throttle any bulk backfill.
cache.query("CREATE TABLE IF NOT EXISTS resolution_times(cond TEXT PRIMARY KEY, res_ts BIGINT)")

_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_RES_TOPIC = "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"


def resolution_time(cond):
    """Exact on-chain resolution timestamp for a condition — None while the
    market is unresolved or without an etherscan_key. First caller pays one
    Etherscan query; cached forever after."""
    r = cache.query("SELECT res_ts FROM resolution_times WHERE cond=?", [cond])
    if r:
        return r[0][0]
    key = os.environ.get("ETHERSCAN_KEY")
    if not key:
        try:
            key = json.load(open(os.path.join(HERE, "..", "config.json"))).get("etherscan_key")
        except Exception:
            key = None
    if not key:
        return None
    url = ("https://api.etherscan.io/v2/api?chainid=137&module=logs&action=getLogs"
           f"&address={_CTF}&topic0={_RES_TOPIC}&topic0_1_opr=and&topic1={cond}"
           f"&fromBlock=0&toBlock=latest&apikey={key}")
    try:
        resp = json.load(urllib.request.urlopen(url, timeout=30, context=_SSL))
        res = resp.get("result") or []
        if resp.get("status") == "1" and res:
            ts = int(res[0]["timeStamp"], 16)
            cache.query("INSERT OR REPLACE INTO resolution_times VALUES (?,?)", [cond, ts])
            return ts
    except Exception:
        pass
    return None
