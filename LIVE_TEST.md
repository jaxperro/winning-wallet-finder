# Live pipeline test — minimum-size real-money runbook

Goal: a few **$5 trades** through the full pipe (detect → filter → size → place →
settle → redeem) with hard caps bounding worst-case loss to the deposit.

Caps in `config.live.json` (gitignored — holds your key, never commit):
**$5/trade · $25/day · $30 max exposure · 6 open positions · 2 per event ·
prices 0.05–0.95 · FAK marketable orders.** Worst case ≈ the $30 exposure cap.

## 1. Fund a dedicated account (you)

- Use (or create) a Polymarket account you're comfortable putting a key on this
  machine for — **deposit only the test money (~$50)**. The key controls only
  what's in the account; keep it that way.
- Deposit USDC to the account (the profile/deposit address = your **funder**).
- Send **~1 POL** to the signing EOA for redeem gas (skip if you set
  `live.auto_redeem: false` and redeem manually in the UI).
- Export the **private key**: email-login accounts → Polymarket Settings →
  Export private key (`signature_type: 1`, already set). Browser-wallet
  accounts → your wallet's key, and set `signature_type: 2`.

## 2. Fill `config.live.json` (you)

```jsonc
"live": {
  "private_key":    "0x…",   // the exported key
  "funder_address": "0x…",   // your Polymarket profile/deposit address
  "signature_type": 1        // 1 = email login · 2 = browser wallet
}
```

## 3. Preflight (read-only, no orders)

    python3 preflight_live.py

Green across the board = auth, USDC balance, market access, and redeem gas all
verified. Fix anything red before arming.

## 4. Pause the paper worker (avoids the two bots fighting over the feed)

    cd ~/…/scratchpad/railway-copybot && railway down --service copybot -y
    # …or pause the service in the Railway dashboard

## 5. Arm (you type the phrase — that's the point of it)

    python3 copybot.py --config config.live.json \
        --state copybot_state.live.json --poll 60 --live

It prints the caps, asks for the confirmation phrase, then baselines (no
retro-copying) and waits for the next conviction trade from Kruto2027 /
shisan888 / fortuneking / LSB1. Placements log to the console/Railway logs (per-trade Discord
pings were retired 2026-07-04). Watch the first
fill: order accepted → shares in the account (Polymarket UI) → on resolution,
SETTLE line + auto-redeem tx hash.

## 6. Wind down

Ctrl-C the live bot, then redeploy the paper worker:

    cd ~/…/scratchpad/railway-copybot && railway up --service copybot --detach

## Notes

- **Venue**: the bot trades the international CLOB (`clob.polymarket.com`).
  If your account lives on the US exchange, its API differs — confirm which
  venue your funds are on before the test.
- **Neg-risk markets** can't auto-redeem — the bot warns; redeem those in the UI.
- The live book/state is separate (`copybot_state.live.json`); the paper July
  test's state is untouched.
- First `redeem.py` run is untested against live — verify the first redemption
  tx on Polygonscan before trusting the loop.
