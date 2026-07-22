#!/bin/bash
# research nightly — scores the frozen studies on fresh tape and versions
# the forward ledger. SILO: touches only research/ (+ the append-only
# resolutions cache via payouts.py). Fired at 09:15 local by
# com.jaxperro.research-nightly, but the clock time is advisory: the Mac
# sleeps and the 08:00 daily pipeline's tape ingest lands whenever it lands
# (2026-07-21: daily started 09:31 on wake, ingest hours later, and the
# 09:15 firing both CRASHED on launchd's bare `python3` — no duckdb — and
# would have scored a stale tape). So: absolute framework python, and WAIT
# for the tape to be fresh (max ts within FRESH_S of now) before scoring,
# up to DEADLINE_S; then run anyway and log the staleness.
# Safe to run by hand any time: bash research/nightly.sh
set -e
cd "$(dirname "$0")"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
FRESH_S=$((6 * 3600))
DEADLINE_S=$((8 * 3600))
POLL_S=900

# ABSOLUTE lock path: the script cd's to the repo root before the git
# steps, so a relative trap rmdir missed the dir entirely (2026-07-21 first
# run: exit 1 + a stale lock that would have skipped every future night).
LOCK="$(pwd)/.nightly.lock.d"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) already running — skip" >> forward.log
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

tape_age() {
  "$PY" - <<'EOF'
import duckdb, time
db = duckdb.connect("../live/rtds.duckdb", read_only=True)
mx, = db.execute("select max(ts) from trades").fetchone()
print(int(time.time() - (mx or 0)))
EOF
}

start=$(date +%s)
while true; do
  age=$(tape_age) || age=999999
  if [ "$age" -le "$FRESH_S" ]; then
    echo "== $(date -u +%FT%TZ) nightly (tape age ${age}s) ==" >> forward.log
    break
  fi
  if [ $(( $(date +%s) - start )) -ge "$DEADLINE_S" ]; then
    echo "== $(date -u +%FT%TZ) nightly DEADLINE — tape still ${age}s stale, scoring anyway ==" >> forward.log
    break
  fi
  sleep "$POLL_S"
done

"$PY" forward.py >> forward.log 2>&1
"$PY" informed_set.py >> forward.log 2>&1     # surge harness reads this daily
"$PY" grade_surge.py >> forward.log 2>&1 || true   # paper book -> chain truth
"$PY" grade_oracle.py >> forward.log 2>&1 || true  # oracle paper -> chain truth

cd ..
git add research/forward_ledger.jsonl research/params/informed_set.json \
        research/surge_paper_ledger.jsonl research/oracle_paper_ledger.jsonl 2>/dev/null
if ! git diff --cached --quiet; then
  git commit -q -m "research: forward ledger $(date -u +%F) [skip ci]"
  git pull --rebase --autostash -q && git push -q
fi
