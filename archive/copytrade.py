#!/usr/bin/env python3
"""Polymarket copy-trade engine.

Watches a list of wallets and mirrors their trades onto your own account:
  - sizing: a fixed % of your configured bankroll per new entry
  - mirror: entries AND exits (sells are mirrored proportionally)
  - guard:  skip a copy if the market has moved >5% from their fill price

SAFETY
------
Runs in PAPER mode by default — it logs exactly what it would do and places
nothing. Live trading requires ALL of:
    1. "mode": "live" in the config,
    2. the --live command-line flag,
    3. typing the confirmation phrase when prompted,
    4. py-clob-client installed and valid credentials in the config.
Hard risk caps (per-trade, daily spend, total exposure, open positions, price
bounds) apply in both modes. This is real money in live mode — you are
responsible for the configuration and the outcomes.

Usage
-----
    python3 copytrade.py --init           # write config.example.json
    python3 copytrade.py                   # paper mode (safe)
    python3 copytrade.py --once            # one polling pass, then exit
    python3 copytrade.py --live            # live mode (requires config + confirm)
    python3 copytrade.py --config my.json  # custom config path
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# reuse the scanner's hardened HTTP helper (SSL fallback, retries)
from smart_money import get_json, SSL_CTX  # noqa: E402

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
CONFIRM_PHRASE = "TRADE LIVE"

DEFAULT_CONFIG = {
    "mode": "paper",                 # "paper" or "live"
    "poll_seconds": 12,              # how often to check each wallet
    "discord_webhook": "",           # paste a Discord webhook URL to get pings
    "watchlist": [],                 # ["0xwallet1", "0xwallet2", ...]
    "bankroll_usd": 1000.0,          # starting stake pool
    "bankroll_pct": 0.02,            # fraction of CURRENT equity per new entry
                                     # (compounds up and down; falls back to a flat
                                     # fraction of bankroll_usd when cash isn't tracked)
    "stake_cap_usd": 0,              # >0: pin stakes at this size once the book grows
                                     # past cap/bankroll_pct — surplus cash is SWEPT to
                                     # state["reserve"], a banked pot that never bets
                                     # (profit ratchet + keeps fills inside book depth)
    "price_guard_pct": 0.05,         # skip if price moved >5% from their fill
    "risk": {
        "max_trade_usd": 50.0,       # hard ceiling on any single copy
        "max_position_usd": 40.0,    # hard ceiling on total cost in one market
        "daily_spend_cap_usd": 250.0,
        "max_total_exposure_usd": 500.0,
        "max_open_positions": 20,
        "max_per_event": 0,          # >0 caps concurrent positions on one real-world
                                     # event (a game's markets are one correlated bet);
                                     # 0 = off — follow every conviction trade
        "min_price": 0.05,           # don't open longshots/near-certainties
        "max_price": 0.95,
        "min_order_usd": 5.0,        # Polymarket min order size
    },
    # live credentials — only read in live mode
    "live": {
        "private_key": "",           # EOA key that controls the funds
        "funder_address": "",        # proxy wallet holding USDC (sig type 1/2)
        "signature_type": 1,         # 0 EOA · 1 email/magic proxy · 2 browser proxy
    },
}

STATE_PATH_DEFAULT = "copytrade_state.json"


def post_discord(webhook, content):
    """POST a message to a Discord webhook. Best-effort; never raises."""
    if not webhook:
        return False
    try:
        body = json.dumps({"content": content}).encode()
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=10, context=SSL_CTX).read()
        return True
    except (urllib.error.URLError, TimeoutError):
        return False


# ── state ─────────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def new_state():
    return {
        "started_at": time.time(),
        "seen_tx": [],               # transactionHashes already processed
        "their_pos": {},             # wallet -> {token_id: shares}, live-tracked
        "seed_tokens": {},           # wallet -> [token_id] held when we started
        "my_pos": {},                # token_id -> {"shares", "cost", "title", "outcome"}
        "spend": {"date": "", "usd": 0.0},
        "seeded": [],                # wallets whose starting positions we loaded
    }


# ── market data ─────────────────────────────────────────────────────────────

def clob_price(token_id, side):
    """Best price to trade `side` ('buy'/'sell') on this token, or None."""
    try:
        url = f"{CLOB_API}/price?token_id={token_id}&side={side}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            return float(json.loads(r.read().decode())["price"])
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        return None


def their_positions(wallet):
    """Current open positions -> {token_id: shares}, for exit-fraction math.
    Cap is generous: a whale can hold >500 open positions, and a position
    missing from the seed both breaks no-backfill (their ADD to an old
    position looks like a fresh OPEN) and the sell-fraction math."""
    pos = {}
    offset = 0
    while offset < 2000:
        page = get_json("/positions",
                        {"user": wallet, "limit": 50, "offset": offset,
                         "sizeThreshold": 0.1})
        if not page:
            break
        for p in page:
            if p.get("asset"):
                pos[p["asset"]] = pos.get(p["asset"], 0) + p.get("size", 0)
        offset += 50
        if len(page) < 50:
            break
    return pos


def recent_trades(wallet, limit=100):
    """Newest-first TRADE activity for a wallet."""
    return get_json("/activity",
                    {"user": wallet, "type": "TRADE", "limit": limit}) or []


def event_key(t):
    """Correlation-group id for a trade: the real-world event its market belongs
    to. Polymarket sub-splits one game across several eventSlugs
    (`…-2026-07-01-more-markets`, `…-2026-07-01-second-half-result`), so dated
    slugs collapse to their `…-YYYY-MM-DD` prefix; undated slugs stand as-is."""
    ev = t.get("eventSlug") or t.get("slug") or ""
    m = re.match(r"(.*?\d{4}-\d{2}-\d{2})", ev)
    return m.group(1) if m else (ev or None)


# ── execution ────────────────────────────────────────────────────────────────

class PaperExecutor:
    """Simulates fills at the current best price. Places nothing."""
    live = False

    def buy(self, token_id, shares, price, meta):
        return {"ok": True, "filled_shares": shares, "price": price, "paper": True}

    def sell(self, token_id, shares, price, meta):
        return {"ok": True, "filled_shares": shares, "price": price, "paper": True}


class LiveExecutor:
    """Places real orders via py-clob-client. Imported lazily."""
    live = True

    def __init__(self, cfg):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
        except ImportError:
            sys.exit("Live mode needs py-clob-client:  pip install py-clob-client")
        self._OrderArgs, self._OrderType = OrderArgs, OrderType
        self._BUY, self._SELL = BUY, SELL
        live = cfg["live"]
        if not live.get("private_key"):
            sys.exit("Live mode needs live.private_key in the config.")
        self.client = ClobClient(
            host=CLOB_API,
            key=live["private_key"],
            chain_id=POLYGON_CHAIN_ID,
            signature_type=live.get("signature_type", 1),
            funder=live.get("funder_address") or None,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def _order(self, token_id, shares, price, side):
        args = self._OrderArgs(price=round(price, 3), size=round(shares, 2),
                               side=side, token_id=token_id)
        signed = self.client.create_order(args)
        resp = self.client.post_order(signed, self._OrderType.GTC)
        ok = bool(resp and resp.get("success", True))
        return {"ok": ok, "filled_shares": shares, "price": price,
                "resp": resp, "paper": False}

    def buy(self, token_id, shares, price, meta):
        return self._order(token_id, shares, price, self._BUY)

    def sell(self, token_id, shares, price, meta):
        return self._order(token_id, shares, price, self._SELL)


# ── engine ────────────────────────────────────────────────────────────────

class CopyTrader:
    def __init__(self, cfg, state, executor, state_path):
        self.cfg = cfg
        self.state = state
        self.ex = executor
        self.state_path = state_path
        self.risk = cfg["risk"]
        self.seen = set(state["seen_tx"])
        self.webhook = cfg.get("discord_webhook", "")
        self._discord_warned = False

    # -- helpers --
    def log(self, msg):
        print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)

    def alert(self, msg, discord_text=None):
        """Log to console AND push to Discord (used for actual placements)."""
        self.log(msg)
        if self.webhook:
            ok = post_discord(self.webhook, discord_text or msg)
            if not ok and not self._discord_warned:
                self.log("  ⚠ Discord webhook post failed (check the URL)")
                self._discord_warned = True

    def reset_daily_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if self.state["spend"]["date"] != today:
            self.state["spend"] = {"date": today, "usd": 0.0}

    def open_exposure(self):
        return sum(p["cost"] for p in self.state["my_pos"].values())

    # ---- dynamic sizing: fraction of CURRENT equity, with a drawdown brake ----
    DD_THRESHOLD = 0.80      # below 80% of the high-water mark…
    DD_FACTOR = 0.5          # …bet half size until equity recovers

    def wallet_class(self, wallet=None):
        """'volume' (default) or 'whale', from follow.wallet_class ({address: class})."""
        classes = (self.cfg.get("follow") or {}).get("wallet_class") or {}
        return classes.get((wallet or "").lower(), "volume")

    def stake_frac(self, wallet=None):
        """Equity fraction for this wallet's signals, by class:
        follow.class_pct maps class -> fraction (e.g. volume 0.04, whale 0.12).
        A class missing from class_pct falls back to bankroll_pct."""
        pcts = (self.cfg.get("follow") or {}).get("class_pct") or {}
        pct = pcts.get(self.wallet_class(wallet))
        return self.cfg["bankroll_pct"] if pct is None else float(pct)

    def stake_usd(self, wallet=None, their=None):
        """Next bet size = the wallet's class fraction (stake_frac) × current
        WORKING equity (cash + open cost basis), halved in a >20% drawdown from
        the high-water mark — and NEVER larger than the followed wallet's own
        stake (`their` = the signal's position size so far): when the
        percentage works out to more than they actually bet, mirror their
        exact amount. The stake cap and banked-reserve sweep are retired
        (2026-07-06, with the backtest's banking logic) — the their-bet
        ceiling is the liquidity bound now: fills stay within size the market
        demonstrably absorbed from the signal itself. Falls back to the flat
        static stake when cash isn't tracked (legacy poll CLI)."""
        frac = self.stake_frac(wallet)
        cash = self.state.get("cash")
        if cash is None:
            stake = self.cfg["bankroll_usd"] * frac
        else:
            eq = cash + self.open_exposure()
            hwm = max(self.state.get("hwm", 0.0), eq)
            self.state["hwm"] = hwm
            if eq < self.DD_THRESHOLD * hwm:
                frac *= self.DD_FACTOR
            stake = frac * eq
        if their and stake > their:
            stake = their
        return stake

    def record_miss(self, wallet, token, cond, title, outcome, price, want, reason):
        """A bet the strategy WOULD have copied but the book couldn't take —
        free cash gone, event cap, price drifted past the guard. Kept in state so
        the live feed can show missed bets and (once the market resolves) their
        would-be P&L — the live counterpart of the backtest's Missed table."""
        missed = self.state.setdefault("missed", [])
        if any(m["token"] == token and m["status"] == "open" for m in missed):
            return                                  # already recorded while open
        missed.append({"ts": int(time.time()), "wallet": wallet, "token": token,
                       "cond": cond, "title": title, "outcome": outcome,
                       "price": round(price or 0, 4), "stake": round(want, 2),
                       "reason": reason, "status": "open", "pnl": None,
                       "settled": None})
        del missed[:-200]                           # keep the recent 200

    def persist(self):
        self.state["seen_tx"] = list(self.seen)[-5000:]
        save_json(self.state_path, self.state)

    # -- risk gate: returns (allowed_usd, reason_if_blocked) --
    def gate_buy(self, want_usd, price, pos_cost=0.0):
        r = self.risk
        if not (r["min_price"] <= price <= r["max_price"]):
            return 0.0, f"price {price:.3f} outside [{r['min_price']},{r['max_price']}]"
        if len(self.state["my_pos"]) >= r["max_open_positions"]:
            return 0.0, f"max open positions ({r['max_open_positions']}) reached"
        self.reset_daily_if_needed()
        # free cash, when tracked (copybot maintains state["cash"], recycled on
        # sell + resolution). All-or-nothing like the dashboard's `if(cash>=stake)`:
        # a bet we can't fully fund from free cash is a MISS, not a partial fill.
        cash = self.state.get("cash")
        if cash is not None and cash < want_usd:
            return 0.0, (f"capital fully deployed (free ${cash:.2f} < "
                         f"stake ${want_usd:.2f})")
        caps = [
            want_usd,
            r["max_trade_usd"],
            r.get("max_position_usd", float("inf")) - pos_cost,
            r["daily_spend_cap_usd"] - self.state["spend"]["usd"],
            r["max_total_exposure_usd"] - self.open_exposure(),
        ]
        if cash is not None:
            caps.append(cash)            # never deploy more than free cash
        allowed = min(caps)
        if allowed < r["min_order_usd"]:
            return 0.0, (f"capped to ${allowed:.2f} < min order "
                         f"${r['min_order_usd']:.2f} (caps)")
        return allowed, None

    # -- process one of their trades --
    def handle_trade(self, wallet, t):
        tx = t.get("transactionHash")
        if not tx or tx in self.seen:
            return
        token = t.get("asset")
        side = t.get("side")          # BUY / SELL
        their_size = t.get("size", 0)
        their_price = t.get("price", 0)
        title = t.get("title", "?")
        outcome = t.get("outcome", "?")
        label = f"{outcome} · {title[:42]}"

        their_book = self.state["their_pos"].setdefault(wallet, {})
        their_prev = their_book.get(token, 0)

        if side == "BUY":
            self._handle_their_buy(wallet, token, their_size, their_price,
                                   label, title, outcome, event=event_key(t),
                                   cond=t.get("conditionId"))
            their_book[token] = their_prev + their_size
        elif side == "SELL":
            self._handle_their_sell(token, their_size, their_prev, label)
            their_book[token] = max(0.0, their_prev - their_size)

        self.seen.add(tx)
        self.persist()

    def _live_price(self, token, side):
        p = clob_price(token, side)
        if p is None:
            self.log(f"  ⚠ no live price for token, skipping")
        return p

    def _price_guard_ok(self, current, their_price):
        if their_price <= 0:
            return True
        # ASYMMETRIC by rule: a better price than the sharp paid is never blocked
        # (paying less for the same outcome is strictly better odds — the guard
        # once skipped a 0.70→0.51 improvement that went on to win). Only adverse
        # drift — chasing the price UP — is gated by price_guard_pct.
        if current <= their_price:
            return True
        return (current - their_price) / their_price <= self.cfg["price_guard_pct"]

    def _handle_their_buy(self, wallet, token, their_size, their_price,
                          label, title, outcome, event=None, cond=None):
        mine = self.state["my_pos"].get(token)
        is_add = mine is not None
        # the signal's position in this token BEFORE this trade — the their-bet
        # ceiling needs it in BOTH branches (their_prev + their_size = their
        # total stake). Defining it only inside the is_add branch made every
        # fresh OPEN crash with UnboundLocalError since the ceiling landed
        # 2026-07-06 — swallowed as a webhook "handler error", so the bot
        # silently placed NO new positions (last fill 2026-07-05, found 07-07).
        their_prev = self.state["their_pos"].get(wallet, {}).get(token, 0)
        # don't backfill: never open a position they already held when we
        # started watching. (A position we built during the run is an ADD;
        # a brand-new position they opened after start is a fresh OPEN.)
        if not is_add and token in self.state["seed_tokens"].get(wallet, []):
            self.log(f"BUY  {label} — skip (held before we started, no backfill)")
            return
        # correlation cap: a game's markets settle together — N bets on one event
        # are one big bet, not N diversified ones (LSB1 once stacked 6 markets on
        # a single match). Cap concurrent positions per real-world event.
        cap = self.risk.get("max_per_event")
        if not is_add and event and cap:
            held = sum(1 for p in self.state["my_pos"].values()
                       if p.get("event") == event)
            if held >= cap:
                self.log(f"BUY  {label} — skip (already {held} positions on this "
                         f"event, cap {cap})")
                self.record_miss(wallet, token, cond, title, outcome, their_price,
                                 self.stake_usd(wallet), f"event cap ({held} held)")
                return

        price = self._live_price(token, "buy")
        if price is None:
            return
        if not self._price_guard_ok(price, their_price):
            self.log(f"BUY  {label} — skip (price {price:.3f} vs their "
                     f"{their_price:.3f}, >{self.cfg['price_guard_pct']:.0%})")
            self.record_miss(wallet, token, cond, title, outcome, price,
                             self.stake_usd(wallet),
                             f"price moved {their_price:.2f}→{price:.2f}")
            return

        if is_add:
            # proportional add: grow my position by the same fraction they did —
            # but SIZING DISCIPLINE binds per market: total position cost never
            # exceeds the current stake rule. Unbounded mirroring once took one
            # game to 2.15 stakes ($90 on a $42-stake book) when fortuneking
            # doubled into his own bet; the backtest is one-market-one-stake,
            # so the bot must be too.
            frac = their_size / their_prev if their_prev > 0 else 0
            want_shares = mine["shares"] * frac
            room = self.stake_usd(wallet, their_prev + their_size) - mine["cost"]
            if room < self.risk["min_order_usd"]:
                self.log(f"ADD  {label} — skip (position ${mine['cost']:.0f} already "
                         f"at the stake size)")
                return
            want_usd = min(want_shares * price, room)
            kind = "ADD "
        else:
            want_usd = self.stake_usd(wallet, their_prev + their_size)  # class % of equity, capped at their bet
            kind = "OPEN"

        pos_cost = mine["cost"] if is_add else 0.0
        allowed, reason = self.gate_buy(want_usd, price, pos_cost)
        if reason:
            self.log(f"{kind} {label} — skip ({reason})")
            if not is_add:                          # a blocked OPEN is a missed bet
                self.record_miss(wallet, token, cond, title, outcome, price,
                                 want_usd, reason)
            return
        shares = allowed / price
        res = self.ex.buy(token, shares, price, {"title": title})
        if not res["ok"]:
            self.log(f"{kind} {label} — ORDER FAILED: {res.get('resp')}")
            return
        spent = res["filled_shares"] * res["price"]
        self.state["spend"]["usd"] += spent
        if is_add:
            mine["shares"] += res["filled_shares"]
            mine["cost"] += spent
        else:
            self.state["my_pos"][token] = {
                "shares": res["filled_shares"], "cost": spent,
                "title": title, "outcome": outcome, "event": event}
        tag = "[PAPER]" if not self.ex.live else "[LIVE]"
        self.alert(
            f"{kind} {label} — {tag} buy {res['filled_shares']:.1f} "
            f"@ {res['price']:.3f}  (${spent:.2f})",
            discord_text=(f"🟢 **{kind.strip()}** {tag}\n{label}\n"
                          f"buy {res['filled_shares']:.0f} @ {res['price']:.3f} "
                          f"= **${spent:.2f}**"))

    def _handle_their_sell(self, token, their_size, their_prev, label):
        mine = self.state["my_pos"].get(token)
        if not mine:
            return  # we don't hold it
        frac = 1.0 if their_prev <= 0 else min(1.0, their_size / their_prev)
        sell_shares = min(mine["shares"], mine["shares"] * frac)
        if sell_shares <= 0:
            return
        price = self._live_price(token, "sell")
        if price is None:
            return
        res = self.ex.sell(token, sell_shares, price, {})
        if not res["ok"]:
            self.log(f"EXIT {label} — ORDER FAILED: {res.get('resp')}")
            return
        proceeds = res["filled_shares"] * res["price"]
        # reduce position; release cost proportionally
        sold_frac = res["filled_shares"] / mine["shares"] if mine["shares"] else 1
        mine["cost"] *= (1 - sold_frac)
        mine["shares"] -= res["filled_shares"]
        tag = "[PAPER]" if not self.ex.live else "[LIVE]"
        verb = "EXIT" if frac >= 0.999 else "TRIM"
        self.alert(
            f"{verb} {label} — {tag} sell {res['filled_shares']:.1f} "
            f"@ {res['price']:.3f}  (${proceeds:.2f})",
            discord_text=(f"🔴 **{verb}** {tag}\n{label}\n"
                          f"sell {res['filled_shares']:.0f} @ {res['price']:.3f} "
                          f"= **${proceeds:.2f}**"))
        if mine["shares"] <= 0.01:
            del self.state["my_pos"][token]

    # -- seed their current positions so exits mirror proportionally --
    def seed_wallet(self, wallet):
        if wallet in self.state["seeded"]:
            return
        self.state["their_pos"][wallet] = their_positions(wallet)
        self.state["seed_tokens"][wallet] = list(self.state["their_pos"][wallet])
        self.state["seeded"].append(wallet)
        n = len(self.state["their_pos"][wallet])
        self.log(f"seeded {wallet[:10]}… with {n} existing positions "
                 f"(won't be copied as new entries)")

    # -- one polling pass over every watched wallet --
    def poll_once(self, first_pass):
        started = self.state["started_at"]
        for wallet in self.cfg["watchlist"]:
            self.seed_wallet(wallet)
            trades = recent_trades(wallet)
            # oldest-first so position math is causal
            for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
                # on the very first pass, ignore anything from before we started
                if first_pass and t.get("timestamp", 0) < started:
                    self.seen.add(t.get("transactionHash"))
                    continue
                self.handle_trade(wallet, t)
        self.persist()

    def run(self, once):
        mode = "LIVE — REAL MONEY" if self.ex.live else "PAPER (no orders placed)"
        self.log(f"copy-trader started · mode: {mode}")
        self.log(f"watching {len(self.cfg['watchlist'])} wallets · "
                 f"bankroll ${self.cfg['bankroll_usd']:.0f} @ "
                 f"{self.cfg['bankroll_pct']:.1%}/entry · "
                 f"guard {self.cfg['price_guard_pct']:.0%}")
        if self.webhook:
            post_discord(self.webhook,
                         f"✅ **Copy-trade tracker connected** ({mode})\n"
                         f"watching {len(self.cfg['watchlist'])} wallets · "
                         f"${self.cfg['bankroll_usd']:.0f} bankroll @ "
                         f"{self.cfg['bankroll_pct']:.1%}/entry · "
                         f"guard {self.cfg['price_guard_pct']:.0%}\n"
                         f"You'll get a ping on every trade it would place.")
        if not self.cfg["watchlist"]:
            self.log("watchlist is empty — add wallets to the config. "
                     "(Run smart_money.py to find them.)")
            return
        first = True
        try:
            while True:
                self.poll_once(first_pass=first)
                first = False
                if once:
                    break
                time.sleep(self.cfg["poll_seconds"])
        except KeyboardInterrupt:
            self.log("stopped.")


# ── cli ──────────────────────────────────────────────────────────────────

def confirm_live(cfg):
    print("\n" + "=" * 64)
    print("  LIVE MODE — this will place REAL orders with REAL money.")
    print(f"  Bankroll ${cfg['bankroll_usd']:.0f} · {cfg['bankroll_pct']:.1%}/entry"
          f" · max ${cfg['risk']['max_trade_usd']:.0f}/trade"
          f" · daily cap ${cfg['risk']['daily_spend_cap_usd']:.0f}")
    print(f"  Watching {len(cfg['watchlist'])} wallets.")
    print("=" * 64)
    typed = input(f'Type "{CONFIRM_PHRASE}" to proceed (anything else aborts): ')
    if typed.strip() != CONFIRM_PHRASE:
        sys.exit("Aborted — not confirmed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", default=STATE_PATH_DEFAULT)
    ap.add_argument("--live", action="store_true",
                    help="enable live trading (also needs mode:live in config)")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--init", action="store_true",
                    help="write config.example.json and exit")
    args = ap.parse_args()

    if args.init:
        save_json("config.example.json", DEFAULT_CONFIG)
        print("Wrote config.example.json — copy to config.json and edit.")
        return

    if not os.path.exists(args.config):
        sys.exit(f"No config at {args.config}. Run --init to create a template.")
    cfg = {**DEFAULT_CONFIG, **load_json(args.config, {})}
    cfg["risk"] = {**DEFAULT_CONFIG["risk"], **cfg.get("risk", {})}
    cfg["live"] = {**DEFAULT_CONFIG["live"], **cfg.get("live", {})}

    want_live = args.live and cfg.get("mode") == "live"
    if args.live and cfg.get("mode") != "live":
        sys.exit('--live given but config "mode" is not "live". Refusing to trade.')

    state = load_json(args.state, new_state())
    if want_live:
        confirm_live(cfg)
        executor = LiveExecutor(cfg)
    else:
        executor = PaperExecutor()

    CopyTrader(cfg, state, executor, args.state).run(once=args.once)


if __name__ == "__main__":
    main()
