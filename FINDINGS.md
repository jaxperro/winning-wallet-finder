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

## Insider detection — what the z-score signal actually found

Building `insider.py` and sweeping markets (`hunt.py`, `huntwide.py`) surfaced
genuinely improbable wallets. Out of ~289 scored:

- **DREAMBIG.** (z=8.9, p≈2e-19) and **qcp14** (z=5.3) on the Iran ceasefire
  market — 45–77% of wins entered <24h before resolution. Textbook insider
  fingerprint, on exactly the theme the *60 Minutes* story covered.
- **Famecesgoal** (z=9.6) won only 14.5% of bets — but bet longshots and hit
  +98 above what the odds implied. The clearest "beats the prices it pays" case.

Two refinements proved essential:
- **Trade count separates insiders from bots.** `bjprolo` scored z=37 — but on
  **306,873** lifetime trades. That's a market-maker grinding a tiny systematic
  edge, not information. Real edge wallets show concentrated z over 1–3k trades.
- **Funding-cluster linking** (Alchemy, the Bubblemaps "who-funded-whom" step)
  works *only* with a personal-hub filter: a shared exchange (everyone uses
  Coinbase) is not a shared operator. Judge a funder by its own outbound degree.

## The copy-trade verdict — in-sample vs out-of-sample

The decisive test: does copying z-selected wallets make money?

- **In-sample** (`copyback.py`): copy the edge wallets from May 30, z-weighted,
  reinvest 100%. Result: **+545%** in 15 days. Looks incredible — and it's
  circular (the wallets were *selected* for winning over that very window).
  86% of it came from one wallet; the highest-z pick contributed $23.
- **Out-of-sample** (`oos.py`): select wallets using **only data through
  Apr 30**, then copy forward May 30→now. Result: **+168%** — but **entirely
  from one longshot lottery wallet** (1.5% pre-period win rate hitting again).
  The two strongest pre-period signals made **$0** forward. Forward hit rate was
  27%. That's variance, not edge that persists.

**Conclusion:** even the one real signal (z-score), when tested for whether you
can *profit by copying it*, fails out-of-sample — joining every other strategy.
The detector is valuable for *finding* anomalous wallets; copying them is not a
proven, fundable edge. The live watcher (`webhook_receiver.py`) exists to gather
real forward (out-of-sample) data on these wallets — observe before you size up.

## Practical conclusion 2

- **Don't** fund a copy strategy — both the +545% and +168% are
  variance/concentration, not repeatable edge.
- **Do** use the detector to find statistically anomalous wallets and watch them
  live; judge persistence forward with your own eyes.
- A durable trading edge has to come from *you* (a niche you know), with this
  tooling built around your judgment.

## The skilled-3% scan, and a clean out-of-sample loss (June 2026)

External validation arrived: an LBS/Yale study (Gomez-Cram, Guo, Kung, Jensen,
Apr 2026; SSRN 5910522) over 1.72M accounts found only **~3.14%** of traders are
genuinely skilled — measured by randomizing each trader's bet *directions* 10k×
(a Monte-Carlo z-score) and requiring out-of-sample persistence. That is exactly
this project's z-score + `oos.py` method, independently confirmed.

Built `live/` to operationalize it at scale: enumerate recent liquid markets →
cache every candidate's resolved bets locally (~26k wallets / 12.5M bets, so
re-scoring at any cutoff is seconds) → a 5-gate funnel (n≥15, z>0, BH-FDR,
split-half OOS, MM/bot cap). It surfaced 107 "validated" wallets.

**The decisive test.** Copying the high-win-rate "favorite-rider" cohort, $1000,
no execution lag, June 1→now:
- selected *through* the test window (look-ahead): 99% win rate, **+23.6%**.
- selected on **pre-June-1 data only** (honest): 68% win rate, **−7.4%**
  (−19% on the settled portion).

The +23.6% was selection bias. Done cleanly, the favorites **lose** — a textbook
reproduction of the paper's "~60% of lucky winners become losers out-of-sample,"
now on our own live data. *Lesson reinforced: high win rate is the most
misleading signal on the platform; favorite-riders are uncopyable.* The
underdog/`value` archetype (beats longshot prices) is the only one left worth
testing.

## Repo layout

- `insider.py` — the detector: z-score/p-value, timing/freshness/sizing signals,
  Alchemy funding-cluster ring detection.
- `hunt.py` / `huntwide.py` — market sweeps that surface edge wallets.
- `copyback.py` / `oos.py` — in-sample and out-of-sample copy-trade backtests.
- `webhook_receiver.py` — push-based live trade watcher (Alchemy → Discord).
- `smart_money.py` — data foundation + dashboard (true-win-rate scanner).
- `live/` — current scanner: cache-backed skilled-3% finder + watchlist + daily
  refresh + dashboard. See `live/README.md`.
- `wide/` — bulk subgraph→DuckDB scanner (survivorship-bias-free, all wallets);
  public subgraph frozen at Jan 2026, so historical-only. See `wide/README.md`.
- `archive/` — the strategies that didn't work, kept for reference. See
  `archive/README.md`.
