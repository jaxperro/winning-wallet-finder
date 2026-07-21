# research/ — the edge factory (SILO)

Standing rules (user directive 2026-07-20: "built in a silo to not affect
anything on the live bot"):

- **Nothing here is imported by, or imports, the bot** (`copybot.py`,
  `copytrade.py`, their configs). The Fly workers run pinned entrypoints;
  this directory is inert to them.
- **Tape access is read-only** (`duckdb.connect(..., read_only=True)`).
- The ONE shared write: `live/cache.duckdb::resolutions` via `live/payouts.py`
  — append-only immutable chain facts, the same store the daily pipeline
  already feeds. Nothing else in `live/` is touched.
- The existing paper bot is the live test's CONTROL — graduated edges get
  their own paper harness here, never that one.
- Studies are pre-registered (GitHub issue per study: hypothesis, params,
  verdict + kill criteria) BEFORE their forward window opens. Exploration
  happens on already-collected tape; **verdicts only come from
  `forward_ledger.jsonl` rows dated after the params freeze commit.**

Layout:
  tape.py     read-only loaders · tape proxy-resolution (terminal-VWAP +
              sibling veto, the 742/742 chain-validated method) · title
              parsers (niche, crypto strike/expiry/sprint)
  sim.py      execution replayer calibrated on OUR live fills ledger
              (lag, FAK no-match, protected band, 3% taker fee)
  study_flow.py    Study A — informed-flow state signal
  study_oracle.py  Study B — crypto oracle fair value vs the book
  requote.py       crater→requote timing measurement (retry tuning)
  forward.py       scores frozen studies on new tape days → forward_ledger.jsonl
  params/          frozen study parameters (committed = frozen)
  nightly.sh       launchd runner (fires 09:15, then WAITS for fresh tape —
                   Stage 0 keeps the tape ~15-min fresh whenever the Mac is
                   awake; deadline 8h, then scores anyway and logs staleness)
