# wwf-recorder — RTDS tape silo. One pip dep (websocket-client), no git, no
# repo clone: the recorder ships its code in the image (a tape must not
# depend on GitHub being up to boot).
FROM python:3.12-slim
RUN pip install --no-cache-dir websocket-client
COPY recorder/recorder.py /recorder.py
CMD ["python3", "-u", "/recorder.py"]
