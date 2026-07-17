# VALUE — the undervalued-market paper bot

A **silo'd** experiment: can systematically buying sub-2¢ Polymarket contracts
beat their price? Research says yes on paper (~1.24x gross); this bot exists
to find out if the fills are real. It shares **no code path, state, app, or
wallet** with the copy trader — see *Silo rules* before touching anything.

## Why this exists (30-second version)

A calibration study over 13.5M trusted resolved bets (2026-07-17, details in
[PLAN.md](PLAN.md)) found Polymarket well-calibrated above 60¢, longshots
2–60¢ systematically OVERpriced — and the tail **below 2¢ UNDERpriced**:
entries at an average 0.82¢ resolved winners 1.02% of the time (1.24x, ≈+20%/$
net of fees, z≈+12 market-clustered). The effect survives excluding skilled
wallets and refunds, and concentrates near expiry. History can't prove the
asks are actually fillable — that's this bot's one job.

## How it works (valuebot.py, single file, pure stdlib)

Every 5 minutes:
1. **Scan** gamma for active markets, soonest-ending first (the edge lives
   near expiry; gamma 422s past offset ~2000, which conveniently caps the
   scan to exactly that window). Any outcome marked ≤ 2¢ is a candidate.
2. **Fill or honest miss**: fetch the REAL CLOB ask ladder; a $1 FAK either
   fully fills inside `min(2¢, best_ask × 1.05)` — the same protected band
   the live copy executor sends — or logs a MISS. No phantom fills, ever
   (the 2026-07-16 paper-parity lesson, inherited from birth).
3. **Guards**: flat $1/ticket (venue minimum = reality), event cap 1
   (correlated dust resolves together), 300-ticket portfolio cap, 6h
   re-check cooldown per token, entry taker fee by category.
4. **Settle at chain truth**: payout vectors from the CTF contract
   (`payoutNumerators`) — wins pay 1.0, **0.5 refunds are real**, losses 0.
   Never trusts CLOB winner flags or price marks.
5. **Publish**: state + feed + fills committed back to this repo
   (`valuebot: paper feed [skip ci]`), same pull-rebase-push discipline as
   the copy books.

## The verdict (what decides paper → real money)

Break-even hit rate ≈ 1.05% at 1¢ entries. Needs ~2,000 resolved tickets
(2–6 weeks). Pre-registered kill criteria (PLAN.md): fill rate <30%, or
realized multiple <1.1x after 2k tickets, or can't deploy >$50/week. Track:
`stats` + `realized_multiple` + `fill_rate` in the feed. Early losses are
EXPECTED — at ~1% hit rate the first hundreds of tickets are mostly Ls; the
multiple is meaningless until refunds/wins accumulate.

## File map

| file | what |
|---|---|
| `valuebot.py` | the whole bot — scan, fill model, settle, publish |
| `PLAN.md` | research findings, strategy spec, rollout gates |
| `valuebot_state.json` | the book (bot-committed; don't hand-edit while it runs) |
| `valuebot.json` | published feed (future jaxperro.com/value reads this) |
| `valuebot_fills.jsonl` | append-only fill ledger |
| `start.sh` | Fly boot: clone main, ls-remote clone-guard, exec bot |
| `fly.Dockerfile` | python:3.12-slim + git; NO pip deps by design |
| `../fly.value.toml` | app `wwf-valuebot` (arn); **no http_service** on purpose — nothing auto-restarts it under a state edit (README gotcha 15) |
| `../tests/test_valuebot.py` | 6 stub tests — fill band, thin-miss, event cap, cooldown, settle math |

## Ops

```bash
python3 tests/test_valuebot.py            # no-network stub tests
python3 value/valuebot.py --once          # one real scan cycle, no publishing
flyctl logs -a wwf-valuebot --no-tail     # watch cycles ("book: cash …")
flyctl deploy --remote-only -c fly.value.toml --ha=false -a wwf-valuebot
                                          # image change (start.sh/Dockerfile);
                                          # code changes need only a machine restart
```

Secrets on `wwf-valuebot`: `ALCHEMY_RPC_URL` (chain settles),
`GITHUB_TOKEN` (fine-grained PAT, contents-RW this repo — state durability +
feed). Without the token the bot trades but rewinds to the last committed
state on restart (it warns at boot).

## Silo rules (do not break these)

1. Nothing under `value/` may import `copybot.py` or `copytrade.py` — the
   ~60 lines of book/fee/payout helpers are duplicated here ON PURPOSE.
   Read-only research libs (`trust`, `smart_money`, `payouts`) are allowed.
2. Never share: state files, feeds, Fly apps, webhooks, or (when real money
   ever comes) the wallet. The copy trader must be un-affectable from here.
3. `bankroll`/stake/cap changes and the paper→real gate are the USER's call.
