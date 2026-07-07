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
stake). Exits MIRROR the signal, like the live bot (2026-07-07): when the wallet
fully closed a position pre-resolution, the replay sells there too — close time from
/closed-positions, exit price reconstructed as avgPrice + realizedPnl/totalBought,
exit taker fee + slippage haircut paid, status SOLD. Bets they held to resolution
settle at the chain-truth payout (1/0/0.5 — refunds are scratches, not losses).
Complete in-window round trips on still-unresolved markets are replayed as
entry+exit (the old hold-to-resolution model missed them entirely). Sizing is
DYNAMIC — each bet stakes PCT of
current equity (Kelly-style compounding), halved in a >20% drawdown, capped at
EVENT_CAP concurrent bets per real-world event — and entries pay the Polymarket taker
fee plus a lag-slippage price haircut (FEE_RATE / SLIP / LAG_EST_S), so the book
models what a real copier nets, not the idealized zero-cost mirror. One position per
market (first wallet to enter wins the slot); when capital is fully deployed a bet is
MISSED.
Resolved history + realized P&L come from the cache; currently-open bets come from a
small live /positions pull so the page can still show what's in flight.
"""
import argparse
import json
import os
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import cache
import payouts
import smart_money as sm
import trust

_SSL = ssl._create_unverified_context()

HERE = os.path.dirname(__file__)
BANK = 1000.0
GAMMA = "https://gamma-api.polymarket.com"

# ---- interchangeable-wallet replay: live/backtest.json ----------------------
# {days, stake_cap_usd, class_pct: {volume, whale}, wallets: [{wallet, name,
# class}]}. Rolling window: "what if I started following these wallets `days`
# ago." Class 'whale' replays EVERY trusted bet at class_pct.whale of equity;
# 'volume' (default) replays conviction bets only (top-20% by stake, threshold
# from PRE-window trusted bets so it can't peek) at class_pct.volume.
# Ad-hoc runs that leave the dashboard feed alone:
#     python3 portfolio.py --wallets 0xabc,0xdef:whale --days 30 --out /tmp/t.json
_ap = argparse.ArgumentParser()
_ap.add_argument("--wallets", help="comma list of addresses; ':whale' suffix opts into whale class")
_ap.add_argument("--days", type=int, help="window length (default backtest.json's, else 30)")
_ap.add_argument("--out", help="output path (default $PORTFOLIO_OUT or portfolio.json)")
_ARGS, _ = _ap.parse_known_args()
try:
    _BT = json.load(open(os.path.join(HERE, "backtest.json")))
except Exception:
    _BT = {}
DAYS = _ARGS.days or int(_BT.get("days", 30))
START = time.time() - DAYS * 86400        # rolling: started following DAYS ago
CLASS_PCT = {"volume": 0.04, "whale": 0.12, **(_BT.get("class_pct") or {})}
BASE_PCT = CLASS_PCT.get("volume", 0.04)  # sweep threshold stays on the base class

# ---- dynamic sizing (mirrors the live copybot) ------------------------------
# Each new bet stakes PCT of CURRENT equity (cash + open cost basis) so the book
# compounds in both directions; the stake is halved while equity sits below
# DD_THRESHOLD of its high-water mark, and clamped to [STAKE_MIN, STAKE_CAP].
# EVENT_CAP >0 limits concurrent bets whose markets belong to the same real-world
# event (a game's markets settle together — one correlated bet, not N diversified
# ones); 0 = off, mirror every conviction trade.
# per-class equity fractions come from CLASS_PCT (backtest.json). No stake cap
# and no banked reserve: the natural ceiling is the FOLLOWED WALLET'S OWN BET —
# a copy is never larger than what the wallet actually staked (you can't
# out-conviction the signal, and it keeps fills inside the size the market
# actually absorbed).
STAKE_MIN = 5.0
EVENT_CAP = 0
DD_THRESHOLD, DD_FACTOR = 0.80, 0.5
# skip entries above this price. High-price favorites win pennies and lose
# whole stakes: the June sweep (caps 0.75-1.0) peaked at 0.95 — >95¢ bets added
# ~23 wins yet LOWERED final equity (slip+fee eat the ~1-3% payouts, and the
# locked capital compounds better elsewhere). Deep caps (<=0.85) cut real
# winners. Mirrored by the bot's follow.max_entry; env-overridable for sweeps.
MAX_ENTRY = float(os.environ.get("MAX_ENTRY", 0.95))
OUT = _ARGS.out or os.environ.get("PORTFOLIO_OUT", "portfolio.json")

# ---- realism model (matches the live copybot) -------------------------------
# Taker fee (Polymarket V2, since 2026-03-30): fee = shares·rate·p·(1−p); for a
# $stake buy that's stake·rate·(1−p). Sports 0.03 — the follow set's category.
# Redeeming at resolution is fee-free, so only entries pay here
# (hold-to-resolution model, no mirrored exits).
FEE_RATE = 0.03
# Copy lag: we enter LAG_EST_S after the wallet does, at a slightly worse price.
# SLIP is the entry-price penalty estimate: the live bot measured +0.35% at ~5min
# lag; a 60s poller should see less — 0.5% is a conservative flat haircut.
LAG_EST_S = 90
SLIP = 0.005

# the replayed wallets — from --wallets, else live/backtest.json (editable:
# add/remove/swap any address there; classes default to 'volume')
if _ARGS.wallets:
    WALLETS = []
    for _tok in _ARGS.wallets.split(","):
        _addr, _, _cls = _tok.strip().partition(":")
        WALLETS.append({"wallet": _addr, "name": _addr[:10], "class": _cls or "volume"})
else:
    WALLETS = [{"wallet": w["wallet"], "name": w.get("name", w["wallet"][:10]),
                "class": w.get("class", "volume")} for w in _BT.get("wallets", [])]
if not WALLETS:
    raise SystemExit("no wallets to replay: create live/backtest.json or pass --wallets")


def entry_model(p, stake):
    """(effective entry price, entry fee, total cash cost) of a $stake copy:
    price worsened by the lag-slippage haircut, taker fee on top of the stake."""
    p_eff = min(0.999, p * (1 + SLIP))
    fee = stake * FEE_RATE * (1 - p_eff)
    return p_eff, fee, stake + fee

_MKT = {}
_SLUG_CACHE = os.path.join(HERE, "slug_cache.json")
try:
    _MKT.update(json.load(open(_SLUG_CACHE)))
except Exception:
    pass


def market_meta(cond):
    """Market title + slug from the CLOB market endpoint (gamma's condition_ids
    filter returns nothing for resolved markets) — cached in-process AND on disk
    (slug_cache.json), since the event cap needs a slug for every replayed market,
    not just the top-60 displayed."""
    if cond not in _MKT:
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                f"https://clob.polymarket.com/markets/{cond}", headers={"User-Agent": "Mozilla/5.0"}),
                timeout=20, context=_SSL)
            m = json.loads(r.read())
            _MKT[cond] = {"title": m.get("question") or "", "slug": m.get("market_slug") or ""}
        except Exception:
            return {"title": "", "slug": ""}      # transient failure — don't cache
    return _MKT[cond]


def save_slug_cache():
    try:
        json.dump({c: v for c, v in _MKT.items() if v.get("slug")},
                  open(_SLUG_CACHE, "w"))
    except Exception:
        pass


def event_key(slug):
    """Correlation-group id: Polymarket sub-splits one game across slugs
    (`…-2026-07-01-more-markets`, `…-2026-07-01-second-half-result`), so dated
    slugs collapse to their `…-YYYY-MM-DD` prefix; undated slugs stand as-is."""
    m = re.match(r"(.*?\d{4}-\d{2}-\d{2})", slug or "")
    return m.group(1) if m else (slug or None)


_WALLET_THR = {}   # wallet -> conviction threshold used this run (0 for whales)


def window_bets():
    """Every replayed wallet's TRUSTED resolved bets entered inside the window,
    with entry time. Trusted rows only (trust.py) — outcomes observed post-
    resolution, res_t=ts poison excluded — so any pasted-in wallet is scored
    honestly, not on cache marks. Class rules: 'whale' replays EVERY bet;
    'volume' only conviction bets, with the p80 threshold computed from
    PRE-window trusted bets (falls back to full history for wallets with no
    pre-window sample) so the threshold can't peek at the window it scores."""
    out = []
    now = time.time()
    trust.ensure_cons(cache.query)
    for w in WALLETS:
        cache.get_bets(w["wallet"])          # ensure pulled/fresh (pulls brand-new wallets)
        ent = cache.get_entries(w["wallet"])               # cond -> first buy ts
        rows = trust.trusted_wallet_rows(cache.query, w["wallet"], now)
        best = {}                            # one bet per market: largest-stake token
        for cond, asset, won, p, res_t, size in rows:
            if cond not in best or size > best[cond][4]:
                best[cond] = (asset, won, p, res_t, size)
        if w.get("class") == "whale":
            thr = 0.0
        else:
            pre = [size for asset, won, p, res_t, size in best.values() if res_t < START]
            thr = cache.conv_cutoff(pre if pre else
                                    [size for *_, size in best.values()])
        _WALLET_THR[w["wallet"]] = thr
        # the wallet's fully-closed positions: close time + reconstructed exit
        # price. The live bot MIRRORS exits, so the replay must too — a bet the
        # signal sold pre-resolution exits the book right there (status SOLD),
        # not at resolution (the old hold-to-resolution ceiling).
        closed = closed_positions(w["wallet"])
        for cond, (asset, won, p, res_t, size) in best.items():
            if size < thr or p > MAX_ENTRY:
                continue
            et = ent.get(cond)
            if not et or et < START:                       # only in-window entries
                continue
            b = {"wallet": w["wallet"], "name": w["name"], "cond": cond,
                 "asset": asset, "cls": w.get("class", "volume"),
                 "their": size, "entry_t": et, "p": p, "won": won,
                 "res_t": res_t or 0}
            cx = closed.get(asset)
            if cx and res_t and cx["ts"] < res_t - 300:    # sold BEFORE resolution
                b["exit_t"], b["exit_p"] = cx["ts"], cx["exit_p"]
            out.append(b)
        # complete round trips the cache can't see: entered AND fully exited
        # in-window on a market that never resolved (or hasn't yet) — the live
        # bot would have copied both legs, so the replay does too
        for asset, cx in closed.items():
            cond = cx["cond"]
            if (cond in best or not cond or cx["ts"] < START
                    or (cx["iv"] or 0) < thr or cx["p"] > MAX_ENTRY):
                continue
            et = ent.get(cond)
            if not et or et < START:
                continue
            if payouts.truth(cond) is not None:
                continue        # resolved on-chain: their close may be a redeem,
                                # not a sell — the cache row will cover it
            best[cond] = None   # one per market, same as everywhere
            out.append({"wallet": w["wallet"], "name": w["name"], "cond": cond,
                        "asset": asset, "cls": w.get("class", "volume"),
                        "their": cx["iv"], "entry_t": et, "p": cx["p"],
                        "won": None, "res_t": 0,
                        "exit_t": cx["ts"], "exit_p": cx["exit_p"],
                        "title": cx["title"]})
    # chain-truth payouts for the replayed markets: refunds pay 0.5/share, and
    # a cache `won` mark can be wrong on operator-resolved markets — the
    # replay must settle at what a redeem actually pays (see payouts.py)
    payouts.ensure({b["cond"] for b in out})
    for b in out:
        b["wp"] = payouts.truth(b["cond"], b.get("asset"))
    return out


def closed_positions(wallet):
    """The wallet's fully-closed positions with in-window close times —
    shared implementation in smart_money.closed_exits (validate_timing uses
    the same one, so the backtest and the sharps stats mirror exits
    identically)."""
    return sm.closed_exits(wallet, since_ts=START)


def open_bets():
    """Currently-held conviction positions (live /positions pull, small) for the
    'current bets' panel — the cache only has resolved bets."""
    out = []
    for w in WALLETS:
        ent = cache.get_entries(w["wallet"])
        ps = sm.get_json("/positions", {"user": w["wallet"], "limit": 500, "sizeThreshold": 0}) or []
        if w.get("class") == "whale":
            thr = 0.0                       # whales: every open position counts
        else:
            thr = _WALLET_THR.get(w["wallet"])
            if thr is None:
                thr = cache.conv_cutoff((p.get("initialValue") or 0) for p in ps)
        for p in ps:
            cp = p.get("curPrice", 0) or 0
            if cp <= 0.001 or cp >= 0.999:                 # resolved -> belongs to history, not open
                continue
            if (p.get("initialValue") or 0) < thr:
                continue
            if (p.get("avgPrice", 0) or 0) > MAX_ENTRY:
                continue
            et = ent.get(p.get("conditionId"))
            if et is not None and et < START:
                continue                     # position predates the window
            # unknown entry time -> queue at the END of the replay (an open
            # position is the newest thing in the book; entry_t=0 used to put
            # it FIRST, draining the bankroll before any historical bet ran)
            out.append({"wallet": w["wallet"], "name": w["name"], "cond": p.get("conditionId"),
                        "cls": w.get("class", "volume"), "their": p.get("initialValue") or 0,
                        "entry_t": et if et is not None else time.time(),
                        "p": max(0.001, min(0.999, p.get("avgPrice", 0) or 0)),
                        "cur": cp, "title": p.get("title") or "", "outcome": p.get("outcome") or "",
                        "end": p.get("endDate")})
    return out


def main():
    now = time.time()
    resolved_pool = window_bets()
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

    # ONLY_CONDS=<json path>: replay only these markets — {cond: bool} or
    # us_listable.py's {cond: {"listed": bool, ...}}. Models "same signal, but
    # I can only execute the subset" (e.g. bets also listed on Polymarket US);
    # thresholds/sizing still come from the full signal, capital only chases
    # the executable bets.
    _only = os.environ.get("ONLY_CONDS")
    if _only:
        _allow = {c for c, v in json.load(open(_only)).items()
                  if (v.get("listed") if isinstance(v, dict) else v)}
        _pre = len(stream)
        stream = [b for b in stream if b["cond"] in _allow]
        print(f"portfolio: ONLY_CONDS filter kept {len(stream)}/{_pre} bets", flush=True)

    # prefetch every replayed market's slug (threaded; disk-cached) so the
    # event-correlation cap can group markets by real-world event
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(market_meta, {b["cond"] for b in stream}))

    cash = BANK
    realized = 0.0
    fees_paid = 0.0
    hwm = BANK
    capped = 0
    reserve = 0.0
    held = []        # (free_t, cost, payoff)  cost = stake + entry fee; payoff paid at free_t
    perW = {w["wallet"]: {"name": w["name"], "wallet": w["wallet"], "bets": 0,
                          "won": 0, "lost": 0, "ref": 0, "sold": 0,
                          "class": w.get("class", "volume"),
                          "invested": 0.0, "realized": 0.0} for w in WALLETS}
    resolved, current, missed = [], [], []

    def cur_stake(frac=BASE_PCT, their=None):
        """The wallet-class fraction of current equity, drawdown-braked, and
        NEVER larger than the followed wallet's own stake: when the percentage
        works out to more than they actually bet, mirror their exact amount."""
        nonlocal hwm
        eq = cash + sum(c for _, c, _, _ in held)
        hwm = max(hwm, eq)
        if eq < DD_THRESHOLD * hwm:
            frac *= DD_FACTOR
        stake = max(STAKE_MIN, frac * eq)
        if their and stake > their:
            stake = their
        return stake

    def free(upto):
        nonlocal cash, realized
        keep = []
        for ft, cost, payoff, rec in held:
            if ft and ft <= upto and rec["kind"] == "res":
                cash += payoff; realized += payoff - cost; perW[rec["wallet"]]["realized"] += payoff - cost
                if rec.get("sold"):
                    # mirrored exit: neither won nor lost — its truth is the price
                    perW[rec["wallet"]]["sold"] = perW[rec["wallet"]].get("sold", 0) + 1
                    rec["won"] = None
                else:
                    wp = rec.get("wp")
                    won = rec["won"] if wp is None else wp > 0.5
                    # refunds are scratches, not losses — count them apart
                    perW[rec["wallet"]]["won" if won else "ref" if wp == 0.5 else "lost"] += 1
                    rec["won"] = won                  # truth-adjusted for the feed
                    if wp == 0.5:
                        rec["refund"] = True
                rec["pnl"] = payoff - cost
                resolved.append(rec)
            else:
                keep.append((ft, cost, payoff, rec))
        held[:] = keep

    for b in stream:
        free(b["entry_t"])
        stake = cur_stake(CLASS_PCT.get(b.get("cls"), BASE_PCT), b.get("their"))
        b["stake"] = round(stake, 2)
        b["event"] = event_key(market_meta(b["cond"])["slug"])
        # correlation cap (off when EVENT_CAP=0): skip a bet when we already hold
        # EVENT_CAP positions on the same real-world event (deliberate risk skip)
        if EVENT_CAP and b["event"] and sum(1 for _, _, _, r in held
                                            if r.get("event") == b["event"]) >= EVENT_CAP:
            b["capped"] = True; capped += 1
            missed.append(b)
            continue
        p_eff, fee, cost = entry_model(b["p"], stake)
        if cash >= cost:
            cash -= cost; fees_paid += fee; perW[b["wallet"]]["bets"] += 1
            shares = stake / p_eff                        # lag-adjusted entry price
            if b["kind"] == "res":
                if b.get("exit_t"):
                    # the signal SOLD pre-resolution -> mirror the exit, like the
                    # live bot: their exit price with the slippage haircut against
                    # us, minus the taker fee (sells pay it; redeems don't)
                    xp = max(0.001, b["exit_p"] * (1 - SLIP))
                    fee_out = shares * FEE_RATE * xp * (1 - xp)
                    fees_paid += fee_out
                    b["sold"] = True
                    held.append((b["exit_t"], cost, shares * xp - fee_out, b))
                else:
                    # held to resolution: chain-truth payout (1/0/0.5) when
                    # known, else the cache mark; redeem is fee-free
                    wp = b.get("wp")
                    if wp is None:
                        wp = 1.0 if b["won"] else 0.0
                    held.append((b["res_t"] or now, cost, shares * wp, b))
            else:                                          # currently open -> mark to market, no free yet
                held.append((None, cost, 0.0, b))
                b["val"] = shares * b["cur"]
        else:
            missed.append(b)
    free(now)
    # finalize open (still held with kind==open): mark to market
    invested = 0.0
    open_cost = 0.0
    for ft, cost, payoff, rec in held:
        if rec["kind"] == "open":
            invested += rec["val"]; rec["pnl"] = rec["val"] - cost
            open_cost += cost
            perW[rec["wallet"]]["invested"] += rec["val"]
            current.append(rec)

    # enrich resolved + missed with titles, keep most-recent 60
    resolved.sort(key=lambda r: r.get("exit_t") or r.get("res_t") or 0, reverse=True)
    for r in resolved[:60]:
        m = market_meta(r["cond"])
        if m["title"]:                # round-trip recs already carry a title
            r["title"] = m["title"]
    # hypothetical P&L had we been able to afford it — same fee + lag model as the
    # placed bets: resolved bets at their outcome, still-open bets marked to the
    # current price. Missed bets can be kind=="open" (no "won"/"res_t" keys) —
    # indexing m["won"] here used to KeyError and kill the whole portfolio step
    # the first time capital ran out while a followed wallet had a live position.
    def hypo_pnl(m):
        stake = m.get("stake") or STAKE_MIN
        p_eff, fee, cost = entry_model(m["p"], stake)
        shares = stake / p_eff
        if m.get("exit_t"):                     # would have mirrored their exit
            xp = max(0.001, m["exit_p"] * (1 - SLIP))
            return shares * xp - shares * FEE_RATE * xp * (1 - xp) - cost
        if "won" in m:
            wp = m.get("wp")
            if wp is None:
                wp = 1.0 if m["won"] else 0.0
            return shares * wp - cost
        return stake * (m.get("cur", p_eff) / p_eff) - cost

    missed.sort(key=lambda m: m.get("res_t") or 0, reverse=True)
    for m in missed[:60]:
        m["title"] = market_meta(m["cond"])["title"]
        m["pnl"] = hypo_pnl(m)
    wins = sum(1 for r in resolved if r.get("won"))
    refunds = sum(1 for r in resolved if r.get("refund"))
    solds = sum(1 for r in resolved if r.get("sold"))
    # per-wallet conviction threshold (cache p80) so the dashboard can filter LIVE open
    # positions the same way; 1e12 = "no sized bets" (nothing qualifies)
    conv_thr = {}
    for w in WALLETS:
        t = _WALLET_THR.get(w["wallet"])
        if t is None:
            t = cache.conv_cutoff(b["size"] for b in cache.get_bets(w["wallet"]) if (b["size"] or 0) > 0)
        conv_thr[w["wallet"]] = round(t) if t != float("inf") else 1e12
    equity = cash + invested + reserve
    out = {
        "started": START, "updated": now, "days": DAYS,
        "bank": BANK, "stake": round(cur_stake(), 2),   # the NEXT bet's size (base class)
        "stake_pct": BASE_PCT, "class_pct": CLASS_PCT,
        "event_cap": EVENT_CAP,
        "hwm": round(hwm, 2),
        "dd_threshold": DD_THRESHOLD, "capped_count": capped,
        "max_entry": MAX_ENTRY,
        "fee_rate": FEE_RATE, "slip": SLIP, "lag_est_s": LAG_EST_S,
        "fees_paid": round(fees_paid, 2),
        "equity": round(equity, 2), "liquid": round(cash, 2), "invested": round(invested, 2),
        "reserve": round(reserve, 2),                    # banked profit, never bet
        "realized": round(realized, 2), "pnl": round(equity - BANK, 2),
        "unreal": round(invested - open_cost, 2),
        "resolved_count": len(resolved), "wins": wins,
        "losses": len(resolved) - wins - refunds - solds,
        "refunds": refunds, "sold": solds,
        "open_count": len(current), "missed_count": len(missed),
        "wallets": [{"name": v["name"], "wallet": v["wallet"], "bets": v["bets"],
                     "won": v["won"], "lost": v["lost"], "ref": v.get("ref", 0),
                     "sold": v.get("sold", 0), "class": v.get("class", "volume"),
                     "invested": round(v["invested"], 2), "realized": round(v["realized"], 2),
                     "conv_thr": conv_thr.get(v["wallet"], 1e12)}
                    for v in perW.values()],
        "current": [{"title": c.get("title", ""), "name": c["name"], "outcome": c.get("outcome", ""),
                     "stake": c.get("stake"), "val": round(c["val"], 2), "pnl": round(c["pnl"], 2),
                     "end": c.get("end")} for c in sorted(current, key=lambda c: c["entry_t"])],
        # status mirrors the live bot's vocabulary: won / lost / refund (50/50
        # scratch — pays $0.50/share, so "not a win" can still be P&L-positive
        # below 50¢ entries) / sold (the signal exited pre-resolution and the
        # replay mirrored it, exit fee + slip paid — same as the live bot).
        "resolved": [{"title": r.get("title", ""), "name": r["name"], "won": r["won"],
                      "status": ("sold" if r.get("sold")
                                 else "refund" if r.get("refund")
                                 else "won" if r["won"] else "lost"),
                      "stake": r.get("stake"), "pnl": round(r["pnl"], 2),
                      "date": r.get("exit_t") or r.get("res_t")}
                     for r in resolved[:60]],
        "missed": [{"title": m.get("title", ""), "name": m["name"],
                    "won": (None if "won" not in m or m.get("exit_t")
                            else (m["won"] if m.get("wp") is None else m["wp"] > 0.5)),
                    "status": ("sold" if m.get("exit_t")
                               else None if "won" not in m
                               else "refund" if m.get("wp") == 0.5
                               else "won" if (m["won"] if m.get("wp") is None else m["wp"] > 0.5)
                               else "lost"),
                    "stake": m.get("stake"), "capped": bool(m.get("capped")),
                    "pnl": round(m["pnl"], 2), "date": m.get("exit_t") or m.get("res_t")}
                   for m in missed[:60]],
        "missed_pnl": round(sum(hypo_pnl(m) for m in missed), 2),
    }
    json.dump(out, open(os.path.join(HERE, OUT) if not os.path.isabs(OUT) else OUT, "w"),
              separators=(",", ":"))
    save_slug_cache()
    print(f"portfolio[{DAYS}d rolling]: equity ${equity:,.0f} ({(equity-BANK)/BANK*100:+.0f}%) | banked ${reserve:,.0f} "
          f"| realized ${realized:+,.0f} | fees ${fees_paid:,.0f} | next stake ${cur_stake():,.0f} "
          f"| {len(resolved)} resolved ({wins}W/{len(resolved)-wins-refunds-solds}L/{refunds}R/{solds}S) | {len(current)} open "
          f"| {len(missed)} missed ({capped} event-capped) | -> {os.path.basename(OUT)}", flush=True)


if __name__ == "__main__":
    main()
