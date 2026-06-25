#!/bin/bash
# Run the copy bot continuously for the 24/7 paper test.
#
#   * caffeinate -i  keeps the Mac from idle-sleeping while the bot runs (it will
#     still stop if the lid is closed on battery, or the Mac is shut down / offline
#     — for true machine-independent 24/7, deploy the webhook receiver to a cloud
#     host instead; see the notes in copybot.py).
#   * --poll 60  checks the four wallets every 60s (well under the 600s freshness
#     window, so no trade is missed; their leads are ≥3h so this is plenty fast).
#
# Used by the launchd agent (com.jaxperro.copybot.plist) which restarts it on crash.
# Logs to copybot.log.
cd "$(dirname "$0")"
exec caffeinate -i python3 copybot.py --poll 60 >> copybot.log 2>&1
