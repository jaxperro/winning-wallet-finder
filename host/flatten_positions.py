#!/usr/bin/env python3
"""Flatten: market-sell every open position at the SDK-resolved wallet (FAK).

Ops utility (unified SDK). Born 2026-07-10: the order probe's buy leg filled
but its fixed 3s indexer wait missed the position, skipping the sell-back.
Also the emergency exit if the live book ever needs manual flattening.

  python3 host/flatten_positions.py            # sell everything
  python3 host/flatten_positions.py <token_id> # sell one token only
"""
import json
import os
import ssl
import sys
import time
import urllib.request

_SSL = ssl._create_unverified_context()


def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=15, context=_SSL))


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    from polymarket import SecureClient
    client = SecureClient.create(private_key=os.environ["LIVE_PRIVATE_KEY"].strip())
    with client:
        wallet = str(client.wallet)
        print("wallet:", wallet)
        # the data-api indexer lags fills by a few seconds — poll briefly
        pos = []
        for _ in range(12):
            pos = [p for p in get("https://data-api.polymarket.com/positions"
                                  f"?user={wallet}&sizeThreshold=0&limit=100")
                   if (p.get("size") or 0) > 0
                   and (only is None or str(p.get("asset")) == str(only))]
            if pos:
                break
            time.sleep(5)
        if not pos:
            print("no open positions" + (f" for token {only}" if only else ""))
            return
        for p in pos:
            tok, sz = str(p["asset"]), float(p["size"])
            print(f"\nSELL {sz} of {p.get('title', tok)[:60]} "
                  f"(avg in {p.get('avgPrice')})")
            try:
                r = client.place_market_order(token_id=tok, side="SELL",
                                              shares=sz, order_type="FAK")
            except Exception as e:
                print("  raised:", type(e).__name__, str(e)[:200])
                continue
            if getattr(r, "ok", False):
                making = float(r.making_amount or 0)   # shares given
                taking = float(r.taking_amount or 0)   # USD received
                px = taking / making if making else 0
                print(f"  {r.status}: sold {making} @ ~{px:.4f} → ${taking:.2f} "
                      f"(order {r.order_id[:18]}…)")
            else:
                print(f"  rejected {getattr(r, 'code', '?')}: "
                      f"{getattr(r, 'message', r)}")
        try:
            b = client.get_balance_allowance(asset_type="COLLATERAL")
            print(f"\ncollateral after: ${b.balance/1e6:.2f}")
        except Exception as e:
            print("balance check:", type(e).__name__, str(e)[:120])


if __name__ == "__main__":
    main()
