#!/usr/bin/env python3
"""T14 EXPLORATORY (2026-07-23) — DEPTH STRUCTURE AS A FILL FILTER: the
harnesses log the book at every attempt (surge A2: top-5 asks; oracle:
top-5 asks + top-3 bids). Does book structure at fill time separate good
fills from adversely-selected ones? A positive, cross-harness-consistent
split would be a portable fill-quality gate for every harness (#23
included) — measured on OUR OWN attempt streams, not simmed.

Features at attempt time (v0, frozen before looking at outcomes):
  imb     bid$/(bid$+ask$) over the logged levels (both harnesses log
          bids at attempt — the bid_top key is simply absent on rows
          where the bid side was empty)
  l1_frac best-ask depth$ / $100 stake — did our FAK eat the whole L1?
  slope   (ask5_px - ask1_px) — book steepness over the logged levels
Outcomes: chain P&L per fill from the graded ledgers (grade_surge /
grade_oracle output; keyed asset+ts) — no RPC, no tape, scorer law by
construction. Refunds excluded. Quartile split per feature per harness;
the credibility gate is SIGN CONSISTENCY across both harnesses (a
single-harness split at these n is a hypothesis, not a filter).
NOT pre-registered — exploration for a possible harness guard.

VERDICT (2026-07-23 run; surge n=1,337 · oracle n=586 graded fills):
  imb    the one REAL structure, and it is a BAND, not a slope: both
         harnesses show an inverted U — extreme imbalance EITHER way is
         adverse (surge Q1 −11.51 / Q4 −12.07; oracle Q1 −24.20 / Q4
         −15.80) while the balanced middle (imb ~0.25-0.47) is the
         least-bad/only-positive region (surge −2.9/−4.3, oracle
         +1.0/+2.4). Empty-bid books = falling knives; bid-heavy books =
         the winner's curse (someone paid up for support you're buying
         through). Both parent signals are dead, so this is not an edge
         — it is a PORTABLE GUARD candidate for live harnesses (#23).
  slope  the printed CONSISTENT gate is an ARTIFACT — books cluster so
         tight (4-5c across 5 levels) that quartile boundaries land
         INSIDE tied values (Q2=[0.04..0.04]); the split is tie-broken
         noise, not structure. Discarded. Lesson: check feature variance
         before trusting any quantile gate.
  l1_frac sign-inconsistent across harnesses — no filter.
The top-minus-bottom gap stat cannot see an inverted U; the imb read
comes from the quartile table, not the gate line."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_jsonl(path):
    out = []
    try:
        for ln in open(os.path.join(HERE, path)):
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def features(att):
    top = att.get("top") or []
    f = {}
    if top:
        ask_d = sum(p * s for p, s in top)
        f["l1_frac"] = top[0][0] * top[0][1] / 100.0
        f["slope"] = (top[-1][0] - top[0][0]) if len(top) > 1 else 0.0
        bt = att.get("bid_top") or []
        if bt:
            bid_d = sum(p * s for p, s in bt)
            if bid_d + ask_d > 0:
                f["imb"] = bid_d / (bid_d + ask_d)
    return f


def quartile_report(tag, rows, feat):
    rs = [(f[feat], pnl) for f, pnl in rows if feat in f]
    if len(rs) < 40:
        print(f"  {tag} {feat}: n={len(rs)} too thin")
        return None
    rs.sort()
    qs = []
    k = len(rs) // 4
    for i in range(4):
        seg = rs[i * k:(i + 1) * k] if i < 3 else rs[3 * k:]
        ev = sum(p for _, p in seg) / len(seg)
        hit = sum(1 for _, p in seg if p > 0) / len(seg)
        qs.append((ev, hit, len(seg), seg[0][0], seg[-1][0]))
    print(f"  {tag} {feat}: " + " | ".join(
        f"Q{i+1}[{lo:.2f}..{hi:.2f}] {ev:+6.2f}/fill {hit:.0%} n={n}"
        for i, (ev, hit, n, lo, hi) in enumerate(qs)))
    return qs[3][0] - qs[0][0]        # top-minus-bottom EV gap


def main():
    results = {}
    for tag, att_f, led_f in (
            ("SURGE", ".surge_attempts.pull.jsonl", "surge_meas_ledger.jsonl"),
            ("ORACLE", ".oracle_attempts.pull.jsonl",
             "oracle_paper_ledger.jsonl")):
        atts = load_jsonl(att_f)
        led = load_jsonl(led_f)
        graded = {}
        for r in led:
            if r.get("chain_payout") == 0.5:
                continue
            graded[(r["asset"], round(float(r["ts"]), 3))] = r["chain_pnl"]
        rows = []
        for a in atts:
            if not a.get("filled"):
                continue
            key = (a["asset"], round(float(a["ts"]), 3))
            if key not in graded:
                continue
            rows.append((features(a), graded[key]))
        print(f"\n{tag}: attempts {len(atts)} · graded fills joined "
              f"{len(rows)}", flush=True)
        results[tag] = {}
        for feat in ("imb", "l1_frac", "slope"):
            results[tag][feat] = quartile_report(tag, rows, feat)

    print("\nCROSS-HARNESS GATE (top-quartile minus bottom-quartile EV):")
    for feat in ("l1_frac", "slope"):
        g_s = results.get("SURGE", {}).get(feat)
        g_o = results.get("ORACLE", {}).get(feat)
        if g_s is None or g_o is None:
            print(f"  {feat}: insufficient data in one harness")
            continue
        consistent = (g_s > 0) == (g_o > 0)
        print(f"  {feat}: surge {g_s:+.2f} · oracle {g_o:+.2f} · "
              f"{'CONSISTENT' if consistent else 'INCONSISTENT — no filter'}")
    g_i = results.get("ORACLE", {}).get("imb")
    if g_i is not None:
        print(f"  imb (oracle only, single-harness hypothesis): {g_i:+.2f}")


if __name__ == "__main__":
    main()
