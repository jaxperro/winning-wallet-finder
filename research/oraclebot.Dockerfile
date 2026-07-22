# wwf-oraclebot — one dep, no git, no clone (a paper harness must not depend
# on GitHub being up to boot; same discipline as surgebot/recorder).
FROM python:3.12-slim
RUN pip install --no-cache-dir websocket-client
COPY research/oraclebot.py /oraclebot.py
CMD ["python3", "-u", "/oraclebot.py"]
