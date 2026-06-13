#!/usr/bin/env python3
"""Wide insider hunt — source candidate wallets from many markets across the
volume spectrum, dedup, score each wallet's full record once, and tally the
insider-grade ones (z-score / p-value) with the markets they showed up in."""

import csv
import json
import ssl
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import insider

ctx = ssl._create_unverified_context()
GAMMA = "https://gamma-api.polymarket.com"
N_MARKETS = 100      # markets to source traders from
TOP_TRADERS = 20     # top traders (by notional) per market
MAX_WALLETS = 400    # cap on unique wallets to score (expensive step)


def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=ctx).read())


def source_markets():
    """A spread of active binary markets: high-volume AND mid/niche (where
    insiders operate). Walk the volume ranking and sample across it."""
    mkts, offset = [], 0
    for _ in range(8):
        try:
            g = get(f"{GAMMA}/markets?limit=100&offset={offset}&active=true"
                    f"&closed=false&order=volume24hr&ascending=false")
        except Exception:
            break
        if not g:
            break
        for m in g:
            try:
                oc = json.loads(m.get("outcomes", "[]"))
            except Exception:
                oc = []
            if [o.lower() for o in oc] == ["yes", "no"] and m.get("conditionId"):
                mkts.append((m["conditionId"], m.get("question", "?")[:48]))
        if len(g) < 100:
            break
        offset += 100
    # sample evenly across the ranking so we get a volume spread, not just whales
    if len(mkts) > N_MARKETS:
        step = len(mkts) / N_MARKETS
        mkts = [mkts[int(i * step)] for i in range(N_MARKETS)]
    return mkts


def main():
    key = insider.sm_load_key()
    print(f"sourcing traders from up to {N_MARKETS} markets...", flush=True)
    markets = source_markets()
    print(f"  {len(markets)} binary markets", flush=True)

    wallet_markets = defaultdict(set)
    wallet_name = {}

    def grab(cm):
        cond, title = cm
        try:
            cands, _ = insider.market_traders(cond, top=TOP_TRADERS)
            return title, cands
        except Exception:
            return title, []

    with ThreadPoolExecutor(max_workers=12) as ex:
        for title, cands in ex.map(grab, markets):
            for c in cands:
                wallet_markets[c["wallet"]].add(title)
                wallet_name.setdefault(c["wallet"], c["username"])
    print(f"  {len(wallet_markets)} unique candidate wallets", flush=True)

    # prioritize wallets seen in more markets (more active → enough resolved bets)
    wallets = sorted(wallet_markets, key=lambda w: len(wallet_markets[w]), reverse=True)[:MAX_WALLETS]
    print(f"scoring {len(wallets)} wallets...", flush=True)

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(insider.analyze, {"wallet": w, "username": wallet_name[w]}): w
                for w in wallets}
        for f in as_completed(futs):
            done += 1
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                r["markets"] = sorted(wallet_markets[r["wallet"]])
                rows.append(r)
            if done % 50 == 0:
                print(f"  {done}/{len(wallets)}", flush=True)

    rows.sort(key=lambda r: r["z"], reverse=True)
    with open("huntwide.csv", "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["z", "pval", "wins", "n", "pre24_pct", "trades", "username", "wallet", "markets"])
        for r in rows:
            w.writerow([r["z"], r["pval"], r["wins"], r["n"], r["pre24_pct"],
                        r["trades"], r["username"], r["wallet"], " | ".join(r["markets"][:6])])

    extra = [r for r in rows if r["z"] >= 5]
    grade = [r for r in rows if 4 <= r["z"] < 5]
    sharp = [r for r in rows if 3 <= r["z"] < 4]
    print(f"\n{'='*80}")
    print(f"SCORED {len(rows)} wallets with >=15 resolved bets")
    print(f"  extraordinary (z>=5): {len(extra)}   insider-grade (z 4-5): {len(grade)}   "
          f"strong sharp (z 3-4): {len(sharp)}")
    print(f"{'='*80}")
    for label, group in (("EXTRAORDINARY (z>=5)", extra), ("INSIDER-GRADE (z 4-5)", grade)):
        if not group:
            continue
        print(f"\n{label}:")
        for r in group:
            print(f"  z={r['z']:>4} p={insider.fmt_p(r['pval']):>8} {r['wins']}/{r['n']} "
                  f"pre24={r['pre24_pct']:>3}% trades={r['trades']:>6}  {r['username'][:20]}")
            print(f"      markets: {', '.join(r['markets'][:4])}")
    print(f"\nfull table → huntwide.csv")


if __name__ == "__main__":
    main()
