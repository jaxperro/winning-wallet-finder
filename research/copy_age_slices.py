#!/usr/bin/env python3
"""T10 EXPLORATORY (2026-07-23) — market-age copy weighting: do sharp
entries on freshly-listed markets (books young, thin, unopinionated) carry
more edge than steady-state copies? Age proxy = signal_ts − first print on
the cond in the tape (complete coverage; markets older than the tape era
bucket as 'pre-tape'). Chain-true grading; parity-era BUY fills both
books. Output: EV by age bucket → a follow-filter weight if a gradient
exists (kill: no gradient at n>=30/bucket)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARITY_T0 = 1784260140
BUCKETS = [(0, 3600, "<1h"), (3600, 6 * 3600, "1-6h"),
           (6 * 3600, 86400, "6-24h"), (86400, 3 * 86400, "1-3d"),
           (3 * 86400, 1e12, ">3d")]


def main():
    db = tape.connect()
    tape.build_resolved(db)
    t_lo, = db.execute("SELECT min(ts) FROM trades").fetchone()
    fills = []
    for path, book in ((os.path.join(ROOT, "copybot_fills.jsonl"), "paper"),
                       (os.path.join(ROOT, "copybot_fills.live.jsonl"), "live")):
        for ln in open(path):
            r = json.loads(ln)
            if (r.get("side") == "SELL" or r.get("untracked")
                    or r.get("ts", 0) < PARITY_T0 or not r.get("my_price")):
                continue
            r["_book"] = book
            fills.append(r)
    pays = fwd.payouts_for(db, [str(f["token"]) for f in fills])
    rows = []
    for f in fills:
        pay = pays.get(str(f["token"]))
        if pay is None or pay == 0.5:
            continue
        first, = db.execute(
            "SELECT min(ts) FROM trades WHERE asset = ?",
            [str(f["token"])]).fetchone()
        if first is None:
            continue
        age = f["ts"] - first
        pre_tape = first <= t_lo + 3600      # market predates tape coverage
        rows.append({"book": f["_book"], "age": age, "pre": pre_tape,
                     "px": f["my_price"],
                     "ev": 100.0 / f["my_price"] * (pay - f["my_price"]),
                     "win": pay == 1.0})
    print(f"chain-graded parity fills: {len(rows)}")

    def line(tag, rs):
        if not rs:
            return
        n = len(rs)
        print(f"  {tag:<10} n={n:<4} EV/$100 "
              f"{sum(r['ev'] for r in rs)/n:+7.2f} · hit "
              f"{sum(r['win'] for r in rs)/n:.2f} · avg px "
              f"{sum(r['px'] for r in rs)/n:.2f}")

    for book in ("paper", "live"):
        rs = [r for r in rows if r["book"] == book]
        print(f"\n== {book.upper()} ==")
        line("pre-tape", [r for r in rs if r["pre"]])
        fresh = [r for r in rs if not r["pre"]]
        for lo, hi, tag in BUCKETS:
            line(tag, [r for r in fresh if lo <= r["age"] < hi])


if __name__ == "__main__":
    main()
