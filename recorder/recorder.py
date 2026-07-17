#!/usr/bin/env python3
"""RTDS tape recorder — every Polymarket trade, hour-rotated gzip segments.

SILO (2026-07-17, user directive): own Fly app (wwf-recorder) + volume, no
imports from any bot, no shared state — a recorder crash can never touch
trading and a bot deploy can never gap the tape. Socket handling copies the
battle-tested RtdsListener patterns (unfiltered subscribe — server-side
filters silently return nothing; app-level ping; 120s silent-stale force
reconnect; capped backoff). One JSON line per trade:
  {ts, wallet, asset, cond, side, price, size, tx, title}
Current hour writes plain to /data/segments/rtds_YYYYMMDD_HH.jsonl; rotation
gzips it. A disk guard deletes oldest segments past 85% volume use. The Mac's
nightly ingest (recorder/ingest.py via daily.sh) pulls closed segments into
live/rtds.duckdb and deletes them here after verified insert."""
import gzip
import json
import os
import shutil
import ssl
import threading
import time

import websocket

URL = "wss://ws-live-data.polymarket.com"
DIR = os.environ.get("TAPE_DIR", "/data/segments")
SUB = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "activity", "type": "trades", "filters": ""}]})


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


class Tape:
    def __init__(self):
        os.makedirs(DIR, exist_ok=True)
        self.hour, self.fh, self.n = None, None, 0
        self.msgs = self.gaps = 0
        self.last_msg = time.time()

    def _rotate(self, hour):
        if self.fh:
            self.fh.close()
            plain = os.path.join(DIR, f"rtds_{self.hour}.jsonl")
            with open(plain, "rb") as i, gzip.open(plain + ".gz", "wb") as o:
                shutil.copyfileobj(i, o)
            os.remove(plain)
            log(f"rotated {self.hour} ({self.n} rows)")
        self.hour, self.n = hour, 0
        self.fh = open(os.path.join(DIR, f"rtds_{hour}.jsonl"), "a")
        # disk guard: drop oldest closed segments past 85% usage
        try:
            st = os.statvfs(DIR)
            while st.f_bavail / st.f_blocks < 0.15:
                old = sorted(f for f in os.listdir(DIR) if f.endswith(".gz"))
                if not old:
                    break
                os.remove(os.path.join(DIR, old[0]))
                log(f"disk guard dropped {old[0]}")
                st = os.statvfs(DIR)
        except Exception:
            pass

    def write(self, p):
        hour = time.strftime("%Y%m%d_%H", time.gmtime())
        if hour != self.hour:
            self._rotate(hour)
        ts = p.get("timestamp") or 0
        if ts > 1e12:
            ts /= 1000.0
        self.fh.write(json.dumps({
            "ts": round(ts or time.time(), 3),
            "wallet": (p.get("proxyWallet") or "").lower(),
            "asset": p.get("asset"), "cond": p.get("conditionId"),
            "side": p.get("side"), "price": p.get("price"),
            "size": p.get("size"), "tx": p.get("transactionHash"),
            "title": str(p.get("title") or "")[:60]}) + "\n")
        self.n += 1
        self.msgs += 1
        self.last_msg = time.time()


def main():
    tape = Tape()

    def hb():
        last = 0
        while True:
            time.sleep(60)
            log(f"tape: {tape.msgs - last} msg/min · hour rows {tape.n} · gaps {tape.gaps}")
            last = tape.msgs
    threading.Thread(target=hb, daemon=True).start()

    backoff = 2
    while True:
        def on_open(ws):
            ws.send(SUB)
            log("rtds: connected — recording unfiltered trades")

            def ping():
                while ws.keep_running:
                    time.sleep(5)
                    try:
                        ws.send('{"action":"ping"}')
                    except Exception:
                        break
                    if time.time() - tape.last_msg > 120:
                        log("rtds: silent 120s — forcing reconnect")
                        tape.gaps += 1
                        try:
                            ws.close()
                        except Exception:
                            pass
                        break
            threading.Thread(target=ping, daemon=True).start()

        def on_message(ws, raw):
            try:
                m = json.loads(raw)
            except Exception:
                return
            if m.get("topic") == "activity" and m.get("type") == "trades":
                tape.write(m.get("payload") or {})

        try:
            app = websocket.WebSocketApp(URL, on_open=on_open, on_message=on_message)
            app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            backoff = 2
        except Exception as e:
            log(f"listener error {str(e)[:70]}")
        tape.gaps += 1
        log(f"stream down — reconnect in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
