#!/usr/bin/env python3
"""Stage-0 warehouse fold — runs ON the recorder box beside recorder.py.

Every FOLD_S seconds: each closed gz segment becomes a zstd Parquet file
under /data/parquet/<family>/date=YYYY-MM-DD/<segment>.parquet, row-parity
verified, manifest-logged — and only then is the raw gz deleted. This moves
the deletion authority that used to live in the Mac's nightly ingest onto
the box, and makes the volume itself the warehouse: immutable Parquet
partitions any client can mirror incrementally (recorder/sync_tape.py).

The capture invariant gets STRONGER, not weaker:
  raw gz     deleted only after its Parquet row count matches on re-read;
  parquet    deleted only by the disk guard, only oldest-first, and only
             files the Mac mirror has ACKED (/data/parquet/acks/<f>.ok) —
             plus the recorder's own last-resort >95% guard still protects
             the live tape.

Crash-safe by re-entry: parquet-without-manifest refolds (overwrite);
manifest-without-rm re-deletes the leftover gz. duckdb is capped at 384MB
so a busy-hour fold can never starve the capture process (1GB VM).
Never touches the current (un-gzipped) hour. Never imports bot code.
"""
import json
import os
import time

import duckdb

SEG = os.environ.get("TAPE_DIR", "/data/segments")
PQ = os.environ.get("PARQUET_DIR", "/data/parquet")
ACKS = os.path.join(PQ, "acks")
MANIFEST = os.path.join(PQ, "manifest.jsonl")
FOLD_S = 120
TRADE_COLS = ("{ts:'DOUBLE', wallet:'VARCHAR', asset:'VARCHAR', "
              "cond:'VARCHAR', side:'VARCHAR', price:'DOUBLE', "
              "size:'DOUBLE', tx:'VARCHAR', title:'VARCHAR'}")
AUX_COLS = ("{ts:'DOUBLE', topic:'VARCHAR', type:'VARCHAR', "
            "payload:'VARCHAR'}")   # VARCHAR = json text, matches rtds.duckdb


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  [fold] {m}", flush=True)


def manifest_names():
    names = set()
    try:
        with open(MANIFEST) as fh:
            for ln in fh:
                try:
                    names.add(json.loads(ln)["segment"])
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return names


def seg_date(name):
    # rtds_YYYYMMDD_HH.jsonl.gz / aux_YYYYMMDD_HH.jsonl.gz -> YYYY-MM-DD
    stamp = name.split("_", 1)[1][:8]
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"


def fold_one(db, name):
    fam = "aux" if name.startswith("aux_") else "trades"
    cols = AUX_COLS if fam == "aux" else TRADE_COLS
    src = os.path.join(SEG, name)
    outdir = os.path.join(PQ, fam, f"date={seg_date(name)}")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, name.replace(".jsonl.gz", ".parquet"))
    rd = (f"read_json('{src}', format='newline_delimited', "
          f"columns={cols}, ignore_errors=true)")
    n_src, = db.execute(f"SELECT count(*) FROM {rd}").fetchone()
    db.execute(f"COPY (SELECT * FROM {rd}) TO '{out}' "
               f"(FORMAT PARQUET, COMPRESSION ZSTD)")
    n_pq, = db.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()
    if n_pq != n_src:
        os.remove(out)
        raise RuntimeError(f"parity {n_src} src != {n_pq} parquet")
    with open(MANIFEST, "a") as fh:
        fh.write(json.dumps({"segment": name, "family": fam,
                             "path": os.path.relpath(out, PQ), "rows": n_src,
                             "bytes": os.path.getsize(out),
                             "folded_at": int(time.time())}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.remove(src)
    log(f"{name} -> {os.path.basename(out)} ({n_src} rows, "
        f"{os.path.getsize(out)//1024}KB)")


def disk_guard():
    """Volume filling: free the oldest ACKED parquet only. Unacked parquet
    is never touched here — the Mac mirror is the second copy."""
    try:
        st = os.statvfs(PQ)
        free = st.f_bavail / st.f_blocks
        if free >= 0.15:
            return
        acked = {f[:-3] for f in os.listdir(ACKS)} if os.path.isdir(ACKS) else set()
        cand = []
        for root, _, files in os.walk(PQ):
            for f in files:
                if f.endswith(".parquet") and f in acked:
                    cand.append(os.path.join(root, f))
        cand.sort()                      # name-sorted = oldest first
        if cand:
            os.remove(cand[0])
            log(f"disk guard: volume {100*(1-free):.0f}% full — dropped "
                f"acked {os.path.basename(cand[0])}")
        else:
            log(f"⚠ volume {100*(1-free):.0f}% full and NO acked parquet to "
                "drop — is the Mac mirror running?")
    except Exception as e:
        log(f"disk guard error: {e}")


def main():
    os.makedirs(PQ, exist_ok=True)
    os.makedirs(ACKS, exist_ok=True)
    db = duckdb.connect()
    db.execute("SET memory_limit='384MB'")
    db.execute("SET threads=1")
    log(f"folding {SEG} -> {PQ} every {FOLD_S}s")
    while True:
        try:
            done = manifest_names()
            for name in sorted(os.listdir(SEG)):
                if not name.endswith(".gz"):
                    continue             # current hour is plain .jsonl — skip
                if name in done:         # crash between manifest and rm
                    os.remove(os.path.join(SEG, name))
                    log(f"{name}: already folded — removed leftover gz")
                    continue
                try:
                    fold_one(db, name)
                except Exception as e:
                    log(f"⚠ {name}: fold failed ({e}) — gz left for retry")
            disk_guard()
        except Exception as e:
            log(f"⚠ loop error: {e}")
        time.sleep(FOLD_S)


if __name__ == "__main__":
    main()
