# Fly.io image for the 24/7 copybot worker (host/start.sh clones the repo
# fresh at boot and persists state via GitHub commits — see host/start.sh).
# Named fly.Dockerfile so Railway's pinned NIXPACKS builder ignores it.
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# LIVE_ROLLOUT 1.3 — live-trading deps (LedgerLiveExecutor + redeem.py).
# The paper path never imports these (import-lazy), so paper boots unchanged.
# polymarket-client = the unified SDK (py-clob-client was ARCHIVED May 2026;
# the CLOB rejects its order format — reads still work, so it stays for the
# legacy read paths). Pinned to the beta the wrap+probe were validated on.
RUN pip install --no-cache-dir py-clob-client==0.34.6 web3==7.16.0 \
    polymarket-client==0.1.0b16
WORKDIR /app
COPY host/ /app/host/
CMD ["bash", "host/start.sh"]
