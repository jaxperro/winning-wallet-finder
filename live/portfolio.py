#!/usr/bin/env python3
"""Precompute the $1,000 paper portfolio server-side, off the cache.

The dashboard's top page used to replay the followed wallets' trades client-side,
which (a) hammered the data-api/clob from the browser and (b) phantom-locked capital
because the data-api misses resolution dates for high-volume wallets. This computes
the same book here instead, sourced from cache.duckdb — which already stores each
resolved bet's entry price, size, win/loss AND resolution time (res_t), so capital
RECYCLES correctly (cash frees at the true resolution moment). Output -> portfolio.json,
which the dashboard reads in one request.

Model: a $1,000 account that mirrors each followed wallet's CONVICTION bets (top-20%
stake) at a flat $50, held to resolution (the cache has no sell events, which is the
right model for the hold-to-resolution wallets we follow). One position per market
(first wallet to enter wins the slot); when capital is fully deployed a bet is MISSED.
Resolved history + realized P&L come from the cache; currently-open bets come from a
small live /positions pull so the page can still show what's in flight.
"""
import json
import os
import ssl
import time
import urllib.request

import cache
import smart_money as sm

_SSL = ssl._create_unverified_context()

HERE = os.path.dirname(__file__)
BANK = 1000.0
STAKE = 50.0
START = time.mktime(time.strptime("2026-06-23", "%Y-%m-%d"))   # forward test: started following 2026-06-23
GAMMA = "https://gamma-api.polymarket.com"

# the followed wallets — single source of truth (dashboard renders names from the feed)
WALLETS = [
    {"name": "raid3r",     "wallet": "0xa1a77ea9382bb8c3610f3303b66e093f644aace4"},
    {"name": "0x6d1A94f4", "wallet": "0x6d1a94f4bdd53114ec483925d025367db68697fb"},
    {"name": "Kruto2027",  "wallet": "0xe8ca3f758c93f44f3ec210542ab78afb7c0bcccb"},
    {"name": "LSB1",       "wallet": "0x41558102a796ba971c7567cad41c307e59f8fa41"},
]

_MKT = {}
def market_meta(cond):
    """Market title for display, from the CLOB market endpoint (gamma's condition_ids
    filter returns nothing for resolved markets) — cached."""
    if cond not in _MKT:
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                f"https://clob.polymarket.com/markets/{cond}", headers={"User-Agent": "Mozilla/5.0"}),
                timeout=20, context=_SSL)
            m = json.loads(r.read())
            _MKT[cond] = {"title": m.get("question") or "", "slug": m.get("market_slug") or ""}
        except Exception:
            _MKT[cond] = {"title": "", "slug": ""}
    return _MKT[cond]


def conviction_bets():
    """Every followed wallet's resolved conviction bets from the cache, with entry time."""
    out = []
    for w in WALLETS:
        ent = cache.get_entries(w["wallet"])               # cond -> first buy ts
        bets = [b for b in cache.get_bets(w["wallet"]) if (b["size"] or 0) > 0]
        thr = cache.conv_cutoff(b["size"] for b in bets)
        for b in bets:
            if b["size"] < thr:
                continue
            et = ent.get(b["cond"])
            if not et or et < START:                       # only June 1+ entries
                continue
            out.append({"wallet": w["wallet"], "name": w["name"], "cond": b["cond"],
                        "entry_t": et, "p": max(0.001, min(0.999, b["p"] or 0)),
                        "won": b["won"], "res_t": b["res_t"] or 0})
    return out


def open_bets():
    """Currently-held conviction positions (live /positions pull, small) for the
    'current bets' panel — the cache only has resolved bets."""
    out = []
    for w in WALLETS:
        ent = cache.get_entries(w["wallet"])
        ps = sm.get_json("/positions", {"user": w["wallet"], "limit": 500, "sizeThreshold": 0}) or []
        sizes = [(p.get("initialValue") or 0) for p in ps]
        thr = cache.conv_cutoff(sizes)
        for p in ps:
            cp = p.get("curPrice", 0) or 0
            if cp <= 0.001 or cp >= 0.999:                 # resolved -> belongs to history, not open
                continue
            if (p.get("initialValue") or 0) < thr:
                continue
            out.append({"wallet": w["wallet"], "name": w["name"], "cond": p.get("conditionId"),
                        "entry_t": ent.get(p.get("conditionId"), 0),
                        "p": max(0.001, min(0.999, p.get("avgPrice", 0) or 0)),
                        "cur": cp, "title": p.get("title") or "", "outcome": p.get("outcome") or "",
                        "end": p.get("endDate")})
    return out


def main():
    now = time.time()
    resolved_pool = conviction_bets()
    open_pool = open_bets()
    # merge into one entry-ordered stream; one position per market (earliest entry wins)
    by_mkt = {}
    for b in resolved_pool:
        b["kind"] = "res"
        if b["cond"] not in by_mkt or b["entry_t"] < by_mkt[b["cond"]]["entry_t"]:
            by_mkt[b["cond"]] = b
    for b in open_pool:
        if b["cond"] and (b["cond"] not in by_mkt or b["entry_t"] < by_mkt[b["cond"]]["entry_t"]):
            b["kind"] = "open"; by_mkt[b["cond"]] = b
    stream = sorted(by_mkt.values(), key=lambda b: b["entry_t"])

    cash = BANK
    realized = 0.0
    held = []        # (free_t, stake, payoff)  payoff paid at free_t
    perW = {w["wallet"]: {"name": w["name"], "wallet": w["wallet"], "bets": 0,
                          "invested": 0.0, "realized": 0.0} for w in WALLETS}
    resolved, current, missed = [], [], []

    def free(upto):
        nonlocal cash, realized
        keep = []
        for ft, stake, payoff, rec in held:
            if ft and ft <= upto and rec["kind"] == "res":
                cash += payoff; realized += payoff - stake; perW[rec["wallet"]]["realized"] += payoff - stake
                rec["pnl"] = payoff - stake
                resolved.append(rec)
            else:
                keep.append((ft, stake, payoff, rec))
        held[:] = keep

    for b in stream:
        free(b["entry_t"])
        if cash >= STAKE:
            cash -= STAKE; perW[b["wallet"]]["bets"] += 1
            shares = STAKE / b["p"]
            if b["kind"] == "res":
                payoff = shares * (1.0 if b["won"] else 0.0)
                held.append((b["res_t"] or now, STAKE, payoff, b))
            else:                                          # currently open -> mark to market, no free yet
                held.append((None, STAKE, 0.0, b))
                b["val"] = shares * b["cur"]; b["stake"] = STAKE
        else:
            missed.append(b)
    free(now)
    # finalize open (still held with kind==open): mark to market
    invested = 0.0
    for ft, stake, payoff, rec in held:
        if rec["kind"] == "open":
            invested += rec["val"]; rec["pnl"] = rec["val"] - stake
            perW[rec["wallet"]]["invested"] += rec["val"]
            current.append(rec)

    # enrich resolved + missed with titles, keep most-recent 60
    resolved.sort(key=lambda r: r.get("res_t") or 0, reverse=True)
    for r in resolved[:60]:
        m = market_meta(r["cond"]); r["title"] = m["title"]
    missed.sort(key=lambda m: m.get("res_t") or 0, reverse=True)
    for m in missed[:60]:
        m["title"] = market_meta(m["cond"])["title"]
        # hypothetical P&L had we been able to afford it (held to resolution)
        m["pnl"] = STAKE * ((1.0 / m["p"]) - 1) if m["won"] else -STAKE
    wins = sum(1 for r in resolved if r.get("won"))
    equity = cash + invested
    out = {
        "started": START, "updated": now,
        "bank": BANK, "stake": STAKE,
        "equity": round(equity, 2), "liquid": round(cash, 2), "invested": round(invested, 2),
        "realized": round(realized, 2), "pnl": round(equity - BANK, 2),
        "unreal": round(invested - STAKE * len(current), 2),
        "resolved_count": len(resolved), "wins": wins, "losses": len(resolved) - wins,
        "open_count": len(current), "missed_count": len(missed),
        "wallets": [{"name": v["name"], "wallet": v["wallet"], "bets": v["bets"],
                     "invested": round(v["invested"], 2), "realized": round(v["realized"], 2)}
                    for v in perW.values()],
        "current": [{"title": c.get("title", ""), "name": c["name"], "outcome": c.get("outcome", ""),
                     "stake": STAKE, "val": round(c["val"], 2), "pnl": round(c["pnl"], 2),
                     "end": c.get("end")} for c in sorted(current, key=lambda c: c["entry_t"])],
        "resolved": [{"title": r.get("title", ""), "name": r["name"], "won": r["won"],
                      "stake": STAKE, "pnl": round(r["pnl"], 2), "date": r.get("res_t")}
                     for r in resolved[:60]],
        "missed": [{"title": m.get("title", ""), "name": m["name"], "won": m["won"],
                    "stake": STAKE, "pnl": round(m["pnl"], 2), "date": m.get("res_t")}
                   for m in missed[:60]],
        "missed_pnl": round(sum(STAKE * ((1.0 / m["p"]) - 1) if m["won"] else -STAKE for m in missed), 2),
    }
    json.dump(out, open(os.path.join(HERE, "portfolio.json"), "w"), separators=(",", ":"))
    print(f"portfolio: equity ${equity:,.0f} ({(equity-BANK)/BANK*100:+.0f}%) | realized ${realized:+,.0f} "
          f"| {len(resolved)} resolved ({wins}W/{len(resolved)-wins}L) | {len(current)} open "
          f"| {len(missed)} missed | -> portfolio.json", flush=True)


if __name__ == "__main__":
    main()
