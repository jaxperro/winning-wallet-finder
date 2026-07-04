#!/usr/bin/env python3
"""Alchemy webhook receiver — push-based wallet trade alerts (no polling).

Alchemy's Address-Activity webhook POSTs here the moment a watched wallet
transacts on Polygon. We confirm/enrich the trade via Polymarket's data-API
(so the alert has market, side, outcome, price, size) and push it to Discord.

Zero dependencies (stdlib http server). Binds to $PORT (Render/Railway/Fly) or
8080. Endpoints:
    POST /alchemy   ← Alchemy points here
    GET  /health    ← uptime check

Config (config.json):
    "discord_webhook": "...",
    "watch": [{"wallet":"0x...","name":"Famecesgoal"}, ...],
    "alchemy_signing_key": "whsec_..."   # optional; verifies the POST is genuine
"""

import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_API = "https://data-api.polymarket.com"
SSL_CTX = ssl._create_unverified_context()


def _load_cfg():
    for p in ("config.json",):
        if os.path.exists(p):
            try:
                return json.load(open(p))
            except Exception:
                pass
    return {}


CFG = _load_cfg()
# secrets come from env in the cloud (never committed); config.json is local fallback
WEBHOOK = os.environ.get("DISCORD_WEBHOOK") or CFG.get("discord_webhook", "")
SIGNING_KEY = os.environ.get("ALCHEMY_SIGNING_KEY") or CFG.get("alchemy_signing_key", "")
# watch list is non-secret → committed watch.json (so it deploys), config fallback
if os.path.exists("watch.json"):
    WATCH = json.load(open("watch.json"))
else:
    WATCH = CFG.get("watch", [])
NAMES = {w["wallet"].lower(): w["name"] for w in WATCH}
SEEN = set()            # tx hashes already alerted (in-memory; dedups retries)


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


def get_json(path, params):
    url = DATA_API + path + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def post_discord(content):
    if not WEBHOOK:
        return
    try:
        body = json.dumps({"content": content}).encode()
        req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Mozilla/5.0"})  # Discord 403s default UA
        urllib.request.urlopen(req, timeout=10, context=SSL_CTX).read()
    except (urllib.error.URLError, TimeoutError):
        log("⚠ Discord post failed")


def alert_for_wallet(wallet):
    """Pull the wallet's latest trades; alert on any we haven't seen."""
    name = NAMES.get(wallet.lower(), wallet[:10] + "…")
    acts = get_json("/activity", {"user": wallet, "type": "TRADE", "limit": 10}) or []
    for t in sorted(acts, key=lambda x: x.get("timestamp", 0)):
        tx = t.get("transactionHash")
        if not tx or tx in SEEN:
            continue
        # only fire on very recent trades (the webhook just told us one happened)
        if time.time() - t.get("timestamp", 0) > 600:
            SEEN.add(tx)
            continue
        SEEN.add(tx)
        side = t.get("side", "?")
        emoji = "🟢" if side == "BUY" else "🔴"
        msg = (f"{emoji} **{name}** {side} **{t.get('outcome', '?')}** "
               f"@ {t.get('price', 0):.3f}  (${t.get('usdcSize', 0):,.0f})\n"
               f"{t.get('title', '?')}\n"
               f"<https://polymarket.com/profile/{wallet}>")
        log(f"ALERT {name}: {side} {t.get('outcome')} @ {t.get('price')} "
            f"${t.get('usdcSize', 0):,.0f}")
        post_discord(msg)


def addresses_in_payload(payload):
    out = set()
    for a in payload.get("event", {}).get("activity", []):
        for k in ("fromAddress", "toAddress"):
            v = a.get(k)
            if v and v.lower() in NAMES:
                out.add(v.lower())
    return out


def verify(raw, sig):
    if not SIGNING_KEY:
        return True            # verification off if no key configured
    digest = hmac.new(SIGNING_KEY.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig or "")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body="ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        self._send(200, "alive" if self.path == "/health" else "polymarket watcher")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(raw, self.headers.get("x-alchemy-signature")):
            log("⚠ bad signature — rejected")
            return self._send(401, "bad signature")
        self._send(200)                      # ack fast; Alchemy retries on non-2xx
        try:
            payload = json.loads(raw or b"{}")
            for w in addresses_in_payload(payload):
                alert_for_wallet(w)
        except Exception as e:
            log(f"handler error: {e}")

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", 8080))
    if WEBHOOK:
        post_discord(f"👀 **Webhook watcher online** · push alerts for "
                     f"{len(NAMES)} wallets (fires only on a real trade).")
    log(f"listening on :{port} · {len(NAMES)} wallets · "
        f"signature-verify {'ON' if SIGNING_KEY else 'OFF'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
