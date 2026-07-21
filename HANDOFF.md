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
bench, #14 edge verdict, #16 surge momentum (forward window OPEN), #17
oracle fair value (accumulating, nothing frozen). #18 (empty-cond copies
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

## Snapshot (2026-07-19)
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
  + crypto ticks, ~8M events/day, dual-socket ~99.9% capture, 25GB volume,
  NOTHING deleted until the Mac verifiably ingests it) → nightly →
  `live/rtds.duckdb` (trades + aux). Current-era ground truth for research.
- **VALUE experiment: CLOSED 2026-07-19** — sub-2¢ hypothesis refuted
  (1W/993L, 0.075x); post-mortem in value/PLAN.md; app destroyed.
- **research/ (NEW 2026-07-20, SILO — never touches the bots)**: tape-era
  edge factory. Sharp screen (`live/tape_sharps.py`, resolution proxy
  chain-validated 742/742, 25 copyable candidates); execution sim fitted on
  our own 29 labeled attempts (−2¢/fill optimism bias, thresholds sit 2×
  above it); Study A surge momentum #16 (identity NULL result — controls
  match the informed set) and Study B oracle fair value #17 (86% craters,
  winner's-curse inversion). Verdicts ONLY from research/forward_ledger.jsonl.
- **Verdicts pending**: edge/size-up (#14, ~end of July, pre-registered) ·
  bench review Friday (#13: five new sharps + AIcAIc's prove-it week —
  cross-read the tape screen's candidates in live/tape_sharps.json) ·
  #16/#17 forward thresholds (in their issues).
- Dashboards: jaxperro.com/{trading,live,value} · daily pipeline on the Mac
  at 08:00 (launchd, lockfile) — floors, bench forward table, edge row,
  tape ingest, Discord digest · research nightly at 09:15
  (com.jaxperro.research-nightly → research/nightly.sh, self-commits the
  forward ledger; remove with `launchctl unload`).

## Ops quick-reference
- Follow-set change: edit live/copybot.paper.json → `./live/deploy_bot.sh`;
  mirror config.live.example.json (nothing auto-writes it) + backtest.json.
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
