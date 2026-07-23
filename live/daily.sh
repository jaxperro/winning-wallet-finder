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
#   5) floors    — pin the live paper bot's per-wallet p80 conviction floors
#                  (trusted-cache p80, same as the backtest) into
#                  copybot.paper.json, so the bot and the backtest gate on the
#                  SAME threshold and boots are deterministic (no per-boot
#                  data-api drift). Committed in the publish step.
#   6) dashboard — regenerate + snapshot for auditable forward history
#   7) publish   — commit + push the refreshed outputs so the live dashboard
#                  (jaxperro.com/trading) picks up the new sharp list
#
# Schedule with launchd/cron (Mac must be awake). Logs to daily.log.
set -u
# Run from a private copy so editing this file mid-run can't shift bash's read
# offset (the 2026-07-06 crash: a commit during step 2 made bash resume mid-word
# and die on a phantom syntax error). DAILY_SRC keeps paths anchored here.
if [ -z "${DAILY_COPY:-}" ]; then
    DAILY_SRC="$(cd "$(dirname "$0")" && pwd)"
    DAILY_COPY="$(mktemp "${TMPDIR:-/tmp}/daily.sh.XXXXXX")"
    cp "$0" "$DAILY_COPY"
    export DAILY_SRC DAILY_COPY
    exec /bin/bash "$DAILY_COPY" "$@"
fi
LOCKDIR="${TMPDIR:-/tmp}/wwf-daily.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "[daily] another run holds the lock ($LOCKDIR) — exiting"; exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null; rm -f "$DAILY_COPY"' EXIT
cd "$DAILY_SRC"
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
echo "[daily] $(date '+%F %T') 5/7 floors: pin copy-bot p80 conviction floors -> copybot.paper.json"
python3 sync_floors.py || echo "[daily] floor sync skipped"
# parity guard (2026-07-13 audit): class_pct lives in BOTH copybot.paper.json
# and backtest.json with no generator — warn if they silently desync so the
# paper book and backtest don't drift apart on sizing.
python3 - <<'PY' || true
import json
p=json.load(open("copybot.paper.json")).get("follow",{}).get("class_pct")
b=json.load(open("backtest.json")).get("class_pct")
if p!=b: print(f"[daily] ⚠ class_pct DESYNC: paper {p} != backtest {b}")
PY
echo "[daily] $(date '+%F %T') portfolio: cache-based \$1k book -> portfolio.json"
python3 portfolio.py || echo "[daily] portfolio skipped"
echo "[daily] $(date '+%F %T') portfolio: followed-set-only book -> portfolio_follow.json"
python3 portfolio.py --follow-only --out portfolio_follow.json || echo "[daily] follow portfolio skipped"
echo "[daily] $(date '+%F %T') calibration: live-book vs model, one row/day"
# The number that sizes real money (FINDINGS "The calibration experiment"):
# the measured ratio between the live paper book and the backtest of the SAME
# set. Live side from the bot-committed feed on GitHub (freshest truth; the
# local checkout can lag), model side from the portfolio.json just written.
python3 - <<'CALIB' || echo "[daily] calibration row skipped"
import csv, json, os, ssl, time, urllib.request
ctx = ssl._create_unverified_context()
live = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/jaxperro/winning-wallet-finder/main/live/copybot_live.json",
    timeout=30, context=ctx))
model = json.load(open("portfolio.json"))
row = {
    "date": time.strftime("%F"),
    "live_equity": round((live.get("cash") or 0) + (live.get("deployed") or 0)
                         + (live.get("reserve") or 0), 2),
    "live_realized": live.get("realized"), "live_open": live.get("open_count"),
    "live_drift": live.get("ledger_drift"),
    "model_equity": model.get("equity"), "model_realized": model.get("realized"),
    "model_bank": model.get("bank"), "wallets": ",".join(live.get("wallets") or []),
}
os.makedirs("history", exist_ok=True)
path = "history/calibration.csv"
new = not os.path.exists(path)
with open(path, "a", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(row))
    if new:
        w.writeheader()
    w.writerow(row)
print(f"[daily] calibration: live ${row['live_equity']:,} vs model "
      f"${row['model_equity']:,} -> {path}")
CALIB
echo "[daily] $(date '+%F %T') edge: parity-era per-signal edge vs fee hurdle -> history/edge.csv"
# The bankroll-decision number (HANDOFF rev 13 / 2026-07-16): one row/day;
# the verdict line rides the Discord digest footer below.
python3 edge.py || echo "[daily] edge skipped"
echo "[daily] $(date '+%F %T') tape: sync parquet mirror -> live/rtds.duckdb"
# Stage-0 warehouse (2026-07-21): the box folds segments to parquet itself;
# we mirror + append (recorder/sync_tape.py, also every 15 min via
# com.jaxperro.tape-sync). Old bulk ingest.py stays as the fallback for a
# fold-less recorder (it no-ops when fold has already eaten the segments).
python3 -u ../recorder/sync_tape.py || python3 ../recorder/ingest.py \
    || echo "[daily] tape sync skipped"
echo "[daily] $(date '+%F %T') 6/7 dashboard"
python3 dashboard.py
mkdir -p history && cp watch_skilled.json "history/watch_$(date '+%Y%m%d').json" 2>/dev/null
echo "[daily] $(date '+%F %T') 7/7 publish (commit + push refreshed outputs)"
PUBLISH="no changes"
git add watch_skilled.json watch_sharps.json conviction_wallets.json dashboard.html portfolio.json portfolio_follow.json copybot.paper.json 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
    echo "[daily] no output changes to publish"
elif git commit -q -m "live: daily refresh — skilled + sharp wallets [skip ci]"; then
    # sync first so a diverged remote (e.g. a manual commit) doesn't wedge the
    # auto-push permanently; abort a conflicting rebase and retry next run.
    git pull --rebase --autostash -q origin main 2>/dev/null || git rebase --abort 2>/dev/null || true
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
EDGE_LINE="$(head -1 edge_verdict.txt 2>/dev/null || true)"
python3 discord_daily.py "feed: $PUBLISH${EDGE_LINE:+ · $EDGE_LINE}" || echo "[daily] discord digest failed (non-fatal)"
