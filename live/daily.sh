#!/bin/bash
# Daily skilled-wallet refresh, cache-backed:
#   1) discover  — enumerate traders of markets resolved in the last ~14 days,
#                  merged into the accumulating candidate pool
#   2) freshen   — force a re-pull of the current watchlist (forward tracking),
#                  then top up the cache (collect re-pulls only new + >14d-stale
#                  wallets, so it's cheap after the initial collection)
#   3) re-score  — the 5-gate funnel, instant from cache -> watch_skilled.json
#   4) dashboard — regenerate + snapshot for auditable forward history
#
# Schedule with launchd/cron (Mac must be awake). Logs to daily.log.
set -u
cd "$(dirname "$0")"
echo "[daily] $(date '+%F %T') 1/4 discover (enumerate last 14d)"
python3 enumerate.py 14
echo "[daily] $(date '+%F %T') 2/4 freshen cache (watchlist forced + new wallets)"
python3 -c "import json,os,cache
if os.path.exists('watch_skilled.json'):
    cache.invalidate([w['wallet'] for w in json.load(open('watch_skilled.json'))])" 2>/dev/null || true
python3 collect.py
echo "[daily] $(date '+%F %T') 3/4 re-score (cache-backed, instant)"
python3 skill.py
echo "[daily] $(date '+%F %T') 4/4 dashboard"
python3 dashboard.py
mkdir -p history && cp watch_skilled.json "history/watch_$(date '+%Y%m%d').json" 2>/dev/null
echo "[daily] $(date '+%F %T') done -> watch_skilled.json + dashboard.html"
