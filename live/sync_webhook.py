#!/usr/bin/env python3
"""Sync the Alchemy address-activity webhook to the live follow set.

Run by deploy_bot.sh so the push-mode webhook (`copybot follow set`,
wh_… in ../config.json) always watches exactly the wallets in
copybot.paper.json. Without this, adding/removing a wallet leaves the
webhook's address list stale — the 5-min backstop poll still catches
trades, but at poll-speed lag instead of push-speed.

Needs `alchemy_notify_token` in the gitignored ../config.json — the
"Auth token" from dashboard.alchemy.com → Webhooks (top right → Copy).
Skips politely when it's missing.
"""
import json
import os
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_SSL = ssl._create_unverified_context()
API = "https://dashboard.alchemy.com/api"


def call(method, path, token, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Alchemy-Token": token, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=_SSL).read())


def sync_one(token, wh_id, want, label):
    have, after = set(), None
    while True:  # paginated
        q = f"/webhook-addresses?webhook_id={wh_id}&limit=100" + \
            (f"&after={after}" if after else "")
        r = call("GET", q, token)
        have |= {a.lower() for a in r.get("data", [])}
        after = (r.get("pagination") or {}).get("cursors", {}).get("after")
        if not after:
            break

    add, drop = sorted(want - have), sorted(have - want)
    if not add and not drop:
        print(f"[webhook-sync] {label} in sync — {len(want)} addresses on {wh_id}")
        return
    call("PATCH", "/update-webhook-addresses", token,
         {"webhook_id": wh_id, "addresses_to_add": add, "addresses_to_remove": drop})
    print(f"[webhook-sync] {label} {wh_id}: +{len(add)} −{len(drop)} → {len(want)} addresses"
          + (f" (added {', '.join(a[:10] for a in add)})" if add else "")
          + (f" (removed {', '.join(a[:10] for a in drop)})" if drop else ""))


def main():
    try:
        cfg = json.load(open(os.path.join(HERE, "..", "config.json")))
    except Exception:
        cfg = {}
    token = cfg.get("alchemy_notify_token")
    if not token:
        print("[webhook-sync] no alchemy_notify_token in ../config.json — skipped."
              " (Copy the Auth token from dashboard.alchemy.com → Webhooks to enable"
              " automatic address sync; until then update the address list there"
              " manually — the 5-min backstop poll covers the gap at poll-speed lag.)")
        return 0
    want = {w["wallet"].lower()
            for w in json.load(open(os.path.join(HERE, "copybot.paper.json")))["wallets"]}

    wh_id = cfg.get("alchemy_webhook_id")
    if not wh_id:  # find it by our railway URL
        hooks = call("GET", "/team-webhooks", token).get("data", [])
        ours = [h for h in hooks if "copybot-production" in (h.get("webhook_url") or "")]
        if not ours:
            print("[webhook-sync] ⚠ no webhook pointing at the copybot URL found — create"
                  " one first (see README) or set alchemy_webhook_id in config.json")
            return 1
        wh_id = ours[0]["id"]
    sync_one(token, wh_id, want, "paper")

    # the live app's own webhook (2026-07-10) — same follow set, separate
    # webhook + signing key so the two books share nothing
    wh_live = cfg.get("alchemy_webhook_id_live")
    if wh_live:
        sync_one(token, wh_live, want, "live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
