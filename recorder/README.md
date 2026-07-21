# RECORDER — the full RTDS firehose tape

Third silo: records **everything Polymarket's real-time socket emits** —
every trade, every order match (the maker side of each fill), every market
comment, every crypto price tick — into a durable, downloadable archive.
~8M events/day, ~$5-6/month all-in. No keys, no repo clone (code baked into
the image), no shared anything with the trading bots: a recorder crash can
never touch trading, a bot deploy can never gap the tape.

## Capture design (why it's ~complete)

- **Dual sockets** to `wss://ws-live-data.polymarket.com`, both subscribed
  to `activity/*` + `comments/*` + `crypto_prices/*` (`rfq`/`prices` probed
  dead 2026-07-19). The stream silences per-CONNECTION every ~10 min; a
  single socket measured **92.9%** minute-coverage — the twin covers each
  gap. Cross-socket dedupe on (tx, asset, side, size, price) for trades,
  payload-hash for aux; a 15s per-connection stale guard (at 3k+ msg/min,
  5s of silence is pathological; false trips cost nothing with a twin).
- **Segments**: hour-rotated to the volume — `rtds_YYYYMMDD_HH.jsonl.gz`
  (trades, stable schema) + `aux_…` (everything else, raw payloads).
- **Preservation (user directive 2026-07-19, STRENGTHENED by Stage 0
  2026-07-21): nothing is deleted without a verified second copy.** The
  fold sidecar deletes a raw gz only after its Parquet re-reads with a
  matching row count; the disk guard deletes Parquet only oldest-first and
  only files the Mac mirror has ACKED. The recorder's own last-resort
  guard (95% -> drop oldest, `⚠⚠ TAPE LOSS`) still protects the live tape
  from a full disk.

## Stage-0 warehouse (2026-07-21): fold on the box, mirror on the Mac

- **`fold.py`** (sidecar in the same machine, capture is PID 1): every
  2 min, each closed gz segment becomes
  `/data/parquet/<family>/date=YYYY-MM-DD/<segment>.parquet` (zstd),
  row-parity verified, appended to `/data/parquet/manifest.jsonl`, then the
  gz is deleted. The volume IS the warehouse: immutable Parquet any client
  mirrors incrementally. duckdb capped at 384MB (1GB VM) so a busy-hour
  fold can never starve capture. ~250-400MB/day of parquet -> months of
  headroom, months more once mirrored+acked files get pruned.
- **`sync_tape.py`** (Mac, every 15 min via `com.jaxperro.tape-sync` +
  from daily.sh): pulls new manifest entries over `flyctl sftp`,
  row-verifies each file, appends its rows into `live/rtds.duckdb`'s
  native tables (research keeps native speed — views-over-parquet was
  rejected: per-asset point queries would crawl), records it in
  `ingested`, ACKs the box (`/data/parquet/acks/<f>.ok`). Tape freshness
  went from "nightly, if the Mac was awake" to ~15 min whenever awake, and
  the box no longer depends on the Mac to stay healthy for weeks.
- **`bootstrap_parquet.py`**: one-shot export of the pre-fold history
  (2026-07-17..21, existed only in rtds.duckdb) into the mirror — ran
  2026-07-21, parity OK (13.84M trades + 3.07M aux). `live/parquet/` is
  the complete durable layer Stage 1 (MotherDuck/ClickHouse) would consume.

## The legacy bulk ingest (fallback)

`recorder/ingest.py` (sftp-first, base64 fallback, integrity guards) stays
as the fallback path for a fold-less recorder; with fold running it finds
no gz segments and no-ops. Same idempotence keys (`ingested` by segment
name) as sync_tape, so the two can never double-insert.

## Ops

```bash
flyctl logs -a wwf-recorder --no-tail          # "tape: N trades/min · aux …"
python3 recorder/ingest.py                     # manual pull any time
flyctl deploy --remote-only -c fly.recorder.toml -a wwf-recorder --ha=false
                                               # ANY code change = image rebuild
flyctl ssh console -a wwf-recorder -C "df -h /data"   # volume headroom
```

Storage: rtds.duckdb grows ~0.5-0.8GB/day on the Mac (~20GB/mo; 1.4TB free
as of 2026-07-19 — revisit around the ~100GB mark: Parquet-spool old months).
