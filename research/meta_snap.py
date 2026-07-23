#!/usr/bin/env python3
"""Nightly market-metadata snapshot — the tape records prints and ticks but
not what markets ARE. This closes that gap additively: one gzipped JSONL
per UTC day under research/meta/ (LOCAL-only, gitignored — re-fetchable,
so repo stays lean) with every active market's cond, question, slug,
end date, tags, tokens/outcomes and neg-risk flag.

Why (2026-07-22 data-moat pass): end dates make τ knowable AT TRIGGER for
every tape trigger (not just harness fills); tags replace title-heuristic
niches for future studies; token->outcome maps kill the label-gap class of
scorer artifact (FINDINGS round 3 disclosure 2). Append-only by
construction: a day file is written once and never rewritten (rerun same
day = idempotent overwrite of today only)."""
import gzip
import json
import os
import ssl
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "meta")
GAMMA = "https://gamma-api.polymarket.com/markets"
PAGE = 100                        # gamma hard-caps limit at 100
SSL_CTX = ssl._create_unverified_context()
KEEP = ("id", "conditionId", "question", "slug", "endDate", "endDateIso",
        "category", "tags", "clobTokenIds", "outcomes", "negRisk",
        "closed", "active", "volumeNum", "liquidityNum", "startDate")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    day = time.strftime("%Y%m%d", time.gmtime())
    out = os.path.join(OUT_DIR, f"meta_{day}.jsonl.gz")
    # gamma 422s past offset ~2100, so page inside end-date windows instead
    now = time.time()
    D = 86400
    edges = [now - 30 * D, now, now + 2 * D, now + 7 * D, now + 30 * D,
             now + 120 * D, now + 730 * D, now + 3650 * D]
    seen, rows = set(), []
    for dmin, dmax in zip(edges, edges[1:]):
        offset = 0
        while True:
            try:
                page = fetch(f"{GAMMA}?active=true&closed=false&limit={PAGE}"
                             f"&offset={offset}&end_date_min={iso(dmin)}"
                             f"&end_date_max={iso(dmax)}")
            except Exception as e:
                print(f"[meta_snap] window {iso(dmin)[:10]}: fetch failed at "
                      f"offset {offset}: {e} — moving on")
                break
            if not page:
                break
            for m in page:
                if m.get("id") in seen:
                    continue
                seen.add(m.get("id"))
                rows.append({k: m.get(k) for k in KEEP})
            offset += len(page)
            if len(page) < PAGE or offset >= 2000:   # stay under the 422 wall
                break
    if not rows:
        print("[meta_snap] nothing fetched — keeping yesterday's file")
        return 0
    fetched_at = int(time.time())
    tmp = out + ".tmp"
    with gzip.open(tmp, "wt") as fh:
        for r in rows:
            r["fetched_at"] = fetched_at
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, out)
    print(f"[meta_snap] {len(rows)} active markets -> {os.path.basename(out)} "
          f"({os.path.getsize(out) // 1024}KB)")
    return 0


if __name__ == "__main__":
    main()
