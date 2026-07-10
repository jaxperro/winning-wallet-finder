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
# wallet omitted: SDK resolves the signer's (now-deployed) Deposit Wallet.
# First construct WITHOUT api_key to mint a Builder key, then reconstruct
# WITH it — the allowance-recovery path (gasless approvals on first order)
# needs it. One-time: once approvals exist, the bot runs without any key.
pk = os.environ["LIVE_PRIVATE_KEY"].strip()
boot = SecureClient.create(private_key=pk)
with boot:
    bk = boot.create_builder_api_key()
    print("builder key minted (in-process only)")
client = SecureClient.create(private_key=pk, api_key=bk)
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
    print("gasless ready:", getattr(client, "is_gasless_ready", "?"))
    print("running setup_trading_approvals()…")
    try:
        h = client.setup_trading_approvals()
        if h is not None and hasattr(h, "wait"):
            out = h.wait()
            print("  approvals outcome:", str(out)[:200])
        else:
            print("  approvals result:", str(h)[:200])
    except Exception as e:
        print("  setup_trading_approvals:", type(e).__name__, str(e)[:250])
    # the SDK approvals list covers the COLLATERAL token only — but funds
    # arriving as raw native USDC get wrapped by the exchange AT MATCH, which
    # needs the maker's USDC approved to the collateral contract. Approve it.
    USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    COLLAT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    try:
        h = client.approve_erc20(token_address=USDC, spender_address=COLLAT, amount="max")
        if h is not None and hasattr(h, "wait"):
            print("  USDC→collateral approval:", str(h.wait())[:150])
        else:
            print("  USDC→collateral approval:", str(h)[:150])
    except Exception as e:
        print("  approve_erc20:", type(e).__name__, str(e)[:250])
    # PRE-WRAP is mandatory: the exchange's balance view counts the
    # COLLATERAL token only. The public wrapper periphery (0x93070a…, holds
    # the wrapper role; used by the UI's 4337 flows) exposes
    # wrap(asset, to, amount). Approve + wrap the full USDC balance in one
    # gasless transaction from the deposit wallet.
    from polymarket.calls import TransactionCall, erc20_approval_call
    from web3 import Web3
    from eth_abi import encode as abi_encode
    WRAPPER = "0x93070a847efef7f70739046a929d47a521f5b8ee"
    dw = str(client.wallet)
    rpc_body = json.dumps({"id":1,"jsonrpc":"2.0","method":"eth_call","params":[
        {"to": USDC, "data": "0x70a08231" + dw[2:].lower().rjust(64, "0")}, "latest"]}).encode()
    rq = urllib.request.Request(os.environ["ALCHEMY_RPC_URL"].strip(), data=rpc_body,
                                headers={"content-type": "application/json"})
    units = int(json.load(urllib.request.urlopen(rq, timeout=20, context=_SSL))["result"], 16)
    print(f"  wrapping {units/1e6:.2f} USDC → collateral…")
    if units > 0:
        appr = erc20_approval_call(token_address=USDC, spender=WRAPPER, amount=units)
        sel = Web3.keccak(text="wrap(address,address,uint256)")[:4]
        data = "0x" + sel.hex().replace("0x", "") + abi_encode(
            ["address", "address", "uint256"], [USDC, dw, units]).hex()
        wrap_call = TransactionCall(to=WRAPPER, data=data)
        try:
            h = client.execute_transaction(calls=[appr, wrap_call], metadata="wrap USDC to collateral")
            print("  wrap outcome:", str(h.wait())[:200])
        except Exception as e:
            print("  wrap failed:", type(e).__name__, str(e)[:250])
    try:
        b = client.get_balance_allowance(asset_type="COLLATERAL")
        print("  exchange-view balance:", str(b)[:220])
    except Exception as e:
        print("  balance check:", type(e).__name__, str(e)[:150])

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
