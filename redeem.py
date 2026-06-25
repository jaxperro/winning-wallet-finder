#!/usr/bin/env python3
"""On-chain redemption of resolved Polymarket positions (live mode, gap 2).

After a market resolves, winning conditional-token shares are still ERC-1155
tokens — you must REDEEM them through Gnosis CTF (ConditionalTokens) to turn them
back into USDC. The CLOB API doesn't do this; copybot calls this module so a
resolved winner's freed capital is actually back in the wallet (in paper mode the
recycle is just a number; live needs the real redemption).

Covers standard binary markets via CTF.redeemPositions. NEG-RISK markets settle
through a different adapter and are NOT handled here — copybot warns and you redeem
those in the Polymarket UI. (Neg-risk auto-redeem is a clean follow-up.)

Requires:  pip install web3   and a Polygon RPC. RPC comes from config
live.rpc_url, else is built from your Alchemy key. Each redeem costs a little POL
(MATIC) in gas — fund the EOA with a few POL.

UNTESTED against live until you run it on a small position — verify one redemption
manually before trusting the loop:
    python3 redeem.py <conditionId>     # redeem one resolved market, print tx hash
"""

import json
import os
import sys

# Polygon mainnet
CHAIN_ID = 137
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # Gnosis ConditionalTokens
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (collateral)
ZERO32 = b"\x00" * 32                                          # parentCollectionId

CTF_ABI = json.loads("""[
  {"constant": false,
   "inputs": [
     {"name": "collateralToken", "type": "address"},
     {"name": "parentCollectionId", "type": "bytes32"},
     {"name": "conditionId", "type": "bytes32"},
     {"name": "indexSets", "type": "uint256[]"}],
   "name": "redeemPositions",
   "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]""")


def _rpc(cfg):
    url = cfg.get("live", {}).get("rpc_url")
    if url:
        return url
    key = cfg.get("alchemy_key")
    if key:
        return f"https://polygon-mainnet.g.alchemy.com/v2/{key}"
    raise RuntimeError("no Polygon RPC — set live.rpc_url or alchemy_key in config")


class Redeemer:
    def __init__(self, cfg):
        from web3 import Web3                       # imported lazily (live-only dep)
        self.Web3 = Web3
        pk = cfg.get("live", {}).get("private_key")
        if not pk:
            raise RuntimeError("live.private_key required to redeem")
        self.w3 = Web3(Web3.HTTPProvider(_rpc(cfg)))
        if not self.w3.is_connected():
            raise RuntimeError("Polygon RPC not reachable")
        self.acct = self.w3.eth.account.from_key(pk)
        self.address = self.acct.address
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        self.usdc = Web3.to_checksum_address(USDC_ADDRESS)

    @staticmethod
    def _cond_bytes(condition_id):
        h = condition_id[2:] if condition_id.startswith("0x") else condition_id
        return bytes.fromhex(h)

    def try_redeem(self, condition_id, index_sets=(1, 2)):
        """Redeem all of this wallet's holdings in a resolved binary market.
        Returns (ok, tx_hash_or_reason). index_sets [1,2] covers both outcome
        slots — the winning one pays USDC, the losing one is a no-op."""
        try:
            fn = self.ctf.functions.redeemPositions(
                self.usdc, ZERO32, self._cond_bytes(condition_id), list(index_sets))
            tx = fn.build_transaction({
                "from": self.address,
                "nonce": self.w3.eth.get_transaction_count(self.address),
                "chainId": CHAIN_ID,
                "gasPrice": int(self.w3.eth.gas_price * 1.25),
            })
            signed = self.acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = self.w3.eth.send_raw_transaction(raw)
            rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
            hx = self.w3.to_hex(h)
            return (True, hx) if rcpt.status == 1 else (False, f"reverted {hx}")
        except Exception as e:
            return False, str(e)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 redeem.py <conditionId>")
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "config.json")))
    r = Redeemer(cfg)
    print(f"redeeming {sys.argv[1]} from {r.address} …")
    ok, info = r.try_redeem(sys.argv[1])
    print(("✅ " if ok else "❌ ") + info)


if __name__ == "__main__":
    main()
