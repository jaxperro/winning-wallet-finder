#!/usr/bin/env python3
"""Read-only preflight for live trading — verifies credentials, balance, and
market access on the UNIFIED SDK (polymarket-client) WITHOUT placing any
order. (Rewritten 2026-07-13: the original validated the archived
py-clob-client stack, whose orders the CLOB rejects — gotcha 16.)

    python3 preflight_live.py            # uses config.live.json
    python3 preflight_live.py --config config.live.example.json   # Fly path

Checks, in order:
  1. config parses; private key present (env LIVE_PRIVATE_KEY wins)
  2. unified-SDK auth: SecureClient.create resolves the Deposit Wallet
  3. pUSD collateral via get_balance_allowance — the balance the exchange
     will actually let the bot trade with (raw USDC reads 0 — gotcha 16)
  4. live order book fetch for a followed wallet's recent market
  5. RTDS trade stream: connect + subscribe + first message (T0 detection)
  6. geo-gate verdict (informational on a blocked box; the bot enforces
     fatally at boot)

Exit code 0 = every check passed; anything else prints what to fix.
"""

import json
import os
import ssl
import sys
import urllib.request

_SSL = ssl._create_unverified_context()
OK, BAD = "  ✓", "  ✗"
failures = []


def check(name, fn):
    try:
        msg = fn()
        print(f"{OK} {name}" + (f" — {msg}" if msg else ""))
    except Exception as e:
        failures.append(name)
        print(f"{BAD} {name} — {type(e).__name__}: {e}")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=15, context=_SSL))


def main():
    path = "config.live.json"
    if "--config" in sys.argv:
        path = sys.argv[sys.argv.index("--config") + 1]
    cfg = json.load(open(path))
    live = cfg.setdefault("live", {})
    if (os.environ.get("LIVE_PRIVATE_KEY") or "").strip():
        live["private_key"] = os.environ["LIVE_PRIVATE_KEY"].strip()
    pk = live.get("private_key")
    if not pk:
        sys.exit(f"{BAD} no live.private_key in {path} and no LIVE_PRIVATE_KEY env")
    print(f"{OK} config parses — key present, pct {cfg.get('bankroll_pct')}, "
          f"band [{cfg['risk'].get('min_price')},{cfg['risk'].get('max_price')}]")

    from polymarket import SecureClient
    client = SecureClient.create(private_key=pk)

    def auth():
        return f"deposit wallet {client.wallet}"
    check("unified-SDK auth (SecureClient.create)", auth)

    def collateral():
        b = client.get_balance_allowance(asset_type="COLLATERAL")
        usd = b.balance / 1e6
        if usd <= 0:
            raise RuntimeError("exchange-view collateral is $0 — bankroll "
                               "not wrapped to pUSD? (gotcha 16)")
        return f"${usd:,.2f} pUSD, {len(b.allowances)} spender allowances"
    check("exchange-view collateral (pUSD)", collateral)

    def book():
        for w in [x["wallet"] for x in cfg.get("wallets", [])][:3]:
            for t in get(f"https://data-api.polymarket.com/activity?user={w}"
                         f"&type=TRADE&limit=5"):
                tok = t.get("asset")
                if not tok:
                    continue
                ob = get(f"https://clob.polymarket.com/book?token_id={tok}")
                bids, asks = ob.get("bids") or [], ob.get("asks") or []
                return (f"{len(bids)} bids / {len(asks)} asks on "
                        f"{(t.get('title') or '?')[:40]}")
        raise RuntimeError("no recent market found across the follow set")
    check("order-book access (followed wallet's market)", book)

    def rtds():
        import threading
        import websocket
        got = []

        def on_open(ws):
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                {"topic": "activity", "type": "trades", "filters": ""}]}))

        def on_message(ws, raw):
            got.append(raw)
            ws.close()
        app = websocket.WebSocketApp("wss://ws-live-data.polymarket.com",
                                     on_open=on_open, on_message=on_message)
        t = threading.Thread(target=lambda: app.run_forever(
            sslopt={"cert_reqs": ssl.CERT_NONE}), daemon=True)
        t.start()
        t.join(timeout=15)
        if not got:
            raise RuntimeError("no message within 15s")
        return "stream delivers (T0 detection reachable)"
    check("RTDS trade stream", rtds)

    def geo():
        r = get("https://polymarket.com/api/geoblock")
        return ("TRADABLE from here" if not r.get("blocked")
                else f"BLOCKED here ({r.get('country')}) — bot must run on "
                     "the Fly box (informational)")
    check("geo-gate", geo)

    client.close()
    if failures:
        sys.exit(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
    print("\nall checks passed — safe to arm")


if __name__ == "__main__":
    main()
