#!/bin/bash
# Massive ingest of the wallet data we still need, from the FAST subgraph
# (accounts + market_positions). The CLOB token map is already sufficient
# (1.27M tokens covering every resolved condition), so it's NOT re-run here —
# run `python3 clob_tokens.py` separately if you want to refresh it.
#
# Both steps are parallel (16 id-shards) and resumable (per-shard cursors), so
# killing and re-running continues from the last checkpoint.
#
#   nohup ./run_full.sh > run_full.log 2>&1 < /dev/null & disown
#   tail -f run_full.log
set -u
cd "$(dirname "$0")"

until python3 -c "import duckdb;duckdb.connect('pmkt.duckdb',read_only=True).close()" 2>/dev/null; do
  sleep 5
done

echo "[run] $(date '+%F %T') 1/2 accounts (parallel, resumable) …"
python3 ingest.py -p accounts

echo "[run] $(date '+%F %T') 2/2 market_positions (parallel, resumable, the big one) …"
python3 ingest.py -p market_positions

echo "[run] $(date '+%F %T') DONE"
python3 - <<'PY'
import duckdb
c = duckdb.connect("pmkt.duckdb", read_only=True)
for t in ("conditions", "market_data", "accounts", "market_positions"):
    print(f"  {t:18} {c.execute('select count(*) from '+t).fetchone()[0]:,}")
PY
