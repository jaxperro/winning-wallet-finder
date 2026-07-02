#!/usr/bin/env python3
"""Select the COPYABLE conviction wallets — by what a copier actually earns.

The earlier version gated on entry->resolution lead time (a proxy for "can we
mirror it"). That was too blunt: it kept scalpers whose position win% looks great
but lose when copied, and dropped fast-resolving holders that are perfect for a
small fast-recycling bankroll. The fix: run a full flat-$50 copy replay on every
conviction wallet and SELECT on copyability directly —
  * copy_pnl > 0                 — copying them actually makes money, AND
  * held_pnl > 0 over >= MIN_HELD — their hold-to-resolution edge is real (the
                                    latency-robust leg), not just scalp-sell timing
  * active in 30d, median lead >= MIN_LEAD_H (light guard vs true sub-hour snipers)
This keeps Kruto (sells often but profitably) and surfaces copy-positive holders
the lead gate used to discard; it drops scalper-traps like a wallet that's only
positive via sells while its held bets lose.
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
# Polymarket taker fee (since 2026-03-30): fee = shares·rate·p·(1−p), paid on
# marketable entries AND mirror exits; redeeming at resolution is free. 0.03 is
# the sports rate (the follow set's category). Making copy_pnl fee-aware makes
# the SELECTION fee-aware — a wallet only counts as a copyable sharp if copying
# it clears the fees a real copier pays.
FEE_RATE = 0.03
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
    # ---- position win%/record/P&L from the cache (large, survivorship-corrected).
    # res_t <= now: the cache stores early-sold positions in UNRESOLVED markets with
    # a future res_t and won = current price — a mark, not an outcome; skip them. ----
    now = time.time()
    bets = [b for b in cache.get_bets(w)
            if (b["size"] or 0) > 0 and (b["res_t"] or 0) <= now]
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
        "copy_pnl": 0, "held_pnl": 0, "held_won": 0, "held_lost": 0, "sold": 0,
        "name": None, "last_trade": 0, "last_conv_bet": 0,
    }
    # ---- resolution map from a FRESH positions pull (curPrice extreme = resolved);
    #      cheap, so the copy replay can run on every conviction wallet. clob fills gaps.
    resmap = {}
    for p in (sm.get_json("/closed-positions", {"user": w, "limit": 500,
                          "sortBy": "TIMESTAMP", "sortDirection": "DESC"}) or []) + \
             (sm.get_json("/positions", {"user": w, "limit": 500, "sizeThreshold": 0}) or []):
        cp = p.get("curPrice", 0) or 0
        if (cp <= 0.001 or cp >= 0.999) and p.get("asset") and p["asset"] not in resmap:
            resmap[p["asset"]] = 1 if cp >= 0.5 else 0
    # ---- activity: name, last-bet, and the flat-$50 copy replay ----
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
        # replay a flat-$50 copy of their conviction markets since Jun 1. Split P&L into
        # the SOLD (scalp) leg and the HELD-to-resolution leg — the held leg is the
        # latency-robust edge; a wallet whose copy P&L is positive only via scalp sells
        # (held leg negative) isn't a reliable copy target.
        ev = sorted([t for t in a if t.get("timestamp", 0) >= JUN1], key=lambda t: t.get("timestamp", 0))
        openp, entered, scalp, held = {}, set(), 0.0, 0.0
        hw = hl = sold = 0
        for t in ev:
            c, pr, asset = t.get("conditionId"), t.get("price", 0) or 0, t.get("asset")
            if not c or pr <= 0:
                continue
            if t.get("side") == "BUY":
                if mkt.get(c, 0) < cthr or c in entered or c in openp:
                    continue
                fee_in = STAKE * FEE_RATE * (1 - pr)            # taker fee on the entry
                entered.add(c); openp[c] = {"sh": STAKE / pr, "a": asset, "fee": fee_in}
            elif c in openp:                                    # mirror their exit (scalp)
                sh = openp[c]["sh"]
                fee_out = sh * FEE_RATE * pr * (1 - pr)         # taker fee on the exit too
                scalp += sh * pr - STAKE - openp[c]["fee"] - fee_out
                sold += 1; del openp[c]
        for c, p in openp.items():                              # settle held bets at resolution
            wv = resmap.get(p["a"])
            if wv is None:
                wv = _clob_winner(c, p["a"])                    # clob fallback for out-of-pull markets
            if wv is None:
                continue                                        # not resolved yet -> exclude
            held += (p["sh"] if wv else 0) - STAKE - p["fee"]   # redeem itself is fee-free
            hw += wv; hl += 1 - wv
        out.update(copy_pnl=round(scalp + held), held_pnl=round(held),
                   held_won=hw, held_lost=hl, sold=sold)
    return out


def lead_profile(w):
    ent = cache.get_entries(w)
    now = time.time()
    bets = [b for b in cache.get_bets(w) if (b["res_t"] or 0) <= now]  # resolved only
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


MIN_HELD = 8          # need this many resolved held conviction bets to trust the held edge
MIN_HELD_WR = 0.55    # held bets must WIN a clear majority — excludes longshot-variance
                      # players (+EV but ~34% win) that don't fit the high-win-rate thesis
MIN_LEAD_H = 1.0      # light sniper guard: drop wallets whose median winning lead < 1h


def main():
    conv = json.load(open(os.path.join(HERE, "conviction_wallets.json")))
    print(f"copy-testing {len(conv)} conviction wallets…\n", flush=True)

    # run the full copy replay on EVERY conviction wallet (cheap now: fresh-positions
    # resolution, clob only fills gaps), then select on copyability — not lead time.
    # Per-wallet guard: one wallet's unexpected error must not kill the whole
    # selection run (a single RemoteDisconnected once took out the nightly refresh);
    # a failed wallet is retried once, then excluded from this run and logged.
    def safe_stats(c):
        for attempt in (1, 2):
            try:
                return display_stats(c["wallet"])
            except Exception as e:
                if attempt == 2:
                    print(f"  ⚠ {c['wallet'][:10]}… stats failed ({e}) — excluded this run",
                          flush=True)
                    return None
                time.sleep(2)

    with ThreadPoolExecutor(max_workers=8) as ex:
        stats = list(ex.map(safe_stats, conv))

    cut30 = time.time() - 30 * 86400
    sharps = []
    for c, ds in zip(conv, stats):
        if ds is None:
            continue
        c.update(ds)
        if ds.get("name"):
            c["name"] = ds["name"]
        lp = lead_profile(c["wallet"])
        c["med_lead_h"] = round(lp["med"], 1) if lp else None
        held_n = ds["held_won"] + ds["held_lost"]
        held_wr = ds["held_won"] / held_n if held_n else 0
        # SELECT a copyable sharp: active, copy-positive, and a genuine hold-to-
        # resolution edge — held leg positive AND winning a clear majority on a real
        # sample, so the edge survives live latency and isn't longshot variance or
        # all sell-timing. A light lead floor drops true sub-hour snipers.
        if ((ds["last_trade"] or 0) >= cut30 and ds["copy_pnl"] > 0
                and ds["held_pnl"] > 0 and held_n >= MIN_HELD and held_wr >= MIN_HELD_WR
                and (c["med_lead_h"] is None or c["med_lead_h"] >= MIN_LEAD_H)):
            sharps.append(c)

    sharps.sort(key=lambda c: c["copy_pnl"], reverse=True)
    print(f"copy-positive holders (copy>0, held>0, held_n>={MIN_HELD}, active, lead>={MIN_LEAD_H}h): "
          f"{len(sharps)} of {len(conv)}\n")
    h = f"{'copyP&L':>8}{'heldP&L':>8}{'held':>9}{'sold%':>6}{'medLeadH':>9}  wallet"
    print(h); print("-" * len(h))
    for c in sharps[:35]:
        n = c["held_won"] + c["held_lost"]
        sp = 100 * c["sold"] / (c["sold"] + n) if (c["sold"] + n) else 0
        ld = f"{c['med_lead_h']:.0f}" if c["med_lead_h"] is not None else "—"
        print(f"{c['copy_pnl']:>+8}{c['held_pnl']:>+8}{(str(c['held_won'])+'-'+str(c['held_lost'])):>9}"
              f"{sp:>5.0f}%{ld:>9}  {(c.get('name') or c['wallet'][:10])}")

    json.dump(sharps, open(os.path.join(HERE, "watch_sharps.json"), "w"), indent=2)
    print(f"\n-> watch_sharps.json ({len(sharps)} copy-positive holders)")


if __name__ == "__main__":
    main()
