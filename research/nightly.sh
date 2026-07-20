#!/bin/bash
# research nightly — scores the frozen studies on fresh tape and versions
# the forward ledger. SILO: touches only research/ (+ the append-only
# resolutions cache via payouts.py). Runs at 09:15 local via
# com.jaxperro.research-nightly (after the 08:00 daily pipeline's ingest);
# safe to run by hand any time: python3 research/forward.py
set -e
cd "$(dirname "$0")"

if ! mkdir .nightly.lock.d 2>/dev/null; then
  echo "$(date -u +%FT%TZ) already running — skip" >> forward.log
  exit 0
fi
trap 'rmdir .nightly.lock.d' EXIT

echo "== $(date -u +%FT%TZ) nightly ==" >> forward.log
python3 forward.py >> forward.log 2>&1

cd ..
git add research/forward_ledger.jsonl
if ! git diff --cached --quiet; then
  git commit -q -m "research: forward ledger $(date -u +%F) [skip ci]"
  git pull --rebase --autostash -q && git push -q
fi
