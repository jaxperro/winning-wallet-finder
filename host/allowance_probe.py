#!/usr/bin/env python3
"""One-shot: print the live funder's USDC balance AND allowance (the thing
preflight's balance check didn't separately assert — an unapproved exchange
allowance rejects every order while the balance reads fine)."""
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

c = ClobClient(host="https://clob.polymarket.com",
               key=os.environ["LIVE_PRIVATE_KEY"].strip(), chain_id=137,
               signature_type=int(os.environ.get("LIVE_SIGNATURE_TYPE") or 1),
               funder=os.environ["LIVE_FUNDER_ADDRESS"].strip())
c.set_api_creds(c.create_or_derive_api_creds())
r = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print("raw:", r)
bal = int(r.get("balance", 0)) / 1e6
print(f"balance ${bal:,.2f}")
for k, v in r.items():
    if "allowance" in k.lower():
        print(f"{k}: {v}")
