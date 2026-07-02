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
#   DISCORD_WEBHOOK  ping on every placement
#   POLL_SECONDS     poll cadence (default 60 — keep well under the bot's
#                    600s stale window so no trade is skipped)
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

# paper config is committed (no secrets); state resumes from the last commit
exec python3 copybot.py \
  --config live/copybot.paper.json \
  --state  copybot_state.json \
  --poll   "${POLL_SECONDS:-60}"
