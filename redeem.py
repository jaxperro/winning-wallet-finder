#!/usr/bin/env python3
"""On-chain redemption of resolved Polymarket positions — 2026 pUSD stack.

REWRITTEN 2026-07-19 (closes #4; audit 3.3). The old web3 version was a
silent no-op booby trap on the current stack: it redeemed with USDC.e
collateral from the EOA, but live positions are pUSD-collateralized
(0xC011a7…, README gotcha 16) and live in the DEPOSIT WALLET (0x455e…45a1).
`redeemPositions` with the wrong collateral/holder SUCCEEDS doing nothing —
try_redeem returned ok and callers booked proceeds that never arrived.

Now: zero web3. The unified SDK's gasless relay executes redeemPositions
FROM the deposit wallet (same `execute_transaction` path the 07-10 bridge
wrap used), with pUSD as the collateral token. Selector 0x01b7037c =
keccak4("redeemPositions(address,bytes32,bytes32,uint256[])"), computed and
pinned 2026-07-19. The result reports the MEASURED pUSD delta so callers can
see what actually landed (the balance can move concurrently with trading, so
the delta is evidence, not the ledger entry).

NEG-RISK markets settle through a different adapter — callers must keep
excluding them (market_neg_risk guard); redeeming them here is a no-op.

    python3 redeem.py <conditionId>    # one-shot manual redeem (local config)
"""

import json
import os
import sys
import time

CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"    # ConditionalTokens
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # CollateralToken (2026 stack)
_SEL_REDEEM = "01b7037c"   # redeemPositions(address,bytes32,bytes32,uint256[])


def _w32(v):
    if isinstance(v, int):
        return hex(v)[2:].rjust(64, "0")
    h = v[2:] if v.startswith("0x") else v
    return h.lower().rjust(64, "0")


def redeem_calldata(condition_id, index_sets=(1, 2)):
    """ABI-encode redeemPositions(pUSD, 0x0, cond, indexSets). Head: 3 static
    words + the dynamic-array offset (0x80); tail: length + members."""
    head = _w32(PUSD) + _w32(0) + _w32(condition_id) + _w32(0x80)
    tail = _w32(len(index_sets)) + "".join(_w32(i) for i in index_sets)
    return "0x" + _SEL_REDEEM + head + tail


class Redeemer:
    """Redeems via an already-authenticated SecureClient (share the live
    executor's — one client, one deposit wallet, no second key path)."""

    def __init__(self, client):
        if client is None:
            raise RuntimeError("Redeemer needs the live executor's SecureClient")
        self.client = client

    def _collateral(self):
        try:
            return self.client.get_balance_allowance(asset_type="COLLATERAL").balance
        except Exception:
            return None

    def try_redeem(self, condition_id, index_sets=(1, 2)):
        """-> (ok, info). ok = the relayed tx was accepted; info carries the
        measured pUSD delta (evidence — concurrent fills can move it too)."""
        try:
            from polymarket import calls as pmcalls
            before = self._collateral()
            call = pmcalls.TransactionCall(to=CTF,
                                           data=redeem_calldata(condition_id,
                                                                index_sets))
            h = self.client.execute_transaction(
                calls=[call], metadata=f"redeem {condition_id[:14]}")
            out = h.wait() if hasattr(h, "wait") else h
            time.sleep(2)                       # let the relayed state settle
            after = self._collateral()
            delta = (after - before) / 1e6 if None not in (before, after) else None
            return True, (f"redeemed · pUSD delta "
                          f"{f'{delta:+.2f}' if delta is not None else 'unmeasured'}"
                          f" · {str(out)[:60]}")
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:80]}"


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 redeem.py <conditionId>")
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "config.json")))
    from polymarket import SecureClient
    pk = (os.environ.get("LIVE_PRIVATE_KEY") or "").strip() \
        or cfg.get("live", {}).get("private_key")
    if not pk:
        sys.exit("no live.private_key / LIVE_PRIVATE_KEY")
    r = Redeemer(SecureClient.create(private_key=pk))
    ok, info = r.try_redeem(sys.argv[1])
    print(("✅ " if ok else "❌ ") + info)


if __name__ == "__main__":
    main()
