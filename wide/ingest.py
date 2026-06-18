#!/usr/bin/env python3
"""Bulk-ingest the Polymarket subgraph into a local DuckDB (pmkt.duckdb).

Each table is cursor-paginated by id and resumable: we checkpoint the last
id seen, and re-running continues where it left off (INSERT OR IGNORE makes
overlap harmless). All ranking/scoring happens later in SQL — see edge.sql.

    python3 ingest.py conditions market_data accounts   # the small tables
    python3 ingest.py market_positions                  # the heavy table
    python3 ingest.py all
"""

import queue
import sys
import threading
import time

import duckdb

import subgraph as sg

DB = "pmkt.duckdb"
BATCH = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS conditions (
    id TEXT PRIMARY KEY, resolution_ts BIGINT,
    payout_num TEXT, payout_den BIGINT, slots INT);
CREATE TABLE IF NOT EXISTS market_data (
    token_id TEXT PRIMARY KEY, condition_id TEXT, outcome_index INT);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, num_trades BIGINT, creation_ts BIGINT,
    scaled_profit DOUBLE, scaled_volume DOUBLE);
CREATE TABLE IF NOT EXISTS market_positions (
    id TEXT PRIMARY KEY, user_id TEXT, token_id TEXT,
    qty_bought HUGEINT, val_bought HUGEINT, net_qty HUGEINT);
CREATE TABLE IF NOT EXISTS _cursor (table_name TEXT PRIMARY KEY, last_id TEXT);
"""

# entity -> (graphql_fields, where_clause, row_mapper, target_table, columns)
def _i(x, d=0):
    try:
        return int(x)
    except (TypeError, ValueError):
        return d

def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d

SPECS = {
    "conditions": dict(
        entity="conditions",
        fields="id resolutionTimestamp payoutNumerators payoutDenominator outcomeSlotCount",
        where="",
        table="conditions",
        cols=("id", "resolution_ts", "payout_num", "payout_den", "slots"),
        row=lambda c: (c["id"], _i(c.get("resolutionTimestamp")),
                       ",".join(c.get("payoutNumerators") or []),
                       _i(c.get("payoutDenominator")), _i(c.get("outcomeSlotCount"))),
    ),
    # NOTE: market_data (token -> outcome) comes from Gamma, not the subgraph —
    # the subgraph's marketData.outcomeIndex is null. See gamma_tokens.py.
    "accounts": dict(
        entity="accounts",
        fields="id numTrades creationTimestamp scaledProfit scaledCollateralVolume",
        where="",
        table="accounts",
        cols=("id", "num_trades", "creation_ts", "scaled_profit", "scaled_volume"),
        row=lambda a: (a["id"], _i(a.get("numTrades")), _i(a.get("creationTimestamp")),
                       _f(a.get("scaledProfit")), _f(a.get("scaledCollateralVolume"))),
    ),
    # marketPosition.id == user_address (0x + 40 hex = 42 chars) + token_id
    # (decimal). We split it out instead of selecting the nested user/market
    # objects, which the subgraph errors on when `market` is null.
    "market_positions": dict(
        entity="marketPositions",
        fields="id quantityBought valueBought netQuantity",
        where="",
        table="market_positions",
        cols=("id", "user_id", "token_id", "qty_bought", "val_bought", "net_qty"),
        row=lambda p: (p["id"], p["id"][:42], p["id"][42:],
                       _i(p.get("quantityBought")), _i(p.get("valueBought")),
                       _i(p.get("netQuantity"))),
    ),
}


def ingest(con, name, limit=0):
    spec = SPECS[name]
    last = con.execute("SELECT last_id FROM _cursor WHERE table_name=?", [name]).fetchone()
    start = last[0] if last else ""
    placeholders = ",".join("?" * len(spec["cols"]))
    insert = (f"INSERT OR IGNORE INTO {spec['table']} "
              f"({','.join(spec['cols'])}) VALUES ({placeholders})")
    buf, total, t0, last_seen = [], 0, time.time(), start

    def flush(cursor_id):
        nonlocal buf
        if buf:
            con.executemany(insert, buf)
            buf = []
        con.execute("INSERT OR REPLACE INTO _cursor VALUES (?, ?)", [name, cursor_id])

    print(f"[{name}] resuming from id={start[:14] or '(start)'}", flush=True)
    for row in sg.paginate(spec["entity"], spec["fields"], where=spec["where"], start_id=start):
        buf.append(spec["row"](row))
        total += 1
        last_seen = row["id"]
        if len(buf) >= BATCH:
            flush(last_seen)
            rate = total / max(1e-9, time.time() - t0)
            print(f"[{name}] {total:>9,}  ({rate:,.0f}/s)", flush=True)
        if limit and total >= limit:
            break
    flush(last_seen)  # remaining buffer + advance cursor to the last id seen
    cnt = con.execute(f"SELECT count(*) FROM {spec['table']}").fetchone()[0]
    print(f"[{name}] done — {total:,} pulled this run, {cnt:,} rows total", flush=True)


def ingest_parallel(con, name, shards=16):
    """Page `shards` id-ranges concurrently; workers fetch and enqueue, this
    (single) thread writes to DuckDB and checkpoints each shard's cursor.
    ~`shards`× the sequential throughput, and fully resumable per shard."""
    spec = SPECS[name]
    bounds = sg.shard_bounds(shards)
    placeholders = ",".join("?" * len(spec["cols"]))
    insert = (f"INSERT OR IGNORE INTO {spec['table']} "
              f"({','.join(spec['cols'])}) VALUES ({placeholders})")
    starts = {}
    for i in range(shards):
        row = con.execute("SELECT last_id FROM _cursor WHERE table_name=?",
                          [f"{name}#{i:02d}"]).fetchone()
        starts[i] = row[0] if row else ""
    q = queue.Queue(maxsize=400)
    DONE = object()

    def worker(i):
        try:
            for rows, last in sg.paginate_pages(spec["entity"], spec["fields"],
                                                lo=bounds[i], hi=bounds[i + 1],
                                                start_id=starts[i]):
                q.put((i, [spec["row"](r) for r in rows], last))
        except Exception as e:
            q.put((i, "ERR", str(e)[:120]))
        q.put((i, DONE, None))

    for i in range(shards):
        threading.Thread(target=worker, args=(i,), daemon=True).start()

    # DuckDB fsyncs per commit, so committing each page caps us at the writer
    # (~380/s) while fetch concurrency does ~4,600/s. Buffer many pages and
    # commit in one transaction to amortize the fsync.
    COMMIT_ROWS = 25000
    finished, total, t0 = 0, 0, time.time()
    buf, shard_last = [], {}

    def flush():
        nonlocal buf
        if not buf:
            return
        con.execute("BEGIN TRANSACTION")
        con.executemany(insert, buf)
        for sh, lid in shard_last.items():
            con.execute("INSERT OR REPLACE INTO _cursor VALUES (?, ?)",
                        [f"{name}#{sh:02d}", lid])
        con.execute("COMMIT")
        buf = []

    print(f"[{name}] {shards} parallel shards", flush=True)
    while finished < shards:
        i, payload, last = q.get()
        if payload is DONE:
            finished += 1
            continue
        if payload == "ERR":
            print(f"[{name}] shard {i:02d} error: {last}", flush=True)
            continue
        buf.extend(payload)
        shard_last[i] = last
        total += len(payload)
        if len(buf) >= COMMIT_ROWS:
            flush()
            rate = total / max(1e-9, time.time() - t0)
            print(f"[{name}] {total:>10,}  ({rate:,.0f}/s, {finished}/{shards} shards done)",
                  flush=True)
    flush()
    cnt = con.execute(f"SELECT count(*) FROM {spec['table']}").fetchone()[0]
    print(f"[{name}] done — {total:,} pulled this run, {cnt:,} rows total", flush=True)


def main(argv):
    parallel = False
    if argv and argv[0] in ("-p", "--parallel"):
        parallel = True; argv = argv[1:]
    limit = 0
    if argv and argv[-1].isdigit():        # optional trailing row-limit (per table)
        limit = int(argv[-1]); argv = argv[:-1]
    targets = argv or ["all"]
    if targets == ["all"]:
        targets = ["conditions", "accounts", "market_positions"]
    con = duckdb.connect(DB)
    con.execute(SCHEMA)
    for t in targets:
        if t not in SPECS:
            print(f"unknown table: {t}", file=sys.stderr); continue
        if parallel and not limit:
            ingest_parallel(con, t)
        else:
            ingest(con, t, limit)
    con.close()


if __name__ == "__main__":
    main(sys.argv[1:])
