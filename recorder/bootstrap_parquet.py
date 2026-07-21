#!/usr/bin/env python3
"""One-shot Stage-0 bootstrap: export the history that exists ONLY in
live/rtds.duckdb (segments already deleted on the box) into the parquet
mirror, so live/parquet/ becomes the complete durable warehouse layer from
day one. Partitioned like fold.py's output (date=YYYY-MM-DD, UTC from ts),
one bootstrap_<date>.parquet per family per day. Idempotent: skips files
that already exist. Run once after the last bulk ingest; verify counts."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "..", "live")
DB = os.path.join(LIVE, "rtds.duckdb")
MIRROR = os.path.join(LIVE, "parquet")


def main():
    import duckdb
    con = duckdb.connect(DB, read_only=True)
    total = {"trades": 0, "aux": 0}
    for fam, cols in (("trades", "ts, wallet, asset, cond, side, price, size, tx, title"),
                      ("aux", "ts, topic, type, payload")):
        days = [d for (d,) in con.execute(
            f"SELECT DISTINCT strftime(to_timestamp(ts), '%Y-%m-%d') FROM {fam} "
            f"WHERE ts IS NOT NULL ORDER BY 1").fetchall()]
        for day in days:
            outdir = os.path.join(MIRROR, fam, f"date={day}")
            os.makedirs(outdir, exist_ok=True)
            out = os.path.join(outdir, f"bootstrap_{day.replace('-', '')}.parquet")
            if os.path.exists(out):
                print(f"[bootstrap] {fam} {day}: exists — skip")
                continue
            con.execute(f"""COPY (SELECT {cols} FROM {fam}
                WHERE strftime(to_timestamp(ts), '%Y-%m-%d') = '{day}')
                TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
            n, = con.execute(
                f"SELECT count(*) FROM read_parquet('{out}')").fetchone()
            total[fam] += n
            print(f"[bootstrap] {fam} {day}: {n:,} rows "
                  f"({os.path.getsize(out)//2**20}MB)")
    for fam in ("trades", "aux"):
        db_n, = con.execute(f"SELECT count(*) FROM {fam}").fetchone()
        pq_n, = con.execute(f"""SELECT count(*) FROM
            read_parquet('{os.path.join(MIRROR, fam)}/*/*.parquet')""").fetchone()
        ok = "OK" if db_n == pq_n else "MISMATCH"
        print(f"[bootstrap] parity {fam}: db {db_n:,} vs mirror {pq_n:,} — {ok}")
        if db_n != pq_n:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
