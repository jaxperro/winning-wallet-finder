# Session handoff — 2026-07-08 (rev 2: Phase 1 COMPLETE)

Self-contained pickup for a fresh session. Read [README.md](README.md) gotchas
1–11, [FINDINGS.md](FINDINGS.md), and [LIVE_ROLLOUT.md](LIVE_ROLLOUT.md) first.
Project memory (`polymarket-resolution-truth`, `polymarket-us-expansion`) has
deeper history; the repo is authoritative.

## Where things stand (all green)

- **Live paper bot**: Fly.io app `wwf-copybot`, region `arn` (Stockholm),
  push mode. Following **Set E** (2026-07-08 PM): LSB1, imwalkinghere,
  Kruto2027, 0xbadaf319, gkmgkldfmg, AIcAIc, 1kto1m — 7 volume wallets chosen
  by the ALIGNED honest replay (Set E $24.4k/+2338% vs Set D $17.4k/+1636%
  30d; every member positive in the shared book; 42021/BikesAreTheBikes
  dropped as mid-pack, oliman2/leegunner excluded by the anti-survivorship
  audit — oliman2's true lifetime is ~$19k, not PM's $112k; leegunner is
  elite lifetime but replays NEGATIVE for a copier, 7.6-day holds). Caveat:
  newcomers gkmgkldfmg z=2.05 / 1kto1m z=2.4 sit near the selection gate
  floor — watch their forward month. `flyctl logs --app wwf-copybot`.
- **P&L is honest end-to-end** (sharps All-Time = realized track record +
  abandoned-loser fold-in; Open P&L = in-flight exposure — see rev 1 in git
  history for the full derivation).
- **Ledger reconciles** (`ledger_drift` 0.00, self-checked every heartbeat AND
  after every trade — see below).
- **Daily pipeline** (launchd 08:00) runs the corrected code; exits cache
  incremental (86/96 wallets `complete`).

## DONE 2026-07-08 (this session) — the proper fix + Phase 1 closeout

**Orphan/cash-debit seam CLOSED (Option A, commit 167a0b9).** The mechanism
that created the $+36.35 drift orphan is fixed, not just guarded:

- `on_wallet_activity` now records EVERY drained buy fill: the tok-match keeps
  `_record_lag` (real lag/slippage); leftovers get `_record_untracked_buy`
  (bet record synthesized from the fill, conds from the position, audit line
  with `"untracked":true`). `reconcile_exits`' drain does the same instead of
  discarding returned buys.
- `check_book()` asserts the invariant **after every trade and at boot**:
  every `my_pos` token ⇒ bet record + conds entry + cash debited. Records and
  conds self-correct in place. The cash leg heals ONLY at boot
  (`check_book(heal_cash=True)`, called after `bot.seed()`) and ONLY when
  `ledger_drift` matches ONE un-vouched position's cost+fee within
  max($0.10, 1%) — vouched = fills-ledger BUY line for the token, or a real
  `_record_lag` record (`their_price` set). A drift that matches nothing stays
  a loud `⚠ LEDGER DRIFT`, never papered over.
- Supporting plumbing: `my_pos` entries carry `cond` (engine stamps it on
  OPEN/ADD); fills-ledger BUY lines carry `token`; `_synth_bet` (shared with
  `write_feed`) estimates the fee with the same `taker_fee` formula
  `_drain_fills` charges, so a synthesized record over correctly-debited cash
  closes to drift 0 — and a never-debited orphan shows drift = cost+fee
  exactly, which is what the heal keys on.
- Tested: 6-case harness (untracked booking, synth+conds, matching heal,
  non-matching refusal, vouched exclusion, multi-orphan refusal) all pass;
  dry-run against the real state was a clean no-op (drift $0.0055, nothing
  touched). Deployed; heartbeats clean.

**Phase 1 is 8/8 DONE** (LIVE_ROLLOUT.md): 1.1 feed separation, 1.2 env
secrets, 1.3 live deps in the worker image (py-clob-client==0.34.6 +
web3==7.16.0 in fly.Dockerfile, import-verified via `flyctl ssh console`),
1.4 on-chain cash anchor, 1.5 fatal geocheck, 1.6 exit retries, 1.7 config
sync, 1.8 dashboard REAL MONEY section (jaxperro `trading/index.html` commit
871e56e — fourth board chip + section reading `live/copybot_live_real.json`,
shared `copybotSection()` renderer, ledger drift/adjustments surfaced on both
books, verified against a mode:"live" feed copy and the 404 NOT STARTED
empty-state; `?rmFeed=`/`?botFeed=` query params override feeds for testing).

## Next: Phase 2 — funding (USER ONLY)

Everything before real money is done. Per LIVE_ROLLOUT.md: fund the live
wallet (test size, rule 0.6 caps stay at $5/trade · $25/day · $30 exposure),
then Phase 3 preflight (`preflight_live.py`), Phase 4 supervised first fill,
Phase 5 edge-case matrix. The typed-phrase interlock (rule 0.7) is the human
checkpoint — never automated.

## Standing to-dos (user-only or external)

- **ROTATE THE LEAKED ALCHEMY KEY.** A real key briefly hit the public repo
  (`config.live.example.json`, removed from HEAD but in git history). Generate a
  new key at dashboard.alchemy.com, then update `alchemy_key` in the local
  gitignored `config.json` AND the `ALCHEMY_RPC_URL` Fly secret
  (`flyctl secrets set ALCHEMY_RPC_URL=...`). Blocks nothing but is a live key.
- Delete the stopped Railway project `magnificent-kindness` when comfortable
  (saves the idle spend; the Fly worker fully replaced it).

## Operational quick-reference

- Change the follow set: edit `live/copybot.paper.json` `wallets`, then
  `./live/deploy_bot.sh` (validates → pins floors → syncs Alchemy webhook →
  restarts Fly → confirms banner). `backtest.json` should mirror it.
- Backtest any set: `python3 live/portfolio.py --wallets 0xa,0xb --days 30
  --out /tmp/t.json`.
- Never keep 2 Fly machines (2 book-writers): `flyctl scale count 1`.
- Cache is single-writer; don't run cache-touching scripts while the daily
  pipeline or a regen is running.
- Bot commits its own state/feed every few minutes — always
  `git pull --rebase --autostash` before pushing.
- Image changes (fly.Dockerfile) need `flyctl deploy --remote-only`; code-only
  changes just need a push + `flyctl apps restart` (start.sh clones main).
