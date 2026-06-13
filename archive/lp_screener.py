#!/usr/bin/env python3
"""Liquidity-rewards market screener.

Ranks Polymarket's reward-eligible markets by *risk-adjusted* yield, so we can
find where providing liquidity actually pays — high reward pool, thin enough
book to capture share, but stable enough not to get picked off.

For each market it pulls the order book and a 24h price series and computes:
  - gross APR  : reward pool / competition, for a $1000 two-sided position
  - vol_24h    : stdev of 15-min midpoint moves, in cents = adverse-selection proxy
  - hrs_to_end : time to resolution (imminent = toxic/live)
  - score      : gross APR penalized by volatility and imminence

GROSS APR ignores pick-off losses — that's exactly what vol_24h flags. A high
APR with high vol is a trap; the sweet spot is decent APR with low vol and
days (not hours) to resolution.

    python3 lp_screener.py --min-rate 50 --capital 1000
"""

import argparse
import csv
import json
import ssl
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

CLOB = "https://clob.polymarket.com"
SSL_CTX = ssl._create_unverified_context()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


def reward_markets():
    out, cursor = [], ""
    for _ in range(20):
        try:
            url = CLOB + "/sampling-markets" + (f"?next_cursor={cursor}" if cursor else "")
            d = get(url)
        except Exception:
            break
        out += d.get("data", [])
        cursor = d.get("next_cursor")
        if not cursor or not d.get("data"):
            break
    return out


def daily_rate(m):
    rts = (m.get("rewards") or {}).get("rates") or []
    return sum(x.get("rewards_daily_rate", 0) for x in rts)


def hours_to_end(m):
    iso = m.get("end_date_iso")
    if not iso:
        return None
    try:
        end = time.mktime(time.strptime(iso.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))
        return (end - time.time()) / 3600
    except ValueError:
        return None


def realized_vol_cents(token_id):
    """Stdev of 15-min midpoint moves over the last 24h, in cents."""
    now = int(time.time())
    try:
        h = get(f"{CLOB}/prices-history?market={token_id}"
                f"&startTs={now - 86400}&endTs={now}&fidelity=15").get("history", [])
    except Exception:
        return None
    prices = [p["p"] for p in h if "p" in p]
    if len(prices) < 4:
        return None
    diffs = [abs(prices[i] - prices[i - 1]) * 100 for i in range(1, len(prices))]
    return round(statistics.pstdev(diffs), 2)


def analyze(m, capital):
    pool = daily_rate(m)
    r = m.get("rewards") or {}
    ms = r.get("max_spread", 0) / 100.0
    toks = m.get("tokens") or []
    if not toks or ms <= 0:
        return None
    tok = toks[0].get("token_id")
    try:
        bk = get(f"{CLOB}/book?token_id={tok}")
    except Exception:
        return None
    bids = [(float(o["price"]), float(o["size"])) for o in bk.get("bids", [])]
    asks = [(float(o["price"]), float(o["size"])) for o in bk.get("asks", [])]
    if not bids or not asks:
        return None
    bb = max(p for p, _ in bids)
    ba = min(p for p, _ in asks)
    mid = (bb + ba) / 2
    comp_bid = sum(p * s for p, s in bids if p >= mid - ms)
    comp_ask = sum((1 - p) * s for p, s in asks if p <= mid + ms)
    myside = capital / 2
    share = min(myside / (myside + comp_bid), myside / (myside + comp_ask))
    apr = pool * share / capital * 365 * 100
    vol = realized_vol_cents(tok)
    hrs = hours_to_end(m)
    # risk-adjusted score: reward yield, penalized by adverse selection (vol)
    # and by imminence (markets resolving within a day are live/toxic).
    vol_pen = 1 + (vol if vol is not None else 5)        # unknown vol treated as risky
    time_pen = 1.0 if (hrs is None or hrs >= 48) else max(0.15, hrs / 48)
    score = round(apr * time_pen / vol_pen, 1)
    return {
        "question": m.get("question", "?")[:50],
        "daily_usd": round(pool),
        "max_spread_c": r.get("max_spread", 0),
        "min_size": r.get("min_size", 0),
        "mid": round(mid, 3),
        "comp_usd": round(min(comp_bid, comp_ask)),
        "gross_apr": round(apr),
        "vol_24h_c": vol if vol is not None else -1,
        "hrs_to_end": round(hrs) if hrs is not None else -1,
        "score": score,
        "token_id": tok,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-rate", type=float, default=50, help="min $/day pool")
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    print(f"[{time.strftime('%H:%M:%S')}] pulling reward markets...", flush=True)
    mkts = [m for m in reward_markets()
            if m.get("active") and not m.get("closed") and daily_rate(m) >= args.min_rate]
    print(f"[{time.strftime('%H:%M:%S')}] {len(mkts)} markets with >=${args.min_rate}/day "
          f"· analyzing books + volatility...", flush=True)

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(analyze, m, args.capital): m for m in mkts}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
            if done % 100 == 0:
                print(f"  {done}/{len(mkts)}", flush=True)

    rows.sort(key=lambda x: x["score"], reverse=True)
    cols = ["question", "score", "gross_apr", "vol_24h_c", "hrs_to_end", "daily_usd",
            "comp_usd", "max_spread_c", "min_size", "mid", "token_id"]
    with open("lp_markets.csv", "w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'score':>6}{'grossAPR':>9}{'vol_c':>7}{'hrs':>6}{'$/day':>7}"
          f"{'comp$':>9}{'spr':>5}  market")
    print("-" * 92)
    for r in rows[:args.top]:
        vol = "n/a" if r["vol_24h_c"] < 0 else f"{r['vol_24h_c']:.1f}"
        hrs = "?" if r["hrs_to_end"] < 0 else r["hrs_to_end"]
        print(f"{r['score']:>6.0f}{r['gross_apr']:>8}%{vol:>7}{hrs:>6}{r['daily_usd']:>7}"
              f"{r['comp_usd']:>9}{r['max_spread_c']:>5}  {r['question'][:42]}")
    print("-" * 92)
    print(f"{len(rows)} markets ranked → lp_markets.csv")
    print("score = gross APR × time-factor ÷ (1+vol).  High APR + low vol + days-to-end = real.")


if __name__ == "__main__":
    main()
