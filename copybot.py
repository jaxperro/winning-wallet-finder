#!/usr/bin/env python3
"""copybot.py — push-driven, live-capable Polymarket copy-trader.

Marries the two halves you already built:

  * webhook_receiver.py's **push** trigger — Alchemy's Address-Activity webhook
    POSTs here the instant a watched wallet transacts on Polygon. No polling.
  * archive/copytrade.py's hardened **execution engine** — paper + live
    (py-clob-client) executor, the full risk-block gates, price guard,
    no-backfill seeding, and proportional entry/exit mirroring.

Flow:
    Alchemy POST /alchemy
        → enrich the tx via the Polymarket data-API (market, side, price, size)
        → FollowFilter  — the "only the trades I actually want" gate
        → CopyTrader.handle_trade  — sizes + places under every risk cap

The execution engine is unchanged; this file only swaps the *trigger* from a
poll loop to a push, and inserts the follow-filter in front of it.

SAFETY — paper by default. Live trading needs ALL of:
    1. "mode": "live" in config.json,
    2. the --live flag,
    3. typing the confirmation phrase when prompted,
    4. py-clob-client installed + live creds in config "live".
The same hard caps (per-trade / daily / exposure / open positions / price band)
apply in both modes. This is real money in live mode.

Endpoints (stdlib http server, binds $PORT or 8080):
    POST /alchemy   ← point the Alchemy webhook here
    GET  /health    ← uptime check

Usage:
    python3 copybot.py                       # paper, listen for webhooks
    python3 copybot.py --conviction-from-sharps live/watch_sharps.json
    python3 copybot.py --test-wallet 0x..    # dry-run the pipeline on a wallet's
                                             #   latest trade, then exit (no server)
    python3 copybot.py --live                # live (needs mode:live + confirm)
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# reuse the proven execution engine as a library (kept in archive/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "archive"))
from copytrade import (  # noqa: E402
    CopyTrader, PaperExecutor, LiveExecutor, DEFAULT_CONFIG,
    load_json, save_json, new_state, recent_trades, confirm_live,
)
import smart_money as sm  # noqa: E402
from smart_money import SSL_CTX  # noqa: E402

CLOB_API = "https://clob.polymarket.com"

# ── on-chain resolution (ConditionalTokens payout vectors) ───────────────────
# The CLOB's `winner` flags NEVER populate for operator-resolved markets
# (in-play set winners, game O/Us — the whale class's staple), and data-api
# curPrice on a dead book reads 0.5 whether the market refunded or one side
# won (2026-07-06 audit: four resolved positions sat unsettleable for hours).
# The chain is the source every redeem actually pays from: payoutNumerators/
# payoutDenominator on the CTF contract — 1/0 winners, [0.5,0.5] refunds,
# denominator 0 = not resolved. Selectors are keccak4 of the signatures.
CTF_ADDR = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_SEL_DEN = "0xdd34de67"    # payoutDenominator(bytes32)
_SEL_NUM = "0x0504c814"    # payoutNumerators(bytes32,uint256)
_RPC_URL = None            # resolved once in main() (env/config), stays None without a key
_PAYOUTS = {}              # cond -> [p0, p1], cached once resolved (immutable)


def _eth_call(data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": CTF_ADDR, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(_RPC_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
        return json.loads(r.read())["result"]


def onchain_payouts(cond):
    """[payout_outcome0, payout_outcome1] for a resolved condition (order matches
    the CLOB market's tokens[] order), or None if unresolved / no RPC configured."""
    if not _RPC_URL or not cond:
        return None
    if cond in _PAYOUTS:
        return _PAYOUTS[cond]
    try:
        c = cond[2:].rjust(64, "0")
        den = int(_eth_call(_SEL_DEN + c), 16)
        if not den:
            return None
        nums = [int(_eth_call(_SEL_NUM + c + hex(i)[2:].rjust(64, "0")), 16)
                for i in (0, 1)]
        _PAYOUTS[cond] = [n / den for n in nums]
        return _PAYOUTS[cond]
    except Exception:
        return None

# follow-filter defaults — merged under cfg["follow"]; permissive so nothing is
# silently dropped until you opt in. The engine's risk caps bound everything
# regardless of these.
FOLLOW_DEFAULT = {
    "buy_only": True,          # SELLs only ever close a position we already hold
    "min_their_usd": 0.0,      # global conviction floor: ignore their bets below this
    "per_wallet_min_usd": {},  # {wallet: usd} — overrides the global floor per wallet
    "min_entry": 0.0,          # only copy entries with their fill price in this band
    "max_entry": 1.0,          #   (the archetype/copyability zone; 0.35–0.70 = value)
}

RECENT_TRADE_WINDOW_S = 600    # webhook just told us a trade happened; ignore stale
# defaults — config keys feed_path / fill_log override them (LIVE_ROLLOUT 1.1:
# a live-money run must never clobber the paper book's feed or fills ledger)
FILL_LOG = "copybot_fills.jsonl"          # append-only ledger of every copy fill + lag/slippage
FEED = os.path.join("live", "copybot_live.json")   # published feed the trading dashboard reads
FEED_PUSH_MIN_S = 120                     # min seconds between feed git-pushes (commit-on-change)

# Polymarket taker fee (Fee Structure V2, live since 2026-03-30):
#   fee = shares × rate × p × (1−p)
# charged on marketable BUYs and SELLs (we always take — FOK/market copies);
# redeeming a resolved position on-chain is fee-free. Rate is per market
# category — sports 0.03, finance/politics/tech 0.04, econ/culture/weather 0.05,
# crypto 0.07, geopolitics 0. The follow set is currently all-sports; override
# with "taker_fee_rate" in config.json if that changes.
TAKER_FEE_RATE = 0.03


def taker_fee(shares, price, rate):
    return shares * rate * price * (1.0 - price)


def log(m):
    print(f"{time.strftime('%H:%M:%S')}  {m}", flush=True)


# ── market resolution lookup (for settling held positions at resolution) ─────

_MKT_CACHE = {}                 # cond -> market dict, cached only once resolved
_MKT_LOCK = threading.Lock()


def _market(cond):
    with _MKT_LOCK:
        if cond in _MKT_CACHE:
            return _MKT_CACHE[cond]
    try:
        req = urllib.request.Request(f"{CLOB_API}/markets/{cond}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
            m = json.loads(r.read().decode()) or {}
    except Exception:
        return None
    # only cache a RESOLVED market (it won't change); re-check live ones each pass.
    # CLOB semantics: unresolved markets report winner=False on EVERY token —
    # resolution flips exactly one to True. Only a True winner means resolved.
    if any(t.get("winner") is True for t in (m.get("tokens") or [])):
        with _MKT_LOCK:
            _MKT_CACHE[cond] = m
    return m


def market_tokens(cond):
    m = _market(cond)
    return (m.get("tokens") or []) if m else None


def market_neg_risk(cond):
    """True if the market settles through the Neg-Risk adapter (different redeem
    path). Best-effort from the CLOB market metadata."""
    m = _market(cond)
    return bool(m.get("neg_risk")) if m else False


def resolution_price(token_id, cond, outcome=None):
    """Settled price of our held token — 1.0 won, 0.0 lost, 0.5 refunded
    (50/50 resolution: walkovers/abandonments), None if not resolved yet.

    Two tiers:
      1. CLOB `winner` flags — authoritative when present. CRITICAL semantics:
         winner=False on EVERY token of an UNRESOLVED market means "not yet",
         not "lost" (treating False as lost booked four winning live bets as
         -$180 of instant losses on 2026-07-02). Token-id match first —
         outcome labels are venue strings, the token id is the position.
      2. On-chain CTF payout vector — the flags NEVER populate for
         operator-resolved markets (in-play set winners / game O/Us) and
         never reflect 50/50 refunds; the chain records both (2026-07-06:
         four refunded Gojo/Heide positions sat locked for hours)."""
    toks = market_tokens(cond)
    if not toks:
        return None
    if any(t.get("winner") is True for t in toks):
        def winp(t):
            return 1.0 if t.get("winner") is True else 0.0
        for t in toks:
            if str(t.get("token_id")) == str(token_id):
                return winp(t)
        if outcome is not None:
            for t in toks:
                if t.get("outcome") == outcome:
                    return winp(t)
        return None
    # tier 2: no winner flag — ask the chain (only meaningful once trading closed)
    m = _market(cond)
    if not (m and m.get("closed")):
        return None
    po = onchain_payouts(cond)
    if po is None:
        return None
    for i, t in enumerate(toks[:2]):
        if str(t.get("token_id")) == str(token_id):
            return po[i]
    return None


class LedgerPaperExecutor(PaperExecutor):
    """Paper executor that records each fill (side/token/shares/price), so the run
    can track free cash, realized P&L, and lag/slippage. Paper fills at the live
    CLOB price the engine fetched — already capturing detection + price-drift lag,
    unlike the dashboard's zero-lag assumption."""

    def __init__(self):
        self.fills = []          # [{side, token, shares, price}] since last drain

    def buy(self, token_id, shares, price, meta):
        r = super().buy(token_id, shares, price, meta)
        self.fills.append({"side": "BUY", "token": token_id,
                           "shares": r["filled_shares"], "price": r["price"]})
        return r

    def sell(self, token_id, shares, price, meta):
        r = super().sell(token_id, shares, price, meta)
        self.fills.append({"side": "SELL", "token": token_id,
                           "shares": r["filled_shares"], "price": r["price"]})
        return r


class LedgerLiveExecutor(LiveExecutor):
    """Live executor with two production fixes over the base GTC executor:

      * **Marketable Fill-Or-Kill orders** (gap 3) — a copy either fills
        immediately at a crossing price or is cleanly killed, never left resting
        on the book half-filled. Order type is configurable (live.order_type:
        FOK all-or-nothing, or FAK fill-what-you-can-then-kill).
      * **Fill recording** — same ledger as paper, so cash/lag/slippage tracking
        works live too. filled_shares comes from the match response when present.
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.fills = []
        name = cfg.get("live", {}).get("order_type", "FOK").upper()
        self._otype = getattr(self._OrderType, name, self._OrderType.FOK)

    def _order(self, token_id, shares, price, side):
        args = self._OrderArgs(price=round(price, 3), size=round(shares, 2),
                               side=side, token_id=token_id)
        signed = self.client.create_order(args)
        resp = self.client.post_order(signed, self._otype)     # marketable FOK/FAK
        ok = bool(resp and resp.get("success", True))
        filled = shares if ok else 0.0
        for k in ("sizeMatched", "size_matched", "makingAmount"):   # use real fill if reported
            if resp and resp.get(k):
                try:
                    filled = float(resp[k]); break
                except (TypeError, ValueError):
                    pass
        return {"ok": ok and filled > 0, "filled_shares": filled, "price": price,
                "resp": resp, "paper": False}

    def buy(self, token_id, shares, price, meta):
        r = self._order(token_id, shares, price, self._BUY)
        if r["ok"]:
            self.fills.append({"side": "BUY", "token": token_id,
                               "shares": r["filled_shares"], "price": r["price"]})
        return r

    def sell(self, token_id, shares, price, meta):
        r = self._order(token_id, shares, price, self._SELL)
        if r["ok"]:
            self.fills.append({"side": "SELL", "token": token_id,
                               "shares": r["filled_shares"], "price": r["price"]})
        return r


# ── follow-filter — "just the ones I want to follow" ────────────────────────

class FollowFilter:
    """Decides whether one of their trades is worth handing to the engine.

    This is the selection gate that sits in front of execution. A BUY must be in
    your follow set, clear the conviction (stake-size) floor, and land in the
    entry-price band. A SELL always passes — the engine then mirrors it only if
    we actually hold the token.
    """

    def __init__(self, cfg):
        f = {**FOLLOW_DEFAULT, **cfg.get("follow", {})}
        self.buy_only = f["buy_only"]
        self.min_their_usd = float(f["min_their_usd"])
        self.per_wallet = {k.lower(): float(v) for k, v in f["per_wallet_min_usd"].items()}
        self.min_entry = float(f["min_entry"])
        self.max_entry = float(f["max_entry"])
        # whale-class wallets (follow.wallet_class) are followed on EVERY trade:
        # no conviction floor at all — their whole book is the signal (both
        # current whales win ~92% across all bets, so size-filtering them only
        # costs flow). The entry-price band still applies (execution guard).
        self.whales = {w.lower() for w, c in (f.get("wallet_class") or {}).items()
                       if c == "whale"}
        wl = cfg.get("watchlist") or [w["wallet"] for w in cfg.get("watch", [])]
        self.wallets = {w.lower() for w in wl}

    def floor(self, wallet):
        if wallet.lower() in self.whales:
            return 0.0
        return self.per_wallet.get(wallet.lower(), self.min_their_usd)

    def check(self, wallet, t):
        """-> (follow: bool, reason_if_skipped: str|None)."""
        if wallet.lower() not in self.wallets:
            return False, "wallet not in follow set"
        side = t.get("side")
        if side == "SELL":
            return True, None                       # engine exits only if we hold
        if side != "BUY":
            return False, f"side {side}"
        usd = t.get("usdcSize") or t.get("size", 0) * t.get("price", 0)
        fl = self.floor(wallet)
        if usd < fl:
            return False, f"${usd:,.0f} < conviction floor ${fl:,.0f}"
        p = t.get("price", 0)
        if not (self.min_entry <= p <= self.max_entry):
            return False, f"entry {p:.2f} outside [{self.min_entry:.2f},{self.max_entry:.2f}]"
        return True, None

    def describe(self):
        pw = f" · {len(self.per_wallet)} per-wallet floors" if self.per_wallet else ""
        wh = f" · {len(self.whales)} whales follow-all" if self.whales else ""
        return (f"follow filter · {'BUY-only' if self.buy_only else 'BUY+SELL'} · "
                f"conviction ≥ ${self.min_their_usd:,.0f}{pw}{wh} · "
                f"entry [{self.min_entry:.2f},{self.max_entry:.2f}]")


# ── the push → filter → execute bridge ──────────────────────────────────────

class Copybot:
    def __init__(self, cfg, engine, filt, redeemer=None):
        self.cfg = cfg
        self.engine = engine
        self.filt = filt
        self.redeemer = redeemer       # gap 2: on-chain redeem of resolved positions (live)
        # trade-by-trade Discord pings retired 2026-07-04: the only Discord
        # output now is the daily sharp digest (live/discord_daily.py)
        self.names = {}
        for w in cfg.get("watch", []):
            self.names[w["wallet"].lower()] = w.get("name", w["wallet"][:10])
        self.skipped = set()       # tx we've already evaluated-and-skipped (no re-log)
        self.negrisk_warned = set()    # conds we've already warned need manual redeem
        self.lock = threading.Lock()   # serialize engine/settle access (webhook is threaded)
        self.here = os.path.dirname(os.path.abspath(__file__))
        self.feed_path = cfg.get("feed_path", FEED)
        self.fill_log = cfg.get("fill_log", FILL_LOG)
        # persisted across restarts via the engine's state file
        self.conds = engine.state.setdefault("conds", {})   # token_id -> conditionId (open positions)
        engine.state.setdefault("cash", cfg["bankroll_usd"])  # free cash (recycles on sell/resolution)
        engine.state.setdefault("lag", {"n": 0, "sum_s": 0.0, "sum_slip_pct": 0.0})
        engine.state.setdefault("fees_paid", 0.0)
        self.fee_rate = float(cfg.get("taker_fee_rate", TAKER_FEE_RATE))

    def _drain_fills(self):
        """Apply cash flows from any fills the engine just made; return the BUY
        fills so the caller can log lag/slippage against the source trade.
        Every marketable fill (buy or sell) pays the taker fee — in live mode the
        protocol charges it at match time, so the paper book must charge it too or
        it overstates the edge."""
        ex = self.engine.ex
        buys = []
        if hasattr(ex, "fills") and ex.fills:
            for f in ex.fills:
                sign = -1 if f["side"] == "BUY" else 1
                fee = taker_fee(f["shares"], f["price"], self.fee_rate)
                f["fee"] = round(fee, 4)
                self.engine.state["cash"] += sign * f["shares"] * f["price"] - fee
                self.engine.state["fees_paid"] = self.engine.state.get("fees_paid", 0.0) + fee
                if f["side"] == "BUY":
                    buys.append(f)
                else:
                    # link the mirror-exit to its bet record so the sold leg shows
                    # up in per-bet P&L (feed) — cash above is already correct.
                    # Accumulate UNROUNDED (rounding per-fill drifted the ledger a
                    # few cents per trim); the feed rounds at render time.
                    b = self.engine.state.get("bets", {}).get(f["token"])
                    if b:
                        b["sold_shares"] = b.get("sold_shares", 0) + f["shares"]
                        b["sold_proceeds"] = (b.get("sold_proceeds", 0)
                                              + f["shares"] * f["price"] - fee)
                    # sells go to the fills ledger too — the audit had no sell
                    # trail to reconcile the ledger against
                    try:
                        with open(os.path.join(self.here, self.fill_log), "a") as fh:
                            fh.write(json.dumps({
                                "ts": round(time.time(), 1), "side": "SELL",
                                "token": f["token"], "shares": round(f["shares"], 4),
                                "price": round(f["price"], 4), "fee": f["fee"],
                                "mode": "live" if self.engine.ex.live else "paper",
                            }) + "\n")
                    except Exception:
                        pass
            ex.fills.clear()
        return buys

    def ledger_drift(self):
        """cash minus what the ledger implies it should be. The invariant:
        cash = bank + Σadjustments + Σsettled-bet P&L + Σopen(-cost-fee+sold).
        Non-zero means a booking bug — the audit found +$15.45 of Jul-5
        accounting-migration residue this check would have caught same-day."""
        st = self.engine.state
        bets = st.get("bets", {})
        adj = sum(a["amount"] for a in st.get("adjustments", []))
        realized = sum(b["pnl"] for b in bets.values() if b.get("pnl") is not None)
        # in-flight flows keyed on "no P&L booked yet", NOT on my_pos membership:
        # a fully-exited bet leaves my_pos immediately but only gets its pnl at
        # the next write_feed reconcile — keying on my_pos made the invariant
        # jump during exactly that window
        flows = sum(-(b["cost"] + (b.get("fee") or 0)) + (b.get("sold_proceeds") or 0)
                    for b in bets.values() if b.get("pnl") is None)
        return st.get("cash", 0) - (self.cfg["bankroll_usd"] + adj + realized + flows)

    def _record_lag(self, wallet, t, fill):
        """Gap 1 — log the detection lag and price slippage of a copy: their fill
        time/price vs ours. Appends to copybot_fills.jsonl and tracks running
        averages for the summary, so the live cost of lag is measurable."""
        now = time.time()
        their_ts = t.get("timestamp", 0) or 0
        detect_s = (now - their_ts) if their_ts else None
        their_p = t.get("price", 0) or 0
        my_p = fill["price"]
        slip_pct = (my_p - their_p) / their_p if their_p else 0.0
        rec = {
            "ts": round(now, 1), "wallet": wallet,
            "name": self.names.get(wallet.lower(), wallet[:10]),
            "outcome": t.get("outcome"), "title": (t.get("title") or "")[:80],
            "detect_lag_s": round(detect_s, 1) if detect_s is not None else None,
            "their_price": round(their_p, 4), "my_price": round(my_p, 4),
            "slippage_pct": round(slip_pct, 4),
            "shares": round(fill["shares"], 2), "cost": round(fill["shares"] * my_p, 2),
            "fee": fill.get("fee", 0),
            "mode": "live" if self.engine.ex.live else "paper",
        }
        try:
            with open(os.path.join(self.here, self.fill_log), "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        if detect_s is not None:
            lag = self.engine.state["lag"]
            lag["n"] += 1
            lag["sum_s"] += detect_s
            lag["sum_slip_pct"] += slip_pct
        # record the placed bet for the live dashboard feed. AGGREGATE across
        # fills: an ADD to an existing open position must accumulate shares/
        # cost/fees, not overwrite the record with just the last fill — that
        # made Cost show one fill while P&L settled the whole position.
        bets = self.engine.state.setdefault("bets", {})
        prev = bets.get(fill["token"])
        if prev and prev.get("status") == "open":
            # accumulate UNROUNDED (per-fill rounding drifted the ledger); the
            # feed rounds at render time
            sh = prev["shares"] + fill["shares"]
            cost = prev["cost"] + fill["shares"] * my_p
            prev.update(shares=sh, cost=cost,
                        my_price=(cost / sh) if sh else prev["my_price"],
                        fee=(prev.get("fee") or 0) + fill.get("fee", 0))
        else:
            bets[fill["token"]] = {
                "token": fill["token"], "wallet": wallet,
                "name": self.names.get(wallet.lower(), wallet[:10]),
                "outcome": t.get("outcome"), "title": (t.get("title") or "")[:90],
                "their_price": round(their_p, 4), "my_price": round(my_p, 4),
                "slippage_pct": round(slip_pct, 4),
                "shares": round(fill["shares"], 2), "cost": round(fill["shares"] * my_p, 2),
                "fee": fill.get("fee", 0),
                "opened": int(their_ts or now), "status": "open",
                "exit_price": None, "pnl": None, "settled": None,
            }
        log(f"  ↳ lag {('%.0fs' % detect_s) if detect_s is not None else '?'} · "
            f"their {their_p:.3f} → mine {my_p:.3f} ({slip_pct:+.1%} slippage)")

    def write_feed(self):
        """Publish the bot's live book to live/copybot_live.json — the feed the
        top of jaxperro.com/trading reads. Reconciles any open bet no longer held
        (mirror-sold) to 'closed'."""
        st = self.engine.state
        bets = st.setdefault("bets", {})
        mp = st["my_pos"]
        for tok, b in bets.items():
            if b["status"] == "open" and tok not in mp:
                b["status"] = "closed"
                b["settled"] = b["settled"] or int(time.time())
                # fully mirror-sold: realize the sold leg on the bet record
                # (cash was already credited fill-by-fill in _drain_fills)
                if b.get("sold_proceeds") is not None and b.get("pnl") is None:
                    b["pnl"] = round(b["sold_proceeds"] - b["cost"] - (b.get("fee") or 0), 2)
                    if b.get("sold_shares"):
                        b["exit_price"] = round(b["sold_proceeds"] / b["sold_shares"], 4)
        bank = self.cfg["bankroll_usd"]
        exp = self.engine.open_exposure()
        cash = st.get("cash", bank)
        lag = st.get("lag", {})
        missed = st.get("missed", [])
        for m in missed:                            # display names for the feed
            m["name"] = self.names.get((m.get("wallet") or "").lower(),
                                       (m.get("wallet") or "")[:10])
        feed = {
            "mode": "live" if self.engine.ex.live else "paper",
            "bankroll": bank, "stake": round(self.engine.stake_usd(), 2),
            "stake_pct": self.cfg["bankroll_pct"],
            "event_cap": self.engine.risk.get("max_per_event"),
            "hwm": round(st.get("hwm", 0.0), 2),
            "cash": round(cash, 2), "deployed": round(exp, 2),
            "reserve": round(st.get("reserve", 0.0), 2),   # banked profit, never bet
            # realized excludes audited ledger adjustments (they're bookkeeping
            # corrections, not P&L) and the feed carries the drift so the
            # dashboard shows a broken ledger instead of hiding one
            "realized": round(cash + exp + st.get("reserve", 0.0) - bank
                              - sum(a["amount"] for a in st.get("adjustments", [])), 2),
            "adjustments": round(sum(a["amount"] for a in st.get("adjustments", [])), 2),
            "ledger_drift": round(self.ledger_drift(), 2),
            "open_count": len(mp),
            "fees_paid": round(st.get("fees_paid", 0.0), 2),
            "fee_rate": self.fee_rate,
            "lag": {"n": lag.get("n", 0),
                    "avg_s": round(lag["sum_s"] / lag["n"], 1) if lag.get("n") else None,
                    "avg_slip_pct": round(lag["sum_slip_pct"] / lag["n"], 4) if lag.get("n") else None},
            "wallets": [w.get("name", w["wallet"][:10]) for w in self.cfg.get("watch", [])],
            "classes": {w.get("name", w["wallet"][:10]): self.engine.wallet_class(w["wallet"])
                        for w in self.cfg.get("watch", [])},
            "floors": {self.names.get(a, a[:10]): v
                       for a, v in self.filt.per_wallet.items()},
            # state accumulators are unrounded — round display fields at render
            "bets": [{**b, **{k: round(b[k], 2) for k in
                              ("shares", "cost", "sold_shares", "sold_proceeds", "fee")
                              if b.get(k) is not None},
                      **({"my_price": round(b["my_price"], 4)} if b.get("my_price") else {})}
                     for b in sorted(bets.values(),
                                     key=lambda b: b.get("settled") or b.get("opened") or 0,
                                     reverse=True)[:100]],
            "missed": sorted(missed,
                             key=lambda m: m.get("settled") or m.get("ts") or 0,
                             reverse=True)[:60],
            "missed_pnl": round(sum(m["pnl"] for m in missed
                                    if m.get("pnl") is not None), 2),
        }
        # only (re)write — and so only commit — when the meaningful content changed,
        # not on every poll. The "updated" stamp advances only on real change, so the
        # scheduled runner doesn't spam a commit every 5 minutes.
        sig = hashlib.md5(json.dumps(feed, sort_keys=True).encode()).hexdigest()
        if sig == self.engine.state.get("feed_sig"):
            return
        self.engine.state["feed_sig"] = sig
        feed["updated"] = int(time.time())
        path = os.path.join(self.here, self.feed_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        json.dump(feed, open(tmp, "w"), indent=1)
        os.replace(tmp, path)

    def publish_feed(self):
        """Commit + push the feed, the state file, and the fills ledger so (a) the
        public dashboard reads the current book, (b) the book survives machine
        loss, and (c) the per-fill lag/slippage evidence is preserved — the Actions
        runner used to discard copybot_fills.jsonl on every run. Committing state
        here (not just the feed) also keeps `git pull --rebase` from wedging on a
        dirty tracked file now that this local poller is the sole runner.
        Throttled and commit-on-change. Best-effort — never crashes the run."""
        st = self.engine.state
        now = time.time()
        if now - st.get("feed_pushed_at", 0) < FEED_PUSH_MIN_S:
            return
        st["feed_pushed_at"] = now
        try:
            import subprocess
            repo = self.here
            # publish only when the FEED itself changed (a bet placed/settled) —
            # the state file churns bookkeeping every cycle and would otherwise
            # commit every FEED_PUSH_MIN_S forever.
            if subprocess.run(["git", "-C", repo, "diff", "--quiet", "--", self.feed_path],
                              capture_output=True).returncode == 0:
                return
            paths = [p for p in (self.feed_path, self.engine.state_path, self.fill_log)
                     if os.path.exists(os.path.join(repo, p))]
            subprocess.run(["git", "-C", repo, "add", "-f"] + paths, capture_output=True)
            if subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"],
                              capture_output=True).returncode == 0:
                return                                  # nothing changed
            c = subprocess.run(["git", "-C", repo, "commit", "-q", "-m",
                                "copybot: live paper feed [skip ci]"],
                               capture_output=True, text=True)
            if c.returncode != 0:
                return
            subprocess.run(["git", "-C", repo, "pull", "--rebase", "--autostash", "-q",
                            "origin", "main"], capture_output=True)
            p = subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"],
                               capture_output=True, text=True)
            log("published live feed → dashboard" if p.returncode == 0
                else "feed push failed (will retry next change)")
        except Exception as e:
            log(f"feed publish error: {e}")

    def seed(self):
        """Load each watched wallet's current positions so exits mirror correctly
        and we never backfill a position they held before we started."""
        for wallet in self.cfg.get("watchlist", []):
            self.engine.seed_wallet(wallet)

    def baseline(self):
        """Mark every currently-visible trade as already seen, so a poll run only
        copies trades that happen AFTER startup (the forward equivalent of the
        dashboard's June-1 START — no retro-copying of history)."""
        n = 0
        for wallet in self.cfg.get("watchlist", []):
            for t in recent_trades(wallet):
                tx = t.get("transactionHash")
                if tx and tx not in self.engine.seen:
                    self.engine.seen.add(tx)
                    n += 1
        self.engine.persist()
        log(f"baseline: {n} existing trades marked seen — copying only NEW trades from now")

    _chain_bal = (0.0, None)     # (checked_at, usdc) — cached; poll ≤1/min

    def chain_cash_gap(self):
        """LIVE only (LIVE_ROLLOUT 1.4): book cash minus the exchange's actual
        USDC balance — the real-money version of ledger_drift. None in paper
        mode or on a failed fetch (never treat silence as a number)."""
        if not self.engine.ex.live:
            return None
        now = time.time()
        ts, bal = self._chain_bal
        if now - ts > 60:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                r = self.engine.ex.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                bal = int(r.get("balance", 0)) / 1e6
                self._chain_bal = (now, bal)
            except Exception:
                return None
        return None if bal is None else self.engine.state.get("cash", 0) - bal

    def summary(self, cycle):
        bank = self.cfg["bankroll_usd"]
        stake = self.engine.stake_usd()       # dynamic: pct of current equity
        exp = self.engine.open_exposure()
        cash = self.engine.state.get("cash", bank)
        reserve = self.engine.state.get("reserve", 0.0)
        adj = sum(a["amount"] for a in self.engine.state.get("adjustments", []))
        realized = cash + exp + reserve - bank - adj   # see _drain_fills / settle_resolved
        n = len(self.engine.state["my_pos"])
        lag = self.engine.state.get("lag", {})
        lagstr = ""
        if lag.get("n"):
            lagstr = (f" · {lag['n']} copies avg lag {lag['sum_s']/lag['n']:.0f}s "
                      f"slip {lag['sum_slip_pct']/lag['n']:+.1%}")
        bankstr = f" · banked ${reserve:,.0f}" if reserve else ""
        drift = self.ledger_drift()
        driftstr = f" · ⚠ LEDGER DRIFT ${drift:+.2f}" if abs(drift) > 0.01 else ""
        gap = self.chain_cash_gap()
        if gap is not None and abs(gap) > 1.0:      # ≥$1: beyond fee/rounding float
            driftstr += f" · ⚠ CASH≠CHAIN ${gap:+.2f}"
        log(f"[{cycle}] open {n} · deployed ${exp:,.0f} · free ${cash:,.0f}/${bank:,.0f}"
            f"{bankstr} · realized ${realized:+,.2f}{lagstr}{driftstr}"
            + (f" · CAN'T OPEN (free < ${stake:,.0f} stake — bets missed)"
               if cash < stake else ""))

    def on_wallet_activity(self, wallet, ignore_stale=False):
        """A watched wallet just transacted — pull its latest trades and route any
        new, recent one through the filter and (if it passes) the engine."""
        name = self.names.get(wallet.lower(), wallet[:10] + "…")
        trades = recent_trades(wallet)
        # oldest-first so the engine's position math stays causal
        for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
            tx = t.get("transactionHash")
            if not tx or tx in self.engine.seen or tx in self.skipped:
                continue
            if not ignore_stale and time.time() - t.get("timestamp", 0) > RECENT_TRADE_WINDOW_S:
                self.skipped.add(tx)               # stale — the webhook is about a newer tx
                continue
            follow, reason = self.filt.check(wallet, t)
            if not follow:
                self.skipped.add(tx)
                log(f"skip {name}: {t.get('side')} {t.get('outcome','?')} "
                    f"@ {t.get('price',0):.3f} — {reason}")
                continue
            log(f"FOLLOW {name}: {t.get('side')} {t.get('outcome','?')} "
                f"@ {t.get('price',0):.3f} (${t.get('usdcSize',0):,.0f})")
            with self.lock:
                self.engine.handle_trade(wallet, t)   # sizes, gates, places (paper/live)
                tok = t.get("asset")
                if tok in self.engine.state["my_pos"] and tok not in self.conds:
                    self.conds[tok] = t.get("conditionId")   # remember for settling
                for f in self._drain_fills():
                    if f["token"] == tok:                    # the fill from this copy
                        self._record_lag(wallet, t, f)

    def reconcile_exits(self):
        """Exits the signal made while we weren't listening. RECENT_TRADE_WINDOW_S
        (10 min) skips stale trades, so a SELL during downtime/restart never
        mirrors — the 2026-07-06 audit found the bot holding McCormick/Sakamoto
        a day after the whale had sold it for +$4.2k. For every open copy, ask
        the data-api (market-filtered, so no pagination cap) whether the signal
        still holds the token: gone + market still trading -> mirror-exit ALL
        of ours now at the live price; gone + market closed -> leave it for
        settle_resolved (selling into a dead book would book winners as
        scratches). Runs at boot and every backstop poll."""
        with self.lock:
            mp = self.engine.state["my_pos"]
            checks = []
            for token in list(mp):
                b = self.engine.state.get("bets", {}).get(token)
                cond = self.conds.get(token)
                if b and b.get("wallet") and cond:
                    checks.append((token, cond, b["wallet"]))
            for token, cond, wallet in checks:
                if token not in mp:
                    continue                        # settled/sold earlier this pass
                # FAIL-SAFE: get_json returns None on failure and [] on a real
                # empty — silence must NEVER read as "they exited" (the dry-run
                # of this very fix tried to liquidate the whole book when the
                # API blipped). An exit needs three affirmative facts:
                #   1. their open positions on this market: fetched AND empty,
                #   2. their closed positions: fetched AND contain our token
                #      (they demonstrably had it and closed it),
                #   3. the market itself: fetched AND still trading.
                ps = sm.get_json("/positions", {"user": wallet, "market": cond,
                                                "limit": 10, "sizeThreshold": 0})
                if ps is None:
                    continue                        # API failure — retry next pass
                held = sum(p.get("size", 0) or 0 for p in ps
                           if str(p.get("asset")) == str(token))
                book = self.engine.state["their_pos"].setdefault(wallet, {})
                if held > 0:
                    book[token] = held              # refresh sell-fraction basis
                    continue
                cps = sm.get_json("/closed-positions", {"user": wallet, "market": cond,
                                                        "limit": 10})
                if cps is None or not any(str(p.get("asset")) == str(token) for p in cps):
                    continue                        # can't corroborate the exit
                m = _market(cond)
                if not m:
                    continue                        # market state unknown — don't act
                book[token] = 0.0
                if m.get("closed"):
                    continue                        # resolved -> settle path pays truth
                pos = mp.get(token)
                if not pos:
                    continue
                name = self.names.get(wallet.lower(), wallet[:10])
                log(f"reconcile: {name} exited {pos.get('title','?')[:42]} while we "
                    f"weren't listening — mirror-exiting {pos['shares']:.1f}sh now")
                # their_prev<=0 -> frac 1.0: sell everything we hold
                self.engine._handle_their_sell(
                    token, 0, 0, f"{pos.get('outcome','?')} · {pos.get('title','?')[:42]}")
                self._drain_fills()                 # book the sell's cash + sold-leg
                # LIVE_ROLLOUT 1.6 — a FAK sell on a thin book can fill 0 and
                # the position silently rides to resolution. This pass re-fires
                # every backstop poll; count the attempts and raise the alarm
                # once it looks stuck (still held after N tries).
                if token in mp:
                    att = self.engine.state.setdefault("exit_attempts", {})
                    att[token] = att.get(token, 0) + 1
                    if att[token] == 10:
                        self.engine.alert(
                            f"⚠ EXIT STUCK: {pos.get('title','?')[:42]} — "
                            f"{att[token]} mirror-exit attempts unfilled; "
                            f"check the book / exit manually in the UI.")
                else:
                    self.engine.state.get("exit_attempts", {}).pop(token, None)
            self.engine.persist()

    def reconcile_entries(self):
        """Entries the signal made while we weren't listening — the mirror of
        reconcile_exits. Every boot baselines history (no retro-copying), so a
        trade during downtime vanishes: not copied, not even MISSED. The
        backtest reads positions (state) while the bot reads trades (events),
        so downtime opens showed up only in the backtest — that asymmetry is
        how this hole was found (2026-07-07: 9 ArbTrader positions opened into
        a dead listener during the Fly trial-expiry crash-loop). For each
        followed wallet: any CURRENT position that (a) isn't seeded, held, in
        bet history, or already recorded, and (b) clears the follow filter,
        becomes a missed bet — reason 'bot offline'. Never copied: the entry
        is stale by definition; record the truth and settle it hypothetically."""
        with self.lock:
            st = self.engine.state
            bets = st.get("bets", {})
            missed_toks = {m["token"] for m in st.get("missed", [])}
            for w in self.cfg.get("watchlist", []):
                ps = sm.get_json("/positions", {"user": w, "limit": 500,
                                                "sizeThreshold": 0})
                if ps is None:
                    continue                      # API failure — retry next pass
                seeded = set(st.get("seed_tokens", {}).get(w, []))
                name = self.names.get(w.lower(), w[:10])
                for p in ps:
                    tok = p.get("asset")
                    if (not tok or tok in seeded or tok in st["my_pos"]
                            or tok in bets or tok in missed_toks):
                        continue
                    iv = p.get("initialValue") or 0
                    if iv < self.engine.risk.get("min_order_usd", 5.0):
                        continue                  # literal dust — not a signal
                    trade = {"side": "BUY", "usdcSize": iv,
                             "price": p.get("avgPrice", 0) or 0,
                             "outcome": p.get("outcome"), "title": p.get("title")}
                    ok, _ = self.filt.check(w, trade)
                    if not ok:
                        continue
                    want = self.engine.stake_usd(w, iv)
                    self.engine.record_miss(
                        w, tok, p.get("conditionId"), p.get("title") or "",
                        p.get("outcome") or "", trade["price"], want,
                        "bot offline (entered while down)")
                    missed_toks.add(tok)
                    log(f"reconcile: {name} entered {(p.get('title') or '?')[:42]} "
                        f"while we weren't listening — recorded as missed")
            self.engine.persist()

    def settle_resolved(self):
        """Free capital like the dashboard: when an open position's market has
        resolved, settle it at the winner price (1/0), recycle the cash, and tally
        realized P&L. This is the resolution path the engine's sell-only mirror
        lacks — without it the $1k never recycles for held-to-resolution bets."""
        with self.lock:
            mp = self.engine.state["my_pos"]
            for token in list(mp):
                cond = self.conds.get(token)
                if not cond:
                    continue
                wp = resolution_price(token, cond, mp[token].get("outcome"))
                if wp is None:
                    continue                            # not resolved yet
                pos = mp[token]
                # gap 2 — LIVE: redeem winning shares on-chain so the freed USDC is
                # actually back in the wallet (paper just recycles a number). Losers
                # are worth $0, no redeem; 50/50 refunds redeem at $0.50/share. If the
                # redeem fails, keep the position and retry next pass.
                if self.redeemer and wp > 0:
                    neg = market_neg_risk(cond)
                    if neg and cond not in self.negrisk_warned:
                        self.negrisk_warned.add(cond)
                        self.engine.alert(f"⚠ {pos.get('title','?')[:42]} is a NEG-RISK "
                                          f"market — auto-redeem unsupported; redeem "
                                          f"manually in the Polymarket UI.")
                    if not neg:
                        ok, info = self.redeemer.try_redeem(cond)
                        if not ok:
                            log(f"  ⚠ redeem failed ({info}) — keeping position, will retry")
                            continue
                        log(f"  ↳ redeemed on-chain: {info}")
                proceeds = pos["shares"] * wp           # redeem is fee-free on-chain
                b = self.engine.state.get("bets", {}).get(token)
                fee_in = (b or {}).get("fee") or 0      # entry taker fee, already off cash
                # a TRIMmed bet realized part of itself pre-resolution: count the
                # sold leg and charge the ORIGINAL cost (pos["cost"] was reduced
                # proportionally at each trim), so per-bet pnl = whole-bet truth
                sold = (b or {}).get("sold_proceeds") or 0
                base_cost = b["cost"] if b else pos["cost"]
                pnl = proceeds + sold - base_cost - fee_in
                self.engine.state["cash"] += proceeds   # recycle freed capital
                status = "won" if wp > 0.5 else "lost" if wp < 0.5 else "refund"
                if b:
                    b.update(status=status,
                             exit_price=wp, pnl=round(pnl, 2), settled=int(time.time()))
                del mp[token]
                self.conds.pop(token, None)
                tag = {"won": "WON ✅", "lost": "LOST ❌", "refund": "REFUND ↩ (50/50)"}[status]
                label = f"{pos.get('outcome','?')} · {pos.get('title','?')[:42]}"
                self.engine.alert(
                    f"SETTLE {label} — {tag} {pos['shares']:.0f}sh -> "
                    f"${proceeds:.2f} (P&L ${pnl:+.2f})",
                    discord_text=(f"🏁 **SETTLE** {tag}\n{label}\n"
                                  f"${pos['cost']:.2f} cost -> ${proceeds:.2f} "
                                  f"= **${pnl:+.2f}**"))
            # settle MISSED bets hypothetically: what the skipped stake would have
            # returned (entry fee included; redeem free) — the live counterpart of
            # the backtest's Missed P&L, "the cost of a small bankroll".
            for m in self.engine.state.get("missed", []):
                if m.get("status") != "open" or not m.get("cond"):
                    continue
                wp = resolution_price(m["token"], m["cond"], m.get("outcome"))
                if wp is None:
                    continue
                p = m.get("price") or 0.5
                fee = taker_fee(m["stake"] / p, p, self.fee_rate)
                pnl = (m["stake"] / p) * wp - m["stake"] - fee
                m.update(status=("won" if wp > 0.5 else "lost" if wp < 0.5 else "refund"),
                         pnl=round(pnl, 2), settled=int(time.time()))
            self.engine.persist()


# ── Alchemy webhook plumbing ────────────────────────────────────────────────

def verify(raw, sig, signing_key):
    if not signing_key:
        return True                                 # verification off if unconfigured
    digest = hmac.new(signing_key.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig or "")


def addresses_in_payload(payload, watched):
    out = set()
    for a in payload.get("event", {}).get("activity", []):
        for k in ("fromAddress", "toAddress"):
            v = a.get(k)
            if v and v.lower() in watched:
                out.add(v.lower())
    return out


def make_handler(bot, signing_key):
    watched = {w.lower() for w in bot.cfg.get("watchlist", [])}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body="ok"):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            self._send(200, "alive" if self.path == "/health" else "copybot")

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not verify(raw, self.headers.get("x-alchemy-signature"), signing_key):
                log("⚠ bad signature — rejected")
                return self._send(401, "bad signature")
            self._send(200)                         # ack fast; Alchemy retries non-2xx
            try:
                bot.settle_resolved()               # recycle any newly-resolved positions
                payload = json.loads(raw or b"{}")
                for w in addresses_in_payload(payload, watched):
                    bot.on_wallet_activity(w)
                bot.write_feed()                    # refresh + publish the dashboard feed
                bot.publish_feed()
            except Exception as e:
                log(f"handler error: {e}")

        def log_message(self, *a):
            pass

    return Handler


# ── config / cli ────────────────────────────────────────────────────────────

def _pctl80(vals):
    s = sorted(v for v in vals if v and v > 0)
    if not s:
        return None
    k = (len(s) - 1) * 0.8
    f = int(k)
    return s[f] if f + 1 >= len(s) else s[f] + (s[f + 1] - s[f]) * (k - f)


def derive_floor(wallet):
    """Cacheless p80 conviction floor: the 80th percentile of the wallet's
    recent position stakes from the data-api (last 500 closed + current open).
    The Railway worker has no cache.duckdb, so this is how an auto floor gets
    computed at boot; it refreshes on every restart."""
    sizes = []
    for p in (sm.get_json("/closed-positions", {"user": wallet, "limit": 500,
                          "sortBy": "TIMESTAMP", "sortDirection": "DESC"}) or []) +              (sm.get_json("/positions", {"user": wallet, "limit": 500,
                          "sizeThreshold": 0}) or []):
        v = p.get("initialValue") or 0
        if v > 0:
            sizes.append(v)
    return _pctl80(sizes)


def normalize_follow_config(cfg):
    """Expand the compact follow format into the legacy structures the filter
    and engine read. One entry per wallet:

        "wallets": [{"wallet": "0x…", "name": "…", "class": "volume"|"whale",
                     "floor": 123.0 (optional)}]

    -> watchlist / watch / follow.wallet_class / follow.per_wallet_min_usd.
    Old-format configs (no "wallets" key, e.g. config.live.json) pass through
    untouched. Floors: explicit "floor" wins; whales need none (follow-all);
    otherwise derived from the data-api at boot (see derive_floor)."""
    ws = cfg.get("wallets")
    if not ws:
        return cfg
    cfg["watchlist"] = [w["wallet"] for w in ws]
    cfg["watch"] = [{"wallet": w["wallet"], "name": w.get("name", w["wallet"][:10])}
                    for w in ws]
    f = cfg.setdefault("follow", {})
    f["wallet_class"] = {w["wallet"].lower(): w.get("class", "volume") for w in ws}
    floors = {}
    for w in ws:
        addr = w["wallet"].lower()
        name = w.get("name", addr[:10])
        if w.get("class", "volume") == "whale":
            continue                            # follow-all: floors ignored
        if w.get("floor") is not None:
            floors[addr] = float(w["floor"])
        else:
            fl = derive_floor(w["wallet"])
            if fl is not None:
                floors[addr] = round(fl, 2)
                log(f"floor[{name}] auto p80 = ${fl:,.0f}")
            else:
                log(f"⚠ floor[{name}] underivable — falls back to the global "
                    f"min_their_usd ${f.get('min_their_usd', 0):,.0f}")
    f["per_wallet_min_usd"] = floors
    return cfg


def load_cfg(path):
    if not os.path.exists(path):
        sys.exit(f"No config at {path}.")
    cfg = {**DEFAULT_CONFIG, **load_json(path, {})}
    cfg["risk"] = {**DEFAULT_CONFIG["risk"], **cfg.get("risk", {})}
    cfg["live"] = {**DEFAULT_CONFIG["live"], **cfg.get("live", {})}
    # env overrides for the credentials (LIVE_ROLLOUT 1.2): the Fly worker
    # clones the repo, and config.live.json is gitignored — secrets reach a
    # cloud box as env vars only. Env wins over file so a deployed worker
    # can't accidentally trade an old key from a stray config.
    for env, key in (("LIVE_PRIVATE_KEY", "private_key"),
                     ("LIVE_FUNDER_ADDRESS", "funder_address"),
                     ("LIVE_SIGNATURE_TYPE", "signature_type")):
        v = os.environ.get(env)
        if v:
            cfg["live"][key] = int(v) if key == "signature_type" else v
    cfg["follow"] = {**FOLLOW_DEFAULT, **cfg.get("follow", {})}
    normalize_follow_config(cfg)
    # accept either "watchlist" (addresses) or "watch" ([{wallet,name}]); fill the gap
    if not cfg.get("watchlist") and cfg.get("watch"):
        cfg["watchlist"] = [w["wallet"] for w in cfg["watch"]]
    if not cfg.get("watch") and cfg.get("watchlist"):
        cfg["watch"] = [{"wallet": w, "name": w[:10]} for w in cfg["watchlist"]]
    return cfg


def conviction_floors_from_sharps(path):
    """Per-wallet conviction floor = that wallet's avg bet size, so only their
    above-average ('conviction') bets get copied — the edge per the research."""
    rows = load_json(path, [])
    return {r["wallet"].lower(): float(r["avg_bet"])
            for r in rows if r.get("avg_bet")}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", default="copybot_state.json")
    ap.add_argument("--live", action="store_true",
                    help="enable live trading (also needs mode:live in config)")
    ap.add_argument("--conviction-from-sharps", metavar="PATH",
                    help="set each wallet's conviction floor to its avg_bet from "
                         "this sharps json (e.g. live/watch_sharps.json)")
    ap.add_argument("--test-wallet", metavar="0x...",
                    help="dry-run: route this wallet's latest trade through the "
                         "pipeline once (paper), print the decision, then exit")
    ap.add_argument("--poll", type=int, metavar="SECONDS",
                    help="run forward by polling every SECONDS (instead of waiting "
                         "for Alchemy webhooks) — lets a paper run go without "
                         "deploying the webhook. Use < 600 so no trade is missed.")
    ap.add_argument("--poll-once", action="store_true",
                    help="run ONE poll pass (settle, copy new trades, write feed) "
                         "then exit — for a scheduled runner (GitHub Actions cron). "
                         "State persists across runs via --state.")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    if args.conviction_from_sharps:
        floors = conviction_floors_from_sharps(args.conviction_from_sharps)
        cfg["follow"]["per_wallet_min_usd"] = {
            **cfg["follow"].get("per_wallet_min_usd", {}), **floors}
        log(f"loaded {len(floors)} per-wallet conviction floors from "
            f"{args.conviction_from_sharps}")

    want_live = args.live and cfg.get("mode") == "live"
    if args.live and cfg.get("mode") != "live":
        sys.exit('--live given but config "mode" is not "live". Refusing to trade.')

    state = load_json(args.state, new_state())
    redeemer = None
    if want_live:
        # LIVE_ROLLOUT 1.5 — the geo-gate is FATAL for real money: an
        # unauthenticated dummy order separates geo-blocked (403 "restricted
        # in your region") from allowed (auth/validation error). Paper mode
        # only reads, so it merely warns (host/start.sh); live must refuse.
        try:
            req = urllib.request.Request(CLOB_API + "/order", method="POST",
                                         data=b"{}", headers={"User-Agent": "Mozilla/5.0",
                                                              "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15, context=SSL_CTX)
            geo_body = ""
        except urllib.error.HTTPError as e:
            geo_body = e.read().decode(errors="replace")[:200]
            if e.code == 403 and "restricted" in geo_body.lower():
                sys.exit("GEO-BLOCKED: this box cannot place Polymarket orders "
                         f"({geo_body[:100]}). Run from the Fly worker (Stockholm) "
                         "or an unrestricted country — see LIVE_ROLLOUT.md 0.1.")
        except Exception as e:
            sys.exit(f"geo-gate probe failed ({e}) — refusing to arm live "
                     "without a TRADABLE verdict (LIVE_ROLLOUT.md 0.1)")
        log("geo-gate: TRADABLE — order endpoint reachable")
        confirm_live(cfg)
        executor = LedgerLiveExecutor(cfg)   # FOK marketable orders + fill recording
        if cfg.get("live", {}).get("auto_redeem", True):
            try:
                from redeem import Redeemer
                redeemer = Redeemer(cfg)
                log("on-chain auto-redeem ENABLED (resolved winners redeemed to USDC)")
            except Exception as e:
                log(f"⚠ auto-redeem unavailable ({e}) — resolved winners must be "
                    f"redeemed manually in the UI. (pip install web3, set live.private_key)")
    else:
        executor = LedgerPaperExecutor()   # tracks cash flows for realized-P&L reporting

    engine = CopyTrader(cfg, state, executor, args.state)
    engine.webhook = ""   # per-trade Discord alerts retired — daily digest only
    filt = FollowFilter(cfg)
    bot = Copybot(cfg, engine, filt, redeemer=redeemer)

    # on-chain resolution RPC (payout vectors for operator-resolved markets):
    # env ALCHEMY_RPC_URL wins (the Fly worker has no config.json), else the
    # local config's alchemy_key. Without either, tier-2 settlement is off and
    # operator-resolved/refunded positions stay open (pre-2026-07-06 behavior).
    global _RPC_URL
    _RPC_URL = (os.environ.get("ALCHEMY_RPC_URL")
                or (f"https://polygon-mainnet.g.alchemy.com/v2/{cfg['alchemy_key']}"
                    if cfg.get("alchemy_key") else None))

    mode = "LIVE — REAL MONEY" if executor.live else "PAPER (no orders placed)"
    log(f"copybot · mode: {mode}")
    log(f"on-chain settle fallback: {'ON' if _RPC_URL else 'OFF — set ALCHEMY_RPC_URL'}")
    log(f"watching {len(cfg.get('watchlist', []))} wallets · {filt.describe()}")
    log(f"bankroll ${cfg['bankroll_usd']:.0f} @ {cfg['bankroll_pct']:.1%}/entry · "
        f"guard {cfg['price_guard_pct']:.0%} · "
        f"caps: ${cfg['risk']['max_trade_usd']:.0f}/trade, "
        f"${cfg['risk']['daily_spend_cap_usd']:.0f}/day, "
        f"${cfg['risk']['max_total_exposure_usd']:.0f} exposure")
    bot.seed()

    # one-shot pipeline test: no server, just push a wallet's latest trade through
    if args.test_wallet:
        log(f"--test-wallet: routing {args.test_wallet[:10]}…'s latest activity "
            f"through filter + engine (paper)")
        bot.on_wallet_activity(args.test_wallet, ignore_stale=True)
        log("test done.")
        return

    # single-pass mode for a scheduled runner (GitHub Actions cron). State persists
    # across runs via --state, so the $1k book carries forward run to run. On the
    # very first run there's no baseline → mark history seen and copy nothing, so we
    # only copy trades that happen after the test starts.
    if args.poll_once:
        if not bot.engine.state.get("baselined"):
            bot.baseline()
            bot.engine.state["baselined"] = True
            bot.write_feed()
            bot.engine.persist()
            log("first run — baselined history; published online feed, copied nothing")
            return
        bot.settle_resolved()
        bot.reconcile_exits()
        bot.reconcile_entries()
        for w in cfg.get("watchlist", []):
            bot.on_wallet_activity(w)
        bot.summary(0)
        bot.write_feed()
        bot.engine.persist()
        log("poll-once complete")
        return

    # forward poll mode: run the same filter+engine pipeline by polling, so a paper
    # run works today without the deployed Alchemy webhook. (Production push uses the
    # webhook below; behaviour through the filter+engine is identical either way.)
    if args.poll:
        bot.baseline()
        bot.reconcile_exits()      # catch exits made while we were down
        bot.reconcile_entries()    # ...and entries: record them as missed
        log(f"poll mode · every {args.poll}s · Ctrl-C to stop")
        bot.write_feed()                              # publish an initial "online" snapshot
        bot.publish_feed()
        cycle = 0
        try:
            while True:
                bot.settle_resolved()                 # recycle capital at resolution
                if cycle % 5 == 0:
                    bot.reconcile_exits()
                    bot.reconcile_entries()
                for w in cfg.get("watchlist", []):
                    bot.on_wallet_activity(w)
                cycle += 1
                bot.summary(cycle)
                bot.write_feed()                      # refresh the dashboard feed each cycle
                bot.publish_feed()                    # push to GitHub (throttled, on change)
                time.sleep(args.poll)
        except KeyboardInterrupt:
            log("stopped.")
        return

    signing_key = (os.environ.get("ALCHEMY_SIGNING_KEY")
                   or cfg.get("alchemy_signing_key", ""))
    port = int(os.environ.get("PORT", 8080))
    bot.baseline()
    bot.reconcile_exits()          # catch exits made while we were down
    bot.reconcile_entries()        # ...and entries: record them as missed

    # webhook mode is event-driven, but the book must not depend on the next
    # push arriving: a heartbeat thread settles resolved positions, refreshes
    # the feed, and logs the summary every 60s — plus a FULL backstop poll of
    # every wallet each 5th tick, so a dropped/misconfigured push costs at most
    # ~5 minutes of lag instead of a silent miss.
    def _heartbeat():
        cycle = 0
        while True:
            time.sleep(60)
            cycle += 1
            try:
                bot.settle_resolved()
                if cycle % 5 == 0:
                    bot.reconcile_exits()
                    bot.reconcile_entries()
                    for w in cfg.get("watchlist", []):
                        bot.on_wallet_activity(w)
                bot.summary(cycle)
                bot.write_feed()
                bot.publish_feed()
            except Exception as e:
                log(f"heartbeat error: {e}")
    threading.Thread(target=_heartbeat, daemon=True).start()

    log(f"push mode · listening on :{port} · POST /alchemy · "
        f"signature-verify {'ON' if signing_key else 'OFF'} · "
        f"heartbeat 60s · backstop poll 300s")
    bot.write_feed()
    bot.publish_feed()
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(bot, signing_key)).serve_forever()


if __name__ == "__main__":
    main()
