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

2026-07-03 holder fix: the held-edge gates no longer use the replay's held leg.
That leg only counts bets entered AND resolved inside the Jun-1->now window, so
a ~7-day-lead holder always showed `held 0-0, ~20 unresolved` and failed
held_n>=8 — the filter structurally rejected the most copyable wallets (whale
0x73afc816: 100% fwd win in conviction_scan, "held 0-0" here; and pre-Jul-2 the
winner=False bug booked those unresolved bets as LOSSES, which is where the
iohihoo −$749 / ArbTrader −$790 "scalper trap" numbers came from). The held-edge
gate now reads the wallet's trailing TRUSTED conviction record from the cache
(trust.py: consensus-resolution rows, outcome observed post-resolution), which
includes bets entered before the window that resolved inside it. The replay's
copy_pnl (fees, mirror exits) remains the other selection leg, and held stats
are still computed for display.
"""

import json
import os
import ssl
import statistics as st
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import cache
import payouts
import smart_money as sm
import trust

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
    market hasn't resolved. Matched by token_id (exact, no outcome-name guessing).

    NB: the CLOB reports winner=False on EVERY token of an UNRESOLVED market —
    only a present True winner means resolved. Treating False as "lost" counted
    every unresolved held bet as a loss, biasing copy_pnl (the selection metric)
    downward."""
    if cond not in _CLOB:
        try:
            req = urllib.request.Request("https://clob.polymarket.com/markets/" + cond,
                                         headers={"User-Agent": "Mozilla/5.0"})
            m = json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())
            toks = m.get("tokens") or []
            resolved = any(t.get("winner") is True for t in toks)
            _CLOB[cond] = {str(t.get("token_id")):
                           ((1 if t.get("winner") is True else 0) if resolved else None)
                           for t in toks}
        except Exception:
            _CLOB[cond] = {}
    return _CLOB[cond].get(str(token))


def _pm_profit(w):
    """The wallet's own all-time account P&L as Polymarket reports it (lb-api
    /profit): realized cash PLUS unrealized marks on open positions. Our
    All-Time P&L (realized only) equals this minus the open book — the two
    reconcile via _open_pnl below."""
    try:
        req = urllib.request.Request(
            "https://lb-api.polymarket.com/profit?window=all&limit=1&address=" + w,
            headers={"User-Agent": "Mozilla/5.0"})
        r = json.loads(urllib.request.urlopen(req, timeout=15, context=_SSL).read())
        return round(r[0]["amount"]) if r else None
    except Exception:
        return None


def _open_split(w):
    """Split the wallet's current /positions into (open_pnl, resolved).

      open_pnl  = unrealized P&L (cashPnl) over GENUINELY OPEN positions
                  (interior price) — real in-flight exposure.
      resolved  = decided-but-UNREDEEMED positions (curPrice pinned at 0 or 1):
                  bets that already won/lost and the wallet just never redeemed.
                  These are NOT open — they're realized outcomes hiding in the
                  positions endpoint (mostly abandoned LOSERS at $0). They
                  belong in the REALIZED track record (cashPnl = their decided
                  P&L: a loser is -cost). Leaving them in "open" is the exact
                  survivorship blind spot — PM /profit under-counts them, so a
                  wallet's realized looks better than the bets it walked away
                  from. Returned as [{realized_pnl, iv, ts}] to fold into
                  All-Time P&L + win/loss.
    """
    open_pnl = 0.0
    resolved = []
    for off in range(0, 100000, 50):
        pg = sm.get_json("/positions", {"user": w, "limit": 50, "offset": off,
                                        "sizeThreshold": 0})
        if not pg:
            break
        for p in pg:
            cpnl = p.get("cashPnl") or 0
            # decided = the data-api's own on-chain resolution flag (redeemable
            # is True once the condition reports payouts, winners AND losers —
            # verified on oliman2's $10.9k Bad Bunny loser). Price-pinning was
            # the old proxy; it misfolded pinned-but-UNRESOLVED longshots as
            # realized (measured −$27/−$434 on oliman2/leegunner — small, but
            # the 2026-07-08 audit proved the flag exact: closed+redeemable
            # decomposition reproduces PM's per-position books to the dollar).
            if p.get("redeemable"):
                resolved.append({"realized_pnl": cpnl,
                                 "iv": p.get("initialValue") or 0,
                                 "ts": p.get("timestamp") or 0})
            else:
                open_pnl += cpnl
        if len(pg) < 50:
            break
    return round(open_pnl), resolved


def _wp(cond, asset, won):
    """Chain-truth payout for a bet (1/0/0.5), falling back to the cache's
    `won` mark when the chain can't say (unresolved, legacy NULL-asset rows on
    decided markets, RPC gaps). The fallback keeps old behavior; the truth
    path kills the two cache lies: 50/50 refunds counted as wins for BOTH
    sides (28% of the follow set's resolved markets!) and stale both-sides-won
    marks on operator-resolved markets."""
    wp = payouts.truth(cond, asset)
    return (1.0 if won else 0.0) if wp is None else wp


def _bet_pnl(b, wp=None):
    """Resolved P&L of one cache bet at payout wp: a $size stake at avg price p
    returns size·(wp−p)/p — wp=1 win, 0 loss, 0.5 refund ($0.50/share, NOT
    money-back: flat near coin-flip entries, ruinous for favorites)."""
    p = max(0.001, min(0.999, b["p"] or 0))
    if wp is None:
        wp = _wp(b.get("cond"), b.get("asset"), b["won"])
    return b["size"] * (wp - p) / p


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
    # ---- ALL-TIME / CONVICTION / 30d records + P&L from the wallet's REALIZED
    # TRACK RECORD: Polymarket's own realizedPnl per closed position, over the
    # wallet's FULL history (cache.closed_exits, incremental). This is exactly
    # what a copier who mirrors their buy/sell/hold banks — it sums to PM
    # /profit (the source of truth) and needs no won×entry×size reconstruction,
    # so it's immune to the four errors that plagued the old math: the 2000-row
    # cap (now full history), both-sides double-drop (each asset is its own
    # realized row), iv=0 mis-sizing (P&L doesn't need size), and corrupt res_t
    # (realized cash is timestamp-independent). A position "won" if it made
    # money as they traded it (realized_pnl > 0) — the mirror lens. ----
    now = time.time()
    exits = cache.closed_exits(w)     # {asset: {ts, iv, realized_pnl, ...}} closed, full history

    def rtally(positions):
        """(won, lost, scratch, pnl) over closed positions by realized_pnl sign."""
        won_ = lost_ = scr_ = 0
        pnl = 0.0
        for e in positions:
            rp = e.get("realized_pnl") or 0
            pnl += rp
            if rp > 0.01:
                won_ += 1
            elif rp < -0.01:
                lost_ += 1
            else:
                scr_ += 1
        return won_, lost_, scr_, round(pnl)

    # realized universe = redeemed/sold closed positions (exits, with realizedPnl)
    # PLUS decided-but-unredeemed positions (resolved losers/winners still sitting
    # in /positions). Folding the latter in is the anti-survivorship correction:
    # a wallet's true realized record includes the bets it walked away from.
    open_pnl, resolved_open = _open_split(w)
    allpos = list(exits.values()) + resolved_open
    all_won, all_lost, all_scr, all_pnl = rtally(allpos)
    # conviction = the wallet's top-20%-by-stake positions (iv); conv30 = those
    # closed in the last 30d. Realized P&L over each set.
    thr = cache.conv_cutoff(e["iv"] for e in allpos if (e.get("iv") or 0) > 0)
    conv = [e for e in allpos if (e.get("iv") or 0) >= thr]
    cut30 = now - 30 * 86400
    conv30 = [e for e in conv if (e.get("ts") or 0) >= cut30]
    recent = sorted(allpos, key=lambda e: e.get("ts") or 0, reverse=True)[:500]
    cw, cl, cscr, cpnl = rtally(conv)
    c3w, c3l, c3scr, c3pnl = rtally(conv30)
    out = {
        "conv_win": round(100 * cw / (cw + cl), 1) if (cw + cl) else None,
        "conv_won": cw, "conv_lost": cl, "conv_ref": cscr, "conv_sold": 0,
        "conv_pnl": cpnl,
        "conv30_win": round(100 * c3w / (c3w + c3l), 1) if (c3w + c3l) else None,
        "conv30_won": c3w, "conv30_lost": c3l, "conv30_ref": c3scr, "conv30_sold": 0,
        "conv30_pnl": c3pnl,
        "realized_pnl": rtally(recent)[3],
        "all_win": round(100 * all_won / (all_won + all_lost), 1) if (all_won + all_lost) else None,
        "all_won": all_won, "all_lost": all_lost, "all_ref": all_scr,
        "all_sold": 0, "all_pnl": all_pnl,
        "open_pnl": open_pnl,          # genuinely in-flight only (resolved folded into realized)
        "pm_pnl": _pm_profit(w),
        "avg_bet": round(sum(e["iv"] for e in conv) / len(conv)) if conv else 0,
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
            wv = payouts.truth(c, p["a"])                       # chain first: refunds pay 0.5
            if wv is None:
                wv = resmap.get(p["a"])
            if wv is None:
                wv = _clob_winner(c, p["a"])                    # clob fallback for out-of-pull markets
            if wv is None:
                continue                                        # not resolved yet -> exclude
            held += p["sh"] * wv - STAKE - p["fee"]             # redeem itself is fee-free
            if wv > 0.5:
                hw += 1
            elif wv < 0.5:
                hl += 1
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


MIN_HELD = 8          # need this many trailing trusted conviction bets to trust the held edge
MIN_HELD_WR = 0.55    # they must WIN a clear majority — excludes longshot-variance
                      # players (+EV but ~34% win) that don't fit the high-win-rate thesis
MIN_LEAD_H = 1.0      # light sniper guard: drop wallets whose median winning lead < 1h
TRUST_DAYS = 90       # trailing window for the trusted conviction record (long enough
                      # that week-lead holders have real resolved sample in it)


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

    trust.ensure_cons(cache.query)
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
        # held-to-resolution edge from the trailing TRUSTED cache record — includes
        # bets entered before the replay window that resolved inside it, so long-lead
        # holders are judged on their real resolved sample (the replay's own held leg
        # is mostly "unresolved" for them and only reported for display).
        tr = trust.conviction_record(cache.query, c["wallet"], days=TRUST_DAYS,
                                     pctile=cache.CONV_PCTILE, truthfn=payouts.truth)
        c["trust_n"], c["trust_wr"], c["trust_roi"] = tr["n"], round(tr["wr"], 3), round(tr["roi"], 3)
        c["trust_refunds"] = tr.get("refunds", 0)
        # SELECT a copyable sharp: active, copy-positive (fee-aware replay), and a
        # genuine hold-to-resolution edge — trailing trusted conviction record wins a
        # clear majority with positive flat-stake ROI on a real sample, so the edge
        # survives live latency and isn't longshot variance or all sell-timing. A
        # light lead floor drops true sub-hour snipers.
        if ((ds["last_trade"] or 0) >= cut30 and ds["copy_pnl"] > 0
                and tr["n"] >= MIN_HELD and tr["wr"] >= MIN_HELD_WR and tr["roi"] > 0
                and (c["med_lead_h"] is None or c["med_lead_h"] >= MIN_LEAD_H)):
            sharps.append(c)

    sharps.sort(key=lambda c: c["copy_pnl"], reverse=True)
    print(f"copy-positive holders (copy>0, trust_n>={MIN_HELD}, trust_wr>={MIN_HELD_WR:.0%}, "
          f"trust_roi>0 over {TRUST_DAYS}d, active, lead>={MIN_LEAD_H}h): "
          f"{len(sharps)} of {len(conv)}\n")
    h = (f"{'copyP&L':>8}{'trustRec':>10}{'trustROI':>9}{'heldP&L':>8}{'held':>9}"
         f"{'sold%':>6}{'medLeadH':>9}  wallet")
    print(h); print("-" * len(h))
    for c in sharps[:35]:
        n = c["held_won"] + c["held_lost"]
        sp = 100 * c["sold"] / (c["sold"] + n) if (c["sold"] + n) else 0
        ld = f"{c['med_lead_h']:.0f}" if c["med_lead_h"] is not None else "—"
        rec = f"{round(c['trust_wr']*c['trust_n'])}-{round((1-c['trust_wr'])*c['trust_n'])}"
        print(f"{c['copy_pnl']:>+8}{rec:>10}{c['trust_roi']:>+9.0%}{c['held_pnl']:>+8}"
              f"{(str(c['held_won'])+'-'+str(c['held_lost'])):>9}"
              f"{sp:>5.0f}%{ld:>9}  {(c.get('name') or c['wallet'][:10])}")

    json.dump(sharps, open(os.path.join(HERE, "watch_sharps.json"), "w"), indent=2)
    print(f"\n-> watch_sharps.json ({len(sharps)} copy-positive holders)")


if __name__ == "__main__":
    main()
