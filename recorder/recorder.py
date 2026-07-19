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
# FULL capture (2026-07-19, probed): activity/* = trades + orders_matched
# (the maker side of every match, ~1.1k/min), comments (market chatter,
# tiny), crypto_prices (~300/min). rfq + prices probed dead. Trades keep the
# rtds_* segment family (ingest pipeline unchanged); everything else tapes
# raw into a parallel aux_* family.
SUB = json.dumps({"action": "subscribe", "subscriptions": [
    {"topic": "activity", "type": "*", "filters": ""},
    {"topic": "comments", "type": "*", "filters": ""},
    {"topic": "crypto_prices", "type": "*", "filters": ""}]})


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


class Tape:
    """Thread-safe: TWO sockets write concurrently (dual-connection capture,
    2026-07-19 — single-socket tape measured 92.9% minute-coverage; the RTDS
    stream silences per-CONNECTION every ~10 min, so a twin covers the gap).
    Dedupe on (tx, asset, side, size, price) with a 2-min recency window."""

    def __init__(self):
        os.makedirs(DIR, exist_ok=True)
        self.files = {}                # family -> [hour, fh, rows]
        self.msgs = self.aux = self.dupes = self.gaps = 0
        self.last_msg = time.time()
        self._wlock = threading.Lock()
        self._recent = {}              # dedupe key -> ts

    def _rotate(self, fam, hour):
        cur = self.files.get(fam)
        if cur and cur[1]:
            cur[1].close()
            plain = os.path.join(DIR, f"{fam}_{cur[0]}.jsonl")
            with open(plain, "rb") as i, gzip.open(plain + ".gz", "wb") as o:
                shutil.copyfileobj(i, o)
            os.remove(plain)
            log(f"rotated {fam}_{cur[0]} ({cur[2]} rows)")
        self.files[fam] = [hour, open(os.path.join(DIR, f"{fam}_{hour}.jsonl"), "a"), 0]
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

    def _dedup(self, key, now):
        if key in self._recent:
            self.dupes += 1
            return True
        self._recent[key] = now
        if len(self._recent) > 80000:          # ~2 busy minutes; prune old
            cut = now - 120
            self._recent = {k: t for k, t in self._recent.items() if t > cut}
        return False

    def _fh(self, fam):
        hour = time.strftime("%Y%m%d_%H", time.gmtime())
        if fam not in self.files or self.files[fam][0] != hour:
            self._rotate(fam, hour)
        return self.files[fam]

    def write_aux(self, topic, ty, p, raw_len):
        """Everything that isn't a trade: raw payload, schema-free."""
        now = time.time()
        with self._wlock:
            self.last_msg = now
            if self._dedup((topic, ty, hash(json.dumps(p, sort_keys=True))), now):
                return
            cur = self._fh("aux")
            cur[1].write(json.dumps({"ts": round(now, 3), "topic": topic,
                                     "type": ty, "payload": p}) + "\n")
            cur[2] += 1
            self.aux += 1

    def write(self, p):
        key = (p.get("transactionHash"), p.get("asset"), p.get("side"),
               str(p.get("size")), str(p.get("price")))
        now = time.time()
        with self._wlock:
            self.last_msg = now        # either socket delivering = stream alive
            if self._dedup(key, now):
                return
            self._do_write(p, now)

    def _do_write(self, p, now):
        cur = self._fh("rtds")
        self.fh, self.n = cur[1], cur[2]
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
        cur[2] += 1
        self.msgs += 1


def run_conn(tag, tape):
    """One socket. Per-connection freshness clock + 15s stale guard: at the
    observed 3k+ trades/min, 5s of per-conn silence is already pathological,
    and with a TWIN connection a false trip costs nothing (dedupe absorbs the
    overlap). gaps counts once per reconnect, per connection."""
    backoff = 2
    while True:
        state = {"fresh": time.time()}

        def on_open(ws):
            ws.send(SUB)
            state["fresh"] = time.time()
            log(f"rtds[{tag}]: connected")

            def ping():
                while ws.keep_running:
                    time.sleep(5)
                    try:
                        ws.send('{"action":"ping"}')
                    except Exception:
                        break
                    if time.time() - state["fresh"] > 15:
                        log(f"rtds[{tag}]: silent 15s — reconnect (twin covers)")
                        try:
                            ws.close()
                        except Exception:
                            pass
                        break
            threading.Thread(target=ping, daemon=True).start()

        def on_message(ws, raw):
            state["fresh"] = time.time()
            try:
                m = json.loads(raw)
            except Exception:
                return
            topic, ty = m.get("topic"), m.get("type")
            if topic == "activity" and ty == "trades":
                tape.write(m.get("payload") or {})
            elif topic:                      # orders_matched / comments / prices
                tape.write_aux(topic, ty, m.get("payload") or {}, len(raw))

        try:
            app = websocket.WebSocketApp(URL, on_open=on_open, on_message=on_message)
            app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            log(f"rtds[{tag}]: listener error {str(e)[:60]}")
        tape.gaps += 1
        time.sleep(backoff + (1 if tag == "b" else 0))   # desync the twins
        backoff = min(backoff * 2, 30)
        if time.time() - state["fresh"] < 60:
            backoff = 2                # healthy until just now — quick return


def main():
    tape = Tape()
    for tag in ("a", "b"):             # dual-connection capture
        threading.Thread(target=run_conn, args=(tag, tape), daemon=True).start()
    last = lastd = lasta = 0
    while True:
        time.sleep(60)
        tr = tape.files.get("rtds"); ax = tape.files.get("aux")
        log(f"tape: {tape.msgs - last} trades/min · aux {tape.aux - lasta}/min "
            f"· dupes {tape.dupes - lastd}/min · hour {tr[2] if tr else 0}+"
            f"{ax[2] if ax else 0} · reconnects {tape.gaps}")
        last, lastd, lasta = tape.msgs, tape.dupes, tape.aux


if __name__ == "__main__":
    main()
