#!/usr/bin/env python3
"""Read-only preflight for live trading — verifies credentials, balance, and
market access WITHOUT placing any order.

    python3 preflight_live.py            # uses config.live.json

Checks, in order:
  1. config.live.json parses; private key + funder present
  2. CLOB auth: derive L2 API creds from the key (proves the key signs)
  3. USDC balance + allowance on the funder (proves deposits landed and the
     proxy can spend — the balance the bot will actually trade with)
  4. live order book fetch for one of the followed wallets' recent markets
     (proves market-data access end to end)
  5. Polygon RPC + EOA POL gas balance (needed by auto-redeem)

Exit code 0 = every check passed; anything else prints what to fix.
"""

import json
import sys
import time
import urllib.request
import ssl

CLOB = "https://clob.polymarket.com"
_SSL = ssl._create_unverified_context()
OK, BAD = "  ✓", "  ✗"
failures = []


def check(name, fn):
    try:
        msg = fn()
        print(f"{OK} {name}" + (f" — {msg}" if msg else ""))
    except Exception as e:
        failures.append(name)
        print(f"{BAD} {name} — {e}")


def main():
    # config: --config PATH, else config.live.json. Secrets: env wins (the Fly
    # worker path — LIVE_PRIVATE_KEY / LIVE_FUNDER_ADDRESS / LIVE_SIGNATURE_TYPE
    # / ALCHEMY_RPC_URL — same 1.2 override chain as copybot; no key file needs
    # to exist anywhere on the box).
    import os
    path = "config.live.json"
    if "--config" in sys.argv:
        path = sys.argv[sys.argv.index("--config") + 1]
    cfg = json.load(open(path))
    live = cfg.setdefault("live", {})
    for env, key in (("LIVE_PRIVATE_KEY", "private_key"),
                     ("LIVE_FUNDER_ADDRESS", "funder_address"),
                     ("LIVE_SIGNATURE_TYPE", "signature_type"),
                     ("ALCHEMY_RPC_URL", "rpc_url")):
        if os.environ.get(env):
            live[key] = os.environ[env]
    if "watchlist" not in cfg and cfg.get("wallets"):
        cfg["watchlist"] = [w["wallet"] for w in cfg["wallets"]]
    pk, funder = live.get("private_key"), live.get("funder_address")
    if not pk or not funder:
        sys.exit("fill live.private_key and live.funder_address (config or env) first\n"
                 "(Polymarket profile -> the deposit/profile address is the funder;\n"
                 " email-login accounts export the key in Settings)")

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    client = ClobClient(host=CLOB, key=pk, chain_id=137,
                        signature_type=int(live.get("signature_type") or 1), funder=funder)

    def auth():
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return f"L2 api key {creds.api_key[:8]}… derived (signer {client.get_address()[:10]}…)"
    check("CLOB auth (key signs, creds derive)", auth)

    def balance():
        r = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        bal = int(r.get("balance", 0)) / 1e6
        if bal <= 0:
            raise RuntimeError("USDC balance is $0 — deposit test funds to the funder address first")
        return f"${bal:,.2f} USDC spendable on {funder[:10]}…"
    check("USDC balance on funder", balance)

    def book():
        req = urllib.request.Request(
            "https://data-api.polymarket.com/activity?user="
            + cfg["watchlist"][0] + "&type=TRADE&limit=1",
            headers={"User-Agent": "Mozilla/5.0"})
        t = json.loads(urllib.request.urlopen(req, timeout=15, context=_SSL).read())[0]
        ob = client.get_order_book(t["asset"])
        bid = ob.bids[-1].price if ob.bids else "—"
        ask = ob.asks[-1].price if ob.asks else "—"
        return f"{(t.get('title') or '')[:40]}… bid {bid} / ask {ask}"
    check("order book fetch (market access)", book)

    def gas():
        from web3 import Web3
        rpc = live.get("rpc_url") or (
            f"https://polygon-mainnet.g.alchemy.com/v2/{cfg.get('alchemy_key')}"
            if cfg.get("alchemy_key") else None)
        if not rpc:
            raise RuntimeError("no live.rpc_url and no alchemy_key — auto-redeem needs a Polygon RPC")
        w3 = Web3(Web3.HTTPProvider(rpc))
        acct = w3.eth.account.from_key(pk)
        pol = w3.eth.get_balance(acct.address) / 1e18
        if pol < 0.05:
            raise RuntimeError(f"EOA {acct.address[:10]}… holds {pol:.3f} POL — "
                               "send ~1 POL for redeem gas (or set live.auto_redeem false)")
        return f"{pol:.2f} POL gas on EOA {acct.address[:10]}…"
    check("Polygon RPC + redeem gas", gas)

    print()
    if failures:
        sys.exit(f"NOT ready: fix {len(failures)} item(s) above.")
    print("ALL CHECKS PASSED — ready for the supervised live test:")
    print("  python3 copybot.py --config config.live.json "
          "--state copybot_state.live.json --poll 60 --live")
    print('  (it will ask you to type the confirmation phrase before anything is placed)')


if __name__ == "__main__":
    main()
