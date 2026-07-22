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
- **SCORER LAW (2026-07-22, FINDINGS "round three"): every scorer resolves
  outcomes through `forward.payouts_for()` — tape proxy first, CTF chain
  overlay for the remainder. A "pending" bucket is NEVER ignorable on
  Polymarket: market liveness encodes the outcome (a losing bet's winning
  sibling keeps trading, so losses hide in pending while wins score).
  This bug inflated Study A from a true −$6/fill to +$46/fill.**
- **Every study gets one instrument that does not share the scorer's
  assumptions** — the surge paper harness (chain-graded from day one) is
  what caught the bug. Divergence between instruments is signal, not noise.

Layout:
  tape.py     read-only loaders · tape proxy-resolution (terminal-VWAP +
              sibling veto — exact per-bet, but resolution TIMING is
              win-biased; see SCORER LAW) · title parsers · chain_overlay()
  sim.py      execution replayer calibrated on OUR live fills ledger
              (lag, FAK no-match, protected band, 3% taker fee)
  study_flow.py    Study A — surge momentum (KILLED 2026-07-22, #16)
  study_oracle.py  Study B — crypto oracle fair value (#17: E0.04 killed,
                   E≥0.07 accumulating)
  requote.py       crater→requote timing (feeds the bots' per-niche retry)
  forward.py       scores frozen studies on new tape days → forward_ledger
                   (payouts_for chain overlay mandatory; controls + sub5c
                   exploratory arm — 0-for-38, dead)
  informed_set.py  publishes params/informed_set.json nightly (surgebot input)
  surgebot.py      real-time PAPER harness (wwf-surgebot, /feed endpoint →
                   jaxperro.com/surge) — $100 book, $5 stakes, event cap 2
  grade_surge.py   nightly chain-truth re-grade of the surge paper book
                   → surge_paper_ledger.jsonl
  params/          frozen study parameters (committed = frozen)
  nightly.sh       launchd runner (fires 09:15, then WAITS for fresh tape —
                   Stage 0 keeps the tape ~15-min fresh whenever the Mac is
                   awake; deadline 8h, then scores anyway and logs staleness)
