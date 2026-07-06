#!/usr/bin/env python3
"""Daily Discord digest: the copyable-sharp list with 30-day conviction stats.

Replaces the retired trade-by-trade pings (archive/webhook_receiver.py and the
copybot placement alerts, removed 2026-07-04): ONE message per daily run,
listing every wallet in watch_sharps.json with a Polymarket profile link and
its 30-day conviction win %, record, and conviction P&L — the same conv30_*
fields the dashboard renders (the wallet's own position stats, not the copy
book). Links require an embed: Discord ignores markdown links in plain
`content` messages.

Webhook: `daily_webhook` in the gitignored repo-root config.json.

    python3 discord_daily.py "feed pushed"    # optional arg shown in the footer
"""
import json
import os
import ssl
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_ROWS = 30            # embed description caps at 4096 chars; 30 rows fits


def _post(hook, payload):
    req = urllib.request.Request(
        hook, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"})     # Discord 403s the default UA
    urllib.request.urlopen(req, timeout=15, context=ssl._create_unverified_context())


def main():
    try:
        hook = json.load(open(os.path.join(HERE, "..", "config.json"))).get("daily_webhook")
    except Exception:
        hook = None
    if not hook:
        print("[discord] no daily_webhook in ../config.json — skipping")
        return
    # --ping "text": one plain status line (used by daily.sh's start-of-run
    # heads-up so the digest hours later isn't the only sign of life)
    if len(sys.argv) > 2 and sys.argv[1] == "--ping":
        try:
            _post(hook, {"content": sys.argv[2]})
            print("[discord] ping sent")
        except Exception as e:
            print("[discord] ping failed:", e)
        return
    try:
        sharps = json.load(open(os.path.join(HERE, "watch_sharps.json")))
    except Exception as e:
        print(f"[discord] watch_sharps.json unreadable ({e}) — skipping")
        return

    sharps.sort(key=lambda s: s.get("conv30_pnl") or 0, reverse=True)
    lines = []
    for i, s in enumerate(sharps[:MAX_ROWS], 1):
        name = (s.get("name") or s["wallet"][:10])
        name = name.replace("[", "(").replace("]", ")")[:24]   # keep the md link intact
        url = "https://polymarket.com/profile/" + s["wallet"]
        if s.get("conv30_win") is None:
            stats = "30D: no conviction bets"
        else:
            stats = (f"**{s['conv30_win']:.0f}%** · "
                     f"{s.get('conv30_won', 0)}-{s.get('conv30_lost', 0)} · "
                     f"**${s.get('conv30_pnl', 0):+,.0f}**")
        lines.append(f"`{i:>2}` [{name}]({url}) — {stats}")

    embed = {
        "title": f"Daily sharps · {len(sharps)} copyable · {time.strftime('%Y-%m-%d')}",
        "description": "\n".join(lines)[:4000],
        "color": 0x3FD17A,
        "footer": {"text": ((sys.argv[1] + " · ") if len(sys.argv) > 1 else "")
                   + "30D win% · conviction record · conviction P&L (wallet's own)"},
    }
    try:
        _post(hook, {"embeds": [embed]})
        print(f"[discord] daily sharp digest sent ({min(len(sharps), MAX_ROWS)} rows)")
    except Exception as e:
        print("[discord] digest failed:", e)


if __name__ == "__main__":
    main()
