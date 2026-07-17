#!/bin/bash
# Boot for the wwf-valuebot Fly worker (VALUE silo — value/PLAN.md).
# Clones main fresh (public read; GITHUB_TOKEN enables state pushes),
# verifies the clone against ls-remote (stale-replica guard, same lesson as
# the copybot's), then runs the paper bot. No HTTP service on purpose: no
# health-check auto-restarts to race a state surgery (README gotcha 15).
set -e
REPO="github.com/jaxperro/winning-wallet-finder.git"
DIR=/tmp/wwf
for i in 1 2 3 4; do
    rm -rf "$DIR"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        git clone -q --depth 1 "https://x-access-token:${GITHUB_TOKEN}@${REPO}" "$DIR"
    else
        git clone -q --depth 1 "https://${REPO}" "$DIR"
    fi
    HEAD=$(git -C "$DIR" rev-parse HEAD)
    REMOTE=$(git ls-remote "https://${REPO}" HEAD | cut -f1)
    if [ "$HEAD" = "$REMOTE" ]; then
        echo "[clone-guard] clone verified @ ${HEAD:0:10}"
        break
    fi
    echo "[clone-guard] stale clone ($HEAD != $REMOTE) — retry $i"
    sleep 5
done
cd "$DIR"
git config user.email "valuebot@wwf" && git config user.name "wwf-valuebot"
[ -z "${GITHUB_TOKEN:-}" ] && echo "⚠ no GITHUB_TOKEN — feed publishing will fail (paper book not durable across restarts)"
exec python3 -u value/valuebot.py
