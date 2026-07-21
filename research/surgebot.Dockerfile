# wwf-surgebot — one dep, no git, no clone (a paper harness must not depend
# on GitHub being up to boot; informed set fetch degrades gracefully).
FROM python:3.12-slim
RUN pip install --no-cache-dir websocket-client
COPY research/surgebot.py /surgebot.py
CMD ["python3", "-u", "/surgebot.py"]
