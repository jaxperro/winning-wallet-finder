#!/usr/bin/env python3
"""Polymarket copy-trade engine.

Watches a list of wallets and mirrors their trades onto your own account:
  - sizing: class % of current equity per new entry (capped at the signal's
            own bet, floored at the venue's $1 minimum order)
  - mirror: entries AND exits (sells are mirrored proportionally)
  - guard:  skip a copy if the price rose >5 POINTS above their fill
            (absolute — 0.14→0.15 follows; better-than-theirs never blocked)

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
    "price_guard_abs": 0.05,         # skip if price moved >5 POINTS above their
                                     # fill (absolute, 2026-07-10: 0.14→0.15 must
                                     # follow; relative % blocked 1-tick moves on
                                     # cheap in-play books). VALIDATED 2026-07-13
                                     # by the missed-ledger counterfactuals: the
                                     # 0.05 line sits at the EV knee (0.05-0.10
                                     # moves ≈ breakeven, >0.10 = −20% ROI).
    "depth_gate": {                  # 2026-07-13, fitted on 131 gated fills —
        "max_spread": 0.08,          # RETIRED as a skip 2026-07-19 (lag-era
                                     # relic) — kept for config compatibility
        "min_ask5c": 50.0,           # dust books mispriced every observed fill
        "max_frac_of_ask5c": 0.10,   # stake ≤10% of 5c ask depth (impact <~2%)
    },
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


def book_depth(token_id):
    """Top-of-book + $-depth within 5c of touch — the DEPTH GATE's input
    (2026-07-13, fitted on 131 gated fills). Returns {bb, ba, spread, bid5c,
    ask5c} or None on any failure (the gate then declines to bind — the
    price guard and protected prices still bound the copy)."""
    try:
        url = f"{CLOB_API}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as r:
            b = json.loads(r.read().decode())
        bids, asks = b.get("bids") or [], b.get("asks") or []
        bb = max((float(x["price"]) for x in bids), default=None)
        ba = min((float(x["price"]) for x in asks), default=None)

        def depth(side, ref, sgn):
            if ref is None:
                return None
            return round(sum(float(x["size"]) * float(x["price"]) for x in side
                             if sgn * (float(x["price"]) - ref) >= -0.05), 2)
        return {"bb": bb, "ba": ba,
                "spread": round(ba - bb, 4) if bb is not None and ba is not None else None,
                "bid5c": depth(bids, bb, 1), "ask5c": depth(asks, ba, -1)}
    except Exception:
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


def recent_trades(wallet, limit=100, offset=0):
    """Newest-first TRADE activity for a wallet. `offset` pages deeper —
    the copybot's per-wallet cursor fetch (H3) walks back through bursts
    that overflow a single page."""
    return get_json("/activity",
                    {"user": wallet, "type": "TRADE", "limit": limit,
                     "offset": offset}) or []


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
    """Simulates fills at the current best price. Places nothing.

    BUYs model live FAK reality (2026-07-15): live sends fill-and-kill with a
    protected cap of quote × (1 + max_slippage_pct); on a thin book with no
    ask inside that band the order dies ('no orders found to match') — the #1
    live miss class once detection got fast (the copy arrives in the crater
    the sharp just swept, before makers requote). Paper used to 'fill' those,
    which biased the live-vs-paper per-signal ratio — the bankroll top-up
    number — optimistic. Now the same book snapshot the depth gate fetched
    (meta['book'], else fetched here) decides: no ask ≤ cap -> rejected, and
    the engine records the same 'order rejected' miss live would. A failed
    book fetch fails OPEN (fills, today's behavior) — a dead data source
    shouldn't fabricate misses. SELLs stay optimistic-fill: exits are
    proportional mirrors of a position we hold, and modeling exit failure
    honestly needs the retry/pending machinery, not a one-line reject."""
    live = False
    max_slippage_pct = 0.05        # mirrors live.max_slippage_pct's default

    def buy(self, token_id, shares, price, meta):
        bk = (meta or {}).get("book")
        if bk is None:
            bk = book_depth(token_id)
        if bk is not None:
            ba, cap = bk.get("ba"), price * (1 + self.max_slippage_pct)
            if ba is None or ba > cap:
                gone = ("no asks" if ba is None
                        else f"best ask {ba:.3f} > cap {cap:.3f}")
                return {"ok": False, "filled_shares": 0.0, "price": price,
                        "resp": "no orders found to match with FAK order "
                                f"(paper model: {gone})", "paper": True}
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
        # venue floor: the CLOB rejects sub-$1 orders, so a small book's pct
        # stake must round UP to the minimum or every copy dies at the gate
        # (4% of the $22 live book = $0.89 — 2026-07-10 paper-parity retune)
        return max(stake, self.risk.get("min_order_usd", 1.0))

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
                                   cond=t.get("conditionId"),
                                   their_ts=t.get("timestamp"))
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
        # drift — chasing the price UP — is gated, in ABSOLUTE points: the old
        # relative 5% blocked one-tick moves on cheap in-play books (0.14→0.15
        # is +7% relative but the same bet; 0.14→0.19 is where the edge is gone).
        if current <= their_price:
            return True
        guard = self.cfg.get("price_guard_abs",
                             self.cfg.get("price_guard_pct", 0.05))
        return (current - their_price) <= guard

    def _handle_their_buy(self, wallet, token, their_size, their_price,
                          label, title, outcome, event=None, cond=None,
                          their_ts=None):
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
            # thin/one-sided book — the sharp is often the MAKER here (weekly
            # scalars especially), so there is no ask to lift when the copy
            # arrives. This silent return hid 9 followed-but-unplaced fills
            # across both books on 2026-07-09; a blocked OPEN is a missed bet.
            if not is_add:
                self.record_miss(wallet, token, cond, title, outcome,
                                 their_price, self.stake_usd(wallet),
                                 "no ask side on the book at copy time")
            return
        if not self._price_guard_ok(price, their_price):
            self.log(f"BUY  {label} — skip (price {price:.3f} vs their "
                     f"{their_price:.3f}, moved >"
                     f"{self.cfg.get('price_guard_abs', 0.05):.2f} abs)")
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
        # DEPTH GATE (2026-07-13, fitted on 131 book-annotated fills): the
        # book must absorb the stake. Fills into <$90 of 5c-depth paid
        # +2.6–4.2%; >20% of visible depth made >+2% slippage 33–50% likely;
        # spread>0.08 books ran median |slip| ~14% (market mid-move). Shrink
        # to 10% of depth, skip dust/unreliable books. A failed book fetch
        # declines to bind — guard + protected prices still bound the copy.
        dg = self.cfg.get("depth_gate")
        bk = None                       # reused by the paper FAK model below
        if dg:
            bk = book_depth(token)
            if bk and bk.get("ask5c") is not None:
                d_reason = None
                # SPREAD SKIP RETIRED (2026-07-19): the 0.08 rule was fitted in
                # the 39-90s-lag era, when a wide spread meant the book had
                # repriced under a late copy (med |slip| ~14%). At RTDS-era
                # ~4s lag the mechanism is gone (median |slip| 3.7%→1.7%,
                # Kruto+gkmg in-play mean +0.4%), and the skip was firing on
                # the informed wallets' BEST moments — in-play chaos IS their
                # signal (17 resolved skips ran 12W/5L, +$11 live/+$674 paper
                # would-be). Overpay is already bounded twice: the price guard
                # (their fill +0.05 abs) and the FAK protected band. The
                # depth-based stake cap and dust skip below KEEP earning.
                if bk["ask5c"] < dg["min_ask5c"]:
                    d_reason = (f"thin book (${bk['ask5c']:.0f} within 5c < "
                                f"${dg['min_ask5c']:.0f})")
                else:
                    cap = dg["max_frac_of_ask5c"] * bk["ask5c"]
                    if cap < self.risk["min_order_usd"]:
                        d_reason = (f"depth cap ${cap:.2f} below min order "
                                    f"(ask5c ${bk['ask5c']:.0f})")
                    elif cap < allowed:
                        self.log(f"{kind} {label} — depth gate shrinks "
                                 f"${allowed:.2f} → ${cap:.2f} "
                                 f"(10% of ${bk['ask5c']:.0f} ask depth)")
                        allowed = cap
                if d_reason:
                    self.log(f"{kind} {label} — skip ({d_reason})")
                    if not is_add:
                        self.record_miss(wallet, token, cond, title, outcome,
                                         price, allowed, d_reason)
                    return
        # ONE outstanding in-play hold per token: a second order while a
        # pending rests re-buys the same signal and poisons the resolver's
        # balance-diff window (2026-07-12: overlapping pendings booked one
        # real fill several times → +$7.86 phantom cash).
        if any(po.get("token") == token
               for po in self.state.get("pending_orders", [])):
            self.log(f"{kind} {label} — skip (in-play hold already pending "
                     "on this token)")
            return
        shares = allowed / price
        res = self.ex.buy(token, shares, price, {"title": title, "book": bk})
        if not res["ok"]:
            # in-play books ACCEPT orders with a delayed hold — the executor
            # reports those as pending (order id + pre-order balance) instead
            # of failed. Park the full copy context; the heartbeat resolver
            # adopts the fill when it lands or converts to a miss at TTL.
            if res.get("pending"):
                self.state.setdefault("pending_orders", []).append({
                    **res["pending"], "token": token, "side": "BUY",
                    "wallet": wallet, "title": title, "outcome": outcome,
                    "event": event, "cond": cond, "their_price": their_price,
                    "their_ts": their_ts, "price": price, "is_add": is_add,
                    "stake": allowed, "ts": time.time(), "ttl_s": 600})
                self.log(f"{kind} {label} — PENDING (in-play hold, "
                         f"order {str(res['pending'].get('order_id'))[:14]}…)")
                self.persist()
                return
            self.log(f"{kind} {label} — ORDER FAILED: {res.get('resp')}")
            if not is_add:                     # a rejected OPEN is a missed bet
                self.record_miss(wallet, token, cond, title, outcome, price,
                                 allowed, f"order rejected: {str(res.get('resp'))[:60]}")
            return
        spent = res["filled_shares"] * res["price"]
        self.state["spend"]["usd"] += spent
        if is_add:
            mine["shares"] += res["filled_shares"]
            mine["cost"] += spent
            if cond:
                mine.setdefault("cond", cond)
        else:
            # wallet/cond ride on the position so the book can always self-repair:
            # wallet for the feed's my_pos->bets safety net, cond so check_book can
            # rebuild the conds map (an orphan without it can never settle)
            self.state["my_pos"][token] = {
                "shares": res["filled_shares"], "cost": spent,
                "title": title, "outcome": outcome, "event": event,
                "wallet": wallet, "cond": cond}
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
        if any(po.get("token") == token
               for po in self.state.get("pending_orders", [])):
            self.log(f"EXIT {label} — skip (in-play hold already pending on "
                     "this token; resolver owns it)")
            return
        res = self.ex.sell(token, sell_shares, price, {})
        if not res["ok"]:
            if res.get("pending"):             # in-play hold — resolver adopts
                self.state.setdefault("pending_orders", []).append({
                    **res["pending"], "token": token, "side": "SELL",
                    "wallet": mine.get("wallet", ""), "title": mine.get("title", ""),
                    "outcome": mine.get("outcome", ""), "event": mine.get("event"),
                    "cond": mine.get("cond"), "their_price": price,
                    "their_ts": None, "price": price, "is_add": False,
                    "stake": sell_shares * price, "ts": time.time(), "ttl_s": 600})
                self.log(f"EXIT {label} — PENDING (in-play hold)")
                self.persist()
                return
            # LIVE_ROLLOUT 1.6 (built 2026-07-10): a failed mirror-exit must
            # not silently ride to resolution — queue a bounded retry; the
            # heartbeat re-attempts each tick and alerts ⚠ EXIT STUCK when
            # exhausted. One entry per token; a repeat failure refreshes it.
            retries = self.state.setdefault("exit_retries", [])
            for r in retries:
                if r["token"] == token:
                    r["shares"] = max(r["shares"], sell_shares)
                    break
            else:
                retries.append({"token": token, "shares": sell_shares,
                                "label": label, "attempts": 0,
                                "ts": int(time.time())})
            self.log(f"EXIT {label} — ORDER FAILED: {str(res.get('resp'))[:80]}"
                     " · queued for retry")
            self.persist()
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
    mt, dc = cfg['risk']['max_trade_usd'], cfg['risk']['daily_spend_cap_usd']
    print(f"  Bankroll ${cfg['bankroll_usd']:.2f} · {cfg['bankroll_pct']:.1%}/entry"
          f" · max {'off' if mt >= 1e5 else '$%.0f' % mt}/trade"
          f" · daily cap {'off' if dc >= 1e5 else '$%.0f' % dc}")
    print(f"  Watching {len(cfg['watchlist'])} wallets.")
    print("=" * 64)
    # Headless arm (Fly live worker): the USER types the exact phrase into
    # `flyctl secrets set LIVE_CONFIRM="…"` — still a human checkpoint, never
    # baked into config or code (LIVE_ROLLOUT rule 0.7). Known property: while
    # the secret stays set, restarts RE-ARM automatically (desired during the
    # days-long Phase 5 matrix); `flyctl secrets unset LIVE_CONFIRM` disarms
    # at the next boot, and any wrong value aborts instead of prompting.
    env_phrase = os.environ.get("LIVE_CONFIRM")
    if env_phrase is not None:
        if env_phrase.strip() == CONFIRM_PHRASE:
            print("confirmed via LIVE_CONFIRM env — armed.")
            return
        sys.exit("Aborted — LIVE_CONFIRM is set but does not match the phrase.")
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
