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

## Train/test wallet selection, and the capital wall (June 2026)

Built `live/strategy.py` (train on bets resolved before May 30, validate June 1+)
and `live/followability.py` (entry-time + lead-time + cadence filter). Selecting
on **copy-ROI + z + monthly consistency + diversification** (not win rate) gave
150 wallets; **59/100 stayed profitable forward** (p=0.044), and filtering to
*followable* markets lifted it to **49/77 (p=0.011), +23.4% pooled** out-of-
sample. So a real, persistent, copyable edge **does** exist — unlike favorites.

Then the reality check (`live/pnl_basket.py`, `live/pnl_focused.py`): a $1,000
copier with **missed-trade accounting** (capital tied in open positions).
- **Broad 10-wallet basket:** the wallets fire **1,210 markets** in June; $1,000
  can follow only ~2–13% of them. At realistic stakes it **loses** (−$384 to
  −$800); the gains sit in the trades you couldn't afford ($14k–$153k "missed").
  **Capital, not edge, is the binding constraint.**
- **Focused + conviction:** copy only 1–2 top wallets and only their larger-stake
  (≥$200) bets → trade count drops to ~30–40, $1,000 affords them all, and it
  **clears: +91% to +247% across stakes, stable, no blowup.**

*Lesson: a small-bankroll copier cannot follow a skilled wallet's whole feed —
the edge is only capturable by concentrating on few wallets' high-conviction
bets. The live tracker (jaxperro.com/trading) now runs exactly that config.*

## The repeatable profile: conviction bets + a timing gate (the best result)

Refining the above: instead of all bets, score wallets on their HIGH-CONVICTION
bets only — the top 20% of each wallet's own stake sizes (per-wallet p80). This
replaced the original flat >= $200 cutoff (2026-06-22): p80 reproduces flat-$200's
win-rate lift across the sharps while adapting to each wallet's scale — a whale's
$200 bet isn't conviction, a minnow's is. The top wallets win **70-80% of their
big bets on genuinely-uncertain (~0.4-0.6 priced) markets** — real edge, not
favorite-riding — and it persists. `live/conviction_scan.py` (train pre-June,
validate June) under p80 finds **218 wallets** matching the profile; forward,
**62/83 stayed profitable (p≈0), +16.0% pooled**. A reproducible class, not a fluke.
(The original flat-$200 run found 69 wallets, 25/37 forward, +11.7%.)

Then the decisive copyability filter, `live/validate_timing.py`: a near-100% win
rate is only useful if we can mirror it. The tell is **entry->resolution lead
time on winning conviction bets** — this is a copyability heuristic, NOT proof of
inside information (a short lead can be a genuine insider or just someone good at
fast-resolving markets; we can't tell, and for copy purposes it doesn't matter).
Of the 218, the gate drops the **"last-minute" wallets** (median lead <24h — you
can't get the trade in before resolution), then a 30-day-active filter, leaving
**~31 validated copyable sharps** (`watch_sharps.json`) with multi-day leads. The standout `0x60ec1744…` held 80%
win over **1,017 forward conviction bets**; even the suspiciously-perfect `0x72e1…`
(99/100% win) enters ~7 days early — a real forecaster, clearly not last-minute.
These 50 are surfaced live on jaxperro.com/trading.

*Lesson: score conviction bets, not all bets; require avg entry ~0.4-0.6 (edge,
not favorites); and gate on lead time to drop last-minute (un-mirrorable) wallets.
That funnel produces a copyable, forward-validated set — the strongest evidence in
this project that
followable skill exists.*

## Copy P&L: position win% ≠ copyability (the scalper trap, 2026-06-23)

The biggest caveat on the whole sharps table: **a high conviction win% does not mean
you can profit copying the wallet.** The win%/record are computed from `curPrice ≥ 0.5`
on resolved positions — a *position snapshot*. For a high-frequency scalper that
massively over-counts: he buys ~$0.50, sells seconds later for ~+$1, and the snapshot
records a "win" even though he never held to resolution. `ArbTraderRookie` shows
**~100% conviction win (398-2)** yet a flat-$50 copy of his conviction bets, held to
**authoritative clob resolution (winner by token_id)**, nets **−$790 (held 0-19)** —
two independent replays agree (live portfolio −$687 ≈ standalone clob −$790).

So `validate_timing.display_stats` now also computes **`copy_pnl`** — what a flat-$50
copier actually realizes since June 1: replay their conviction entries, mirror their
exits, settle held bets at clob resolution. This is surfaced as the **Copy P&L**
column on the dashboard (default sort). The verdict it delivers: **most "sharps" lose
when copied.** Of the ~31, only a handful are copy-positive — `Kruto2027 +$1,184` and
`fortuneking +$430` (true hold-to-resolution betters); names that looked great on win%
(`iohihoo` 88.7% → **−$749**, `ArbTrader` 99.5% → **−$790**) are scalpers that bleed.
The live tracker now follows **fortuneking + Kruto2027** — the two copy-positive
wallets — at $50/trade.

*Lesson: judge a copy target by Copy P&L (a trade-replay with real resolution), never
by position win%. Conviction must be measured at the position level (a wallet's total
stake in a market), not per individual buy — a scalper splits one position across many
small buys, so a per-trade threshold copies far more (and worse) bets than intended.*

## Capital recycling & the $1k book (2026-06-23)

The $1,000 paper book (`live/portfolio.py` → `portfolio.json`, rendered at
jaxperro.com/trading) surfaced two things:

- **"Saturation" was mostly a measurement artifact.** The old browser replay froze
  capital in positions whose resolution date the data-api didn't return, so it
  skipped bets it could afford (340 phantom misses on a 4-wallet book). Computing
  the book **off the cache** — which stores each bet's resolution time (`res_t`) —
  frees cash at the true resolution moment: misses dropped to ~0 and the book
  recycled ~23× over the window. With *real* money this isn't even a problem (cash
  returns on redemption); it was purely the paper sim mis-measuring.
- **More wallets help only up to the bankroll's slot count.** A combo backtest over
  the copy-positive holders showed returns rise with basket size *until* peak
  concurrent demand exceeds ~$1k ÷ $50 = 20 slots, after which a high-volume wallet
  just crowds out the others. So: pick wallets that **fit** the bankroll, favor
  **fast-resolving** markets (capital velocity > bet size on $1k), and don't
  diversify past what you can fund. Two well-chosen holders beat four that overflow.

## The holder blind spot: two data bugs that hid the best copy targets (2026-07-04)

A clean re-run of the May→June train/test on a trusted subset of the cache
overturned two earlier verdicts. Two mechanisms were poisoning the data:

1. **The `res_t = ts` fallback.** When the data-api omits `endDate` on a closed
   position, `insider.resolved_bets` stores the wallet's *sell time* as `res_t`
   and `won = curPrice >= 0.5` *at pull time* — so a scalper's sold-at-profit
   position looks like a resolved win at a fake resolution time.
   ArbTraderRookie's 1,997 cached rows were 100% this. Fix: `live/trust.py` —
   only trust rows whose `res_t` matches the market's modal `res_t` across ≥2
   wallets (endDate rows agree; sell-time rows scatter), market over, wallet
   pulled after resolution, `resolved` not False. 13.5M of 19.2M rows pass.
2. **The held-leg window bug.** `validate_timing`'s Jun-1→now replay only
   counted held bets entered *and* resolved inside the window; a ~7-day-lead
   holder always showed `held 0-0, ~20 unresolved` and failed `held_n>=8`. And
   before the 2026-07-02 `winner=False` settle fix, those unresolved held bets
   were booked as **losses** — which is exactly where the "scalper trap"
   numbers for iohihoo (−$749) and ArbTrader (−$790) came from. **Those two
   verdicts were bug artifacts, not scalper traps.** (ArbTrader still deserved
   rejection pre-fix — his *cache* stats were res_t=ts poison — but his real
   trade record was a ~160h-lead holder.)

**What the clean test found** (select on ≤May trusted rows only, validate on
June, fees+slip): population baseline −1.4%/bet; the existing profile +8.7%
pooled; adding a **whole-book z gate (`z_all > 2`) roughly doubles it** at
every tier; a practical top-basket (also gated on med conviction stake ≥ $50
and holder/borderline lead verdicts) went **+80% pooled, 7/7 wallets
profitable**. A capital-aware $1k replay of the 8-wallet pre-June basket did
**+504% in June** (118 bets, 97W-21L, fees+slip, 53 missed for cash) — with the
three informed holders a combined 62-0 and two basket members *losing* money
(toosmart 4-12), so the selection is good, not magic.

**Where the edge lives:** the top holders (Stavenson, whale `0x73afc816…` with
$20–120k clips, iohihoo; ArbTraderRookie until 2026-07-03) bet **low-tier
tennis (ITF/qualifiers/Wimbledon doubles) and tier-3 esports (CCT CS, Dota 2
EPL)** at ~0.5 entries, win 95–100%, enter ~160h before resolution, and hold.
That's informed money — plausibly match-fixing-adjacent — which is copyable
precisely because of the long lead. The regime risk is real and demonstrated:
**ArbTraderRookie was wiped from every data-api endpoint mid-analysis** on
2026-07-03. Treat every month of this edge as possibly its last; re-select
weekly; never size as if the 100% win rates are permanent.

*Lesson: selection metrics are only as honest as the rows they read. Gate on
trusted rows, judge held edges on windows longer than the wallet's lead time,
and add `z_all` — skill must show in the whole book, not just the big bets.*

## Repo layout

- `insider.py` — the detector: z-score/p-value, timing/freshness/sizing signals,
  Alchemy funding-cluster ring detection.
- `hunt.py` / `huntwide.py` — market sweeps that surface edge wallets.
- `copyback.py` / `oos.py` — in-sample and out-of-sample copy-trade backtests.
- `webhook_receiver.py` — push-based live trade watcher (Alchemy → Discord).
- `smart_money.py` — data foundation + dashboard (true-win-rate scanner).
- `live/` — current system: cache-backed finder + **copy-positive-holder sharps
  selection** (`conviction_scan.py` + `validate_timing.py` → `watch_sharps.json`,
  ranked by Copy P&L) + **$1k paper book** (`portfolio.py` → `portfolio.json`) +
  daily refresh. The dashboard (jaxperro repo) renders those two JSON feeds. See
  `live/README.md`. *Copy execution (`copybot.py`, `sync_floors.py`) is a separate,
  in-progress system — this finder is selection + tracking only.*
- `wide/` — bulk subgraph→DuckDB scanner (survivorship-bias-free, all wallets);
  public subgraph frozen at Jan 2026, so historical-only. See `wide/README.md`.
- `archive/` — the strategies that didn't work, kept for reference. See
  `archive/README.md`.
