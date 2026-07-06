#!/bin/bash
# 24/7 copybot runner for an always-on host (Railway worker / Fly.io / any $5 VPS).
#
# Why a fresh clone: cloud build images usually ship the source WITHOUT .git, but
# the bot persists its book by committing state + feed + fills back to GitHub
# (copybot.publish_feed). Cloning at boot gives it a real repo, and the last
# committed copybot_state.json — so the $1k book survives restarts and redeploys
# with no volume attached.
#
# Required env:
#   GITHUB_TOKEN     fine-grained PAT with Contents: read+write on
#                    jaxperro/winning-wallet-finder (the push credential)
# Optional env:
#   ALCHEMY_SIGNING_KEY  presence switches the bot to PUSH mode: it serves
#                        POST /alchemy for the Alchemy address-activity
#                        webhook (~2-5s detection) with a 60s housekeeping
#                        heartbeat + 5min backstop poll. Requires the service
#                        to have a public domain. Absent -> classic poll mode.
#   POLL_SECONDS         poll cadence in poll mode (default 60 — keep well
#                        under the bot's 600s stale window)
#
# Railway setup (one time):
#   1. New service -> Deploy from GitHub repo -> jaxperro/winning-wallet-finder
#   2. Settings -> Deploy -> Custom Start Command:  bash host/start.sh
#   3. Variables -> add GITHUB_TOKEN (and DISCORD_WEBHOOK if wanted)
#   4. No public networking needed (poll mode makes outbound calls only)
# Then STOP the Mac poller so the book has one writer:
#   launchctl unload ~/Library/LaunchAgents/com.jaxperro.copybot.plist
set -euo pipefail

: "${GITHUB_TOKEN:?set GITHUB_TOKEN (PAT with repo contents read+write)}"
REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/jaxperro/winning-wallet-finder.git"
DIR="${COPYBOT_DIR:-/tmp/wwf}"

rm -rf "$DIR"
git clone --depth 20 "$REPO_URL" "$DIR"
cd "$DIR"
git config user.name  "copybot[bot]"
git config user.email "copybot@users.noreply.github.com"

# paper config is committed (no secrets); state resumes from the last commit.
# ALCHEMY_SIGNING_KEY set -> push mode (webhook server); else 60s poll mode.
if [ -n "${ALCHEMY_SIGNING_KEY:-}" ]; then
  exec python3 copybot.py \
    --config live/copybot.paper.json \
    --state  copybot_state.json
else
  exec python3 copybot.py \
    --config live/copybot.paper.json \
    --state  copybot_state.json \
    --poll   "${POLL_SECONDS:-60}"
fi
