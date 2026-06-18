#!/usr/bin/env python3
"""Goldsky Polymarket orderbook subgraph client.

The hosted subgraph times out on any `orderBy` over a non-indexed field
(numTrades, scaledProfit, ...), so we never sort server-side. Instead we
cursor-paginate by `id` (the indexed primary key) — `where:{id_gt:<last>}`,
`first:1000` — which is stable and resumable. Ranking happens locally in
DuckDB after ingest. That constraint is exactly why the bulk-ingest design
is the only one that scales here.
"""

import json
import ssl
import time
import urllib.request

ENDPOINT = ("https://api.goldsky.com/api/public/"
            "project_cl6mb8i9h0003e201j6li0diw/subgraphs/"
            "polymarket-orderbook-resync/prod/gn")

_CTX = ssl._create_unverified_context()
PAGE = 1000


def query(gql, variables=None, retries=6):
    """POST a GraphQL query, retrying on transient errors / timeouts."""
    body = json.dumps({"query": gql, "variables": variables or {}}).encode()
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                ENDPOINT, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            r = json.loads(urllib.request.urlopen(req, timeout=60, context=_CTX).read())
            if "errors" in r:
                # statement-timeout is transient under load; back off and retry
                msg = r["errors"][0].get("message", "")
                if "timeout" in msg.lower() or "timed out" in msg.lower():
                    raise TimeoutError(msg)
                raise RuntimeError(msg[:300])
            return r["data"]
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return None


def paginate(entity, fields, where="", page=PAGE, start_id="", on_page=None):
    """Yield every row of `entity`, cursor-paginating by id.

    `fields` is the GraphQL selection set (a string). `where` is extra filter
    clauses (without braces), e.g. 'resolutionTimestamp_not: null'. Resume by
    passing the last id seen as `start_id`.
    """
    last = start_id
    while True:
        clauses = f'id_gt: "{last}"'
        if where:
            clauses += ", " + where
        gql = (f'{{ {entity}(first: {page}, orderBy: id, orderDirection: asc, '
               f'where: {{ {clauses} }}) {{ {fields} }} }}')
        rows = query(gql).get(entity, [])
        if not rows:
            return
        for row in rows:
            yield row
        last = rows[-1]["id"]
        if on_page:
            on_page(len(rows), last)
        if len(rows) < page:
            return


def shard_bounds(n=16):
    """Hex-prefix boundaries after '0x' that split the id space into n shards.
    ids are lowercase hex; '0xg' sorts after every '0xf…' so it caps the last
    shard. Returns n+1 bounds; shard i spans [bounds[i], bounds[i+1])."""
    d = "0123456789abcdef"
    if n == 16:
        return ["0x" + c for c in d] + ["0xg"]
    if n == 256:
        return ["0x" + a + b for a in d for b in d] + ["0xg"]
    raise ValueError("n must be 16 or 256")


def paginate_pages(entity, fields, lo="", hi="", start_id="", page=PAGE):
    """Yield (rows, last_id) per page for ids in (max(start_id,lo), hi).
    Page-level so the caller can checkpoint each shard's cursor."""
    last = start_id or lo
    while True:
        clauses = f'id_gt: "{last}"'
        if hi:
            clauses += f', id_lt: "{hi}"'
        gql = (f'{{ {entity}(first: {page}, orderBy: id, orderDirection: asc, '
               f'where: {{ {clauses} }}) {{ {fields} }} }}')
        rows = query(gql).get(entity, [])
        if not rows:
            return
        yield rows, rows[-1]["id"]
        if len(rows) < page:
            return
        last = rows[-1]["id"]


if __name__ == "__main__":
    # smoke test: count a few resolved conditions
    n = 0
    for c in paginate("conditions", "id resolutionTimestamp payoutNumerators",
                      where="resolutionTimestamp_not: null"):
        n += 1
        if n <= 2:
            print(c)
        if n >= 2500:
            break
    print(f"paged {n} resolved conditions ok")
