# wwf-valuebot — VALUE silo image. Deliberately separate from the copybot's
# fly.Dockerfile: valuebot is pure stdlib, so this never needs to move when
# the copybot's SDK pins change (and vice versa).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY value/start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/bin/bash", "/start.sh"]
