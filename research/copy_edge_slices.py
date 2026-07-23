#!/usr/bin/env python3
"""T5 EXPLORATORY (2026-07-23) — niche x lead-time x entry-band decomposition
of the copybot's parity-era edge. WHERE does copy alpha live?

Feeds the #14 sizing verdict: the pooled edge fights a measured ~1.9%-of-
stake live fee hurdle (+2pp comfort, live/edge.py decision rule). If the
edge concentrates (informed-niche prior: ITF/esports), per-niche follow
filters beat one global rule.

Substrate: fills ledgers (paper + live), BUYs opened >= PARITY_T0
(2026-07-16 03:49Z, same boundary as live/edge.py), graded with
forward.payouts_for (tape proxy + mandatory chain overlay — scorer law).
Slight substrate difference vs edge.py (feed bets, bot-graded) is
deliberate: chain truth + per-fill lag/price fields; the pooled number is
printed next to edge.py's for reconciliation. Niches: meta_snap category/
tags where the token appears in a snapshot, title heuristic fallback
(coverage % reported — snapshots only start 2026-07-23, so early-era
closed markets fall back). NOT pre-registered; measurement of a live edge."""
import glob
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PARITY_T0 = 1784260140          # keep == live/edge.py
HURDLE = 0.019                  # measured live round-trip fee drag
LAG_BUCKETS = [(0, 3, "<=3s"), (3, 10, "3-10s"), (10, 30, "10-30s"),
               (30, 1e9, ">30s")]
PX_BANDS = [(0, .30, "<30c"), (.30, .50, "30-50c"), (.50, .70, "50-70c"),
            (.70, .96, "70-95c")]


def load_meta():
    """token -> niche from ALL meta snapshots (accrete over days)."""
    tok2niche = {}
    for f in sorted(glob.glob(os.path.join(HERE, "meta", "meta_*.jsonl.gz"))):
        for ln in gzip.open(f, "rt"):
            try:
                m = json.loads(ln)
            except Exception:
                continue
            tags = " ".join(str(t) for t in (m.get("tags") or [])).lower()
            cat = (m.get("category") or "").lower()
            niche = ("esports" if "esports" in tags
                     else "tennis" if "tennis" in tags
                     else cat or None)
            if not niche:
                continue
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
            except Exception:
                toks = []
            for t in toks:
                tok2niche[str(t)] = niche
    return tok2niche


def main():
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
    db = tape.connect()
    tape.build_resolved(db)
    pays = fwd.payouts_for(db, [str(f["token"]) for f in fills])
    tok2niche = load_meta()
    meta_hit = 0
    rows = []
    for f in fills:
        pay = pays.get(str(f["token"]))
        if pay is None:
            continue
        niche = tok2niche.get(str(f["token"]))
        if niche:
            meta_hit += 1
        else:
            niche = tape.niche(f.get("title") or "")
        rows.append({"book": f["_book"], "niche": niche,
                     "lag": f.get("detect_lag_s"),
                     "px": f["my_price"], "cost": f.get("cost") or
                     f["my_price"] * f.get("shares", 0),
                     "slip": (f["my_price"] - f["their_price"])
                     if f.get("their_price") else None,
                     "name": f.get("name") or "?",
                     "pnl": f.get("shares", 0) * (pay - f["my_price"])
                     - (f.get("fee") or 0),
                     "win": pay == 1.0, "refund": pay == 0.5})
    print(f"parity-era BUY fills: {len(fills)} · chain-graded {len(rows)} "
          f"({len(fills)-len(rows)} unresolved) · meta-niche coverage "
          f"{100*meta_hit/max(len(rows),1):.0f}% (title fallback rest)")

    def cell(tag, rs, show_hurdle=True):
        if not rs:
            return
        n = len(rs)
        staked = sum(r["cost"] for r in rs)
        pnl = sum(r["pnl"] for r in rs)
        ret = pnl / staked if staked else 0
        hit = sum(r["win"] for r in rs) / n
        mark = ""
        if show_hurdle and n >= 15:
            mark = (" ✅ clears hurdle+2pp" if ret > HURDLE + 0.02
                    else " ❌ under hurdle" if ret < HURDLE else " ~ marginal")
        print(f"  {tag:<22} n={n:<4} ret {ret*100:+6.1f}% · EV/fill "
              f"{pnl/n:+6.2f} · hit {hit:.2f} · staked ${staked:,.0f}{mark}")

    for book in ("paper", "live"):
        rs = [r for r in rows if r["book"] == book]
        print(f"\n== {book.upper()} pooled ==")
        cell("ALL", rs)
        print(f"-- by niche --")
        for niche in sorted({r['niche'] for r in rs}):
            cell(niche, [r for r in rs if r["niche"] == niche])
        print(f"-- by detection lag --")
        for lo, hi, tag in LAG_BUCKETS:
            cell(tag, [r for r in rs
                       if r["lag"] is not None and lo <= r["lag"] < hi])
        print(f"-- by entry band --")
        for lo, hi, tag in PX_BANDS:
            cell(tag, [r for r in rs if lo <= r["px"] < hi])
        print(f"-- by wallet --")
        for nm in sorted({r['name'] for r in rs}):
            cell(nm, [r for r in rs if r["name"] == nm], show_hurdle=False)
    # slippage by lag (execution cost of latency — T3 motivation)
    print("\n== slippage paid vs their print, by lag (both books) ==")
    for lo, hi, tag in LAG_BUCKETS:
        sl = [r["slip"] for r in rows
              if r["slip"] is not None and r["lag"] is not None
              and lo <= r["lag"] < hi]
        if sl:
            sl.sort()
            print(f"  {tag:<8} n={len(sl):<4} mean {sum(sl)/len(sl)*100:+5.2f}c"
                  f" · p90 {sl[int(len(sl)*.9)]*100:+5.2f}c")


if __name__ == "__main__":
    main()
