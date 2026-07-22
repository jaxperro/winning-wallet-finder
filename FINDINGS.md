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
| **In-play surge momentum (tape)** | ❌ dead | The +$24-46/fill was resolution-timing survivorship (bias round 3, below). Chain truth: **−$6/fill over 1,108 forward fills** — pre-registered kill met 2026-07-22, three days before the funding decision. Wallet-identity null stands. |
| **Sub-5¢ surge longshots** | ❌ dead | 0-for-38 under chain truth; the exploratory scan's 4 "winners" were the same resolution bias. |
| **Crypto oracle fair-value taking** | ⏳ forward window | Chain resolution grew the sample 30× and flipped the read: noisy-around-zero, slight positive tilt at E≥0.07 (+$1.8-2.3/fill, n≈470-860 fwd). E0.04 tier killed; stricter tiers accumulating — #17. |

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

## Making P&L equal reality — the survivorship correction, finished (2026-07-08)

The sharps table's All-Time P&L had been a `won × entry × size` reconstruction,
and decomposing it against each wallet's Polymarket profile (lb-api `/profit`,
the **PM P&L** column) exposed it diverging by **up to 10× — and flipping
signs.** Four distinct bugs, each earned by decomposing an outlier:

1. **A 2,000-row pull cap** (`max_pages=40`) truncated high-volume wallets —
   ewww1's 4,088 positions ($409k) showed as 740 ($40k).
2. **Both-sides positions double-dropped** — one-per-market dedup kept the
   winning leg and silently dropped the paired losing leg (suraxy: +$35k of
   phantom profit).
3. **`initialValue = 0`** on big longshot winners mis-sized the reconstruction.
4. **Corrupt near-epoch `res_t`** rows polluted the sums.

**The fix that killed all four at once:** stop reconstructing, and sum
Polymarket's own `realizedPnl` per closed position over the wallet's *full*
history (`cache.closed_exits`, incremental). This is the wallet's realized
track record — what a copier mirroring their buy/sell/hold actually banks — and
it needs no size/entry/timestamp, so both-sides, `iv=0`, and bad `res_t` all
become moot, and it sums to PM by construction.

**Then the deeper one — the founding survivorship bias, live inside PM itself.**
A residual gap remained: PM `/profit` is *itself* survivorship-biased. Bets that
lost, went to $0, and were never redeemed sit in `/positions` at `curPrice 0` —
real losses, but PM under-counts them **unevenly** (it subtracts Coteykens'
$52k of abandoned losers to land at $14k = our number, but does *not* subtract
oliman2's $161k, leaving PM at $112k against a true $20k). So `_open_split` now
folds those decided-but-unredeemed positions into the realized total, leaving
only genuinely in-flight positions in a new **Open P&L** column. The result:
**where our All-Time reads below PM, PM is the biased number and ours is the
truth.** oliman2 $181k → **$20k**, JuiceFarm $380k → **$32k** — wallets that
looked elite on redeemed-only P&L are mediocre once you count the bets they
walked away from. Full-list check: 27 of 31 sharps match PM within a few
percent, 4 are honestly-lower, and — the correctness signal — **zero
over-count.**

*Lesson: a wallet's redeemed P&L flatters "sell your winners, abandon your
losers." The honest record counts the abandoned losers; the profile number
doesn't. Rank on realized-including-abandoned, and read the open book as a
separate risk.*

## Choosing the month's follow set from corrected data (Set D, 2026-07-08)

With P&L finally honest, the follow set was rebuilt by simulation. Ranking on
the signals that actually predict forward copy profit — 2-month **Copy P&L**
(validated outside the backtest window), 30-day conviction form, copyable lead
(no sub-hour snipers), a clean open book, and moderate bet size (a $1k book
can't follow a $3k-clip wallet) — then backtesting candidate portfolios:

| set | equity (30d, $1k) | W–L | missed |
|-----|-------------------|-----|--------|
| old live set | $15,359 | 250–79 | 0 |
| 5 high-lead big-bettors | $3,661 | 44–18 | **144** (capital-capped) |
| **Set D (6 moderate-bet)** | **$27,799** | **279–75** | **0** |

**Set D = LSB1, imwalkinghere, Kruto2027, 42021, 0xbadaf319, BikesAreTheBikes**
— the sweet spot where return, a 79% decided win rate, full capture (0 missed),
and diversification all peak. Two rules fell out: **moderate-bet wallets beat
big-bettors** on a small book (the whales get capital-capped, 144 missed), and
**imwalkinghere + LSB1 are irreplaceable** (dropping them halves the return).
The backtest is in-sample (a ceiling), but every Set D wallet also clears the
out-of-window Copy P&L signal — that's what separates it from curve-fitting.
The live July book remains the only out-of-sample truth.

*Superseded the same day by Set E, after the alignment audit below found the
replay itself was still dropping and mislabeling bets.*

## Aligning the three books — backtest ↔ bot ↔ Polymarket (2026-07-08)

The live bot showed Kruto2027 mirror-sells the backtest didn't have. Pulling
that thread found the replay was **silently losing real bets** three ways:

1. **Stale entry maps** — the daily freshen reset the bets and exits cursors
   but never `pulled_entries` (14-day TTL), so any market a wallet first
   entered since the last entries pull had no `first_buy` and the replay
   dropped its bets entirely (`if not et: continue`) — not won, not lost, not
   open. *Gone.* The recovered bets included hidden LOSSES — the stale
   backtest was flattering.
2. **`res_t` can't detect in-play sells** — it's endDate metadata (game-day
   midnight, sometimes future), so the `exit < res_t − 300` test never fired
   on in-play markets and every pre-resolution sell booked as
   held-to-resolution. Fixed with the price test: a redeem prints exactly the
   payout; a mid print is a sell (booked at the wallet's actual exit price —
   which also self-corrects poisoned `won` flags).
3. **Resolved round trips vanished** — the round-trip path skipped
   resolved-on-chain markets assuming "the cache row will cover it", but rows
   with bogus forward `res_t` never qualify. Now redeem-closes book at chain
   truth and sell-closes mirror the sell.

Plus one parity fix: Set wallets now replay on the bot's **pinned floors**
(copybot.paper.json), not a recomputed p80 that drifts a few percent and takes
different bets.

**Proof of alignment:** matching every settled bet in the bot's real book
against the backtest row-by-row — **7/9 agree exactly, 0 absent** (was 0/9
agree before the fixes). The 2 disagreements are genuine execution divergence
(the sharp sold on a fast-resolving market, the bot couldn't catch the exit
and rode to resolution) — each book correctly records what happened *to it*,
and that divergence class is permanent.

**The honest price:** the 30d Set D replay fell **$29.1k → $17.4k** as the
flattering artifacts (phantom held-to-$1.00 winners, missing salvage exits,
hidden losses) came out.

**Set E (deployed 2026-07-08):** with the replay finally honest, all 35 sharps
were re-ranked by individual 30d copy replay, and combined sets tested in a
shared book (one position per market, shared cash):

| set | equity (30d, $1k) | note |
|-----|-------------------|------|
| top-4 only | $12,777 | pruning alone loses carry |
| Set D (control) | $17,362 | |
| **Set E (7)** | **$24,378 (+2338%)** | every member positive |
| Set E + lma0o0o0o | $24,437 | carries a −$1,775 wallet — rejected |

**Set E = LSB1, imwalkinghere, Kruto2027, 0xbadaf319 + gkmgkldfmg, AIcAIc,
1kto1m.** Dropped: 42021 (+16% on 22 bets), BikesAreTheBikes (+12%). Rejected
on the audit evidence: oliman2 (true lifetime ~$19k, not PM's $112k; +21% to
copy with 22 stuck-open) and leegunner (elite lifetime +$274k but **negative**
to copy — 7.6-day holds kill compounding). Newcomer caveat: gkmgkldfmg
(z=2.05) and 1kto1m (z=2.4) sit near the selection gate floor — their seats
are earned on a strong month, not deep statistical edge; AIcAIc's held-win is
only 42% (his profit is sell-timing, the most lag-fragile edge class). They
are the demotion watch-list, in that order.

## The refund harvesters — a new sharp archetype (2026-07-08)

Splitting SOLD out of the record columns (same W/L/R/S taxonomy as the bot and
backtest; win% is now held-outcomes only) exposed something the sign-based
tally had been calling "wins": two of the highest-z wallets in the list are
**refund-harvesting machines**. The signature is exits at *exactly* $0.50 to
float precision — only refund redemptions print there — at enormous scale:

- **0xb0E43B…** (z=20.2, "94.4% all-time" under the old tally): 797 exact-0.5
  redeems = **$148k of his $218k lifetime P&L**. True held record: 76W–31L.
- **ArbTraderRookie** (z=29, "99%"): 1,150 of the same.

The strategy: buy ITF tennis totals just under 50¢, harvest the chronic ITF
cancellation/retirement rate (50/50 refunds pay $0.50/share). The edge is
real, clever, and **structurally uncopyable** — it clears 1–2¢/share and a
taker copy pays ~0.75¢ fee each way plus slippage into it (honest replay: +6%
and −5% respectively). This closes the loop on the project's oldest lesson:
*win rate lies, z finds real skill, and only the fee-and-lag replay decides
whether the skill transfers to a follower.*

Also fixed in the same pass: `_open_split` now classifies decided-unredeemed
positions by the data-api's **`redeemable` flag** (exact, set at on-chain
resolution for winners and losers alike) instead of price-pinning — verified
by reproducing PM's per-position books to the dollar on the two biggest
All-Time-vs-PM divergences.

## The calibration experiment (started 2026-07-08)

Everything above makes the *accounting* honest. It does not make the +2338%
**forecast** honest: Set E is an in-sample maximum (ranked and assembled on
the same 30-day window it's scored on — winner's curse applies), and the
replay's fill model (their price +0.5%, always filled) ignores adverse
selection — the market moves fastest on exactly the bets where the sharp knew
something, and thin ITF books won't always fill a FAK copy at size. The one
piece of measured ground truth — the old paper book's +$229.79 (~23%) over two
buggy weeks vs. four-figure replay percentages for the same era — says the
live-to-model discount is large.

So the paper book was **reset to a fresh $1,000 on 2026-07-08** (old book
archived in git history + `archive/copybot_fills.pre-reset-2026-07-08.jsonl`)
running Set E with every fix live from day one. **The measured ratio between
this book and the published backtest over the coming weeks is the number that
sizes real money** — not the replay percentage. Bank-size note for that
decision: the replay compounds *faster* on smaller banks (`--bank 500` →
+2728% vs $1k's +2103%) because 4%-of-equity stakes hit the never-bigger-than-
their-bet ceilings later — percentage returns from small books are the most
optimistic view, discount accordingly.

## The tape era opens: first RTDS findings (2026-07-20)

> **CORRECTED 2026-07-22**: the Study-A/B *EV figures* in this section were
> inflated by resolution-timing survivorship (see the next section). The
> structural findings — sharp screen, identity-null, crater physics —
> survive; the surge P&L numbers do not.

Three days of the recorder's firehose in `live/rtds.duckdb` (13.8M fills,
160k wallets, every fill *including the losers*) killed the survivorship
problem at the source and produced four findings in one day — details, code,
and frozen parameters in `research/` (silo'd from the bots; see its README):

**1. The tape sharp screen works, and its resolution proxy is exact.**
Terminal-VWAP convergence (≥0.97/≤0.03, 2h quiet, sibling veto) agreed with
on-chain CTF payout vectors on **742 of 742** validated bets. First run
(`live/tape_sharps.py`): 2,360 wallets with ≥8 resolved held bets, 25
copyable candidates at z 4.0–5.5 (discrete conviction entries, sports/
esports in-play profile — the Set-E archetype), cleanly separated from an
uncopyable **algo-flow tier** (z 9–12, 10–37 fills/bet, $M volumes: the
crater-sweepers). Benchmark sanity: benched sharps scored positive, benched
disappointments negative.

**2. Wallet identity might not matter (the null that redirects the program).**
Study A (#16): a $300/60s net-flow surge into an in-play sports/esports
market at 0.10–0.90 simulates to ~+$24/$100-fill after fees *at our
measured execution* — but 10 activity-matched random wallet sets produce
the SAME EV (+23.85 pooled vs +23.68 informed). The herd's lean is the
signal; *who* leans adds nothing so far. Hypothesis revised at freeze:
surge momentum primary, identity lift secondary. Verdict comes only from
the forward ledger (research nightly, pre-registered thresholds).

**3. The oracle edge is real on paper and mostly untakeable — worse at size.**
Study B (#17): fair value from the venue's own settlement tick feed flags
~9k mispricings/21h, but 86% die as FAK craters (crypto makers requote
<4s), and the fillable remainder shows winner's-curse inversion — demanding
a 10¢ edge *loses* $31/fill. Nothing froze (no cell hit 30 fills);
candidate v2 uses are inverted: copy filter / maker-side.

**4. Craters refill on a clock, and it's niche-shaped.** 775k crater prints:
crypto refills within 4s 94% of the time, esports 83% by 10s, sports needs
~25s, geo/politics tails run minutes. Shipped straight into the bots as
per-niche `fak_retry` waits (the flat 10s was calibrated to nothing).

Execution realism note for everything above: the research simulator is
fitted on the live bot's own 29 labeled attempts (fills + FAK misses) and
carries a measured **−2¢/fill optimism bias** — every pre-registered pass
threshold sits at least 2× above it.

## Survivorship bias, round three: resolution timing kills Study A (2026-07-22)

The forward ledger said the surge signal earned **+$35–46/fill**. The truth
was **−$6/fill**. The gap was a third, subtler survivorship mechanism — and
the way it was caught is the real finding.

**The mechanism.** The ledger graded fills with the tape's proxy-resolution
and skipped "pending" markets. But tape resolution is *win-biased*: when our
side wins, the losing sibling goes quiet and the market resolves into the
ledger within hours. When our side **loses**, the *winning* sibling keeps
printing at 99¢ until close — the sibling-veto keeps the market "alive" and
the loss hides in the ignored pending bucket. Wins scored same-day; losses
waited. Jul-21 audit: tape-resolved fills hit 81% (+$46/fill); the 329
"pending" fills, chain-resolved on the spot, hit **26% (−$49.61/fill)**.
Combined: 53%, ≈ breakeven-negative.

**How it was caught: an independent instrument refused to agree.** The
surge paper harness (wwf-surgebot — real-time, $5 stakes, graded nightly
against CTF payout vectors from day one) read 57.5% while the ledger read
73–81%. Decomposition ruled out selection (same-trigger cohorts hit alike)
and entry prices (~1¢ effect); what remained was the scorer itself. The
same pattern as rounds one (unredeemed losers) and two (res_t poison): a
too-good number met a measurement that couldn't be sweet-talked.

**The fix** (`research/forward.py payouts_for()`, d34a4c5): chain-truth
overlay is now mandatory for every arm — tape proxy first, CTF payout
vectors for the remainder, refunds as scratches. Full recompute:

| arm | corrected verdict |
|---|---|
| Study A surge (#16) | **KILL met**: −$6.03/fill over 1,108 forward fills; even the fit day negative. Identity-null unchanged (controls deflated equally). |
| sub-5¢ longshots | **0-for-38** — the scan's "winners" were the same bias. |
| Study B oracle (#17) | Sample ×30 under chain resolution (crypto sprints were veto-stuck). Now noisy-zero with a positive tilt at E≥0.07; the 4¢ tier killed, stricter tiers accumulating. |

**What the pre-registration bought.** The kill line was written 2026-07-20,
before any forward data; when the corrected numbers crossed it there was
nothing to argue about and no money at risk — the $100 deployment plan
(#19) was three days from its funding gate. Total cost of the false edge:
$0 real, ~2 days of compute, one paper book that ended at $99-and-change.

**Standing rules extracted:** (1) any scorer that can say "pending" must
prove pending is outcome-neutral, or chain-resolve it — on Polymarket it
never is neutral, because market liveness itself encodes the outcome;
(2) every study needs one instrument that doesn't share the scorer's
assumptions (the paper harness earns its keep even when — especially
when — it disagrees); (3) at 20–50× payoffs, no small sample means
anything: the sub-5¢ "edge" survived a 31-fill scan and died at 38.

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
- `research/` — the tape-era edge factory (SILO'd from the bots): read-only
  RTDS loaders, execution sim calibrated on the live ledger, pre-registered
  studies (#16 surge momentum, #17 oracle fair value), nightly forward
  ledger. Verdicts come from `research/forward_ledger.jsonl` only. See
  `research/README.md`.
- `wide/` — bulk subgraph→DuckDB scanner (survivorship-bias-free, all wallets);
  public subgraph frozen at Jan 2026, so historical-only. See `wide/README.md`.
- `archive/` — the strategies that didn't work, kept for reference. See
  `archive/README.md`.
