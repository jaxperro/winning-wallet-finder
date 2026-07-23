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
- **DATA LAW (2026-07-22): tests are additive, never subtractive.** Raw
  streams (tape, attempts, markouts, settles, meta snapshots) are
  append-only; analysis opens them read-only; no script rewrites or
  rotates a raw stream. Bot-state trims exist ONLY where an append-only
  log on the volume carries the same rows durably (settles) or the nightly
  pull archives them (attempts/markouts). A study that would need to
  mutate a stream records a NEW stream instead.

Layout:
  tape.py     read-only loaders · tape proxy-resolution (terminal-VWAP +
              sibling veto — exact per-bet, but resolution TIMING is
              win-biased; see SCORER LAW) · title parsers · chain_overlay()
  sim.py      execution replayer calibrated on OUR live fills ledger
              (lag, FAK no-match, protected band, 3% taker fee)
  study_flow.py    Study A — surge momentum (#16 CLOSED 2026-07-23; A2
                   chain grade −$7.54/fill × 1,344 independently confirms)
  study_oracle.py  Study B — crypto oracle fair value (#17: E0.04 killed;
                   E≥0.07/0.1 ledger-positive but the harness chain grade
                   2026-07-23 reads −$8.51/−$5.90 per fill at real latency
                   — taker arm evidence-dead, tiers accrue to their formal
                   bars, maker pivot is the successor hypothesis)
  copy_edge_slices.py   T5 — parity-era copy edge by niche/lag/band/wallet
                        (esports carries it; feeds #14)
  copy_maker_entry.py   T3 — resting-bid copy entries beat taker FAKs
                        (+$17.45 vs +$12.86/signal; basis of #20)
  sell_mirror_study.py  sharp exits are bankroll ops, not signal
                        (basis of #21 hold-through)
  maker_sharps.py       T2 — 673 improbably-winning MAKERS in
                        orders_matched (86% invisible to taker screens);
                        follow-on: inventory-lean signal
  sibling_sum_scan.py   T4 — print-substrate sum-arb scan (artifact-
                        dominated; needs standing-book data; parked)
  maker_quote_sim.py    T1 — crypto maker quoting at fair−m (stale-quote
                        latency model; re-run pending)
  requote.py       crater→requote timing (feeds the bots' per-niche retry)
  forward.py       scores frozen studies on new tape days → forward_ledger
                   (payouts_for chain overlay mandatory; controls + sub5c
                   exploratory arm — 0-for-38, dead)
  informed_set.py  publishes params/informed_set.json nightly (surgebot input)
  surgebot.py      A2 MEASUREMENT harness (wwf-surgebot → jaxperro.com/test):
                   every cooldown-passed trigger paper-FAK'd at $100 against
                   the live asks; v1's cash-gated $100/5% book (halted at its
                   −50% line 2026-07-22) is replayed offline instead —
                   surge_book_replay.py → surge_book.json. v1 artifacts
                   frozen: surge_paper_ledger.jsonl + /data/surge_state.json.
  oraclebot.py     Study B real-time PAPER harness (wwf-oraclebot →
                   jaxperro.com/test): fair value tick-by-tick on the venue's
                   own settlement feed, all E-tiers tracked, three settle
                   layers (own-feed tick → CLOB flags → nightly chain truth)
  grade_surge.py / grade_oracle.py   nightly chain-truth re-grades →
                   surge_meas_ledger.jsonl / oracle_paper_ledger.jsonl;
                   also pull the raw volume streams to local .pull copies
  markout_flow.py  exploratory markout-exit curve (chain-truth verdict: NO
                   scalp inside the dead surge signal — losers bleed from
                   minute one; v0's res_tok version was round-3-biased)
  meta_snap.py     nightly gzipped snapshot of ALL active markets (~12k/day,
                   research/meta/, local-only) — τ knowable at trigger for
                   every tape trigger; token→outcome maps close the
                   label-gap artifact class
  params/          frozen study parameters (committed = frozen)
  nightly.sh       launchd runner (fires 09:15, then WAITS for fresh tape —
                   Stage 0 keeps the tape ~15-min fresh whenever the Mac is
                   awake; deadline 8h, then scores anyway and logs staleness)

Data streams (the moat — all append-only, per DATA LAW):
  Fly volumes (accrue 24/7, Mac-independent; daily Fly snapshots ×5):
    wwf-recorder /data   full firehose → Parquet folds (25GB, ~3+ weeks of
                         Mac-offline headroom before the 85% disk guard)
    wwf-surgebot /data   surge_attempts.jsonl (every attempt + top-5 asks +
                         top-3 bids + latency) · surge_markouts.jsonl (book
                         re-reads +60/300/1800s per fill) · surge_settles
                         .jsonl (durable settles) · surge2_state.json
    wwf-oraclebot /data  oracle_attempts / oracle_markouts / oracle_settles
                         .jsonl + oracle_state.json (same shapes)
  Mac (nightly pulls + git): ledgers + params committed; raw .pull copies
    and meta/ gzips local. forward.py backfills any tape-covered day the
    ledger has never seen — a Mac gap > RESCORE_DAYS leaves no holes.
