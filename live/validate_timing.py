#!/usr/bin/env python3
"""Last-minute-vs-sharp check on the standout conviction wallets.

This is a COPYABILITY heuristic, not proof of inside information: a near-100%
win rate is only useful to us if we can actually mirror it. The tell is entry->
resolution lead time on their WINNING conviction bets:
  * mostly < 1h before resolution  -> last-minute, you can't follow it in time
  * hours-to-days of lead          -> a sharp you could actually mirror
A short lead can mean a genuine insider OR just someone who trades fast-resolving
markets (live sports, hourly) well — we can't tell which, and for copy purposes
it doesn't matter: either way the window is too tight to mirror.
"""

import json
import os
import ssl
import statistics as st
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import cache
import smart_money as sm

HERE = os.path.dirname(__file__)
COPYABLE_MED_LEAD = 24.0     # median lead (h) on winning conviction bets to count as copyable
JUN1 = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))   # portfolio copy-start
STAKE = 50.0                 # flat $/trade the copy portfolio uses
_SSL = ssl._create_unverified_context()
_CLOB = {}                   # conditionId -> {token_id: winner-price 1/0/None}


def _clob_winner(cond, token):
    """Authoritative resolution for a token: 1 if it won, 0 if it lost, None if the
    market hasn't resolved. Matched by token_id (exact, no outcome-name guessing)."""
    if cond not in _CLOB:
        try:
            req = urllib.request.Request("https://clob.polymarket.com/markets/" + cond,
                                         headers={"User-Agent": "Mozilla/5.0"})
            m = json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())
            _CLOB[cond] = {str(t.get("token_id")):
                           (1 if t.get("winner") is True else 0 if t.get("winner") is False else None)
                           for t in (m.get("tokens") or [])}
        except Exception:
            _CLOB[cond] = {}
    return _CLOB[cond].get(str(token))


def _bet_pnl(b):
    """Resolved (outcome) P&L of one cache bet: a $size stake at avg price p pays
    size/p if won, else $0 — so P&L = size·(1−p)/p if won else −size."""
    p = max(0.001, min(0.999, b["p"] or 0))
    return b["size"] * ((1 - p) / p if b["won"] else -1)


def display_stats(w):
    """Everything the dashboard's sharp table renders, precomputed so the page makes
    ZERO per-wallet data-api calls.

      conv win%/record/P&L : over the wallet's conviction (top-20%-stake) bets — a
                             POSITION stat from the cache (large 180d sample)
      realized P&L         : reconstructed P&L over the last 500 resolved bets
      copy P&L             : the TRUTH for a copier — what a flat-$50 copy of their
                             conviction bets ACTUALLY realizes since Jun 1: replays
                             their entries, mirrors their exits, settles held bets at
                             AUTHORITATIVE clob resolution (by token id). This exposes
                             scalpers whose position win% looks great but don't copy
                             (e.g. ArbTrader: ~100% conv win but −$790 copy P&L).
      name / last-bet      : from the /activity pull
    """
    # ---- position win%/record/P&L from the cache (large, survivorship-corrected) ----
    bets = [b for b in cache.get_bets(w) if (b["size"] or 0) > 0]
    thr = cache.conv_cutoff(b["size"] for b in bets)
    conv = [b for b in bets if b["size"] >= thr]
    won = sum(1 for b in conv if b["won"])
    recent = sorted(bets, key=lambda b: b["res_t"] or 0, reverse=True)[:500]
    cut30 = time.time() - 30 * 86400
    conv30 = [b for b in conv if (b["res_t"] or 0) >= cut30]
    won30 = sum(1 for b in conv30 if b["won"])
    out = {
        "conv_win": round(100 * won / len(conv), 1) if conv else None,
        "conv_won": won, "conv_lost": len(conv) - won,
        "conv_pnl": round(sum(_bet_pnl(b) for b in conv)),
        "conv30_win": round(100 * won30 / len(conv30), 1) if conv30 else None,
        "conv30_won": won30, "conv30_lost": len(conv30) - won30,
        "conv30_pnl": round(sum(_bet_pnl(b) for b in conv30)),
        "realized_pnl": round(sum(_bet_pnl(b) for b in recent)),
        "avg_bet": round(sum(b["size"] for b in conv) / len(conv)) if conv else 0,
        "copy_pnl": 0, "name": None, "last_trade": 0, "last_conv_bet": 0,
    }
    # ---- activity: name, last-bet, and the flat-$50 copy replay (copy P&L) ----
    a = []
    for off in range(0, 4000, 500):
        pg = sm.get_json("/activity", {"user": w, "type": "TRADE", "limit": 500, "offset": off}) or []
        a += pg
        if len(pg) < 500 or (pg and (pg[-1].get("timestamp", 0) < JUN1)):
            break
    if a:
        out["last_trade"] = a[0].get("timestamp", 0)
        out["name"] = next((t.get("name") for t in a if t.get("name")), None)
        # position-level conviction: each market's TOTAL buy stake, top-20% (p80)
        mkt = {}
        for t in a:
            if t.get("side") == "BUY" and t.get("conditionId"):
                mkt[t["conditionId"]] = mkt.get(t["conditionId"], 0) + (t.get("usdcSize", 0) or 0)
        cthr = cache.conv_cutoff(mkt.values())
        for t in a:
            if t.get("side") == "BUY" and mkt.get(t.get("conditionId"), 0) >= cthr:
                out["last_conv_bet"] = t.get("timestamp", 0)
                break
        # replay a flat-$50 copy of their conviction markets since Jun 1
        ev = sorted([t for t in a if t.get("timestamp", 0) >= JUN1], key=lambda t: t.get("timestamp", 0))
        openp, entered, copy = {}, set(), 0.0
        for t in ev:
            c, pr, asset = t.get("conditionId"), t.get("price", 0) or 0, t.get("asset")
            if not c or pr <= 0:
                continue
            if t.get("side") == "BUY":
                if mkt.get(c, 0) < cthr or c in entered or c in openp:
                    continue
                entered.add(c); openp[c] = {"sh": STAKE / pr, "a": asset}
            elif c in openp:                                    # mirror their exit
                copy += openp[c]["sh"] * pr - STAKE; del openp[c]
        for c, p in openp.items():                              # settle held bets at AUTHORITATIVE resolution
            wv = _clob_winner(c, p["a"])
            if wv is None:
                continue                                        # not resolved yet -> exclude
            copy += (p["sh"] if wv else 0) - STAKE
        out["copy_pnl"] = round(copy)
    return out


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
    verdict = ("last-minute" if (med < 6 or sum(1 for l in leads if l < 1) / len(leads) > 0.5)
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
    # enrich the sharps with the exact stats the dashboard renders, so it reads them
    # straight from the feed (1 request) instead of 3 data-api calls per wallet.
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, ds in zip(sharps, ex.map(lambda c: display_stats(c["wallet"]), sharps)):
            c.update(ds)
            if ds.get("name"):
                c["name"] = ds["name"]          # real Polymarket username (else keep prefix)

    # drop wallets that haven't traded in 30 days — the feed should only list
    # currently-active sharps (last_trade comes from display_stats' /activity pull)
    cut30 = time.time() - 30 * 86400
    before = len(sharps)
    sharps = [c for c in sharps if (c.get("last_trade") or 0) >= cut30]
    print(f"active filter: dropped {before - len(sharps)} sharp(s) inactive >30d -> {len(sharps)} active")

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
    print(f"\n-> watch_sharps.json ({len(sharps)} copyable sharps, last-minute wallets filtered out)")


if __name__ == "__main__":
    main()
