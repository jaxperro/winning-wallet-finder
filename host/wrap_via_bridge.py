#!/usr/bin/env python3
"""One-time bankroll conversion: native USDC (deposit wallet) → pUSD collateral.

WHY (2026-07-10, verified on-chain): the 2026 exchange's balance view counts
ONLY the pUSD CollateralToken (0xC011a7…). pUSD accepts BOTH native USDC and
USDC.e by contract (`onlyValidAsset`), but the public CollateralOnramp
(0x93070a…) has native USDC PAUSED (paused(0x3c499c…)=1 — the exact cause of
the 'batch would revert' on the direct wrap attempt). The sanctioned path for
native USDC is Polymarket's own Bridge (the UI deposit flow): it swaps native
USDC → USDC.e and delivers to the registered wallet; USDC.e wrapping is live.

Flow (stage-gated, aborts on any failed link — never guesses with live funds):
  A. bridge a $3 test slice: gasless ERC-20 transfer → per-wallet bridge
     deposit address → poll until USDC.e lands at the deposit wallet
  B. bridge the remainder the same way
  C. gasless approve(onramp) + onramp.wrap(USDC.e, wallet, all) → pUSD
  D. success = get_balance_allowance(COLLATERAL) ≈ the bankroll

Run on the live Fly box (needs LIVE_PRIVATE_KEY; geo-gated venue):
  python3 host/wrap_via_bridge.py
"""
import json
import os
import ssl
import sys
import time
import urllib.request

_SSL = ssl._create_unverified_context()

USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"      # native (paused on onramp)
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"     # bridged (wrappable)
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"      # CollateralToken
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"    # CollateralOnramp
DW = "0x455e252e45Ee46d6C4cc1c8fAdD3899d68f245a1"        # our deposit wallet
# bridge deposit address minted for DW on 2026-07-10 (bridge.polymarket.com);
# pinned so a drifting registration answer aborts instead of misdirecting funds
EXPECTED_BRIDGE_EVM = "0x66C1A4b43824CB0DDa54c94F118fc868A6270b91"
BRIDGE = "https://bridge.polymarket.com"
TEST_SLICE = 3_000_000        # $3 — above the bridge's $2 Polygon minimum
POLL_S, DELIVER_TIMEOUT_S = 15, 900


def http(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=30, context=_SSL))


def rpc_call(to, data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    url = os.environ.get("ALCHEMY_RPC_URL", "https://polygon.drpc.org").strip()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20, context=_SSL))["result"]


def bal(token, holder=DW):
    return int(rpc_call(token, "0x70a08231" + holder[2:].lower().rjust(64, "0")), 16)


def fmt(u):
    return f"${u/1e6:,.2f}"


def wait_handle(label, h):
    out = h.wait() if h is not None and hasattr(h, "wait") else h
    print(f"  {label}: {str(out)[:220]}")
    return out


def delivered_units():
    """pUSD + USDC.e at the wallet — the $3 test slice (2026-07-10) proved the
    bridge wraps on delivery (native → USDC.e swap, then pUSD credited), so
    delivery must be measured on pUSD; USDC.e is counted in case a future
    bridge change stops short of the wrap (the wrap step below picks it up)."""
    return bal(PUSD) + bal(USDCE)


def bridge_leg(client, calls_mod, amount, bridge_addr, label):
    """Gasless-transfer `amount` native USDC to the bridge and wait until the
    deposit wallet's pUSD/USDC.e rises by ~amount. Returns True on delivery."""
    before = delivered_units()
    print(f"\n[{label}] sending {fmt(amount)} native USDC → bridge {bridge_addr}")
    t = calls_mod.erc20_transfer_call(token_address=USDC, recipient=bridge_addr,
                                      amount=amount)
    try:
        h = client.execute_transaction(calls=[t], metadata=f"bridge deposit {label}")
        wait_handle("relayer", h)
    except Exception as e:
        print(f"  transfer FAILED before leaving the wallet: {type(e).__name__} {e}")
        return False
    deadline = time.time() + DELIVER_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(POLL_S)
        now = delivered_units()
        try:
            st = http(f"{BRIDGE}/status/{bridge_addr}")
            txs = st.get("transactions") or []
            latest = txs[0].get("status") if txs else "none"
        except Exception as e:
            latest = f"status err {e}"
        print(f"  pUSD+USDC.e {fmt(now)} (Δ{fmt(now-before)}) · bridge: {latest}", flush=True)
        # bridge swap costs a few tenths of a cent — 1% tolerance is generous
        if now - before >= amount * 0.99:
            print(f"  [{label}] DELIVERED")
            return True
    print(f"  [{label}] NOT delivered within {DELIVER_TIMEOUT_S}s — ABORT. "
          f"Track {BRIDGE}/status/{bridge_addr}; funds are at the bridge, "
          f"recovery: https://matic-recovery.polymarket.com/")
    return False


def main():
    pk = os.environ["LIVE_PRIVATE_KEY"].strip()
    from polymarket import SecureClient
    from polymarket import calls as pmcalls
    from web3 import Web3

    native = bal(USDC)
    print(f"deposit wallet {DW}\n  native USDC {fmt(native)} · USDC.e "
          f"{fmt(bal(USDCE))} · pUSD {fmt(bal(PUSD))}")

    # pause-state preflight: if Polymarket unpauses native USDC later, the
    # bridge becomes unnecessary — but also if they pause USDC.e we must stop.
    paused_sel = Web3.keccak(text="paused(address)")[:4].hex()
    usdce_paused = int(rpc_call(ONRAMP, "0x" + paused_sel
                                + USDCE[2:].lower().rjust(64, "0")), 16)
    if usdce_paused:
        sys.exit("onramp has USDC.e PAUSED — nothing safe to do, aborting")

    boot = SecureClient.create(private_key=pk)
    with boot:
        bk = boot.create_builder_api_key()
        print("builder key minted (in-process only)")
    client = SecureClient.create(private_key=pk, api_key=bk)
    # revoke on ANY exit path — unrevoked keys are inert but pile up forever
    # (revocation is HMAC-authed by the key's own secret; see order_probe_v2)
    import atexit
    atexit.register(lambda: (client.revoke_builder_api_key(),
                             print("builder key revoked")))
    with client:
        if str(client.wallet).lower() != DW.lower():
            sys.exit(f"client.wallet {client.wallet} != expected {DW} — aborting")

        if native > 0:
            reg = http(f"{BRIDGE}/deposit", {"address": DW})
            bridge_addr = (reg.get("address") or {}).get("evm", "")
            print(f"bridge deposit address: {bridge_addr}")
            if bridge_addr.lower() != EXPECTED_BRIDGE_EVM.lower():
                sys.exit(f"bridge address drifted (expected {EXPECTED_BRIDGE_EVM})"
                         " — aborting, investigate before sending funds")
            if native <= TEST_SLICE + 2_000_000:   # too small to stage: one leg
                legs = [(native, "all")]
            else:
                legs = [(TEST_SLICE, "test-slice"),
                        (native - TEST_SLICE, "remainder")]
            for amount, label in legs:
                if not bridge_leg(client, pmcalls, amount, bridge_addr, label):
                    sys.exit(1)
        else:
            print("no native USDC left to bridge — proceeding to wrap")

        usdce = bal(USDCE)
        if usdce == 0:
            # normal: the bridge wraps on delivery, so everything is already pUSD
            print(f"\nno USDC.e residue — bridge delivered wrapped. "
                  f"pUSD {fmt(bal(PUSD))}")
            try:
                b = client.get_balance_allowance(asset_type="COLLATERAL")
                print("exchange-view COLLATERAL:", str(b)[:220])
            except Exception as e:
                print("balance check raised:", type(e).__name__, str(e)[:150])
            return
        print(f"\n[wrap] approve onramp + wrap {fmt(usdce)} USDC.e → pUSD")
        appr = pmcalls.erc20_approval_call(token_address=USDCE, spender=ONRAMP,
                                           amount=usdce)
        wrap_sel = Web3.keccak(text="wrap(address,address,uint256)")[:4]
        from eth_abi import encode as abi_encode
        wrap_data = "0x" + wrap_sel.hex().replace("0x", "") + abi_encode(
            ["address", "address", "uint256"], [USDCE, DW, usdce]).hex()
        wrap_call = pmcalls.TransactionCall(to=ONRAMP, data=wrap_data)
        try:
            h = client.execute_transaction(calls=[appr, wrap_call],
                                           metadata="wrap USDC.e to pUSD")
            wait_handle("batch approve+wrap", h)
        except Exception as e:
            print(f"  batch failed ({type(e).__name__} {str(e)[:150]}) — "
                  "falling back to two sequential transactions")
            try:
                wait_handle("approve", client.execute_transaction(
                    calls=[appr], metadata="approve USDC.e to onramp"))
                wait_handle("wrap", client.execute_transaction(
                    calls=[wrap_call], metadata="wrap USDC.e to pUSD"))
            except Exception as e2:
                print(f"  sequential wrap FAILED too: {type(e2).__name__} {e2}")
                sys.exit(1)

        time.sleep(5)
        print(f"\nfinal balances · native {fmt(bal(USDC))} · USDC.e "
              f"{fmt(bal(USDCE))} · pUSD {fmt(bal(PUSD))}")
        try:
            b = client.get_balance_allowance(asset_type="COLLATERAL")
            print("exchange-view COLLATERAL:", str(b)[:220])
        except Exception as e:
            print("balance check raised:", type(e).__name__, str(e)[:150])
        print("\nDONE — success = exchange-view collateral ≈ the bankroll")


if __name__ == "__main__":
    main()
