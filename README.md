# 🏆 Winning Wallet Finder

Find Polymarket wallets with a **real, statistically-verifiable edge**, test
whether copying them actually makes money, **get pinged the moment they trade**,
and watch a **live $1,000 paper portfolio** follow them in real time.

This started as "copy the smart money." Along the way we tested — and ruled out
— six systematic public-data strategies, and found that the *only* signal that
holds up is **statistical improbability**: wallets that win far more than the
prices they paid imply. This repo is the tooling for finding and watching those
wallets, plus an honest record of everything that didn't work.

> **Read [`FINDINGS.md`](FINDINGS.md) for the full story.** TL;DR: detection of
> edge wallets works; *profitably copying them* is unproven (it survived a naive
> backtest but collapsed to one-wallet variance out-of-sample). Treat this as a
> research + monitoring tool, not a money printer.

---

## The core idea: z-score, not win rate

Every Polymarket bet has an entry price that *is* the market's estimate of its
odds (a YES at 30¢ ⇒ market thinks 30%). If you have no edge, over many bets
you win about the **sum of your entry prices** — call it *expected wins*.

```
z = (actual wins − expected wins) / standard deviation
```

- **z = 0** → you won exactly what your prices implied → no edge.
- **z = 3** → ~1-in-740 by luck. **z = 5** → ~1-in-3.5M. **z = 9** → astronomical.
- **`p(luck)`** is z as a probability: the chance a no-edge bettor does this well by chance.

Why this beats win rate: a wallet that bets longshots and **wins 14% when the
odds implied 8%** has a huge edge (high z) despite a low win rate. A wallet
buying 90¢ favorites and winning 90% has z≈0 — no edge, just paying for
favorites. **z measures beating the prices you paid.**

Two refinements separate signal from noise:
- **Lifetime trade count** — high z + tens of thousands of trades = a
  market-maker bot, not an insider. Real edge wallets have *concentrated* edge
  over a few thousand trades.
- **Pre-resolution timing + fresh wallet** — entering minutes/hours before
  resolution on a new account is the insider fingerprint (the Bubblemaps /
  *60 Minutes* pattern).

---

## How the pieces fit

```
 data layer        detection           hunting              validation          live system
 ──────────        ─────────           ───────              ──────────          ───────────
 smart_money.py ─▶ insider.py     ──▶  hunt.py / huntwide ▶ copyback / oos.py ▶ webhook_receiver.py ─▶ Discord ping
 (Polymarket API,  (z-score, timing,   (sweep markets,      (does copying them   (Alchemy webhook)
  true win rate)    freshness, funding  surface edge         actually pay? in- &  trading/ (paper) ────▶ live $1k
                    clustering)         wallets)             out-of-sample)       jaxperro.com/trading   portfolio
```

| File | Role |
|------|------|
| `insider.py` | **The detector.** z-score/p-value, pre-resolution timing, fresh-wallet & sizing flags, and Alchemy funding-cluster ring detection. `--scan` / `--market` / `--wallet`. |
| `smart_money.py` | Data foundation + dashboard. Survivorship-corrected **true** win rate. |
| `hunt.py` | Ring-hunt sweep over a fixed list of news-driven event markets. |
| `huntwide.py` | Wide sweep — source wallets from ~100 markets, score each, tier by z. |
| `copyback.py` | Backtest: copy edge wallets' entries from a date, weighted, compounding. |
| `oos.py` | **Out-of-sample test** — select wallets on pre-period data, copy forward. The honesty gate. |
| `webhook_receiver.py` | Push-based live watcher: Alchemy on-chain webhook → enrich → Discord. |
| `watch.json` | The tracked wallet set + edge weights (shared by the watcher and the tracker). |
| paper tracker | Client-side `$1,000` running portfolio → [jaxperro.com/trading](https://jaxperro.com/trading) (page lives in the personal site repo). |
| **`live/`** | **The current scanner** — find & track the skilled ~3% from the live API at scale: enumerate → cache → 5-gate skill funnel → dashboard → daily refresh. Caches ~26k wallets / 12.5M bets locally so every re-score is seconds. ([live/README](live/README.md)) |
| `wide/` | Bulk subgraph→DuckDB scanner: survivorship-bias-free over all 1.76M wallets, but the public subgraph is **frozen at Jan 2026**, so it's a historical tool only. ([wide/README](wide/README.md)) |
| `archive/` | The six strategies that didn't work, kept for reference ([details](archive/README.md)). |

---

## Quickstart

Zero dependencies — Python 3 stdlib only (no `pip install`). macOS python.org
builds lack CA certs, so the code falls back to unverified SSL for these public
read-only APIs.

```bash
git clone https://github.com/jaxperro/winning-wallet-finder
cd winning-wallet-finder
cp config.example.json config.json     # then edit (see Config below)

python3 insider.py --scan 40           # score the top-40 leaderboard wallets
python3 insider.py --market <slug>     # score a market's traders + detect rings
python3 insider.py --wallet 0xABC…     # deep-profile one wallet
python3 huntwide.py                    # wide sweep → huntwide.csv (tiered by z)
python3 oos.py                         # the out-of-sample copy test
python3 smart_money.py                 # dashboard at http://localhost:8899
```

### Config (`config.json`, gitignored — holds your secrets)

```jsonc
{
  "discord_webhook": "https://discord.com/api/webhooks/…",  // alerts
  "alchemy_key": "…",            // Polygon RPC for funding-cluster detection
  "alchemy_signing_key": "…",    // verifies inbound webhook POSTs (live watcher)
  "watch": [ {"wallet": "0x…", "name": "Famecesgoal"}, … ]  // wallets to track
}
```

---

## Data sources

| Source | Used for |
|--------|----------|
| `data-api.polymarket.com` | positions, trades, leaderboard, activity, true win rate |
| `gamma-api.polymarket.com` | market metadata, resolution times, best bid/ask |
| `clob.polymarket.com` | order books, prices, liquidity-reward configs |
| `api.elections.kalshi.com` | Kalshi prices (cross-venue arb research) |
| Alchemy (Polygon) | on-chain USDC funding traces + the live trade webhook |

---

## The live system

Two always-on pieces, both running on near-zero infrastructure cost. The wallets
they track live in `watch.json` (currently the **4 sharpest, followable**
wallets — high z, not bots, not in-game — re-weighted by z).

### 1. Discord watcher — pinged on every trade (`webhook_receiver.py`)

Push-based, no polling. The instant a tracked wallet's proxy transacts on
Polygon (~2–5s), Alchemy POSTs the receiver, which enriches the trade via the
data-API and pings Discord: `🟢 Famecesgoal BUY Yes @ 0.34 ($120) — <market>`.
It's a tiny stdlib HTTP server that idles at ~zero CPU between trades.

1. **Discord webhook** → set `DISCORD_WEBHOOK` env (or `config.json`).
2. **Deploy** the receiver to an always-on host (Railway / Fly / a $5 VPS — *not*
   Render free, it sleeps). `Procfile`, `requirements.txt`, `nixpacks.toml`
   included; binds to `$PORT`, exposes `/alchemy` (POST) and `/health` (GET).
3. **Alchemy** → create an *Address Activity* webhook (Polygon mainnet), add the
   `watch.json` addresses, point it at `https://your-host/alchemy`, set the
   signing key as `ALCHEMY_SIGNING_KEY` (turns on HMAC verification).

Keep the two wallet lists in sync: Alchemy's address list (what *triggers*) and
`watch.json` (what *names* the alert).

### 2. Live paper portfolio — `trading/` → [jaxperro.com/trading](https://jaxperro.com/trading)

A **$1,000 paper account that behaves like real money**: it replays every
watched-wallet trade since inception, **enters when they enter** (if there's
cash), holds each bet **to resolution**, then settles (win → payout, loss → $0)
and frees the cash. Shows **Liquid** (cash), **Invested** (open bets marked to
market), **Realized** (settled P&L), a **Current Bets** table with per-bet entry
/ mark / P&L / *settle date*, and — crucially — **Missed P&L**: the profit left
on the table from trades skipped because the bankroll was fully deployed (the
real cost of a small account). It runs **100% client-side** off Polymarket's
public API (CORS-open) — zero backend, zero added cost.

> **What the tracker taught us:** $1,000 across many hyperactive wallets gets
> fully deployed almost instantly — you can follow only a few percent of their
> trades. Concentrating on a handful of high-conviction wallets with bigger
> stakes is the only way a small bankroll meaningfully mirrors them.

---

## The skilled-wallet scanner (`live/`)

The newest pipeline operationalizes the [LBS/Yale finding](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5910522)
that ~3% of accounts are genuinely skilled. It scans the **live** data-api at
scale and tracks the survivors forward.

**The 5-gate funnel** — a wallet is "skilled" only if it clears all five:
`n ≥ 15 resolved bets` → `z > 0` (beats its entry prices) → `Benjamini–Hochberg
FDR @5%` → `split-half out-of-sample persists` → `not a market-maker/bot`. Win
rate is never a gate.

**The cache makes it cheap.** Each wallet's full resolved-bet history is pulled
once into `cache.duckdb` (~26k wallets / 12.5M bets), keyed with per-bet
resolution times — so any cutoff (pre-June-1, full-window, archetypes) re-scores
in **seconds** instead of hours of API pulls.

**The clean out-of-sample result (June 2026).** Copying the "favorite-rider"
skilled wallets, $1000, no execution lag:

| Selection | Win rate | Forward P&L (June 1+) |
|-----------|----------|-----------------------|
| In-sample (peeks at test window) | 99% | **+23.6%** |
| **Clean (pre-June-1 data only)** | 68% | **−7.4%** (−19% on settled) |

The +23.6% was pure selection bias. Selected honestly, the favorites **lose** —
exactly the paper's "~60% of lucky winners become losers out-of-sample." High
win rate ≠ edge, again. (The `value`/longshot archetype — wallets that beat
*underdog* prices — is the one worth testing next.) Full pipeline in
[`live/README.md`](live/README.md).

**What does work (the repeatable find).** Scoring wallets on their **high-
conviction bets — the top 20% by stake size (per-wallet p80, not a flat $200)** —
which win 70–80% on genuinely-uncertain (~0.4–0.6)
markets — trained pre-June and validated June: **25/37 stayed profitable forward
(p=0.024)**. A lead-time gate (`validate_timing.py`) then drops the ~30% that are
uncopyable insiders (entry <6h before resolution), leaving **23 validated
copyable sharps** (`watch_sharps.json`), surfaced live on
[jaxperro.com/trading](https://jaxperro.com/trading). One held **80% win over
1,017 forward bets**. This is the strongest evidence in the project that
*followable* skill exists — though execution lag/fees and ongoing forward
validation still gate turning it into real money.

---

## The honest verdict

- **Detection works.** z-score + timing + funding-cluster reliably surfaces
  statistically anomalous wallets (the 60-Minutes use case).
- **Copying them is not proven.** In-sample a weighted, compounding copy
  returned +545%; out-of-sample (select pre-May, copy forward) it was +168% —
  but driven entirely by *one* longshot lottery wallet, with the strongest
  signals contributing nothing. That's variance, not a durable, fundable edge.
- **No turnkey public-data edge survived** — copy-trading, win-rate ranking, LP
  reward farming, binary/multi-outcome arb, and cross-venue arb all came back
  efficient or illusory. See [`FINDINGS.md`](FINDINGS.md).

Use this to **find and watch** edge wallets and gather forward data — not as a
green light to bet size on copying them.
