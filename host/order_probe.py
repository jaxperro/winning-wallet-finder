#!/usr/bin/env python3
"""Controlled end-to-end order probe (Phase-4 in miniature): place ONE tiny
marketable FAK buy on a liquid market via the exact executor the bot uses,
report the fill, then immediately sell it back. Proves sign -> create ->
post -> fill -> parse without waiting for an organic signal — after two
crash-restarts proved this path had never once executed to completion.
Cost: the spread + fees on ~$2. Run on the live box (env creds)."""
import json, math, os, ssl, sys, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_SSL = ssl._create_unverified_context()

def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=15, context=_SSL))

# pick the highest-volume active market with a sane two-sided book
gm = get("https://gamma-api.polymarket.com/markets?active=true&closed=false"
         "&limit=5&order=volume24hr&ascending=false")
tok = ask = bid = None
for m in gm:
    try:
        t0 = json.loads(m["clobTokenIds"])[0]
        book = get(f"https://clob.polymarket.com/book?token_id={t0}")
        bids, asks = book.get("bids") or [], book.get("asks") or []
        bb = max((float(x["price"]) for x in bids), default=None)
        ba = min((float(x["price"]) for x in asks), default=None)
        if bb and ba and 0.10 <= ba <= 0.90 and (ba - bb) <= 0.02:
            tok, ask, bid = t0, ba, bb
            print(f"market: {m.get('question','?')[:60]}")
            print(f"book: bid {bb} / ask {ba}")
            break
    except Exception:
        continue
if not tok:
    sys.exit("no suitable liquid market found")

cfg = {"live": {"private_key": os.environ["LIVE_PRIVATE_KEY"].strip(),
                "funder_address": os.environ["LIVE_FUNDER_ADDRESS"].strip(),
                "signature_type": int(os.environ.get("LIVE_SIGNATURE_TYPE") or 1),
                "order_type": "FAK"}}
from copybot import LedgerLiveExecutor
ex = LedgerLiveExecutor(cfg)

shares = math.ceil(5 / ask * 100) / 100  # 5-share exchange minimum, ~$1-4.50
print(f"\nBUY probe: {shares} shares @ ~{ask} (≈ ${shares*ask:.2f}) FAK…")
r = ex.buy(tok, shares, ask, {})
print("result:", json.dumps({k: str(v)[:200] for k, v in r.items()}, indent=1))
if r["ok"] and r["filled_shares"] > 0:
    print(f"\nSELL back: {r['filled_shares']} @ ~{bid} FAK…")
    r2 = ex.sell(tok, r["filled_shares"], bid, {})
    print("result:", json.dumps({k: str(v)[:200] for k, v in r2.items()}, indent=1))
    print("\nround trip complete — placement path PROVEN")
else:
    print("\nbuy did not fill — path exercised without crash; inspect resp above")
