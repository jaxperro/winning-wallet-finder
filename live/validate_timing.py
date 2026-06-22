#!/usr/bin/env python3
"""Insider-vs-sharp check on the standout conviction wallets.

A near-100% win rate is either genuine foresight (copyable) or information /
last-second entry (uncopyable insider). The tell is entry->resolution lead time
on their WINNING conviction bets:
  * mostly < 1h before resolution  -> insider/news-reaction, you can't follow it
  * hours-to-days of lead          -> a sharp you could actually mirror
"""

import json
import os
import statistics as st
from concurrent.futures import ThreadPoolExecutor

import cache

HERE = os.path.dirname(__file__)
COPYABLE_MED_LEAD = 24.0     # median lead (h) on winning conviction bets to count as copyable


def lead_profile(w):
    ent = cache.get_entries(w)
    bets = cache.get_bets(w)
    cut = cache.conv_cutoff(b["size"] for b in bets)   # this wallet's top-20% stake cutoff
    leads = [(b["res_t"] - ent[b["cond"]]) / 3600.0 for b in bets
             if b["won"] and (b["size"] or 0) >= cut and b["cond"] in ent
             and b["res_t"] and b["res_t"] >= ent[b["cond"]]]
    if not leads:
        return None
    med = st.median(leads)
    u6 = sum(1 for l in leads if l < 6) / len(leads)
    verdict = ("insider" if (med < 6 or sum(1 for l in leads if l < 1) / len(leads) > 0.5)
               else "borderline" if med < COPYABLE_MED_LEAD else "sharp")
    return dict(n=len(leads), med=med, u6=u6, verdict=verdict)


def main():
    conv = json.load(open(os.path.join(HERE, "conviction_wallets.json")))
    print(f"validating timing on {len(conv)} conviction wallets…\n", flush=True)
    with ThreadPoolExecutor(max_workers=10) as ex:
        profs = list(ex.map(lambda c: (c, lead_profile(c["wallet"])), conv))

    sharps = []
    for c, p in profs:
        if p:
            c["med_lead_h"] = round(p["med"], 1)
            c["timing"] = p["verdict"]
            if p["verdict"] == "sharp":
                sharps.append(c)
    sharps.sort(key=lambda c: (c["fwd_conv_roi"] is not None, c.get("fwd_conv_roi") or -9,
                               c["train_conv_roi"]), reverse=True)

    counts = {}
    for c, p in profs:
        counts[p["verdict"] if p else "no-data"] = counts.get(p["verdict"] if p else "no-data", 0) + 1
    print(f"timing breakdown: {counts}")
    print(f"COPYABLE SHARPS (median lead >= {COPYABLE_MED_LEAD:.0f}h): {len(sharps)}\n")

    h = (f"{'tr_win':>7}{'tr_roi':>7}{'medLeadH':>9}{'fw_win':>7}{'fw_roi':>7}{'fw_n':>5}  wallet")
    print(h); print("-" * len(h))
    for c in sharps[:30]:
        fw = f"{c['fwd_win']:.0f}%" if c["fwd_win"] is not None else "—"
        fr = f"{c['fwd_conv_roi']:+.0%}" if c["fwd_conv_roi"] is not None else "—"
        print(f"{c['train_win']:>6.0f}%{c['train_conv_roi']:>+6.0%}{c['med_lead_h']:>9.0f}"
              f"{fw:>7}{fr:>7}{c['fwd_n']:>5}  {c['wallet']}")

    json.dump(sharps, open(os.path.join(HERE, "watch_sharps.json"), "w"), indent=2)
    print(f"\n-> watch_sharps.json ({len(sharps)} copyable sharps, insiders filtered out)")


if __name__ == "__main__":
    main()
