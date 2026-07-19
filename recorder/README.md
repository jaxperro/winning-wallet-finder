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
- **Preservation (user directive 2026-07-19): nothing is deleted until the
  Mac has verifiably downloaded it.** Only `ingest.py` deletes, after a
  committed transaction. The 25GB volume holds ~5 weeks un-pulled; the
  recorder warns from 80% full and only at 95% drops the single oldest
  hour per rotation, loudly (`⚠⚠ TAPE LOSS`) — a full disk would otherwise
  kill the CURRENT tape too.

## The nightly ingest (Mac, daily.sh)

`recorder/ingest.py`: lists closed segments over `flyctl ssh`, pulls each
(base64 — see issue #8 for the sftp upgrade), inserts into
`live/rtds.duckdb` (`trades` + `aux` tables; own DB file so it can never
fight cache.duckdb's writer lock), marks it in `ingested`, then deletes on
the box. Idempotent and resumable; a crash mid-segment re-ingests cleanly
(single transaction per segment).

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
