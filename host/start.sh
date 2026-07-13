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

# geo-gate probe first (host/geocheck.py, no keys needed). GEOCHECK_ONLY=1 ->
# probe and idle WITHOUT starting the bot: lets a new host/region prove it
# passes Polymarket's IP geoblock while the old deployment is still the book's
# single writer. Otherwise the probe is informational — the PAPER bot only
# reads, so a blocked region only matters once real orders are on the table.
if [ -n "${GEOCHECK_ONLY:-}" ]; then
  exec python3 "$(dirname "$0")/geocheck.py" --idle
fi
python3 "$(dirname "$0")/geocheck.py" \
  || echo "⚠ geo-gate BLOCKED/unknown here — fine for paper, do NOT go live from this box"

# COPYBOT_ROLE=live -> the REAL MONEY worker (separate Fly app
# wwf-copybot-live; NEVER convert the paper worker — the two roles must never
# share a process or a state file). Unarmed = missing any of
# LIVE_PRIVATE_KEY / LIVE_FUNDER_ADDRESS / LIVE_CONFIRM -> idle harmlessly
# (no clone, no book, no orders) so the machine can exist safely before
# Phase 2 funding. Arming is three `flyctl secrets set` calls by the USER;
# copybot.py additionally re-checks the geo-gate fatally and validates the
# phrase itself (LIVE_ROLLOUT 0.7, 1.5).
if [ "${COPYBOT_ROLE:-paper}" = "live" ]; then
  if [ -z "${LIVE_PRIVATE_KEY:-}" ] || [ -z "${LIVE_FUNDER_ADDRESS:-}" ] || [ -z "${LIVE_CONFIRM:-}" ]; then
    echo "live role UNARMED (need LIVE_PRIVATE_KEY + LIVE_FUNDER_ADDRESS + LIVE_CONFIRM) — idling"
    exec python3 "$(dirname "$0")/geocheck.py" --idle
  fi
fi

: "${GITHUB_TOKEN:?set GITHUB_TOKEN (PAT with repo contents read+write)}"
REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/jaxperro/winning-wallet-finder.git"
DIR="${COPYBOT_DIR:-/tmp/wwf}"

rm -rf "$DIR"
git clone --depth 20 "$REPO_URL" "$DIR"
cd "$DIR"

# stale-clone guard (2026-07-10): a boot seconds after a push can clone a
# stale GitHub replica (gotcha 15) — it bit twice today (a 4h run on
# pre-push config). Verify the clone's HEAD against the API's view of main
# and re-clone until they agree (bounded; proceeds with a warning if the
# API is unreachable — never blocks the boot forever).
for try in 1 2 3 4; do
  LOCAL_SHA=$(git rev-parse HEAD)
  # ask over the GIT transport, not the REST API: ls-remote uses the exact
  # same authenticated smart-HTTP path the clone itself just used, so it is
  # reachable by construction (api.github.com was NOT from Fly boxes —
  # Bearer 401s and the anonymous path rate-limits on shared egress IPs;
  # the guard failed open on every boot until 2026-07-13).
  REMOTE_SHA=$(git ls-remote "$REPO_URL" refs/heads/main 2>/dev/null | cut -f1)
  echo "$REMOTE_SHA" | grep -qE '^[0-9a-f]{40}$' || REMOTE_SHA=""
  if [ -z "$REMOTE_SHA" ]; then
    echo "[clone-guard] GitHub API unreachable — proceeding with $LOCAL_SHA"
    break
  fi
  if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    echo "[clone-guard] clone verified @ ${LOCAL_SHA:0:10}"
    break
  fi
  echo "[clone-guard] STALE clone (${LOCAL_SHA:0:10} != ${REMOTE_SHA:0:10}) — re-cloning (try $try)"
  cd /
  rm -rf "$DIR"
  sleep 5
  git clone --depth 20 "$REPO_URL" "$DIR"
  cd "$DIR"
  [ "$try" = "4" ] && echo "[clone-guard] ⚠ still stale after 4 tries — proceeding; verify the first heartbeat"
done

git config user.name  "copybot[bot]"
git config user.email "copybot@users.noreply.github.com"

# live role, ARMED: own config (committed template + env secrets), own state
# file, own feed/fills (config paths). ALCHEMY_SIGNING_KEY set -> PUSH mode
# (the live app's OWN webhook, wired 2026-07-10 — ~3s detection vs the 60s
# poll's ~39s avg; 60s heartbeat + 5min backstop poll are built in). Without
# the key -> classic poll, same as before.
if [ "${COPYBOT_ROLE:-paper}" = "live" ]; then
  if [ -n "${ALCHEMY_SIGNING_KEY:-}" ]; then
    exec python3 copybot.py \
      --config config.live.example.json \
      --state  copybot_state.live.json \
      --live
  fi
  exec python3 copybot.py \
    --config config.live.example.json \
    --state  copybot_state.live.json \
    --live \
    --poll   "${POLL_SECONDS:-60}"
fi

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
