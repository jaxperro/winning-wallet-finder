# Archive — strategies that didn't work

These tools were built and tested during the research in
[`../FINDINGS.md`](../FINDINGS.md). They all proved to be dead ends (the market
is efficient / the metric was biased), so they're archived here for reference
rather than deleted. Each one *works* as written — it's the *strategy* that
didn't clear. They import `smart_money`/`copytrade` from the repo root, so to
run one you'd adjust the import path.

| File | What it did | Why it's here |
|------|-------------|---------------|
| `copytrade.py` | Paper/live copy-trade engine — mirror a watchlist's entries/exits, % -of-bankroll sizing, price guard, per-position cap, Discord alerts. | Copying entries is −EV; followed wallets win ~50%. Backtested −48%. |
| `backtest.py` | Replay a watchlist over a window, mark outcomes from resolution. | The tool that proved copy-trading loses. |
| `edge_research.py` | Scan ~2000 wallets for reliable weekly consistency (% green weeks, profit factor, Sharpe). | "Consistent" wallets were mostly young accounts (survivorship); no durable edge. |
| `lookback.py` | Deep-dive a wallet list over a long window, split into halves for out-of-sample reads. | Showed the "best" wallets had <90 days of history. |
| `table_77.py` | Aggregate a wallet set to CSV (ROI, total staked, consistency). | Supported the above; ROI inversely related to size. |
| `lp_screener.py` | Rank reward-eligible markets by risk-adjusted LP yield. | The high APRs were illusory — see `lp_paper`. |
| `lp_paper.py` | Paper liquidity-provision loop: simulate quoting, track net = rewards − adverse selection. | Polymarket refunds unearned reward pool; thin-book "jackpots" don't pay. |
| `xarb.py` | Cross-venue scanner: match the same event on Polymarket vs Kalshi, flag price gaps. | Venues priced efficiently (~1¢); both legs cost >$1 after fees. |

The keeper that came out of all this lives at the repo root: `insider.py`.

## Later additions (2026-07-06 cleanup)

| Path | What it was | Why it's here |
|------|-------------|---------------|
| `hunt.py` / `huntwide.py` / `oos.py` / `copyback.py` / `watch.json` | pre-`live/` research sweeps: candidate hunts, out-of-sample replays, copy-backtests | superseded by the `live/` selection layer (skill → conviction → validate_timing) |
| `live-research/` | the June 2026 selection experiments: `strategy.py`, `followability.py`, `pnl_basket/focused.py`, `backtest_june.py`, `clean_test.sh` + their outputs | the research is settled (see FINDINGS); the surviving ideas live on in `live/` |
| `us-venue/` | `us_listable.py` — mapped the replay stream against Polymarket US listings (95/794 matched; US settles off-chain via FCM/clearinghouse, so no wallet tracking) | the US move was scrapped 2026-07-06: no on-chain wallets, no copyable signal |
| `retired-infra/` | Railway build config (`railway.json`, `nixpacks.toml`, `.railwayignore`, `runtime.txt`), the Mac launchd runner (`com.jaxperro.copybot.plist`, `run_copybot.sh`), the GH-Actions cron (`copybot.workflow.yml`) | the worker now runs on Fly.io `arn` (see root README); the Actions cron was throttled to uselessness; the Mac poller was replaced by the cloud worker |
| `local/` | untracked logs/CSVs/state from the above experiments | kept out of git; safe to delete wholesale |

**Still-live code in this directory:** `copytrade.py` is imported by the root
`copybot.py` as its execution engine (sizing, risk gates, executors) — archived
as a *strategy* (raw copy-trading is −EV), kept as a *library*.
