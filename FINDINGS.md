# Findings — what works and what doesn't on Polymarket

A research log of an honest attempt to find a systematic, automatable edge on
Polymarket using public data. The short version: **almost nothing works** — the
market is efficient — and the one thing that does isn't a money-printer, it's a
detection signal.

## The goal

Find a repeatable way to make money on Polymarket: identify "smart money"
wallets, copy them, farm rewards, or arbitrage — anything systematic and
automatable from public data.

## Scorecard

| Approach | Verdict | Why |
|----------|---------|-----|
| **Copy high-win-rate wallets** | ❌ dead | Win rate was an illusion (see below). True rates ~50%. Flat-size copying backtested **−48%** over a week. |
| **Rank by leaderboard / PnL** | ❌ dead | Raw PnL is variance; top wallets win ~50% and profit via sizing/timing you can't copy. |
| **LP reward farming** | ❌ dead | The fat "thin-book" APRs are illusory — Polymarket **refunds unearned pool to the sponsor** when liquidity is low. Real yield is modest and adverse-selection-dominated. |
| **Binary YES+NO arbitrage** | ❌ dead | Efficient — min observed sum was 1.001 (the spread). Closed instantly by the engine. |
| **Multi-outcome logical arb** | ❌ dead | True partitions priced efficiently (min sum 0.999). Apparent "arbs" were non-exclusive market groupings. |
| **Cross-venue arb (Polymarket↔Kalshi)** | ❌ dead | Venues agree to ~1¢; locking both legs costs **>$1 after fees**. Real gaps last ~seconds and are taken by bots. |
| **Insider / sharp detection** | ✅ **works** | Statistical improbability (z-score of wins vs. odds) is a real, hard-to-fake edge signal. See `insider.py`. |

## The big technical findings

**1. Win rate on Polymarket is survivorship-biased — badly.**
The platform only redeems *winning* shares; losing shares are worth $0 and sit
unredeemed in `/positions` at `curPrice 0` forever, never entering
`/closed-positions`. Measuring win rate over `/closed-positions` alone counts
almost only winners. We saw a wallet read **90.6%** that was truly **48.3%**.
A correct win rate must union both endpoints. *Lesson: a high reported win rate
is a red flag for a measurement bug, not a sharp.*

**2. Win rate ≠ profit, and PnL ≠ reliability.**
A wallet winning 54% made millions; the all-time #1 wallet (43% win) was −$3.8M
over 90 days. Profit comes from sizing and entry prices, not hit rate.

**3. The market is efficient.** Six systematic public-data edges, all closed or
illusory. There is no turnkey retail edge sitting in public data in 2026 —
durable edge requires *speed/infrastructure* (arb bots), *private information*,
or *getting paid to provide liquidity* (modest, adverse-selection-dominated).

**4. The one real signal: statistical improbability (z-score).**
Each bet entered at price `p` has an odds-implied win probability `p`. A wallet
winning far more than `Σp` is beating the market's own pricing — measured as a
z-score and one-sided p-value. This is the rigorous version of the edge metric
the whole project was chasing. It distinguishes:
- **Sharps** — high z, normal entry timing (skill over many bets).
- **Insiders** — high z **+** late (pre-resolution) entry **+** fresh wallet.

Plus **funding-cluster linking** (à la Bubblemaps / the 2026 *60 Minutes*
investigation): trace each wallet's USDC funders on Polygon and link wallets
that share a *personal* funding hub — judged by the funder's own outbound degree
so shared exchanges don't false-link everyone. (See `insider.py`.)

## Practical conclusion

- **Don't** fund copy-trading, LP farming, or arb based on this work — we tested
  them and they don't clear.
- **Do** use `insider.py`'s z-score as a rigorous "who actually has edge" filter,
  far better than leaderboard or win rate.
- A genuine money-making edge has to come from *you* — a niche you understand
  better than the market — with tooling built around it, not from a public-data
  scanner.
- **Legal note:** *detecting* suspected insider trading is fine; *trading on*
  material nonpublic information is illegal, and blindly following a suspected
  insider is not a safe strategy.

## Repo layout

- `insider.py` — the keeper: z-score/p-value detection, timing/freshness/sizing
  signals, and Alchemy funding-cluster ring detection.
- `smart_money.py` — data foundation + dashboard (true-win-rate scanner).
- `archive/` — the strategies that didn't work, kept for reference. See
  `archive/README.md`.
