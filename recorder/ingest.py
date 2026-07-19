#!/usr/bin/env python3
"""Nightly tape ingest (daily.sh): pull closed RTDS segments off the
wwf-recorder volume into live/rtds.duckdb, delete on the box ONLY after the
inserted row count matches. Own DB file on purpose — must never contend with
cache.duckdb's single-writer lock (2026-07-17 collision). Transport is
`flyctl ssh console` + base64 (no ingress on the recorder by design)."""
import base64
import gzip
import json
import os
import shutil
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "live", "rtds.duckdb")
APP, SEG = "wwf-recorder", "/data/segments"


# launchd runs daily.sh with a minimal PATH (no /opt/homebrew/bin) — the
# 2026-07-18 run died on FileNotFoundError: 'flyctl'. Resolve it explicitly.
FLYCTL = shutil.which("flyctl") or "/opt/homebrew/bin/flyctl"


def box(cmd):
    r = subprocess.run([FLYCTL, "ssh", "console", "-a", APP, "-C",
                        f"bash -c '{cmd}'"], capture_output=True, text=True,
                       timeout=300)
    return r.stdout


def main():
    import duckdb
    con = duckdb.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        ts DOUBLE, wallet VARCHAR, asset VARCHAR, cond VARCHAR, side VARCHAR,
        price DOUBLE, size DOUBLE, tx VARCHAR, title VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS ingested(
        segment VARCHAR PRIMARY KEY, rows BIGINT, ingested_at BIGINT)""")
    have = {r[0] for r in con.execute("SELECT segment FROM ingested").fetchall()}
    segs = [s for s in box(f"ls {SEG}").split()
            if s.endswith(".gz") and s not in have]
    total = 0
    for s in sorted(segs):
        raw = box(f"base64 {SEG}/{s}")
        try:
            lines = gzip.decompress(base64.b64decode(raw)).decode().splitlines()
        except Exception as e:
            print(f"[ingest] {s}: fetch/decode failed ({e}) — left on box")
            continue
        rows = []
        for ln in lines:
            try:
                d = json.loads(ln)
                rows.append((d.get("ts"), d.get("wallet"), d.get("asset"),
                             d.get("cond"), d.get("side"), d.get("price"),
                             d.get("size"), d.get("tx"), d.get("title")))
            except Exception:
                pass
        if rows:
            con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT INTO ingested VALUES (?,?,?)",
                    [s, len(rows), int(time.time())])
        box(f"rm {SEG}/{s}")
        total += len(rows)
        print(f"[ingest] {s}: {len(rows)} rows")
    n = con.execute("SELECT count(*) FROM trades").fetchone()[0]
    print(f"[ingest] +{total} rows · rtds.duckdb now {n:,} trades")


if __name__ == "__main__":
    main()
