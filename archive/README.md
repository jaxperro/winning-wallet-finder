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
