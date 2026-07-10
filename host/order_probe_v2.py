#!/usr/bin/env python3
"""End-to-end order probe via the NEW unified SDK (polymarket-client) — the
old py-clob-client is archived and the CLOB rejects its order format
('invalid order version', discovered 2026-07-09 after two crash-restarts).
Buys ~$5 FAK on a liquid binary, introspects the result object (the port
needs its fill fields), then sells the position back."""
import json, os, ssl, sys, time, urllib.request

_SSL = ssl._create_unverified_context()
def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=15, context=_SSL))

gm = get("https://gamma-api.polymarket.com/markets?active=true&closed=false"
         "&limit=25&order=volume24hr&ascending=false")
tok = None
for m in gm:
    try:
        if m.get("negRisk") or m.get("negRiskAugmented"):
            continue
        t0 = json.loads(m["clobTokenIds"])[0]
        book = get(f"https://clob.polymarket.com/book?token_id={t0}")
        bids, asks = book.get("bids") or [], book.get("asks") or []
        bb = max((float(x["price"]) for x in bids), default=None)
        ba = min((float(x["price"]) for x in asks), default=None)
        if bb and ba and 0.10 <= ba <= 0.90 and (ba - bb) <= 0.02:
            tok = t0
            print(f"market: {m.get('question','?')[:60]} · bid {bb} / ask {ba}")
            break
    except Exception:
        continue
if not tok:
    sys.exit("no suitable market")

from polymarket import SecureClient
# wallet OMITTED on purpose: the SDK derives the signer's Deposit Wallet
# (passing the profile address raw produced 'maker address not allowed' —
# the new wallet architecture encodes wallet type into the maker)
client = SecureClient.create(private_key=os.environ["LIVE_PRIVATE_KEY"].strip())
for attr in ("wallet", "address", "deposit_wallet"):
    v = getattr(client, attr, None)
    if v:
        print(f"client.{attr} = {v}")

def dump(label, obj):
    print(f"\n--- {label} ---")
    print("type:", type(obj).__name__)
    d = getattr(obj, "__dict__", None) or {}
    if not d and hasattr(obj, "model_dump"):
        try: d = obj.model_dump()
        except Exception: pass
    print(json.dumps({k: str(v)[:120] for k, v in dict(d).items()}, indent=1, default=str)
          if d else repr(obj)[:800])

with client:
    print("\nBUY $5.00 FAK…")
    try:
        r = client.place_market_order(token_id=tok, side="BUY", amount=5, order_type="FAK")
        dump("buy result", r)
    except Exception as e:
        print("BUY raised:", type(e).__name__, str(e)[:300])
        sys.exit(1)
    time.sleep(3)
    funder = os.environ["LIVE_FUNDER_ADDRESS"].strip()
    pos = get(f"https://data-api.polymarket.com/positions?user={funder}&sizeThreshold=0&limit=50")
    mine = [p for p in pos if str(p.get("asset")) == str(tok)]
    print("\nposition after buy:", [{ "size": p.get("size"), "avg": p.get("avgPrice")} for p in mine])
    if mine and (mine[0].get("size") or 0) > 0:
        sh = mine[0]["size"]
        print(f"\nSELL {sh} shares FAK…")
        try:
            r2 = client.place_market_order(token_id=tok, side="SELL", shares=sh, order_type="FAK")
            dump("sell result", r2)
            print("\nROUND TRIP COMPLETE — new-SDK placement path PROVEN")
        except Exception as e:
            print("SELL raised:", type(e).__name__, str(e)[:300])
