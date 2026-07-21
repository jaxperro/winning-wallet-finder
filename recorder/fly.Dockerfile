# wwf-recorder — RTDS tape silo. Two pip deps (websocket-client for capture,
# duckdb for the parquet fold sidecar), no git, no repo clone: the recorder
# ships its code in the image (a tape must not depend on GitHub being up to
# boot).
FROM python:3.12-slim
RUN pip install --no-cache-dir websocket-client duckdb
COPY recorder/recorder.py /recorder.py
COPY recorder/fold.py /fold.py
COPY recorder/start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
