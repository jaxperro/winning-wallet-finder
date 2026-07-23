# HANDOFF — the living state of the world

**The work queue lives in GitHub Issues** (migrated 2026-07-19):
`gh issue list` · labels = silo (`copybot`/`recorder`/`pipeline`/`dashboard`)
× type (`hardening`/`bug`/`research`/`strategy`/`ops`/`user-todo`/
`pre-registered`/`post-mortem`) · milestones = the standing rhythms (audit
backlog, Friday bench reviews, verdict gates). Commits that resolve one say
`closes #N`. The July-2026 narrative/evidence log is FROZEN in
[HANDOFF_ARCHIVE.md](HANDOFF_ARCHIVE.md) — incident post-mortems, decision
rationale, the whole story. This file stays ~a page: boundary, snapshot, ops.

## Issue burndown 2026-07-19 (audit → shipped)
Migrated the queue to GitHub Issues (labels/milestones; `closes #N` in
commits). Closed same day: #3 webhook rotated · #5 listeners start after
seed · #6 dead-code sweep (retired copytrade CLI + LiveExecutor) · #7
chain-balance gate in reconcile_exits + retry (kills the BetBoom zombie
class) · #8 rpc-down heartbeat flag · #9 memory caps (skipped recency-
bounded, settled bets >30d spool to an archive with drift-invariant
compensation — TESTED identical drift) · #10 publish-failure alarm · #11
tape ingest sftp-first (base64 fallback) · #12 reconcile_entries 5m→30m.
REOPENED #4: the redeemer's phantom-cash BOOBY TRAP is fixed + proven
(on-chain sim SUCCESS; live fails honestly), but actually redeeming needs a
Builder API Key (SDK gasless relay) the runtime bot deliberately lacks —
LOW urgency, the platform auto-redeems winners itself. auto_redeem OFF.
Open now: #1 Signal A, #2 tape research, #4 redeem builder-key, #13 Fri
bench, #14 edge verdict, #15 tape-pull batching (Stage-0 fold+mirror
superseded the bulk path; sync now sftp-with-base64-fallback), #16 surge
momentum (**KILL CRITERIA MET 2026-07-22** — resolution-timing scorer bug,
FINDINGS round-3 section; formal close at Friday's read), #17 oracle fair
value (sample ×30 under chain scoring; E0.04 tier killed, E≥0.07 slightly
positive and accumulating), #19 surge sprint plan (Friday decision matrix;
surgebot stays paper through the read). #18 (empty-cond copies
unsettleable) closed same day: RTDS seed enrichment + falsy-cond repair
pass + 1h alarm.

## Operating boundary (user, 2026-07-13 — standing)
**Full autonomy on the bots**; the real-money bot **stays ARMED**. Never
touch the private key, Discord alerts webhook ROTATED 2026-07-19 (#3 closed),
sizing/caps/deposits are the USER's call. USER DIRECTIVE 2026-07-16: the
live bot mirrors the paper test EXACTLY (one deliberate exception:
min_order_usd $1 = venue reality). If something looks genuinely dangerous,
DISARM (`flyctl secrets unset LIVE_CONFIRM -a wwf-copybot-live`) rather than
push through.

## Snapshot (2026-07-21)
- **wwf-copybot-live** (REAL, ARMED): ~$62 equity ($66.42 contributed,
  realized −$12.17 lifetime — day-one incident + honest recognitions),
  6-wallet Set E rev 4, 4% of working equity/bet, alarm-free after the
  07-20 reconcile (CASH≠CHAIN −$1.75: empty-cond Odyssey bet the venue
  auto-redeemed → hand-settled won +$0.68, $0.05 rounding folded; root
  cause FIXED same day, #18: RTDS metadata-less rows now enrich at seed,
  repair_market_meta un-sticks cond-less books each settle pass). **wwf-copybot** (paper $1k): same set, FAK-parity fills.
  Both on the audit-hardened build (locks, chain-gated sweep, boot-id
  single-writer guard, TLS'd user-ws — HANDOFF_ARCHIVE rev 16). 2026-07-20:
  FAK no-match OPENs get one re-quote retry on both bots, PER-NICHE waits
  from measured crater-refill times (crypto 4s / esports 10s / sports+slow
  25s — research/params/requote_timing.json; `fak_retry_niche_s` override,
  `fak_retry_s` fallback+kill-switch; second rejection tags "twice").
- **wwf-recorder**: the FULL firehose (trades + order matches + comments
  + crypto ticks, ~8M events/day, dual-socket ~99.9% capture, 25GB volume).
  **Stage-0 warehouse 2026-07-21**: the box folds its own segments →
  zstd Parquet partitions on the volume (fold.py sidecar, row-parity
  verified, manifest + Mac-ack deletion protocol — invariant STRONGER than
  before); the Mac mirrors + appends into `live/rtds.duckdb` every 15 min
  (`com.jaxperro.tape-sync` → recorder/sync_tape.py). Tape freshness:
  nightly → ~15 min; the box no longer needs the Mac to stay healthy.
  `live/parquet/` = complete durable layer (Stage-1 MotherDuck feedstock).
- **VALUE experiment: CLOSED 2026-07-19** — sub-2¢ hypothesis refuted
  (1W/993L, 0.075x); post-mortem in value/PLAN.md; app destroyed.
- **research/ (SILO — never touches the bots)**: tape-era edge factory.
  Sharp screen (`live/tape_sharps.py`, proxy chain-validated 742/742, 25
  copyable candidates); execution sim fitted on 29 labeled live attempts.
  **2026-07-22 SCORER LAW (FINDINGS round-3): every arm scores through
  payouts_for() — tape proxy + mandatory CTF chain overlay. "Pending" is
  never outcome-neutral on Polymarket (market liveness encodes the
  outcome: losses hide behind the still-trading winning sibling).** Study
  A surge (#16): KILLED by that correction (−$6.03/fill × 1,108 forward
  fills; identity-null stands). Study B oracle (#17): E0.04 killed,
  E≥0.07 accumulating slightly positive. Verdicts ONLY from
  research/forward_ledger.jsonl.
- **wwf-surgebot (PAPER, ~$3/mo)**: THE instrument that caught the scorer
  bug (its 57.5% refused to match the ledger's 81%). v1 cash-gated book
  halted 2026-07-22 at its pre-registered −50% line (state + ledger frozen
  as audit artifacts); relaunched same day as **A2 measurement arm** —
  every trigger paper-FAK'd at $100, attempts/markouts/settles append-only
  on the volume, the $100/5% spec replayed OFFLINE nightly
  (surge_book_replay.py → surge_book.json). Runs through Friday's read;
  its capture/depth/latency streams feed successor hypotheses per #19.
- **wwf-oraclebot (PAPER, ~$3/mo, live 2026-07-22)**: Study B real-time
  harness — fair value tick-by-tick on the venue's own settlement feed,
  all E-tiers tracked, $100 FAK walks, three settle layers ending in
  nightly chain truth (grade_oracle.py → oracle_paper_ledger.jsonl). Early
  live cells lean positive at E≥0.07 — consistent with the corrected tape
  read. Same attempts/markouts/settles streams as A2.
- **Data moat (2026-07-22, DATA LAW in research/README)**: all raw streams
  append-only and Mac-independent (Fly volumes + daily snapshots; recorder
  has ~3+ weeks offline headroom); forward.py backfills ledger-missing
  days after any Mac gap; meta_snap.py snapshots ~12k active markets
  nightly (τ-at-trigger + token→outcome for every future study). Markout
  study (chain-true): NO scalp inside the dead surge signal — hidden-loss
  cohort bleeds from minute one; taker case closed at every horizon.
- **Verdicts pending**: edge/size-up (#14, ~end of July, pre-registered) ·
  Friday's combined read (#13 bench + #16 formal close + #17 oracle-tier
  decision + #19 sprint-plan disposition — every number now chain-true).
- Dashboards: jaxperro.com/{trading,live,test,value} — /test = both paper
  studies on one page (old /surge + /oracle URLs redirect) · daily pipeline
  on the Mac at 08:00 (launchd, lockfile) — floors, bench forward table,
  edge row, tape sync, Discord digest · tape mirror every 15 min
  (com.jaxperro.tape-sync → sync_tape.py, sftp + base64-console fallback)
  · research nightly fires 09:15 then WAITS for fresh tape
  (com.jaxperro.research-nightly; self-commits ledger, informed set,
  surge/oracle grades, virtual book, meta snapshot). All launchd agents
  removable with `launchctl unload ~/Library/LaunchAgents/<label>.plist`.

## Ops quick-reference
- Follow-set change: edit live/copybot.paper.json → `./live/deploy_bot.sh`;
  mirror config.live.example.json (nothing auto-writes it) + backtest.json.
- Tape now: `python3 recorder/sync_tape.py` (launchd does it every 15 min);
  box-side fold health: `flyctl logs -a wwf-recorder` (grep `[fold]`).
- State surgery: stop the machine, **watch heartbeats actually CEASE**
  (gotcha 15c — and the boot-id guard now makes a zombie writer yield),
  pull → edit → push → start → verify the first heartbeat.
- Code deploy: push + `flyctl apps restart <app>` (boots clone main,
  clone-guard verifies). Image changes: `flyctl deploy --remote-only -c
  <toml> --ha=false`. Never two machines per app.
- The bots commit their own state — always `git pull --rebase --autostash`
  before pushing from a session.
- Read next: README.md (architecture + gotchas 1-18) · FINDINGS.md (research
  story) · value/PLAN.md (the refutation) · HANDOFF_ARCHIVE.md (history).
