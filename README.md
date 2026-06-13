# 🏆 Winning Wallet Finder

Find Polymarket wallets with a **real, statistically-verifiable edge**, test
whether copying them actually makes money, and **get pinged the moment they
trade**.

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
 data layer            detection              hunting                 validation            live
 ──────────            ─────────              ───────                 ──────────            ────
 smart_money.py  ──▶   insider.py     ──▶     hunt.py / huntwide.py ─▶ copyback.py / oos.py  webhook_receiver.py
 (Polymarket API,      (z-score, timing,      (sweep markets,         (does copying them    (Alchemy webhook →
  true win rate)        freshness, funding     surface edge wallets)   actually pay? in- &    Discord ping on
                        clustering)                                    out-of-sample)         every trade)
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

## Live watcher — get pinged on every trade

Push-based (no polling). The instant a tracked wallet's proxy transacts on
Polygon (~2–5s), Alchemy POSTs `webhook_receiver.py`, which enriches the trade
via the data-API and pings Discord: `🟢 Famecesgoal BUY Yes @ 0.34 ($120) — <market>`.

1. **Discord webhook** → set `DISCORD_WEBHOOK` env (or `config.json`).
2. **Deploy** the receiver to an always-on host (Railway / Fly / a $5 VPS — *not*
   Render free, it sleeps). `Procfile` included; binds to `$PORT`.
3. **Alchemy** → create an *Address Activity* webhook (Polygon mainnet), add the
   `watch.json` addresses, point it at `https://your-host/alchemy`, and set the
   signing key as `ALCHEMY_SIGNING_KEY`.

Keep the two wallet lists in sync: Alchemy's address list (what *triggers*) and
`watch.json` (what *names* the alert).

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
