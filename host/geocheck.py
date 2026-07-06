#!/usr/bin/env python3
"""Can this box place Polymarket orders? (IP geo-gate probe, no keys needed)

Polymarket geoblocks TRADING by IP (reads are open everywhere): an order POST
from a restricted country gets 403 "Trading restricted in your region" before
auth is even checked, so an unauthenticated dummy order cleanly separates
geo-blocked (403 geo message) from allowed (400/401 auth/validation error).
Restricted list is long and includes surprises — US, UK, France, Germany,
Brazil (so no Fly gru!), Netherlands, Singapore, Australia:
https://docs.polymarket.com/developers/CLOB/geoblock

Probes, in order:
  1. ipinfo.io           — where does the internet think this box is
  2. polymarket geoblock — Polymarket's own verdict for this IP
  3. clob POST /order    — the gate that actually matters

Usage:
    python3 host/geocheck.py            # print verdict, exit 0 = tradable
    python3 host/geocheck.py --idle     # then sleep forever (test deploys:
                                        #   keeps the machine up for fly ssh)
"""
import json
import ssl
import sys
import time
import urllib.request

CLOB = "https://clob.polymarket.com"
# macOS framework Pythons often ship without CA certs (same workaround as the
# rest of the repo); the verdict is informational, so unverified is acceptable
_CTX = [None, ssl._create_unverified_context()]


def get(url, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Content-Type": "application/json"})
    err = None
    for ctx in _CTX:
        try:
            r = urllib.request.urlopen(req, timeout=20, context=ctx)
            return r.status, r.read().decode(errors="replace")[:300]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:300]
        except Exception as e:
            err = e
    return None, str(err)


def main():
    ok = True

    st, body = get("https://ipinfo.io/json")
    import re
    loc = dict(re.findall(r'"(country|city|org)":\s*"([^"]*)"', body or ""))
    if loc:
        print(f"[geo] ip location : {loc.get('country')} {loc.get('city')} ({loc.get('org')})")
    else:
        print(f"[geo] ip location : lookup failed ({st} {(body or '')[:80]})")

    st, body = get("https://polymarket.com/api/geoblock")
    print(f"[geo] pm geoblock  : {st} {body[:160]}")

    st, body = get(f"{CLOB}/order", method="POST", data=b"{}")
    geo_blocked = (st == 403 and "restricted" in body.lower())
    print(f"[geo] clob order   : {st} {body[:160]}")

    if geo_blocked:
        ok = False
        print("[geo] VERDICT: BLOCKED — this box cannot place Polymarket orders")
    elif st is None:
        ok = False
        print("[geo] VERDICT: UNKNOWN — clob unreachable from this box")
    else:
        print("[geo] VERDICT: TRADABLE — order endpoint reachable past the geo-gate")

    if "--idle" in sys.argv:
        print("[geo] --idle: sleeping so the machine stays up for inspection…", flush=True)
        while True:
            time.sleep(3600)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
