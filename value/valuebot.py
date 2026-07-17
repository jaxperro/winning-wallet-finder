#!/usr/bin/env python3
"""VALUE paper bot — systematic sub-2¢ portfolio (value/PLAN.md, strategy V0).

SILO RULES (user directive 2026-07-17): this file must not import copybot.py
or copytrade.py, share no state/feed/webhook/wallet with the copy trader, and
touch only value/* paths. The ~60 lines of book/fee/payout helpers are
DUPLICATED here on purpose — total blast-radius isolation is worth it.

The strategy is a law-of-large-numbers portfolio: every active market with an
ask ≤ 2¢ is a candidate; stake is flat $1 (the venue minimum — reality, not
choice); positions hold to resolution and settle at CHAIN truth (payout
vectors — 0.5 refunds are real). The calibration study says such entries
resolved ~1.24x their price; the ONE thing history can't say is whether the
fills exist, so the fill model is brutally honest (2026-07-16 parity lesson):
a candidate with less than $1 of asks inside the protected band is a MISS,
never a pretend fill.

Run:  python3 value/valuebot.py --once     # one scan cycle, no publishing
      python3 value/valuebot.py            # loop (Fly worker; publishes feed)
"""
import argparse
import calendar
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SSL_CTX = ssl._create_unverified_context()

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_SEL_DEN = "0xdd34de67"            # payoutDenominator(bytes32)
_SEL_NUM = "0x0504c814"            # payoutNumerators(bytes32,uint256)

STATE = os.path.join(HERE, "valuebot_state.json")
FEED = os.path.join(HERE, "valuebot.json")
FILLS = os.path.join(HERE, "valuebot_fills.jsonl")

BANK = 1000.0                      # paper bankroll
STAKE = 1.0                        # flat, = venue minimum (reality)
MAX_PX = 0.02                      # the studied bucket boundary
BAND = 1.05                        # protected band: ask*(1+5%), like the live executor
MAX_OPEN = 300                     # portfolio cap -> max $300 deployed
SCAN_S = 300
BOOK_BUDGET = 60                   # CLOB book fetches per cycle (be a good citizen)
SETTLE_BUDGET = 40                 # payout-vector checks per cycle
COOLDOWN_S = 6 * 3600              # re-look at a skipped/missed token after 6h
FEED_PUSH_MIN_S = 300
MISS_KEEP = 500                    # ledger rows kept in state (totals never truncate)

# Fee Structure V2 rates by category keyword (entry side only — redeem is free)
FEE_RATES = [("crypto", 0.07), ("sport", 0.03), ("esport", 0.03),
             ("finance", 0.04), ("politic", 0.04), ("tech", 0.04),
             ("geopolit", 0.0)]
FEE_DEFAULT = 0.05


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


def fee_rate(category):
    c = (category or "").lower()
    for k, r in FEE_RATES:
        if k in c:
            return r
    return FEE_DEFAULT


def taker_fee(shares, price, rate):
    return shares * rate * price * (1.0 - price)


def event_key(slug):
    """Correlation group: sub-split slugs collapse to their date prefix (the
    copy book learned this the hard way — one game, six markets)."""
    m = re.match(r"(.*?\d{4}-\d{2}-\d{2})", slug or "")
    return m.group(1) if m else (slug or None)


# ── market data ──────────────────────────────────────────────────────────────

def scan_universe(max_pages=60):
    """Active gamma markets with any outcome priced ≤ MAX_PX. Yields candidate
    dicts. outcomePrices is gamma's own mark — cheap prefilter only; the CLOB
    book is the truth a fill model is allowed to use."""
    out, offset = [], 0
    for _ in range(max_pages):
        try:
            # soonest-ending first: the calibration edge concentrates at short
            # time-to-resolution, so the book budget goes there before the
            # 2028-politics dust the default page order surfaces
            page = get_json(f"{GAMMA}/markets?active=true&closed=false"
                            f"&order=endDate&ascending=true"
                            f"&end_date_min={time.strftime('%Y-%m-%d')}"
                            f"&limit=100&offset={offset}")
        except Exception as e:
            log(f"gamma page {offset} failed: {str(e)[:60]}")
            break
        if not page:
            break
        for m in page:
            try:
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                toks = json.loads(m.get("clobTokenIds") or "[]")
                outs = json.loads(m.get("outcomes") or "[]")
            except Exception:
                continue
            if len(prices) != len(toks) or not toks:
                continue
            for i, px in enumerate(prices):
                if 0.0 < px <= MAX_PX:
                    ev = (m.get("events") or [{}])[0]
                    out.append({
                        "token": toks[i], "outcome": outs[i] if i < len(outs) else "?",
                        "mark": px, "cond": m.get("conditionId"),
                        "title": m.get("question") or "",
                        "end": m.get("endDate"), "cat": m.get("category")
                               or ev.get("category") or "",
                        "event": event_key(ev.get("slug") or m.get("slug")),
                        "tok_index": i, "n_outcomes": len(toks)})
        offset += 100
        if len(page) < 100:
            break
    return out


def book_asks(token):
    """Ask ladder [(price, size)] cheapest-first, or None on failure."""
    try:
        b = get_json(f"{CLOB}/book?token_id={token}", timeout=8)
        asks = sorted(((float(a["price"]), float(a["size"]))
                       for a in b.get("asks") or []), key=lambda x: x[0])
        return asks
    except Exception:
        return None


def model_fill(asks, stake, max_px, band=BAND):
    """Walk the real ask ladder inside min(max_px, best_ask*band); a FAK for
    `stake` dollars either fully fills inside the band or is an honest MISS
    (None, reason). Returns (shares, avg_price, None) on fill."""
    if asks is None:
        return None, None, "book fetch failed"
    if not asks:
        return None, None, "no asks on the book"
    best = asks[0][0]
    if best > max_px:
        return None, None, f"best ask {best:.3f} above {max_px:.2f}"
    cap = min(max_px, round(best * band, 6))
    usd, shares = 0.0, 0.0
    for px, sz in asks:
        if px > cap:
            break
        take_usd = min(stake - usd, px * sz)
        shares += take_usd / px
        usd += take_usd
        if usd >= stake - 1e-9:
            return shares, usd / shares, None
    return None, None, (f"only ${usd:.2f} of asks inside the band "
                        f"(cap {cap:.3f}) — FAK no-match")


# ── chain-truth settlement ───────────────────────────────────────────────────

def _rpc_url():
    url = os.environ.get("ALCHEMY_RPC_URL")
    if url:
        return url
    try:
        k = json.load(open(os.path.join(REPO, "config.json"))).get("alchemy_key")
        return f"https://polygon-mainnet.g.alchemy.com/v2/{k}" if k else None
    except Exception:
        return None


def onchain_payouts(cond, rpc):
    """[p0, p1, ...] in the market's token order, or None if unresolved.
    Denominator 0 = not resolved; [0.5, 0.5] refunds are REAL payouts."""
    if not (rpc and cond):
        return None

    def call(data):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                           "params": [{"to": CTF, "data": data}, "latest"]}).encode()
        req = urllib.request.Request(rpc, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            return json.loads(r.read())["result"]
    try:
        c = cond[2:].rjust(64, "0")
        den = int(call(_SEL_DEN + c), 16)
        if not den:
            return None
        n = 2
        return [int(call(_SEL_NUM + c + hex(i)[2:].rjust(64, "0")), 16) / den
                for i in range(n)]
    except Exception:
        return None


# ── the bot ──────────────────────────────────────────────────────────────────

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"cash": BANK, "my_pos": {}, "resolved": [], "missed": [],
                "attempted": {}, "stats": {"attempts": 0, "fills": 0,
                "misses": 0, "resolved": 0, "wins": 0, "refunds": 0,
                "losses": 0, "staked": 0.0, "returned": 0.0, "fees": 0.0},
                "started": int(time.time())}


def save_state(st):
    json.dump(st, open(STATE, "w"))


def open_positions(st, cands, budget):
    """Try to open new $1 tickets, honest-fill-model, event cap 1."""
    held_events = {p.get("event") for p in st["my_pos"].values() if p.get("event")}
    now = time.time()
    checked = 0
    for c in cands:
        if checked >= budget or len(st["my_pos"]) >= MAX_OPEN:
            break
        tok = c["token"]
        if tok in st["my_pos"]:
            continue
        if now - st["attempted"].get(tok, 0) < COOLDOWN_S:
            continue
        if c["event"] and c["event"] in held_events:
            continue                       # correlated dust resolves together
        if st["cash"] < STAKE:
            log("CAN'T OPEN — cash exhausted (portfolio at size)")
            break
        st["attempted"][tok] = now
        checked += 1
        st["stats"]["attempts"] += 1
        shares, px, reason = model_fill(book_asks(tok), STAKE, MAX_PX)
        if reason:
            st["stats"]["misses"] += 1
            st["missed"].append({"ts": int(now), "token": tok, "mark": c["mark"],
                                 "title": c["title"][:60], "reason": reason})
            st["missed"] = st["missed"][-MISS_KEEP:]
            continue
        rate = fee_rate(c["cat"])
        fee = taker_fee(shares, px, rate)
        st["cash"] -= STAKE + fee
        st["stats"]["fills"] += 1
        st["stats"]["staked"] += STAKE
        st["stats"]["fees"] += fee
        st["my_pos"][tok] = {"shares": shares, "cost": STAKE, "fee": round(fee, 6),
                             "price": round(px, 6), "cond": c["cond"],
                             "title": c["title"][:80], "outcome": c["outcome"],
                             "event": c["event"], "end": c["end"],
                             "tok_index": c["tok_index"], "opened": int(now)}
        held_events.add(c["event"])
        with open(FILLS, "a") as fh:
            fh.write(json.dumps({"ts": int(now), "side": "BUY", "token": tok,
                                 "shares": round(shares, 4), "price": round(px, 6),
                                 "fee": round(fee, 6), "title": c["title"][:60]}) + "\n")
        log(f"OPEN {shares:,.0f} sh @ {px:.4f} (${STAKE}) · {c['title'][:50]}")
    # prune the cooldown map so state can't grow unbounded
    st["attempted"] = {t: ts for t, ts in st["attempted"].items()
                       if now - ts < 2 * COOLDOWN_S}


def settle(st, rpc, budget):
    """Chain-truth settlement for positions past their end date."""
    now = time.time()
    done = 0
    for tok, p in list(st["my_pos"].items()):
        if done >= budget:
            break
        end = p.get("end")
        try:
            # gamma endDate is UTC — timegm, NOT mktime (repo lesson: mktime
            # assumes local and shifts settles by the box's UTC offset)
            end_ts = calendar.timegm(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")) if end else 0
        except Exception:
            end_ts = 0
        if end_ts and now < end_ts - 300:
            continue                        # not due yet
        vec = onchain_payouts(p["cond"], rpc)
        done += 1
        if vec is None:
            continue                        # unresolved — try next cycle
        idx = min(p.get("tok_index", 0), len(vec) - 1)
        payout = vec[idx] * p["shares"]
        st["cash"] += payout
        s = st["stats"]
        s["resolved"] += 1
        s["returned"] += payout
        kind = ("refund" if 0 < vec[idx] < 1 else
                "win" if vec[idx] >= 1 else "loss")
        s["wins" if kind == "win" else "refunds" if kind == "refund" else "losses"] += 1
        st["resolved"].append({"ts": int(now), "token": tok, "price": p["price"],
                               "cost": p["cost"], "payout": round(payout, 4),
                               "kind": kind, "title": p["title"][:60]})
        st["resolved"] = st["resolved"][-MISS_KEEP:]
        del st["my_pos"][tok]
        log(f"SETTLE {kind.upper()} {payout:+.2f} · entered {p['price']:.4f} · "
            f"{p['title'][:50]}")


def write_feed(st):
    s = st["stats"]
    deployed = sum(p["cost"] for p in st["my_pos"].values())
    mult = (s["returned"] / s["staked"]) if s["staked"] else None
    # break-even multiple is 1 + fee drag; the study's promise was ~1.24x
    feed = {"mode": "paper-value", "bank": BANK, "cash": round(st["cash"], 2),
            "deployed": round(deployed, 2), "open_count": len(st["my_pos"]),
            "stats": s, "realized_multiple": round(mult, 4) if mult else None,
            "fill_rate": round(s["fills"] / s["attempts"], 4) if s["attempts"] else None,
            "recent_resolved": st["resolved"][-40:], "recent_missed": st["missed"][-40:],
            "open": [{"t": p["title"], "px": p["price"], "out": p["outcome"],
                      "end": p.get("end")} for p in list(st["my_pos"].values())[:60]],
            "updated": int(time.time())}
    json.dump(feed, open(FEED, "w"))
    return feed


def publish(last_push):
    """Commit value/* only. Same pull-rebase-push discipline as the books."""
    if time.time() - last_push < FEED_PUSH_MIN_S:
        return last_push
    try:
        subprocess.run(["git", "add", "value/valuebot_state.json",
                        "value/valuebot.json", "value/valuebot_fills.jsonl"],
                       cwd=REPO, check=True, capture_output=True)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO)
        if r.returncode == 0:
            return last_push
        subprocess.run(["git", "commit", "-q", "-m",
                        "valuebot: paper feed [skip ci]"], cwd=REPO, check=True,
                       capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"],
                       cwd=REPO, capture_output=True)
        subprocess.run(["git", "push", "-q"], cwd=REPO, check=True,
                       capture_output=True, timeout=60)
        return time.time()
    except Exception as e:
        log(f"publish failed (non-fatal): {str(e)[:70]}")
        return last_push


def cycle(st, rpc, publish_feed=False, last_push=0.0):
    cands = scan_universe()
    log(f"universe: {len(cands)} sub-{MAX_PX:.0%} candidates")
    settle(st, rpc, SETTLE_BUDGET)
    open_positions(st, cands, BOOK_BUDGET)
    save_state(st)
    feed = write_feed(st)
    s = st["stats"]
    log(f"book: cash ${st['cash']:,.2f} · open {len(st['my_pos'])} · "
        f"fills {s['fills']}/{s['attempts']} · resolved {s['resolved']} "
        f"({s['wins']}W/{s['losses']}L/{s['refunds']}R) · "
        f"multiple {feed['realized_multiple']}")
    if publish_feed:
        last_push = publish(last_push)
    return last_push


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one cycle, no publish")
    args = ap.parse_args()
    rpc = _rpc_url()
    log(f"valuebot · paper · chain settle {'ON' if rpc else 'OFF (no RPC!)'}")
    st = load_state()
    if args.once:
        cycle(st, rpc, publish_feed=False)
        return
    last_push = 0.0
    while True:
        try:
            last_push = cycle(st, rpc, publish_feed=True, last_push=last_push)
        except Exception as e:
            log(f"cycle error: {str(e)[:100]}")
        time.sleep(SCAN_S)


if __name__ == "__main__":
    main()
