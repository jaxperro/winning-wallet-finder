#!/usr/bin/env python3
"""Parquet mirror sync (Stage-0 warehouse, replaces the nightly bulk ingest).

Pulls NEW parquet files the recorder's fold sidecar produced (/data/parquet
on the wwf-recorder volume) into live/parquet/, row-verifies each against
the box manifest, appends its rows into live/rtds.duckdb's NATIVE tables
(same tables as ever — research and tape_sharps keep native-table speed;
views-over-parquet was rejected: sim's per-asset point queries would turn
minutes into hours), marks it in `ingested`, and ACKs it back (touch
/data/parquet/acks/<file>.ok — the fold disk-guard may only ever delete
ACKED parquet). The mirror itself is the durable warehouse layer Stage 1
(MotherDuck/ClickHouse) would consume.

Transport is the battle-tested flyctl sftp get (+ ssh console for acks);
no ingress on the recorder, by design. Quiet no-op when the box is
unreachable or another process holds the db write lock — files are
immutable and manifest-driven, so the next run catches up. Runs every 15
min via com.jaxperro.tape-sync and from daily.sh (old bulk ingest.py kept
as fallback for a fold-less recorder).
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "..", "live")
MIRROR = os.path.join(LIVE, "parquet")
LOCAL_MANIFEST = os.path.join(MIRROR, ".mirror_manifest.jsonl")
DB = os.path.join(LIVE, "rtds.duckdb")
APP, PQ = "wwf-recorder", "/data/parquet"
FLYCTL = shutil.which("flyctl") or "/opt/homebrew/bin/flyctl"


def box(cmd, timeout=120):
    r = subprocess.run([FLYCTL, "ssh", "console", "-a", APP, "-C",
                        f"bash -c '{cmd}'"], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    return r.stdout


def sftp_get(remote, local, timeout=240):
    """sftp first (a healthy multi-MB fetch takes <60s — the old 900s window
    just burned the 15-min cadence when it hung), then the base64-over-
    console fallback that carried ingest.py through the same flakiness
    (2026-07-22: sftp timeouts left magic-byte-less partial parquets while
    console commands stayed fast). Integrity is the caller's manifest
    row-count verify either way; partial files are removed here."""
    try:
        subprocess.run([FLYCTL, "ssh", "sftp", "get", remote, local, "-a", APP],
                       capture_output=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    if os.path.exists(local) and os.path.getsize(local) > 0:
        return True
    try:
        os.remove(local)
    except OSError:
        pass
    try:
        import base64
        raw = box(f"base64 {remote}", timeout=600)
        blob = base64.b64decode(raw)
        if not blob:
            return False
        with open(local, "wb") as fh:
            fh.write(blob)
        print(f"[sync] {os.path.basename(remote)}: sftp failed — "
              f"base64 fallback carried {len(blob)//1024}KB")
        return True
    except Exception as e:
        print(f"[sync] {os.path.basename(remote)}: both transports failed "
              f"({type(e).__name__})")
        try:
            os.remove(local)
        except OSError:
            pass
        return False


def load_manifest(path):
    out = {}
    try:
        with open(path) as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                    out[d["segment"]] = d
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def connect_rw(duckdb, tries=3, wait=10):
    """The 15-min cadence can collide with a long research read (one-writer-
    OR-many-readers). Retry briefly, then yield to the next run."""
    for i in range(tries):
        try:
            return duckdb.connect(DB)
        except Exception as e:
            if i == tries - 1:
                print(f"[sync] db locked ({str(e)[:60]}) — yielding to next run")
                return None
            time.sleep(wait)


def main():
    os.makedirs(MIRROR, exist_ok=True)
    tmp_man = os.path.join(MIRROR, ".remote_manifest.tmp")
    if not sftp_get(f"{PQ}/manifest.jsonl", tmp_man, timeout=120):
        print("[sync] box unreachable or no manifest yet — nothing to do")
        return 0
    remote = load_manifest(tmp_man)
    local = load_manifest(LOCAL_MANIFEST)
    new = [d for s, d in sorted(remote.items()) if s not in local]
    os.remove(tmp_man)
    if not new:
        print(f"[sync] mirror current ({len(local)} files)")
        return 0
    import duckdb
    con = connect_rw(duckdb)
    if con is None:
        return 0
    have = {r[0] for r in con.execute("SELECT segment FROM ingested").fetchall()}
    got = 0
    for d in new:
        rel, seg = d["path"], d["segment"]
        dst = os.path.join(MIRROR, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst) and not sftp_get(f"{PQ}/{rel}", dst):
            print(f"[sync] {rel}: fetch failed — retry next run")
            continue
        try:
            n, = con.execute(
                f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()
        except Exception as e:
            print(f"[sync] {rel}: unreadable ({e}) — dropped, retry next run")
            try:
                os.remove(dst)
            except OSError:
                pass
            continue
        if n != d["rows"]:
            print(f"[sync] {rel}: rows {n} != manifest {d['rows']} — dropped")
            os.remove(dst)
            continue
        # append into the native tables exactly once (segment-keyed), then
        # mirror-manifest + ack. A crash between COMMIT and manifest write
        # re-runs into the `have` guard — no double insert.
        if seg not in have:
            con.execute("BEGIN")
            if d["family"] == "aux":
                con.execute(f"""INSERT INTO aux
                    SELECT ts, topic, type, payload
                    FROM read_parquet('{dst}')""")
            else:
                con.execute(f"""INSERT INTO trades
                    SELECT ts, wallet, asset, cond, side, price, size, tx, title
                    FROM read_parquet('{dst}')""")
            con.execute("INSERT INTO ingested VALUES (?,?,?)",
                        [seg, d["rows"], int(time.time())])
            con.execute("COMMIT")
        with open(LOCAL_MANIFEST, "a") as fh:
            fh.write(json.dumps(d) + "\n")
        box(f"touch {PQ}/acks/{os.path.basename(rel)}.ok")
        got += 1
        print(f"[sync] {rel}: {n} rows -> db + mirror + ack")
    n, = con.execute("SELECT count(*) FROM trades").fetchone()
    print(f"[sync] +{got}/{len(new)} files · rtds.duckdb now {n:,} trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
