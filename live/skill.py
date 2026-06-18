#!/usr/bin/env python3
"""Score candidate wallets for genuine skill — the ~3% the research identifies.

The 5-gate funnel (a wallet must pass all to count as skilled):
  1. >= MIN_N resolved bets in the window               (assessability)
  2. z = (wins - Σp)/√Σp(1-p) clearly > 0               (beats its entry prices)
  3. survives Benjamini-Hochberg FDR across the scan    (not luck-of-the-draw)
  4. split-half: skill in the earlier half persists in  (out-of-sample — the
     the recent half (z_oos > 0)                          decisive gate)
  5. <= MAX_N bets (market-maker / bot proxy, since the data-api gives no
     reliable trade count) and not pure favorite-riding

This mirrors the LBS/Yale method (randomize direction 10k× ≈ the z benchmark;
split-events persistence) and uses an UNBIASED win rate (insider.resolved_bets
unions /positions + /closed-positions, so unredeemed losers are counted).

Refinements used for ranking: odds-band 0.2-0.4 concentration (where alpha
concentrates per Hubble), entry timing / freshness available via insider.

    python3 skill.py                 # score top candidates, write watch_skilled.json
    python3 skill.py 2500            # score the top 2500 candidates by activity
"""

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import insider  # noqa: E402
import cache    # noqa: E402  — local per-wallet bet cache (avoids re-pulling)

HERE = os.path.dirname(__file__)
WINDOW_DAYS = 180
MIN_N = 15           # floor to say anything (paper's skilled avg ~79)
OOS_MIN_N = 24       # need >=12 bets/half for a meaningful split
MAX_N = 2500         # market-maker/bot proxy (no trade count available)
FDR_Q = 0.05
SCORE_TOP = int(sys.argv[1]) if len(sys.argv) > 1 else 2000   # cap scoring cost
WORKERS = 10
# clean out-of-sample re-selection: SKILL_BEFORE=YYYY-MM-DD scores ONLY bets
# resolved before that date, so selection can't peek at the test window.
BEFORE = (time.mktime(time.strptime(os.environ["SKILL_BEFORE"], "%Y-%m-%d"))
          if os.environ.get("SKILL_BEFORE") else 0)
OUT = os.environ.get("SKILL_OUT", "watch_skilled.json")


def zstats(bets):
    n = len(bets)
    if not n:
        return 0, 0, 0.0, 0.0
    wins = sum(1 for b in bets if b["won"])
    exp = sum(b["p"] for b in bets)
    var = sum(b["p"] * (1 - b["p"]) for b in bets) or 1e-9
    return n, wins, exp, (wins - exp) / math.sqrt(var)


def score_wallet(c):
    bets = cache.get_bets(c["wallet"])   # cached — pulls the data-api only once per wallet
    if BEFORE:                       # clean OOS: only bets resolved before cutoff
        bets = [b for b in bets if (b.get("res_t") or 0) < BEFORE]
    n = len(bets)
    if n < MIN_N or n > MAX_N:
        return None
    n, wins, exp, z = zstats(bets)
    # split-half out-of-sample (chronological): does early skill persist forward?
    bets.sort(key=lambda b: b.get("res_t") or 0)
    if n >= OOS_MIN_N:
        h = n // 2
        _, _, _, z_is = zstats(bets[:h])
        _, _, _, z_oos = zstats(bets[h:])
    else:
        z_is = z_oos = None
    band = sum(1 for b in bets if 0.2 <= b["p"] <= 0.4) / n
    avg_p = sum(b["p"] for b in bets) / n
    return {
        "wallet": c["wallet"], "username": c.get("username") or c["wallet"][:10],
        "n": n, "wins": wins, "win_rate": round(100 * wins / n, 1),
        "exp_wins": round(exp, 1), "z": round(z, 2), "pval": insider.norm_sf(z),
        "z_is": None if z_is is None else round(z_is, 2),
        "z_oos": None if z_oos is None else round(z_oos, 2),
        "avg_entry": round(avg_p, 2), "band_0204": round(band, 2),
        "markets_seen": c.get("markets_seen", 0),
    }


def bh_threshold(rows, q):
    m = len(rows)
    if not m:
        return 0.0
    ps = sorted(r["pval"] for r in rows)
    k = 0
    for i, p in enumerate(ps, 1):
        if p <= q * i / m:
            k = i
    return ps[k - 1] if k else 0.0


def main():
    cands = json.load(open(os.path.join(HERE, "candidates.json")))
    cands.sort(key=lambda c: c.get("markets_seen", 0), reverse=True)
    cands = cands[:SCORE_TOP]
    print(f"scoring {len(cands):,} candidates (window {WINDOW_DAYS}d, "
          f"min_n {MIN_N}, max_n {MAX_N})…", flush=True)

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(score_wallet, c): c for c in cands}
        for f in as_completed(futs):
            r = f.result()
            if r:
                rows.append(r)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(cands)} scored · {len(rows)} with >= {MIN_N} bets",
                      flush=True)

    if not rows:
        print("no wallets cleared the bet minimum.")
        return
    thresh = bh_threshold(rows, FDR_Q)

    # the skilled set: FDR-significant AND out-of-sample-positive
    skilled = [r for r in rows
               if thresh > 0 and r["pval"] <= thresh
               and (r["z_oos"] is None or r["z_oos"] > 0)]
    # tier: "validated" = enough bets for a real OOS split and it held
    for r in skilled:
        r["tier"] = ("validated" if (r["z_oos"] is not None and r["z_oos"] > 0)
                     else "candidate")
    skilled.sort(key=lambda r: (r["tier"] == "validated", r["z"]), reverse=True)

    # z-weighted watchlist, compatible with webhook_receiver (wallet/name/weight)
    tot = sum(r["z"] for r in skilled) or 1
    watch = [{"wallet": r["wallet"], "name": r["username"],
              "weight": round(r["z"] / tot, 4), "tier": r["tier"],
              "z": r["z"], "z_oos": r["z_oos"], "n": r["n"],
              "win_rate": r["win_rate"], "avg_entry": r["avg_entry"],
              "band_0204": r["band_0204"]} for r in skilled]
    json.dump(watch, open(os.path.join(HERE, OUT), "w"), indent=2)
    json.dump(rows, open(os.path.join(HERE, OUT.replace(".json", "_scored.json")), "w"))

    val = sum(1 for r in skilled if r["tier"] == "validated")
    print(f"\nscored {len(rows):,} wallets · BH@{int(FDR_Q*100)}% threshold p<= {thresh:.1e}")
    print(f"SKILLED: {len(skilled)} ({val} validated OOS, {len(skilled)-val} candidate) "
          f"-> watch_skilled.json\n")
    hdr = f"{'tier':>10}{'z':>6}{'z_oos':>6}{'rec':>12}{'win%':>6}{'avgP':>6}{'0.2-0.4':>8}  wallet"
    print(hdr); print("-" * len(hdr))
    for r in skilled[:40]:
        oos = "n/a" if r["z_oos"] is None else f"{r['z_oos']:.1f}"
        rec = f"{r['wins']}/{r['n']}"
        print(f"{r['tier']:>10}{r['z']:>6.1f}{oos:>6}{rec:>12}"
              f"{r['win_rate']:>5.0f}%{r['avg_entry']:>6.2f}{r['band_0204']:>8.2f}  {r['username'][:18]}")


if __name__ == "__main__":
    main()
