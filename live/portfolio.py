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
stake), held to resolution (the cache has no sell events, which is the right model for
the hold-to-resolution wallets we follow). Sizing is DYNAMIC — each bet stakes PCT of
current equity (Kelly-style compounding), halved in a >20% drawdown, capped at
EVENT_CAP concurrent bets per real-world event — and entries pay the Polymarket taker
fee plus a lag-slippage price haircut (FEE_RATE / SLIP / LAG_EST_S), so the book
models what a real copier nets, not the idealized zero-cost mirror. One position per
market (first wallet to enter wins the slot); when capital is fully deployed a bet is
MISSED.
Resolved history + realized P&L come from the cache; currently-open bets come from a
small live /positions pull so the page can still show what's in flight.
"""
import json
import os
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import cache
import smart_money as sm

_SSL = ssl._create_unverified_context()

HERE = os.path.dirname(__file__)
BANK = 1000.0
START = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))   # backfilled: replay from June 1
GAMMA = "https://gamma-api.polymarket.com"

# ---- dynamic sizing (mirrors the live copybot) ------------------------------
# Each new bet stakes PCT of CURRENT equity (cash + open cost basis) so the book
# compounds in both directions; the stake is halved while equity sits below
# DD_THRESHOLD of its high-water mark, and clamped to [STAKE_MIN, STAKE_CAP].
# EVENT_CAP >0 limits concurrent bets whose markets belong to the same real-world
# event (a game's markets settle together — one correlated bet, not N diversified
# ones); 0 = off, mirror every conviction trade.
PCT = 0.04
# stakes pin at STAKE_CAP (where marketable fills still sit inside typical book
# depth); once working equity exceeds STAKE_CAP/PCT, surplus cash is SWEPT into
# a banked reserve — a profit ratchet that never bets and can't be drawn down.
STAKE_MIN, STAKE_CAP = 5.0, 250.0
EVENT_CAP = 0
DD_THRESHOLD, DD_FACTOR = 0.80, 0.5
# skip entries above this price. High-price favorites win pennies and lose
# whole stakes: the June sweep (caps 0.75-1.0) peaked at 0.95 — >95¢ bets added
# ~23 wins yet LOWERED final equity (slip+fee eat the ~1-3% payouts, and the
# locked capital compounds better elsewhere). Deep caps (<=0.85) cut real
# winners. Mirrored by the bot's follow.max_entry; env-overridable for sweeps.
MAX_ENTRY = float(os.environ.get("MAX_ENTRY", 0.95))
OUT = os.environ.get("PORTFOLIO_OUT", "portfolio.json")

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

# the followed wallets — single source of truth (dashboard renders names from the feed)
WALLETS = [
    {"name": "Kruto2027",     "wallet": "0xe8ca3f758c93f44f3ec210542ab78afb7c0bcccb"},
    {"name": "shisan888",     "wallet": "0xf3488e52ac2d7f0628b04481db5a5b0446f0e543"},
    {"name": "fortuneking",   "wallet": "0x86c878cde72660ec52f5e6f0f0438b76de8fc867"},
    {"name": "LSB1",          "wallet": "0x41558102a796ba971c7567cad41c307e59f8fa41"},
    {"name": "imwalkinghere", "wallet": "0xd96750bf8d941a8186e592b0ae6e096da66aa266"},
]


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


def conviction_bets():
    """Every followed wallet's resolved conviction bets from the cache, with entry time."""
    out = []
    now = time.time()
    for w in WALLETS:
        ent = cache.get_entries(w["wallet"])               # cond -> first buy ts
        bets = [b for b in cache.get_bets(w["wallet"]) if (b["size"] or 0) > 0]
        thr = cache.conv_cutoff(b["size"] for b in bets)
        for b in bets:
            if b["size"] < thr:
                continue
            if b["p"] > MAX_ENTRY:
                continue
            if (b["res_t"] or 0) > now:
                # unresolved market (early-sold position): won is a curPrice mark,
                # not an outcome — and a future res_t would never free its stake
                # (cash out at entry, freed at res_t > now, absent from `invested`
                # = equity silently loses $STAKE). The live /positions pull is the
                # source for genuinely-open bets.
                continue
            et = ent.get(b["cond"])
            if not et or et < START:                       # only post-START entries
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
            if (p.get("avgPrice", 0) or 0) > MAX_ENTRY:
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
                          "won": 0, "lost": 0,
                          "invested": 0.0, "realized": 0.0} for w in WALLETS}
    resolved, current, missed = [], [], []

    def cur_stake():
        """PCT of current WORKING equity, drawdown-braked, clamped — the copybot's
        rule. Sweeps surplus cash to the banked reserve first, so working equity
        stays at the level where stakes ~= STAKE_CAP."""
        nonlocal hwm, cash, reserve
        eq = cash + sum(c for _, c, _, _ in held)
        excess = eq - STAKE_CAP / PCT
        if excess > 0:
            sweep = min(cash, excess)
            cash -= sweep; reserve += sweep; eq -= sweep
        hwm = max(hwm, eq)
        frac = PCT * (DD_FACTOR if eq < DD_THRESHOLD * hwm else 1.0)
        return max(STAKE_MIN, min(STAKE_CAP, frac * eq))

    def free(upto):
        nonlocal cash, realized
        keep = []
        for ft, cost, payoff, rec in held:
            if ft and ft <= upto and rec["kind"] == "res":
                cash += payoff; realized += payoff - cost; perW[rec["wallet"]]["realized"] += payoff - cost
                perW[rec["wallet"]]["won" if rec["won"] else "lost"] += 1
                rec["pnl"] = payoff - cost
                resolved.append(rec)
            else:
                keep.append((ft, cost, payoff, rec))
        held[:] = keep

    for b in stream:
        free(b["entry_t"])
        stake = cur_stake()
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
                payoff = shares * (1.0 if b["won"] else 0.0)   # redeem is fee-free
                held.append((b["res_t"] or now, cost, payoff, b))
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
    resolved.sort(key=lambda r: r.get("res_t") or 0, reverse=True)
    for r in resolved[:60]:
        m = market_meta(r["cond"]); r["title"] = m["title"]
    # hypothetical P&L had we been able to afford it — same fee + lag model as the
    # placed bets: resolved bets at their outcome, still-open bets marked to the
    # current price. Missed bets can be kind=="open" (no "won"/"res_t" keys) —
    # indexing m["won"] here used to KeyError and kill the whole portfolio step
    # the first time capital ran out while a followed wallet had a live position.
    def hypo_pnl(m):
        stake = m.get("stake") or STAKE_MIN
        p_eff, fee, cost = entry_model(m["p"], stake)
        if "won" in m:
            return (stake / p_eff) - cost if m["won"] else -cost
        return stake * (m.get("cur", p_eff) / p_eff) - cost

    missed.sort(key=lambda m: m.get("res_t") or 0, reverse=True)
    for m in missed[:60]:
        m["title"] = market_meta(m["cond"])["title"]
        m["pnl"] = hypo_pnl(m)
    wins = sum(1 for r in resolved if r.get("won"))
    # per-wallet conviction threshold (cache p80) so the dashboard can filter LIVE open
    # positions the same way; 1e12 = "no sized bets" (nothing qualifies)
    conv_thr = {}
    for w in WALLETS:
        t = cache.conv_cutoff(b["size"] for b in cache.get_bets(w["wallet"]) if (b["size"] or 0) > 0)
        conv_thr[w["wallet"]] = round(t) if t != float("inf") else 1e12
    equity = cash + invested + reserve
    out = {
        "started": START, "updated": now,
        "bank": BANK, "stake": round(cur_stake(), 2),   # the NEXT bet's size
        "stake_pct": PCT, "stake_cap": STAKE_CAP, "event_cap": EVENT_CAP,
        "hwm": round(hwm, 2),
        "dd_threshold": DD_THRESHOLD, "capped_count": capped,
        "max_entry": MAX_ENTRY,
        "fee_rate": FEE_RATE, "slip": SLIP, "lag_est_s": LAG_EST_S,
        "fees_paid": round(fees_paid, 2),
        "equity": round(equity, 2), "liquid": round(cash, 2), "invested": round(invested, 2),
        "reserve": round(reserve, 2),                    # banked profit, never bet
        "realized": round(realized, 2), "pnl": round(equity - BANK, 2),
        "unreal": round(invested - open_cost, 2),
        "resolved_count": len(resolved), "wins": wins, "losses": len(resolved) - wins,
        "open_count": len(current), "missed_count": len(missed),
        "wallets": [{"name": v["name"], "wallet": v["wallet"], "bets": v["bets"],
                     "won": v["won"], "lost": v["lost"],
                     "invested": round(v["invested"], 2), "realized": round(v["realized"], 2),
                     "conv_thr": conv_thr.get(v["wallet"], 1e12)}
                    for v in perW.values()],
        "current": [{"title": c.get("title", ""), "name": c["name"], "outcome": c.get("outcome", ""),
                     "stake": c.get("stake"), "val": round(c["val"], 2), "pnl": round(c["pnl"], 2),
                     "end": c.get("end")} for c in sorted(current, key=lambda c: c["entry_t"])],
        "resolved": [{"title": r.get("title", ""), "name": r["name"], "won": r["won"],
                      "stake": r.get("stake"), "pnl": round(r["pnl"], 2), "date": r.get("res_t")}
                     for r in resolved[:60]],
        "missed": [{"title": m.get("title", ""), "name": m["name"], "won": m.get("won"),
                    "stake": m.get("stake"), "capped": bool(m.get("capped")),
                    "pnl": round(m["pnl"], 2), "date": m.get("res_t")}
                   for m in missed[:60]],
        "missed_pnl": round(sum(hypo_pnl(m) for m in missed), 2),
    }
    json.dump(out, open(os.path.join(HERE, OUT) if not os.path.isabs(OUT) else OUT, "w"),
              separators=(",", ":"))
    save_slug_cache()
    print(f"portfolio: equity ${equity:,.0f} ({(equity-BANK)/BANK*100:+.0f}%) | banked ${reserve:,.0f} "
          f"| realized ${realized:+,.0f} | fees ${fees_paid:,.0f} | next stake ${cur_stake():,.0f} "
          f"| {len(resolved)} resolved ({wins}W/{len(resolved)-wins}L) | {len(current)} open "
          f"| {len(missed)} missed ({capped} event-capped) | -> portfolio.json", flush=True)


if __name__ == "__main__":
    main()
