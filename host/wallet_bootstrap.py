#!/usr/bin/env python3
"""Deposit-wallet bootstrap: mint a Builder API key with existing auth, then
let the SDK deploy the signer's default Deposit Wallet (gasless). Prints the
deposit wallet address — the destination for the live bankroll under the new
architecture. Deploys an EMPTY wallet; moves no funds."""
import os
from polymarket import SecureClient

pk = os.environ["LIVE_PRIVATE_KEY"].strip()
funder = os.environ["LIVE_FUNDER_ADDRESS"].strip()

print("1) auth with legacy wallet ref (no deploy)…")
c1 = SecureClient.create(private_key=pk, wallet=funder)
with c1:
    print("   auth OK")
    try:
        bk = c1.create_builder_api_key()
        print("   builder key minted:", str(bk)[:60], "…")
    except Exception as e:
        print("   builder key failed:", type(e).__name__, str(e)[:200])
        raise SystemExit(1)

print("2) reconstruct with api_key, no wallet → deploy default Deposit Wallet…")
try:
    c2 = SecureClient.create(private_key=pk, api_key=bk)
    with c2:
        for attr in ("wallet", "address", "deposit_wallet"):
            v = getattr(c2, attr, None)
            if v:
                print(f"   c2.{attr} = {v}")
        print("   DEPOSIT WALLET READY")
except Exception as e:
    print("   deploy failed:", type(e).__name__, str(e)[:300])
