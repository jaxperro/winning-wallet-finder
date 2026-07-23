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
momentum **CLOSED 2026-07-23** (kill executed as pre-registered — both
instruments chain-true; A2 runs through Friday's #19 read), #17 oracle fair
value (E0.04 killed; higher tiers ledger-positive but harness-vetoed —
taker arm evidence-dead, decision Friday), #19 surge sprint plan (Friday:
disposition), #20/#21 execution flips **FLIPPED LIVE 2026-07-23 18:36Z**
(windows open, bars at n≥30 each), #22 Study C lean-follow
(pre-registered, frozen 052eda0; window opens with the first nightly
lean rows), #23 Study D sibling lead-lag (pre-registered;
**wwf-lagbot DEPLOYED 2026-07-23 20:56Z**, shakedown numbers on the
issue). #18 (empty-cond copies unsettleable) closed same day: RTDS seed
enrichment + falsy-cond repair pass + 1h alarm.

## Operating boundary (user, 2026-07-13 — standing)
**Full autonomy on the bots**; the real-money bot **stays ARMED**. Never
touch the private key, Discord alerts webhook ROTATED 2026-07-19 (#3 closed),
sizing/caps/deposits are the USER's call. USER DIRECTIVE 2026-07-16: the
live bot mirrors the paper test EXACTLY (one deliberate exception:
min_order_usd $1 = venue reality). If something looks genuinely dangerous,
DISARM (`flyctl secrets unset LIVE_CONFIRM -a wwf-copybot-live`) rather than
push through.

## Snapshot (2026-07-23)
- **wwf-copybot-live** (REAL, ARMED): ~$62 equity ($66.42 contributed,
  realized −$12.17 lifetime — day-one incident + honest recognitions),
  **7-wallet Set E rev 5** (JuiceFarm promoted 2026-07-23 off the #13
  bench method run early: +17%/+34% both windows, insider z 2.5, 1%
  refunds; auto p80 floor $753), 4% of working equity/bet.
  **#20/#21 FLIPPED LIVE 2026-07-23 18:36 UTC at 0108cca** — both books
  now run `entry_mode maker` (GTC at the sharp's price, 60s registry TTL,
  no chase) + `exit_mode hold` (mirrored exits ignored + ledgered to
  copybot_ignored_exits[.live].jsonl as the live counterfactual). Forward
  windows open at that commit; verdict bars per #20/#21 at n>=30; revert =
  one config flip back. **wwf-copybot** (paper $1k): same set, same modes.
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
  (1W/993L, 0.075x); post-mortem in archive/value/PLAN.md; app destroyed.
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
  nightly chain truth (grade_oracle.py → oracle_paper_ledger.jsonl).
  **First chain grade (2026-07-23): E≥0.07 −$8.51/fill (n=235), E≥0.10
  −$5.90 (n=127), ~40% hit — real execution loses at every tier while the
  sim ledger reads positive. Both instruments share the scorer now, so the
  divergence is the sim's 6.7s-lag FILL model flattering the tiers (round
  3's lesson recurring in the fill model). Taker arm is evidence-dead;
  formal tier bars keep accruing; maker pivot (T1 sim) KILLED at Stage 1
  same night — staleness IS the adverse selection.**
- **wwf-lagbot (PAPER, ~$3/mo, deployed 2026-07-23 20:56Z)**: Study D #23
  — T9's sibling lead-lag edge (+$9.73/$100 tape, n=2,028, stale-print
  optimism stated) at real books. Leader bursts ≥10¢/120s → paper-FAK
  $100 on ≤2 lagging same-outcome siblings at ≤ stale+4¢; down-moves buy
  the sibling's COMPLEMENT token; cooldown 600s/event. Attempts log every
  standing ask premium — the observational kill-switch (median ≥+8¢ over
  3 days = mirage) can kill without a paper sample. Shakedown 07-23: first
  8 attempts' median +8.5¢ (AT the bar — mirages concentrate in
  handicap/spread siblings with ancient prints); the 3 fills were +1-2¢,
  one $0.55-of-$100 partial → grade_lag reports $/episode AND %-of-staked.
  PASS ≥+$4/$100 @ n≥400/≥5d · KILL ≤0 @ n≥300. Nightly grade_lag.py →
  lag_paper_ledger.jsonl.
- **Data moat (2026-07-22, DATA LAW in research/README)**: all raw streams
  append-only and Mac-independent (Fly volumes + daily snapshots; recorder
  has ~3+ weeks offline headroom); forward.py backfills ledger-missing
  days after any Mac gap; meta_snap.py snapshots ~12k active markets
  nightly (τ-at-trigger + token→outcome for every future study). Markout
  study (chain-true): NO scalp inside the dead surge signal — hidden-loss
  cohort bleeds from minute one; taker case closed at every horizon.
- **Verdicts pending**: edge/size-up (#14, ~end of July, pre-registered) ·
  Friday's combined read (#13 bench + #16 formal close [A2 chain grade
  −$7.54/fill × 1,344 independently confirms the kill; virtual book's
  +26% on n=46 is the variance footnote, not a signal] + #17 taker-arm
  decision + #19 disposition + early #20/#21 window check — every number
  chain-true) · **#22 lean-follow** (PASS ≥+$2/lean & hit≥.56 @ n≥1,500;
  KILL ≤0 @ n≥1,000) · **#23 lead-lag** (bars above). Ten tandem tests
  2026-07-23 (scripts + verdicts in research/): batch one — T3 maker
  entries +$17.45 vs +$12.86 taker → #20; sells anti-signal → #21;
  673-wallet maker-sharp species → inventory-lean line; sibling-sum =
  print artifact; T5 esports concentration → #14 tension. Batch two —
  T6 lean-follow → #22 (fade arm FAILED its concentration gate,
  report-only); T9 lead-lag → #23; T1 maker-quote Stage-1 KILL; T7
  settlement-discount industrialized, parked; T8 crater-rejects were
  good misses; T10 age gradient needs sample. Batch three (same night)
  — T11: #20's 60s TTL already optimal, patience past 60s = adverse
  selection; T12: maker-sharp unwinds NOT exit signals (STAY
  +$5.95/unwind n=265; Study C keeps no exit rule; #22 comment); T14:
  balanced-book imbalance band (0.25-0.47) = portable fill-guard
  candidate for #23; T15: sharp-screen churn 49%/1d, ~10%/28d
  (EV-by-age grade pending its chain-ensure).
- **Backtest = paper-execution parity (2026-07-23, 004911b)**:
  live/portfolio.py replays the PAPER book's follow flags (maker
  feeless entries — declared optimistic bound; hold-through exits;
  sold-unresolved carry as open at the exit print). The backtest is NO
  LONGER a taker control — it tracks whatever copybot.paper.json runs.
  First maker/hold run: equity +36%, 0 fees, 86 resolved (vs 390 under
  mirror) — hold-mode LOCKS capital, misses swell to 1,588; that is
  #21's cost side, visible. /trading gained a "Profitable bench"
  section (realized + would-be-missed > 0): 12/15 clear it; JuiceFarm
  trimmed (+$33 realized, −$72 would-be). JuiceFarm's conviction floor
  pins at the next 08:00 sync_floors run (whole-book copies until
  then — flagged 2026-07-23 evening).
- Dashboards: jaxperro.com/{trading,live,test,value} — /test = all four
  studies on one page (old /surge + /oracle URLs redirect) · daily pipeline
  on the Mac at 08:00 (launchd, lockfile) — floors, bench forward table,
  edge row, tape sync, Discord digest · tape mirror every 15 min
  (com.jaxperro.tape-sync → sync_tape.py, sftp + base64-console fallback)
  · research nightly fires 09:15 then WAITS for fresh tape
  (com.jaxperro.research-nightly; self-commits ledger + lean rows, informed
  set, surge/oracle/lag grades, virtual book, meta snapshot). All launchd agents
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
  story) · archive/value/PLAN.md (the refutation) · HANDOFF_ARCHIVE.md (history).
