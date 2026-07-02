#!/usr/bin/env python3
"""Polymarket insider-pattern detector.

Replicates the per-wallet methodology behind the Bubblemaps / 60 Minutes work:
flag wallets whose results are too good to be luck and whose behavior fits the
insider fingerprint. All signals come from Polymarket's public data API.

Signals per wallet (over resolved bets in a recent window):
  1. IMPROBABILITY  — wins vs the wins their entry odds imply. Each bet entered
     at price p has expected win prob p; observed wins far above Σp is the
     "luck alone can't explain this" z-score (and one-sided p-value).
  2. PRE-RESOLUTION TIMING — how long before a market resolved they entered.
     Entering minutes/hours before resolution is the classic advance-knowledge
     tell. We report median lead time and the share of wins entered <24h out.
  3. FRESH WALLET — account age (first observed trade). New account + big
     improbable wins is a strong flag.
  4. SIZING — average / max bet size.

Composite suspicion 0-10. Funding-cluster linking (the Bubblemaps "who funded
whom" step) needs a Polygonscan/Alchemy key — see cluster_stub().

    python3 insider.py --scan 40        # score top-40 leaderboard wallets
    python3 insider.py --wallet 0xABC…  # deep profile one wallet
"""

import argparse
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import smart_money as sm

WINDOW_DAYS = 120
WEEK = 7 * 86400


def _parse_end(end):
    if not end:
        return 0
    end = end.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(end, fmt))
        except ValueError:
            continue
    return 0


def resolved_bets(wallet, cutoff, max_pages=40, strict=False):
    """Resolved bets with entry price, conditionId, token (asset), resolution
    time, size, and provenance (cache schema v2).

    * ``p`` is the RAW avgPrice (0 when the API omits it) — callers clamp for
      the z math; storing raw keeps "missing price" distinguishable from a real
      0.1¢ longshot in the cache.
    * ``asset`` (token id) is the position identity — it disambiguates the
      two-endpoint union (same asset in /closed-positions and /positions is ONE
      position seen twice, not two bets) and YES/NO both-sides holdings.
    * ``resolved`` is False for early-sold positions in markets that had not
      ended at pull time — their ``won`` is a curPrice mark, not an outcome.
    * ``strict``: raise on a failed page pull instead of returning a silently
      truncated history — a partial pull must never be cached as a wallet's
      complete record.
    """
    now = time.time()
    out = []
    for endpoint in ("/closed-positions", "/positions"):
        off = 0
        while off < max_pages * 50:
            params = {"user": wallet, "limit": 50, "offset": off}
            if endpoint == "/closed-positions":
                params.update(sortBy="TIMESTAMP", sortDirection="DESC")
            else:
                params["sizeThreshold"] = 0.0
            page = sm.get_json(endpoint, params)
            if page is None and strict:
                raise RuntimeError(f"{endpoint} pull failed for {wallet} at offset {off}")
            if not page:
                break
            for p in page:
                end = _parse_end(p.get("endDate"))
                if endpoint == "/closed-positions":
                    ts = p.get("timestamp", 0)
                    if ts < cutoff:
                        continue
                    res_t = end or ts
                    resolved = bool(end) and end <= now
                else:
                    ts = None
                    if not (cutoff <= end < now):
                        continue
                    res_t = end
                    resolved = True
                out.append({
                    "won": p.get("curPrice", 0) >= 0.5,
                    "p": p.get("avgPrice", 0) or 0,          # raw — callers clamp
                    "cond": p.get("conditionId"),
                    "asset": p.get("asset"),
                    "res_t": res_t,
                    "size": p.get("initialValue") or
                            (p.get("avgPrice", 0) * p.get("totalBought", 0)),
                    "src": "closed" if endpoint == "/closed-positions" else "open",
                    "ts": ts,
                    "resolved": resolved,
                })
            off += 50
            if len(page) < 50:
                break
            if endpoint == "/closed-positions" and page[-1].get("timestamp", 0) < cutoff:
                break
    return out


def entry_times(wallet, max_pages=20):
    """conditionId -> earliest BUY timestamp; plus account-age (first trade)."""
    first_buy = {}
    earliest = time.time()
    off = 0
    while off < max_pages * 500:
        page = sm.get_json("/activity",
                           {"user": wallet, "type": "TRADE", "limit": 500, "offset": off})
        if not page:
            break
        for t in page:
            ts = t.get("timestamp", 0)
            earliest = min(earliest, ts) if ts else earliest
            if t.get("side") == "BUY" and t.get("conditionId"):
                c = t["conditionId"]
                if c not in first_buy or ts < first_buy[c]:
                    first_buy[c] = ts
        off += 500
        if len(page) < 500:
            break
    return first_buy, earliest


def norm_sf(z):
    """One-sided normal survival P(Z>z) — the 'probability this was luck'."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def analyze(cand):
    wallet = cand["wallet"]
    cutoff = time.time() - WINDOW_DAYS * 86400
    bets = resolved_bets(wallet, cutoff)
    if len(bets) < 15:
        return None
    for b in bets:                       # v2 returns raw p — clamp for the z math
        b["p"] = max(0.001, min(0.999, b["p"] or 0))
    first_buy, _ = entry_times(wallet)
    total_trades = (sm.get_json("/traded", {"user": wallet}) or {}).get("traded", 0)

    n = len(bets)
    wins = sum(1 for b in bets if b["won"])
    exp = sum(b["p"] for b in bets)                       # expected wins by odds
    var = sum(b["p"] * (1 - b["p"]) for b in bets) or 1e-9
    z = (wins - exp) / math.sqrt(var)                     # wins above odds-implied
    pval = norm_sf(z)

    # pre-resolution timing on WINNING bets we can time
    leads = []
    for b in bets:
        if b["won"] and b["cond"] in first_buy and b["res_t"]:
            lead_h = (b["res_t"] - first_buy[b["cond"]]) / 3600
            if lead_h >= 0:
                leads.append(lead_h)
    median_lead = sorted(leads)[len(leads) // 2] if leads else None
    pre24 = (sum(1 for l in leads if l < 24) / len(leads)) if leads else 0

    avg_size = sum(b["size"] for b in bets) / n
    max_size = max(b["size"] for b in bets)

    # IMPROBABILITY GATES the score: a wallet winning at/below its odds cannot
    # be an insider regardless of timing or size. Only when wins clearly exceed
    # what the entry odds imply (z high) do the other signals amplify.
    improb = 0.0 if z < 1 else min(7, (z - 1) * 2.0)      # z=1→0, 2→2, 4.5→7
    score = improb
    if improb > 0:                                        # amplifiers, gated
        score += 1.5 if total_trades < 20 else (0.7 if total_trades < 60 else 0)
        score += 1.0 if avg_size > 5000 else (0.5 if avg_size > 1000 else 0)
        score += 1.0 if pre24 > 0.5 else 0                # late entry + improbable
    score = round(min(10, score), 1)

    return {
        "username": cand["username"], "wallet": wallet,
        "n": n, "wins": wins, "exp_wins": round(exp, 1),
        "z": round(z, 1), "pval": pval,
        "med_lead_h": round(median_lead, 1) if median_lead is not None else None,
        "pre24_pct": round(pre24 * 100),
        "trades": total_trades, "avg_size": round(avg_size),
        "max_size": round(max_size), "score": score,
    }


def market_traders(market, top=40):
    """Wallets who traded a market, ranked by notional in it. Start from a
    suspicious market and score everyone — the Bubblemaps approach."""
    cond = market
    if not market.startswith("0x"):                      # treat as slug
        g = sm.get_json("/markets" if False else None) or None
        import urllib.request, json as _j, ssl as _ssl
        c = _ssl._create_unverified_context()
        req = urllib.request.Request(
            f"https://gamma-api.polymarket.com/markets?slug={market}",
            headers={"User-Agent": "Mozilla/5.0"})
        gm = _j.loads(urllib.request.urlopen(req, timeout=20, context=c).read())
        cond = gm[0]["conditionId"] if gm else market
    notional = {}
    names = {}
    off = 0
    while off < 8000:
        page = sm.get_json("/trades", {"market": cond, "limit": 500, "offset": off})
        if not page:
            break
        for t in page:
            w = t.get("proxyWallet")
            if not w:
                continue
            notional[w] = notional.get(w, 0) + t.get("usdcSize", 0)
            names.setdefault(w, t.get("name") or w[:10] + "…")
        off += 500
        if len(page) < 500:
            break
    ranked = sorted(notional, key=notional.get, reverse=True)[:top]
    return [{"wallet": w, "username": names[w]} for w in ranked], cond


USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def wallet_funders(wallet, key, max_count=200):
    """Set of addresses that sent USDC to this wallet (its funding sources),
    via Alchemy getAssetTransfers (full history, no block-range cap)."""
    import urllib.request
    import ssl as _ssl
    url = f"https://polygon-mainnet.g.alchemy.com/v2/{key}"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
                       "params": [{"fromBlock": "0x0", "toBlock": "latest",
                                   "toAddress": wallet, "contractAddresses": [USDC],
                                   "category": ["erc20"], "excludeZeroValue": True,
                                   "maxCount": hex(max_count)}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=30,
                       context=_ssl._create_unverified_context()).read())
        return {x["from"].lower() for x in d.get("result", {}).get("transfers", [])}
    except Exception:
        return set()


def funder_outdegree(addr, key, cap=300):
    """Distinct USDC recipients this address has sent to. Exchanges/bridges fan
    out to hundreds–thousands; a personal funding hub sends to a handful. This
    is what separates a real shared-operator link from 'both used Coinbase'."""
    url = f"https://polygon-mainnet.g.alchemy.com/v2/{key}"
    import urllib.request
    import ssl as _ssl
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
                       "params": [{"fromBlock": "0x0", "toBlock": "latest", "fromAddress": addr,
                                   "contractAddresses": [USDC], "category": ["erc20"],
                                   "excludeZeroValue": True, "maxCount": hex(cap)}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=30,
                       context=_ssl._create_unverified_context()).read())
        xf = d.get("result", {}).get("transfers", [])
        return len({x["to"].lower() for x in xf if x.get("to")}), len(xf) >= cap
    except Exception:
        return 9999, True


# a personal funding hub sends to at most ~this many distinct wallets
MAX_HUB_RECIPIENTS = 15


def cluster(wallets, key, max_workers=8):
    """Link wallets that share a *personal* funding source (same operator).
    A shared funder only counts if it isn't an exchange/bridge/infra address —
    judged by its outbound degree, not by how many of our wallets it touches."""
    funders = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut = {ex.submit(wallet_funders, w, key): w for w in wallets}
        for f in as_completed(fut):
            funders[fut[f]] = f.result()
    by_funder = {}
    for w, fs in funders.items():
        for src in fs:
            by_funder.setdefault(src, set()).add(w)
    shared = {src: ws for src, ws in by_funder.items() if len(ws) >= 2}
    # keep only shared funders that look like a personal hub (low outbound degree)
    links = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        deg = {src: ex.submit(funder_outdegree, src, key) for src in shared}
        for src, fu in deg.items():
            recipients, capped = fu.result()
            if not capped and recipients <= MAX_HUB_RECIPIENTS:
                links[src] = shared[src]
    # union-find to merge wallets linked through any shared funder
    parent = {w: w for w in wallets}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        parent[find(a)] = find(b)
    for ws in links.values():
        ws = list(ws)
        for w in ws[1:]:
            union(ws[0], w)
    groups = {}
    for w in wallets:
        groups.setdefault(find(w), []).append(w)
    return [g for g in groups.values() if len(g) > 1], links


def fmt_p(p):
    if p <= 0:
        return "<1e-12"
    if p < 0.001:
        return f"{p:.1e}"
    return f"{p:.3f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", type=int, default=40, help="score top-N leaderboard wallets")
    ap.add_argument("--wallet", help="deep-profile a single wallet")
    ap.add_argument("--market", help="score the traders of a market (conditionId or slug)")
    args = ap.parse_args()

    if args.wallet:
        cands = [{"wallet": args.wallet, "username": args.wallet[:12] + "…"}]
    elif args.market:
        cands, cond = market_traders(args.market)
        print(f"market {cond[:20]}… · scoring {len(cands)} top traders by notional\n")
    else:
        cands = sm.leaderboard_candidates(args.scan)

    rows = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(analyze, c): c for c in cands}
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
    rows.sort(key=lambda r: r["score"], reverse=True)

    h = (f"{'susp':>5}{'z':>6}{'p(luck)':>9}{'rec':>11}{'medLead':>8}"
         f"{'pre24':>6}{'trades':>7}{'avgSz':>8}  trader")
    print(h)
    print("-" * len(h))
    for r in rows:
        rec = f"{r['wins']}/{r['n']}(E{r['exp_wins']:.0f})"
        lead = "n/a" if r["med_lead_h"] is None else f"{r['med_lead_h']:.0f}h"
        print(f"{r['score']:>5.1f}{r['z']:>6.1f}{fmt_p(r['pval']):>9}{rec:>11}"
              f"{lead:>8}{r['pre24_pct']:>5}%{r['trades']:>7}{'$'+format(r['avg_size'],','):>8}"
              f"  {r['username'][:22]}")
    print("-" * len(h))
    print("susp gated by improbability: a wallet must win ABOVE its odds (z>1) to score at all.")
    print("z=wins above odds-implied · p(luck)=prob it was chance · pre24=% wins entered <24h out")

    key = sm_load_key()
    if key and len(cands) > 1:
        print("\nfunding-trace clustering (shared USDC funders -> same operator)...")
        wallets = [c["wallet"] for c in cands]
        name = {c["wallet"]: c["username"] for c in cands}
        groups, _ = cluster(wallets, key)
        if groups:
            print(f"** {len(groups)} linked cluster(s) — wallets sharing a non-infra funder:")
            for g in sorted(groups, key=len, reverse=True):
                print("   - " + "  +  ".join(name.get(w, w[:10]) for w in g))
        else:
            print("no shared-funder clusters (independently funded, or via shared exchange/infra).")
    elif not key:
        print("\n(add \"alchemy_key\" to config.json to enable funding-cluster linking)")


def sm_load_key():
    try:
        with open("config.json") as f:
            return json.load(f).get("alchemy_key", "")
    except Exception:
        return ""


if __name__ == "__main__":
    main()
