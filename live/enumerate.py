#!/usr/bin/env python3
"""Enumerate candidate wallets from markets resolved in the last N months.

The bulk subgraph is frozen at Jan 2026, so recent data comes from the LIVE
data-api. We can't cheaply page every global trade back 6 months, but we don't
need to: skilled traders concentrate in liquid markets and recur across many of
them. So we source high-volume recently-resolved markets from Gamma, pull each
market's top traders by notional (insider.market_traders), dedup, and tally how
many markets each wallet shows up in (a prioritization signal for scoring).

Output: candidates.json  ->  scored by skill.py.

    python3 enumerate.py            # last 180 days, liquid markets
    python3 enumerate.py 90         # last 90 days
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import insider  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/markets"
CTX = ssl._create_unverified_context()
WINDOW_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 180
MAX_MARKETS = 1200      # enough high-volume recent markets to surface recurring sharps
MAX_SCAN = 25000        # stop scanning the volume ranking after this many markets
TOP_TRADERS = 30        # top traders by notional per market
MIN_VOLUME = 20000      # $ — focus on liquid markets where skilled traders play


def gamma(params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{GAMMA}?{q}", headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=40, context=CTX).read())


def _closed_ts(m):
    """Parse Gamma's closedTime ('2026-05-12 06:44:09+00') -> epoch, else 0."""
    s = m.get("closedTime")
    if not s:
        return 0
    s = s.replace(" ", "T").split("+")[0].split(".")[0]
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return insider._parse_end(m.get("endDateIso"))   # fallback to date


def _page(offset, end_min):
    # end_date_min restricts to recently-resolved markets directly, so we don't
    # scan the whole (mostly-old) volume ranking. Retry once on transient error.
    for _ in range(2):
        try:
            return gamma({"limit": 100, "offset": offset, "closed": "true",
                          "end_date_min": end_min, "order": "volume",
                          "ascending": "false"})
        except Exception:
            time.sleep(2)
    return "ERR"


def recent_markets():
    """Markets that resolved within the window (via end_date_min), kept if liquid
    (volumeNum >= MIN_VOLUME and closedTime in window). Gamma is slow (~5-11s/
    page) so we page CONCURRENTLY in waves and stop at end-of-data or MAX_SCAN."""
    cutoff = time.time() - WINDOW_DAYS * 86400
    end_min = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(cutoff))
    out, scanned, base, WAVE = [], 0, 0, 4   # low concurrency — gamma throttles bursts
    with ThreadPoolExecutor(max_workers=WAVE) as ex:
        while len(out) < MAX_MARKETS and scanned < MAX_SCAN:
            pages = list(ex.map(lambda o: _page(o, end_min),
                                [base + i * 100 for i in range(WAVE)]))
            base += WAVE * 100
            ended = False
            for page in pages:
                if page == "ERR" or page is None:
                    continue                     # skip transient gaps, keep going
                if len(page) < 100:
                    ended = True                 # genuine end of data
                scanned += len(page)
                for m in page:
                    try:
                        vol = float(m.get("volumeNum") or 0)
                    except (TypeError, ValueError):
                        vol = 0
                    if vol >= MIN_VOLUME and _closed_ts(m) >= cutoff and m.get("conditionId"):
                        out.append((m["conditionId"], m.get("question", "?")[:60]))
            print(f"  scanned {scanned:,}… kept {len(out)}", flush=True)
            if ended:
                break
    return out[:MAX_MARKETS]


def main():
    print(f"sourcing markets resolved in last {WINDOW_DAYS}d (>= ${MIN_VOLUME:,} vol)…",
          flush=True)
    mkts = recent_markets()
    print(f"  {len(mkts)} markets", flush=True)

    seen, name = defaultdict(int), {}

    def grab(cm):
        try:
            cands, _ = insider.market_traders(cm[0], top=TOP_TRADERS)
            return cands
        except Exception:
            return []

    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for cands in ex.map(grab, mkts):
            for c in cands:
                seen[c["wallet"]] += 1
                name.setdefault(c["wallet"], c["username"])
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(mkts)} markets · {len(seen):,} wallets", flush=True)

    # merge with any existing candidates so daily re-runs ACCUMULATE the pool
    path = os.path.join(os.path.dirname(__file__), "candidates.json")
    merged = {}
    if os.path.exists(path):
        for c in json.load(open(path)):
            merged[c["wallet"]] = c
    new = 0
    for w, n in seen.items():
        if w in merged:
            merged[w]["markets_seen"] = max(merged[w].get("markets_seen", 0), n)
            merged[w].setdefault("username", name[w])
        else:
            merged[w] = {"wallet": w, "username": name[w], "markets_seen": n}
            new += 1
    rows = sorted(merged.values(), key=lambda c: c["markets_seen"], reverse=True)
    json.dump(rows, open(path, "w"))
    print(f"{len(rows):,} candidate wallets (+{new} new this run) -> candidates.json "
          f"({sum(1 for r in rows if r['markets_seen'] >= 15):,} seen in >=15 markets)",
          flush=True)


if __name__ == "__main__":
    main()
