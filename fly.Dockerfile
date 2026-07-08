# Fly.io image for the 24/7 copybot worker (host/start.sh clones the repo
# fresh at boot and persists state via GitHub commits — see host/start.sh).
# Named fly.Dockerfile so Railway's pinned NIXPACKS builder ignores it.
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# LIVE_ROLLOUT 1.3 — live-trading deps (LedgerLiveExecutor + redeem.py).
# Pinned to the versions preflight_live.py was validated against on the Mac.
# The paper path never imports these (import-lazy), so paper boots unchanged.
RUN pip install --no-cache-dir py-clob-client==0.34.6 web3==7.16.0
WORKDIR /app
COPY host/ /app/host/
CMD ["bash", "host/start.sh"]
