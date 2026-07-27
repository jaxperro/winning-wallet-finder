# wwf-leanbot — one dep, no git, no clone (same discipline as the others).
FROM python:3.12-slim
RUN pip install --no-cache-dir websocket-client
COPY research/leanbot.py /leanbot.py
CMD ["python3", "-u", "/leanbot.py"]
