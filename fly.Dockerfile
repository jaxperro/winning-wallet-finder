# Fly.io image for the 24/7 copybot worker (host/start.sh clones the repo
# fresh at boot and persists state via GitHub commits — see host/start.sh).
# Named fly.Dockerfile so Railway's pinned NIXPACKS builder ignores it.
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY host/ /app/host/
CMD ["bash", "host/start.sh"]
