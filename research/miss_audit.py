#!/usr/bin/env python3
"""T8 EXPLORATORY (2026-07-23) — guard counterfactual audit: every recorded
miss, scored to chain truth, bucketed by the guard that caused it. Which
gates discard positive EV and which earn their keep?

Sources: copybot state missed lists (both books) + missed_archive spools
where present. Chain-true where token resolvable (payouts_for); falls back
to the bot's own settled would-be pnl otherwise. $100-normalized EV per
guard bucket + realized would-be at recorded stakes."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUCKETS = [
    ("event_cap", r"event cap"),
    ("price_guard", r"price moved"),
    ("depth_thin", r"thin book|depth cap|dust"),
    ("no_ask", r"no ask"),
    ("crater_reject", r"order rejected|no orders found"),
    ("hold_expired", r"hold expired|expired unfilled"),
    ("maker_unfilled", r"maker bid unfilled"),
    ("backfill_seed", r"held before we started"),
]


def bucket(reason):
    r = (reason or "").lower()
    for tag, pat in BUCKETS:
        if re.search(pat, r):
            return tag
    return "other"


def load():
    rows = []
    for st_path, arch, book in (
            ("copybot_state.json", "copybot_missed_archive.jsonl", "paper"),
            ("copybot_state.live.json", "copybot_missed_archive.live.jsonl",
             "live")):
        try:
            for m in json.load(open(os.path.join(ROOT, st_path))).get(
                    "missed", []):
                rows.append({**m, "_book": book})
        except FileNotFoundError:
            pass
        ap = os.path.join(ROOT, arch)
        if os.path.exists(ap):
            for ln in open(ap):
                try:
                    rows.append({**json.loads(ln), "_book": book})
                except Exception:
                    pass
    return rows


def main():
    rows = load()
    print(f"missed rows: {len(rows)} "
          f"(paper {sum(1 for r in rows if r['_book']=='paper')} / "
          f"live {sum(1 for r in rows if r['_book']=='live')})")
    db = tape.connect()
    tape.build_resolved(db)
    toks = [str(r.get("token")) for r in rows if r.get("token")]
    pays = fwd.payouts_for(db, toks)
    graded = []
    for r in rows:
        # CHAIN-ONLY (v0 fallback reconstructed payout from mark-based
        # miss-pnl and inflated every riser into a 'win' — retracted)
        pay = pays.get(str(r.get("token"))) if r.get("token") else None
        if pay is None or pay == 0.5 or not r.get("price"):
            continue
        graded.append((r, pay))
    print(f"chain/bot-graded: {len(graded)}")

    def line(tag, rs):
        if not rs:
            return
        n = len(rs)
        ev = sum(100.0 / r["price"] * (p - r["price"]) for r, p in rs) / n
        hit = sum(1 for _, p in rs if p == 1.0) / n
        stake_pnl = sum((r.get("stake", 0) / r["price"]) * (p - r["price"])
                        for r, p in rs)
        px = sum(r["price"] for r, _ in rs) / n
        top = sorted((100.0 / r["price"] * (p - r["price"]) for r, p in rs),
                     reverse=True)[:3]
        print(f"  {tag:<15} n={n:<5} EV/$100 {ev:+7.2f} · hit {hit:.2f} · "
              f"avg px {px:.2f} · top3 {['%+.0f' % t for t in top]} · "
              f"at real stakes {stake_pnl:+9.2f}")

    for book in ("paper", "live"):
        rs = [(r, p) for r, p in graded if r["_book"] == book]
        print(f"\n== {book.upper()} ==")
        line("ALL MISSES", rs)
        tags = sorted({bucket(r.get("reason")) for r, _ in rs})
        for t in tags:
            line(t, [(r, p) for r, p in rs if bucket(r.get("reason")) == t])
    from collections import Counter
    census = Counter((r.get("reason") or "")[:44] for r, _ in graded
                     if bucket(r.get("reason")) == "other")
    print("\n'other' reason census:")
    for why, n in census.most_common(6):
        print(f"  {n:>4}  {why}")


if __name__ == "__main__":
    main()
