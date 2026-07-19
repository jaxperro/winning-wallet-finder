# HANDOFF — the living state of the world

**The work queue lives in GitHub Issues** (migrated 2026-07-19):
`gh issue list` · labels = silo (`copybot`/`recorder`/`pipeline`/`dashboard`)
× type (`hardening`/`bug`/`research`/`strategy`/`ops`/`user-todo`/
`pre-registered`/`post-mortem`) · milestones = the standing rhythms (audit
backlog, Friday bench reviews, verdict gates). Commits that resolve one say
`closes #N`. The July-2026 narrative/evidence log is FROZEN in
[HANDOFF_ARCHIVE.md](HANDOFF_ARCHIVE.md) — incident post-mortems, decision
rationale, the whole story. This file stays ~a page: boundary, snapshot, ops.

## Operating boundary (user, 2026-07-13 — standing)
**Full autonomy on the bots**; the real-money bot **stays ARMED**. Never
touch the private key, never rotate the Discord webhook (user's login — #3),
sizing/caps/deposits are the USER's call. USER DIRECTIVE 2026-07-16: the
live bot mirrors the paper test EXACTLY (one deliberate exception:
min_order_usd $1 = venue reality). If something looks genuinely dangerous,
DISARM (`flyctl secrets unset LIVE_CONFIRM -a wwf-copybot-live`) rather than
push through.

## Snapshot (2026-07-19)
- **wwf-copybot-live** (REAL, ARMED): ~$59 equity ($66.42 contributed,
  realized −$16.94 lifetime — day-one incident + honest recognitions),
  6-wallet Set E rev 4, 4% of working equity/bet, alarm-free after the
  07-19 reconcile. **wwf-copybot** (paper $1k): same set, FAK-parity fills.
  Both on the audit-hardened build (locks, chain-gated sweep, boot-id
  single-writer guard, TLS'd user-ws — HANDOFF_ARCHIVE rev 16).
- **wwf-recorder**: tapes every platform trade → nightly ingest →
  `live/rtds.duckdb` (5M+ rows). The current-era ground truth for research.
- **VALUE experiment: CLOSED 2026-07-19** — sub-2¢ hypothesis refuted
  (1W/993L, 0.075x); post-mortem in value/PLAN.md; app destroyed.
- **Verdicts pending**: edge/size-up (#14, ~end of July, pre-registered) ·
  bench review Friday (#13: five new sharps + AIcAIc's prove-it week).
- Dashboards: jaxperro.com/{trading,live,value} · daily pipeline on the Mac
  at 08:00 (launchd, lockfile) — floors, bench forward table, edge row,
  tape ingest, Discord digest.

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
