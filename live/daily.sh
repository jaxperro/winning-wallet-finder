#!/bin/bash
# Daily skilled-wallet refresh, cache-backed:
#   1) discover  — enumerate traders of markets resolved in the last ~14 days,
#                  merged into the accumulating candidate pool
#   2) freshen   — force a re-pull of the current watchlists (skilled + sharps,
#                  for forward tracking), then top up the cache (collect re-pulls
#                  only new + >14d-stale wallets, so it's cheap after the first run)
#   3) re-score  — the 5-gate skill funnel, instant from cache -> watch_skilled.json
#   4) sharps    — conviction-profile scan + last-minute timing gate ->
#                  conviction_wallets.json + watch_sharps.json (the set the live
#                  trading dashboard reads via raw.githubusercontent)
#   5) floors    — recompute the copy bot's per-wallet p80 conviction floors from
#                  the (now-fresh) cache into ../config.json, so copybot.py stays
#                  in parity with the dashboard's top-20%-by-stake gate. Local-only
#                  (config.json is gitignored); never touches the curated watchlist.
#   6) dashboard — regenerate + snapshot for auditable forward history
#   7) publish   — commit + push the refreshed outputs so the live dashboard
#                  (jaxperro.com/trading) picks up the new sharp list
#
# Schedule with launchd/cron (Mac must be awake). Logs to daily.log.
set -u
cd "$(dirname "$0")"
# heads-up ping so the run's start is visible in Discord (digest comes at the end)
python3 discord_daily.py --ping "🔄 Daily pipeline started $(date '+%H:%M') — refreshing the bet cache (takes a while); sharp digest lands when it finishes." || true
echo "[daily] $(date '+%F %T') 1/6 discover (enumerate last 14d)"
python3 enumerate.py 14
echo "[daily] $(date '+%F %T') 2/6 freshen cache (watchlists forced + new wallets)"
python3 -c "import json,os,cache
wl=[]
for f in ('watch_skilled.json','watch_sharps.json'):
    if os.path.exists(f):
        wl += [w['wallet'] for w in json.load(open(f))]
if wl:
    cache.invalidate(wl)" 2>/dev/null || true
python3 collect.py
echo "[daily] $(date '+%F %T') 3/6 re-score skilled (cache-backed, instant)"
python3 skill.py
echo "[daily] $(date '+%F %T') 4/7 sharps: conviction scan + last-minute timing gate"
python3 conviction_scan.py
python3 validate_timing.py
echo "[daily] $(date '+%F %T') 5/7 floors: recompute copy-bot p80 conviction floors -> ../config.json"
python3 sync_floors.py || echo "[daily] floor sync skipped (no config/watchlist)"
echo "[daily] $(date '+%F %T') portfolio: cache-based \$1k book -> portfolio.json"
python3 portfolio.py || echo "[daily] portfolio skipped"
echo "[daily] $(date '+%F %T') 6/7 dashboard"
python3 dashboard.py
mkdir -p history && cp watch_skilled.json "history/watch_$(date '+%Y%m%d').json" 2>/dev/null
echo "[daily] $(date '+%F %T') 7/7 publish (commit + push refreshed outputs)"
PUBLISH="no changes"
git add watch_skilled.json watch_sharps.json conviction_wallets.json dashboard.html portfolio.json 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
    echo "[daily] no output changes to publish"
elif git commit -q -m "live: daily refresh — skilled + sharp wallets [skip ci]"; then
    # sync first so a diverged remote (e.g. a manual commit) doesn't wedge the
    # auto-push permanently; abort a conflicting rebase and retry next run.
    git pull --rebase -q origin main 2>/dev/null || git rebase --abort 2>/dev/null || true
    if git push -q origin main; then
        echo "[daily] pushed refreshed sharp list"; PUBLISH="pushed"
    else
        echo "[daily] push failed — committed locally, will retry next run"; PUBLISH="push failed (committed locally)"
    fi
fi
echo "[daily] $(date '+%F %T') done -> watch_sharps.json + dashboard.html"

# Discord: the daily sharp-list digest (profile links + 30D conviction stats).
# The only Discord output in the system — per-trade pings retired 2026-07-04.
# Webhook lives in gitignored ../config.json -> daily_webhook.
python3 discord_daily.py "feed: $PUBLISH" || echo "[daily] discord digest failed (non-fatal)"
