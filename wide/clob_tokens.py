#!/usr/bin/env python3
"""Build token -> (condition, outcome_index, winner) from the CLOB /markets feed.

Gamma offset-paginates and 422s past ~10k markets; the subgraph's outcomeIndex
is null. CLOB /markets cursor-paginates the full market set (no offset cap),
1000 per page, and each token carries `winner` directly — so win/loss comes
straight from here instead of parsing payoutNumerators.

    python3 clob_tokens.py        # page all markets, fill market_data
"""

import json
import ssl
import time
import urllib.request

import duckdb

DB = "pmkt.duckdb"
CLOB = "https://clob.polymarket.com/markets"
_CTX = ssl._create_unverified_context()
END = "LTE="          # CLOB's end-of-pagination cursor (base64 of -1)


def get(cursor, retries=6):
    url = CLOB + (f"?next_cursor={cursor}" if cursor else "")
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=40, context=_CTX).read())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay); delay = min(delay * 2, 20)


def main():
    con = duckdb.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS market_data (
        token_id TEXT PRIMARY KEY, condition_id TEXT,
        outcome_index INT, winner BOOLEAN);""")
    con.execute("CREATE TABLE IF NOT EXISTS _cursor (table_name TEXT PRIMARY KEY, last_id TEXT);")
    row = con.execute("SELECT last_id FROM _cursor WHERE table_name='market_data#clob'").fetchone()
    cursor = row[0] if row else ""        # resume from saved CLOB cursor
    total, pages, t0 = 0, 0, time.time()
    if cursor:
        print(f"resuming token map from cursor {cursor}", flush=True)
    while cursor != END:
        d = get(cursor)
        rows = []
        for m in d.get("data", []):
            cond = m.get("condition_id")
            toks = m.get("tokens") or []
            if not cond:
                continue
            for idx, t in enumerate(toks):             # tokens ordered by outcome
                tid = t.get("token_id")
                if tid:
                    rows.append((str(tid), cond, idx, bool(t.get("winner"))))
        pages += 1
        nxt = d.get("next_cursor")
        if rows:
            con.execute("BEGIN TRANSACTION")
            con.executemany("INSERT OR IGNORE INTO market_data VALUES (?,?,?,?)", rows)
            # checkpoint the NEXT cursor so a resume continues past this page
            con.execute("INSERT OR REPLACE INTO _cursor VALUES ('market_data#clob', ?)",
                        [nxt or cursor])
            con.execute("COMMIT")
            total += len(rows)
        if not nxt or nxt == cursor:                    # safety: no progress
            break
        cursor = nxt
        if pages % 25 == 0:
            print(f"  {pages} pages · {total:,} tokens ({total/max(1e-9,time.time()-t0):,.0f}/s)", flush=True)
    cnt = con.execute("SELECT count(*) FROM market_data").fetchone()[0]
    won = con.execute("SELECT count(*) FROM market_data WHERE winner").fetchone()[0]
    print(f"done — {cnt:,} token rows ({won:,} winning) over {pages} pages", flush=True)
    con.close()


if __name__ == "__main__":
    main()
