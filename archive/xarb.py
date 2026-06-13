#!/usr/bin/env python3
"""Polymarket <-> Kalshi cross-venue arbitrage scanner.

Pulls live prices from both venues, matches the same event across them, and
flags executable spreads: buy YES on one + NO on the other for < $1 (net of
fees) = locked profit regardless of resolution.

    python3 xarb.py                 # one-shot scan -> xarb_hits.csv
    python3 xarb.py --min-vol 5000  # only liquid markets

Matching is conservative (token overlap + same resolution month) to limit
false matches — a wrong match isn't an arb, it's two different bets
(resolution risk). Treat flagged hits as candidates to eyeball, not gospel.
"""

import argparse
import csv
import json
import re
import ssl
import urllib.request
from collections import defaultdict

ctx = ssl._create_unverified_context()
K = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"

STOP = {"will", "the", "a", "an", "to", "of", "in", "by", "be", "win", "wins",
        "winner", "2026", "2025", "at", "on", "for", "and", "vs", "game", "match",
        "who", "what", "during", "this", "next", "before", "after", "his", "her"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode())


def get_safe(url):
    try:
        return get(url)
    except Exception:
        return None


def fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return {t for t in s.split() if t not in STOP and len(t) > 1}


def nums(s):
    """Numeric tokens (thresholds, scores, dates) that must match exactly for
    two markets to be the *same* contract, not just the same event."""
    return set(re.findall(r"\d+", (s or "").lower()))


def kalshi_fee(price):
    """Kalshi taker fee per $1 contract ≈ 0.07 * P * (1-P)."""
    return 0.07 * price * (1 - price)


# ── data pulls ──────────────────────────────────────────────────────────────

def pull_kalshi(min_vol):
    evs, cur = [], ""
    for _ in range(60):
        d = get(K + "/events?limit=200&status=open&with_nested_markets=true"
                + (f"&cursor={cur}" if cur else ""))
        evs += d.get("events", [])
        cur = d.get("cursor")
        if not cur:
            break
    out = []
    for e in evs:
        for m in e.get("markets", []):
            ya, na = fnum(m.get("yes_ask_dollars")), fnum(m.get("no_ask_dollars"))
            if not (0 < ya < 1 and 0 < na < 1):
                continue
            vol = fnum(m.get("volume_24h_fp"))
            if vol < min_vol:
                continue
            text = f"{e.get('title','')} {m.get('yes_sub_title','')}"
            out.append({
                "venue": "kalshi", "ticker": m["ticker"], "text": text,
                "tokens": norm(text), "end": (m.get("close_time") or "")[:7],
                "yes_ask": ya, "no_ask": na,
                "yes_bid": fnum(m.get("yes_bid_dollars")),
                "no_bid": fnum(m.get("no_bid_dollars")), "vol": vol,
            })
    return out


def pull_polymarket(min_vol):
    out, offset = [], 0
    for _ in range(200):
        g = get_safe(f"{GAMMA}/markets?limit=100&offset={offset}&active=true"
                     f"&closed=false&order=volumeNum&ascending=false")
        if not g:
            break
        for m in g:
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
            except Exception:
                outcomes = []
            if [o.lower() for o in outcomes] != ["yes", "no"]:
                continue
            ask = fnum(m.get("bestAsk"))
            bid = fnum(m.get("bestBid"))
            if not (0 < ask < 1 and 0 < bid < 1):
                continue
            vol = fnum(m.get("volumeNum"))
            if vol < min_vol:
                continue
            q = m.get("question", "")
            out.append({
                "venue": "poly", "text": q, "tokens": norm(q),
                "end": (m.get("endDateIso") or m.get("endDate") or "")[:7],
                "yes_ask": ask, "no_ask": round(1 - bid, 4),  # NO ask ≈ 1 - YES bid
                "vol": vol,
            })
        if len(g) < 100:
            break
        offset += 100
    return out


# ── matching + arb ────────────────────────────────────────────────────────

def match_and_scan(poly, kalshi, min_sim):
    # inverted index: token -> kalshi markets containing it
    idx = defaultdict(list)
    for k in kalshi:
        for t in k["tokens"]:
            idx[t].append(k)
    hits = []
    for p in poly:
        if len(p["tokens"]) < 2:
            continue
        p_nums = nums(p["text"])
        cand = {id(k): k for t in p["tokens"] for k in idx.get(t, [])}
        best, best_sim = None, 0
        for k in cand.values():
            if p["end"] and k["end"] and p["end"] != k["end"]:
                continue  # different resolution month → skip
            if nums(k["text"]) != p_nums:
                continue  # different thresholds/scores/dates → not the same contract
            inter = len(p["tokens"] & k["tokens"])
            sim = inter / len(p["tokens"] | k["tokens"])
            if sim > best_sim:
                best, best_sim = k, sim
        if not best or best_sim < min_sim:
            continue
        # two arb directions
        # A: poly YES + kalshi NO
        a_cost = p["yes_ask"] + best["no_ask"]
        a_edge = 1 - a_cost - kalshi_fee(best["no_ask"])
        # B: kalshi YES + poly NO
        b_cost = best["yes_ask"] + p["no_ask"]
        b_edge = 1 - b_cost - kalshi_fee(best["yes_ask"])
        if a_edge >= b_edge:
            edge, leg = a_edge, "poly YES + kalshi NO"
        else:
            edge, leg = b_edge, "kalshi YES + poly NO"
        hits.append({
            "edge_c": round(edge * 100, 2), "sim": round(best_sim, 2),
            "leg": leg, "poly": p["text"][:46], "kalshi": best["text"][:46],
            "p_yes": p["yes_ask"], "p_no": p["no_ask"],
            "k_yes": best["yes_ask"], "k_no": best["no_ask"],
            "min_vol": round(min(p["vol"], best["vol"])),
        })
    hits.sort(key=lambda h: h["edge_c"], reverse=True)
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-vol", type=float, default=2000)
    ap.add_argument("--min-sim", type=float, default=0.5, help="token-overlap threshold")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    print("pulling Kalshi...", flush=True)
    kalshi = pull_kalshi(args.min_vol)
    print(f"  {len(kalshi)} liquid Kalshi markets", flush=True)
    print("pulling Polymarket...", flush=True)
    poly = pull_polymarket(args.min_vol)
    print(f"  {len(poly)} liquid Polymarket markets", flush=True)

    hits = match_and_scan(poly, kalshi, args.min_sim)
    with open("xarb_hits.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(hits[0].keys()) if hits else
                           ["edge_c", "sim", "leg", "poly", "kalshi"])
        w.writeheader()
        w.writerows(hits)

    arbs = [h for h in hits if h["edge_c"] > 0]
    print(f"\nmatched pairs: {len(hits)} · positive-edge (after fees): {len(arbs)}")
    print(f"\n{'edge¢':>6}{'sim':>5}{'minVol':>9}  match (poly ↔ kalshi)")
    print("-" * 92)
    for h in hits[:args.top]:
        print(f"{h['edge_c']:>6.1f}{h['sim']:>5.2f}{h['min_vol']:>9}  "
              f"{h['poly'][:34]:34} ↔ {h['kalshi'][:30]}")
    print("-" * 92)
    print(f"saved {len(hits)} matched pairs → xarb_hits.csv  (edge>0 = arb after fees)")
    return arbs, hits


if __name__ == "__main__":
    main()
