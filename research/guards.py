#!/usr/bin/env python3
"""Concentration guards, shared by every harness grader (#26/#27).

Two studies passed their headline bars this week on results that one
session or five episodes were carrying (Study C ex-best-day +$1.47 vs a
+$2 bar; Study D ex-best-day −$5.86 on a +$4.49 headline). These cuts
turn that lesson into machinery: every grader reports them nightly, and
a PASS whose ex-best-day EV is <= 0 is recorded CONCENTRATED and does
not graduate.

rows: dicts with chain_pnl, cost, ts, and (optionally) event.
"""
import collections
import datetime as dt


def cuts(rows, pnl_key="chain_pnl"):
    """-> dict of the guard readings (empty dict when there is nothing)."""
    rows = [r for r in rows if r.get(pnl_key) is not None]
    n = len(rows)
    if not n:
        return {}
    tot = sum(r[pnl_key] for r in rows)
    staked = sum(r.get("cost") or 0 for r in rows)
    out = {"n": n, "pnl": round(tot, 2), "ev": round(tot / n, 2),
           "pct_staked": round(100 * tot / staked, 1) if staked else None,
           "wins": sum(1 for r in rows if r.get("chain_payout") == 1.0)}
    out["hit"] = round(out["wins"] / n, 3)
    # episode concentration
    srt = sorted(rows, key=lambda r: -abs(r[pnl_key]))
    for k in (1, 5):
        if n > k:
            ex = sum(r[pnl_key] for r in srt[k:])
            out[f"ex_top{k}_ev"] = round(ex / (n - k), 2)
    out["top5_share"] = (round(100 * sum(r[pnl_key] for r in srt[:5]) / tot, 0)
                         if tot else None)
    # TIME concentration — the guard both studies needed
    byday = collections.defaultdict(lambda: [0, 0.0])
    for r in rows:
        d = dt.datetime.utcfromtimestamp(r["ts"]).strftime("%m-%d")
        byday[d][0] += 1
        byday[d][1] += r[pnl_key]
    out["days"] = len(byday)
    out["by_day"] = {d: [v[0], round(v[1], 0)] for d, v in sorted(byday.items())}
    if len(byday) > 1:
        best = max(byday.values(), key=lambda v: v[1])
        exn = n - best[0]
        if exn > 0:
            out["ex_best_day_ev"] = round((tot - best[1]) / exn, 2)
            out["ex_best_day_n"] = exn
    # EVENT concentration (the gate Study C's fade arm failed at 32%)
    ev = collections.defaultdict(float)
    for r in rows:
        if r.get("event"):
            ev[r["event"]] += r[pnl_key]
    if ev and tot:
        out["events"] = len(ev)
        out["top_event_share"] = round(
            100 * max(ev.values(), key=abs) / tot, 0)
    return out


def line(tag, c, pass_ev=None):
    """One printable verdict line + the guard verdict."""
    if not c:
        return f"{tag}: no graded rows"
    s = (f"{tag}: n={c['n']} {c['wins']}W · EV/fill {c['ev']:+.2f} · "
         f"hit {c['hit']:.3f} · {c['pct_staked']:+.0f}% staked · "
         f"{c['days']}d")
    if "ex_best_day_ev" in c:
        s += f" · EX-BEST-DAY {c['ex_best_day_ev']:+.2f}"
    if "ex_top5_ev" in c:
        s += f" · ex-top5 {c['ex_top5_ev']:+.2f}"
    if "top_event_share" in c:
        s += f" · top-event {c['top_event_share']:+.0f}%"
    if pass_ev is not None:
        bars = [c["ev"] >= pass_ev,
                c.get("ex_best_day_ev", -1) > 0,
                c.get("ex_top5_ev", -1) > 0,
                abs(c.get("top_event_share") or 0) < 30]
        s += ("  -> " + ("ALL GUARDS CLEAR" if all(bars) else
                         "CONCENTRATED (headline only)" if bars[0] else
                         "below bar"))
    return s
