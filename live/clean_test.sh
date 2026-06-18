#!/bin/bash
# Clean out-of-sample test:
#   1) re-select skilled wallets using ONLY bets resolved before June 1
#      (selection cannot peek at the June 1+ test window)
#   2) copy the resulting FAVORITE-rider wallets' June 1+ entries, $1000, no lag
# This removes the selection contamination of the first backtest.
set -u
cd "$(dirname "$0")"
echo "[clean] $(date '+%F %T') re-scoring candidates on PRE-June-1 data only…"
SKILL_BEFORE=2026-06-01 SKILL_OUT=watch_prejune.json python3 skill.py 2000
echo "[clean] $(date '+%F %T') backtest: copy pre-June-1 favorites' June1+ entries"
BT_WATCH=watch_prejune.json python3 backtest_june.py favorite
echo "[clean] $(date '+%F %T') done"
