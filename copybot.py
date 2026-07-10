#!/usr/bin/env python3
"""copybot.py — push-driven, live-capable Polymarket copy-trader.

Marries the two halves you already built:

  * webhook_receiver.py's **push** trigger — Alchemy's Address-Activity webhook
    POSTs here the instant a watched wallet transacts on Polygon. No polling.
  * copytrade.py's hardened **execution engine** — paper executor + the LIVE
    executor on the unified SDK (`polymarket-client`; py-clob-client is
    archived and the CLOB rejects its orders — README gotcha 16), risk gates,
    absolute price guard, no-backfill seeding, proportional entry/exit
    mirroring, and the pending-order registry for in-play `delayed` holds
    (gotcha 17: an accepted-but-held order is NEVER a rejection).

Flow:
    Alchemy POST /alchemy
        → enrich the tx via the Polymarket data-API (market, side, price, size)
        → FollowFilter  — the "only the trades I actually want" gate
        → CopyTrader.handle_trade  — sizes + places under the risk gates

The execution engine is unchanged; this file only swaps the *trigger* from a
poll loop to a push, and inserts the follow-filter in front of it.

SAFETY — paper by default. Live trading needs ALL of:
    1. "mode": "live" in config.json,
    2. the --live flag,
    3. typing the confirmation phrase (env LIVE_CONFIRM on the worker),
    4. polymarket-client installed + live.private_key (env LIVE_PRIVATE_KEY).
Sizing is paper-parity 4%-of-equity floored at the venue $1 minimum (hard
caps retired 2026-07-10, user decision); the geo-gate is fatal in live mode.
This is real money in live mode.

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
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from copytrade import (  # the execution engine (sizing, gates, executors)
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


class LedgerLiveExecutor:
    """Live executor on the unified SDK (polymarket-client). py-clob-client was
    ARCHIVED May 2026 — the CLOB rejects its order format globally ('invalid
    order version'), which is why this no longer extends LiveExecutor.

      * **Marketable FAK/FOK** via place_market_order — the SDK owns tick
        conformity, neg-risk exchange routing, and fee handling internally
        (the hand-rolled tick rounding here crashed the bot twice on
        2026-07-09; all of that machinery is gone).
      * **Protected prices** — max_price (BUY) / min_price (SELL) bound the
        fill at the engine's quoted price ± live.max_slippage_pct (default
        5%), replacing the old round-to-tick limit. Clamped to [0.01, 0.99],
        valid on both 1c and 0.1c books, so the SDK's band check can't raise.
      * **Fill recording** — same ledger as paper. AcceptedOrder reports the
        matched amounts: BUY gives collateral (making) for shares (taking),
        SELL the reverse; avg fill price falls out of the ratio.
      * **Never raises into the trade loop** — any exception is an honest
        ok:False (the engine records a missed row).
    """
    live = True

    def __init__(self, cfg):
        try:
            from polymarket import SecureClient
        except ImportError:
            sys.exit("Live mode needs the unified SDK: "
                     "pip install --pre polymarket-client")
        live = cfg.get("live", {})
        if not live.get("private_key"):
            sys.exit("Live mode needs live.private_key in the config.")
        # wallet auto-resolves to the signer's Deposit Wallet; no api_key at
        # runtime — trading approvals already exist (host/order_probe_v2.py).
        # create() is ready to use as-is (__enter__ is a no-op returning self).
        self.client = SecureClient.create(private_key=live["private_key"])
        self.fills = []
        ot = str(live.get("order_type", "FAK")).upper()
        self._otype = ot if ot in ("FAK", "FOK") else "FAK"
        self._slip = float(live.get("max_slippage_pct", 0.05))

    def _shares_held(self, token_id):
        """Exchange-view share balance for a token — chain truth, the arbiter
        when an order's fate is ambiguous."""
        b = self.client.get_balance_allowance(asset_type="CONDITIONAL",
                                              token_id=str(token_id))
        return b.balance / 1e6

    def _settle_uncertain(self, token_id, side, bal0, price, order_id=None,
                          deadline_s=20, cancel=True):
        """An order may be resting/held at the exchange (in-play 'delayed'
        acceptance, or an exception after posting). Poll it to a terminal
        state, cancel whatever remains (unless the caller keeps it alive for
        the pending registry), and return (filled, avg_price) from the
        exchange's own balance diff. INVARIANT (2026-07-10 incident: six
        in-play acceptances were logged as misses and filled untracked
        minutes later): no order outlives this call untracked — it either
        reports its fill here or is handed to state["pending_orders"]."""
        import time
        px = price
        deadline = time.time() + deadline_s
        while order_id and time.time() < deadline:
            time.sleep(2)
            try:
                o = self.client.get_order(order_id=order_id)
                if float(o.size_matched or 0) > 0:
                    px = float(o.price or price)
                if o.status not in ("live", "delayed"):
                    break
            except Exception:      # gone from the open view — terminal
                break
        if cancel:
            try:
                if order_id:
                    self.client.cancel_order(order_id=order_id)
                else:               # exception path: sweep the whole token
                    ids = [o.id for o in self.client.list_open_orders(
                        token_id=str(token_id))]
                    if ids:
                        self.client.cancel_orders(order_ids=ids)
            except Exception:
                pass                # cancel of a just-matched order — fine
        try:
            bal1 = self._shares_held(token_id)
        except Exception:
            return 0.0, price       # can't read truth: claim nothing; the
        #                             chain_cash_gap alarm catches the rest
        filled = (bal1 - bal0) if side == "BUY" else (bal0 - bal1)
        return max(filled, 0.0), px

    def _order(self, token_id, shares, price, side):
        import math
        sz = math.floor(shares * 100) / 100.0   # cost never exceeds the gated stake
        try:
            bal0 = self._shares_held(token_id)
        except Exception as e:      # no truth anchor → refuse to place at all
            return {"ok": False, "filled_shares": 0.0, "price": price,
                    "resp": f"pre-check failed: {e}", "paper": False}
        try:
            if side == "BUY":
                r = self.client.place_market_order(
                    token_id=token_id, side="BUY",
                    amount=round(sz * price, 2),
                    max_price=min(round(price * (1 + self._slip), 2), 0.99),
                    order_type=self._otype)
            else:
                r = self.client.place_market_order(
                    token_id=token_id, side="SELL", shares=sz,
                    min_price=max(round(price * (1 - self._slip), 2), 0.01),
                    order_type=self._otype)
        except Exception as e:                # NEVER raise into the trade loop —
            # but a timed-out post may still be resting: sweep + measure first
            filled, px = self._settle_uncertain(token_id, side, bal0, price)
            if filled > 0:
                return {"ok": True, "filled_shares": filled, "price": px,
                        "resp": f"filled despite client error: {e}",
                        "paper": False}
            return {"ok": False, "filled_shares": 0.0, "price": price,
                    "resp": f"exception: {e}", "paper": False}
        if not getattr(r, "ok", False):       # RejectedOrder: typed code + message
            return {"ok": False, "filled_shares": 0.0, "price": price,
                    "resp": f"{getattr(r, 'code', '?')}: {getattr(r, 'message', r)}",
                    "paper": False}
        making = float(r.making_amount or 0)  # what we gave (matched)
        taking = float(r.taking_amount or 0)  # what we got (matched)
        filled, usd = (taking, making) if side == "BUY" else (making, taking)
        px = usd / filled if filled else price
        if filled <= 0:
            # ACCEPTED with zero matched = in-play 'delayed'/'live' hold, NOT
            # a rejection (the 2026-07-10 lesson). Wait briefly in-call; if
            # still held, hand the order to the PENDING registry — the
            # heartbeat resolver adopts the fill when it lands or cancels at
            # TTL (the 20s cancel-everything version forfeited a Rune-Eaters
            # hold that filled at +4.5min and paid +$7.50).
            filled, px = self._settle_uncertain(token_id, side, bal0, price,
                                                order_id=r.order_id,
                                                deadline_s=8, cancel=False)
            if filled <= 0:
                return {"ok": False, "filled_shares": 0.0, "price": price,
                        "pending": {"order_id": r.order_id, "bal0": bal0},
                        "resp": {"order_id": r.order_id, "status": r.status,
                                 "note": "in-play hold — pending resolver"},
                        "paper": False}
        return {"ok": filled > 0, "filled_shares": filled, "price": px,
                "resp": {"order_id": r.order_id, "status": r.status,
                         "making": making, "taking": taking,
                         "trades": len(r.trade_ids)},
                "paper": False}

    def buy(self, token_id, shares, price, meta):
        r = self._order(token_id, shares, price, "BUY")
        if r["ok"]:
            self.fills.append({"side": "BUY", "token": token_id,
                               "shares": r["filled_shares"], "price": r["price"]})
        return r

    def sell(self, token_id, shares, price, meta):
        r = self._order(token_id, shares, price, "SELL")
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


# ── T0: the real-time trade stream (RTDS) ───────────────────────────────────

class RtdsListener:
    """PRIMARY detection: Polymarket's real-time data socket streams EVERY
    platform trade, wallet-attributed, ~1s after it happens (measured
    2026-07-10: median 0.8s over 22k msgs, ~45 msg/s at US-evening peak,
    zero drops in 45 min). topic=activity/type=trades is undocumented but
    official (it powers polymarket.com's live feed and is spec'd in
    Polymarket/real-time-data-client). Server-side filters are broken
    (real-time-data-client#34) — subscribe unfiltered, filter client-side.

    Resilience: if the lib is missing or the stream dies, detection degrades
    to the existing backstops (Alchemy push ~3s, 300s poll, reconcile
    janitor) — never to zero. Reconnects with capped backoff forever."""

    URL = "wss://ws-live-data.polymarket.com"

    def __init__(self, bot):
        self.bot = bot
        self.watched = {w.lower() for w in bot.cfg.get("watchlist", [])}
        self.last_msg = 0.0          # last firehose message of any kind
        self.hits = 0                # Set E messages dispatched
        self.state = "off"

    def start(self):
        try:
            import websocket         # websocket-client (fly.Dockerfile pin)
        except ImportError:
            log("rtds: websocket-client not installed — listener OFF "
                "(backstops cover detection)")
            return False
        threading.Thread(target=self._run, args=(websocket,),
                         daemon=True, name="rtds").start()
        return True

    def status(self):
        if self.state != "up":
            return self.state
        age = time.time() - self.last_msg if self.last_msg else -1
        return f"up {age:.0f}s" if age >= 0 else "up"

    def _run(self, websocket):
        backoff = 1
        sub = json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "activity", "type": "trades", "filters": ""}]})

        def on_open(ws):
            ws.send(sub)
            self.state = "up"
            log("rtds: connected — activity/trades stream up (T0 detection)")

            def ping():                     # app-level ping keeps RTDS alive
                while ws.keep_running:
                    time.sleep(5)
                    try:
                        ws.send('{"action":"ping"}')
                    except Exception:
                        break
            threading.Thread(target=ping, daemon=True).start()

        def on_message(ws, raw):
            self._handle(raw)

        while True:
            try:
                app = websocket.WebSocketApp(self.URL, on_open=on_open,
                                             on_message=on_message)
                app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception as e:
                log(f"rtds: listener error {str(e)[:80]}")
            self.state = "down"
            log(f"rtds: stream down — reconnect in {backoff}s "
                "(backstops still detecting)")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _handle(self, raw):
        self.last_msg = time.time()
        try:
            m = json.loads(raw)
        except Exception:
            return
        if m.get("topic") != "activity" or m.get("type") != "trades":
            return
        p = m.get("payload") or {}
        w = (p.get("proxyWallet") or "").lower()
        if w not in self.watched:
            return
        ts = p.get("timestamp") or 0
        if ts > 1e12:
            ts /= 1000.0
        lat = (time.time() - ts) if ts else None
        self.hits += 1
        name = self.bot.names.get(w, w[:10])
        log(f"rtds: {name} {p.get('side')} {p.get('size')} @ {p.get('price')}"
            f" · {str(p.get('title'))[:40]}"
            f"{f' · lat {lat:.1f}s' if lat is not None else ''}")
        # shadow ledger — the durable record the RTDS-vs-Alchemy comparison
        # reads (Fly logs are ephemeral; this file rides the publish commit).
        # `seen` = another trigger already processed this tx before RTDS won.
        try:
            tx = p.get("transactionHash")
            with open(os.path.join(self.bot.here, self.bot.shadow_log), "a") as fh:
                fh.write(json.dumps({
                    "ts": round(time.time(), 2), "trade_ts": round(ts, 2),
                    "lat_s": round(lat, 2) if lat is not None else None,
                    "wallet": w, "name": name, "side": p.get("side"),
                    "size": p.get("size"), "price": p.get("price"),
                    "title": str(p.get("title") or "")[:60], "tx": tx,
                    "seen": bool(tx and tx in self.bot.engine.seen)}) + "\n")
            if self.hits % 500 == 0:      # keep the committed file bounded
                path = os.path.join(self.bot.here, self.bot.shadow_log)
                lines = open(path).readlines()[-2000:]
                open(path, "w").writelines(lines)
        except Exception:
            pass
        try:                              # same funnel as the Alchemy push —
            self.bot.on_wallet_activity(w)   # locks internally, never raises out
        except Exception as e:
            log(f"rtds handler error: {e}")


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
        self.shadow_log = cfg.get("shadow_log", "rtds_shadow.jsonl")
        # persisted across restarts via the engine's state file
        self.conds = engine.state.setdefault("conds", {})   # token_id -> conditionId (open positions)
        engine.state.setdefault("cash", cfg["bankroll_usd"])  # free cash (recycles on sell/resolution)
        engine.state.setdefault("lag", {"n": 0, "sum_s": 0.0, "sum_slip_pct": 0.0})
        engine.state.setdefault("fees_paid", 0.0)
        # 24h rolling lag window — backfill from the fills ledger on first boot
        # after the rolling-window change (older state has 'lag' but no 'lag_recent')
        if "lag_recent" not in engine.state:
            engine.state["lag_recent"] = self._backfill_lag_recent()
        self.fee_rate = float(cfg.get("taker_fee_rate", TAKER_FEE_RATE))

    def _backfill_lag_recent(self, window_s=86400):
        """[[ts, lag_s, slip], …] for fills in the last window_s, read from the
        fills ledger — seeds the rolling avg so it's populated immediately."""
        out = []
        now = time.time()
        try:
            with open(os.path.join(self.here, self.fill_log)) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ts, lag = r.get("ts"), r.get("detect_lag_s")
                    if ts and lag is not None and ts >= now - window_s:
                        out.append([round(ts, 1), round(lag, 1),
                                    round(r.get("slippage_pct") or 0, 5)])
        except FileNotFoundError:
            pass
        return out

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

    def _book_snapshot(self, token):
        """Top-of-book spread + $-depth within 5c of touch, captured per copy
        into the fills ledger. The calibration experiment's weakest model
        assumption is the flat SLIP haircut — 2026-07-08 probe: an open
        position showed a 9c spread with $77 of bids within 5c vs our $40
        stakes. Weeks of these snapshots = an EMPIRICAL fill model for the
        backtest and a depth gate before sizing up. NB captured just AFTER
        our own fill, so ask-side depth is net of what we took — fine for a
        first-order model. Best-effort: never blocks or fails a copy."""
        try:
            req = urllib.request.Request(f"{CLOB_API}/book?token_id={token}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            b = json.loads(urllib.request.urlopen(req, timeout=6, context=SSL_CTX).read())
            bids = b.get("bids") or []
            asks = b.get("asks") or []
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
            "ts": round(now, 1), "wallet": wallet, "token": str(fill["token"]),
            "name": self.names.get(wallet.lower(), wallet[:10]),
            "outcome": t.get("outcome"), "title": (t.get("title") or "")[:80],
            "detect_lag_s": round(detect_s, 1) if detect_s is not None else None,
            "their_price": round(their_p, 4), "my_price": round(my_p, 4),
            "slippage_pct": round(slip_pct, 4),
            "shares": round(fill["shares"], 2), "cost": round(fill["shares"] * my_p, 2),
            "fee": fill.get("fee", 0),
            "mode": "live" if self.engine.ex.live else "paper",
            "book": self._book_snapshot(fill["token"]),
        }
        try:
            with open(os.path.join(self.here, self.fill_log), "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        if detect_s is not None:
            lag = self.engine.state["lag"]
            lag["n"] += 1                    # lifetime copy count (kept for the total)
            lag["sum_s"] += detect_s
            lag["sum_slip_pct"] += slip_pct
            # rolling 24h window so the reported avg reflects CURRENT execution
            # (push mode ~3s), not a lifetime average dragged up by the retired
            # 60s poll era (which made a 3s bot read as 48s)
            rec24 = self.engine.state.setdefault("lag_recent", [])
            rec24.append([round(now, 1), round(detect_s, 1), round(slip_pct, 5)])
            cutoff = now - 86400
            self.engine.state["lag_recent"] = [r for r in rec24 if r[0] >= cutoff]
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
            self._archive_settled(bets, fill["token"])
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

    @staticmethod
    def _archive_settled(bets, tok):
        """Re-entering a token whose previous bet already settled must not
        OVERWRITE it — the dict is keyed by token and the old record's pnl
        vanished from realized (2026-07-09: WTI −9.97 and a Dota +19.30
        clobbered; drift alarm caught it). Move the settled record to a
        history key; every aggregator iterates values(), so archived rows
        keep counting in realized/feed/win-rates."""
        prev = bets.get(tok)
        if prev is not None and prev.get("status") != "open":
            bets[f"{tok}#{prev.get('settled') or int(time.time())}"] = prev

    def _synth_bet(self, tok, pos):
        """Bet record for an open my_pos position that has none (their_price
        None marks it synthesized — lag/slippage unknowable, the source trade
        is gone). Fee is ESTIMATED with the same taker_fee formula _drain_fills
        charges, so for a position whose cash WAS debited the ledger closes to
        0 (same inputs, same result) — while a never-debited orphan shows
        drift = cost+fee exactly, which is what check_book's heal keys on."""
        bets = self.engine.state.setdefault("bets", {})
        self._archive_settled(bets, tok)
        b = bets.get(tok) or {}
        sh = pos.get("shares") or 0
        cost = pos.get("cost") or 0
        fee = b.get("fee") or (taker_fee(sh, cost / sh, self.fee_rate) if sh else 0)
        bets[tok] = {
            "token": tok, "wallet": pos.get("wallet", ""),
            "name": self.names.get((pos.get("wallet") or "").lower())
                    or b.get("name") or "?",
            "outcome": pos.get("outcome"), "title": (pos.get("title") or "")[:90],
            "their_price": None,
            "my_price": round(cost / sh, 4) if sh else None,
            "slippage_pct": None,
            "shares": round(sh, 2), "cost": round(cost, 2),
            "fee": round(fee, 4),
            "opened": b.get("opened") or int(time.time()), "status": "open",
            "exit_price": None, "pnl": None, "settled": None,
        }
        return bets[tok]

    def _record_untracked_buy(self, f):
        """A drained BUY fill no _record_lag call claimed — the handler that
        placed it died between handle_trade and _drain_fills (webhook catches
        the exception, the fill sits in ex.fills), so a LATER drain booked its
        cash under some other trade's iteration. Before 2026-07-08 that fill
        vanished: position in my_pos, cash debited, but no bet record and no
        conds entry — the invisible-orphan seam behind the $+36.35 drift.
        Book it now: audit line, bet record (from the fill + my_pos
        attribution), conds from the position."""
        tok = f["token"]
        pos = self.engine.state["my_pos"].get(tok, {})
        if pos.get("cond") and tok not in self.conds:
            self.conds[tok] = pos["cond"]
        try:
            with open(os.path.join(self.here, self.fill_log), "a") as fh:
                fh.write(json.dumps({
                    "ts": round(time.time(), 1), "side": "BUY", "untracked": True,
                    "token": str(tok), "shares": round(f["shares"], 4),
                    "price": round(f["price"], 4), "fee": f.get("fee", 0),
                    "mode": "live" if self.engine.ex.live else "paper",
                }) + "\n")
        except Exception:
            pass
        bets = self.engine.state.setdefault("bets", {})
        prev = bets.get(tok)
        if prev and prev.get("status") == "open":
            # ADD to an open record: accumulate unrounded, like _record_lag
            sh = prev["shares"] + f["shares"]
            cost = prev["cost"] + f["shares"] * f["price"]
            prev.update(shares=sh, cost=cost,
                        my_price=(cost / sh) if sh else prev["my_price"],
                        fee=(prev.get("fee") or 0) + f.get("fee", 0))
        else:
            self._archive_settled(bets, tok)
            bets[tok] = {
                "token": tok, "wallet": pos.get("wallet", ""),
                "name": self.names.get((pos.get("wallet") or "").lower(), "?"),
                "outcome": pos.get("outcome"), "title": (pos.get("title") or "")[:90],
                "their_price": None,
                "my_price": round(f["price"], 4),
                "slippage_pct": None,
                "shares": round(f["shares"], 2),
                "cost": round(f["shares"] * f["price"], 2),
                "fee": f.get("fee", 0),
                "opened": int(time.time()), "status": "open",
                "exit_price": None, "pnl": None, "settled": None,
            }
        log(f"  ↳ untracked buy booked: {(pos.get('title') or '?')[:42]} — "
            f"{f['shares']:.1f}sh @ {f['price']:.3f}")

    def _ledger_buy_tokens(self):
        """Tokens with a BUY line in the fills ledger — i.e. positions whose
        cash demonstrably went through _drain_fills. Lines from before
        2026-07-08 carry no 'token' (they can't vouch for anyone) and no
        'side' (they are all buys — sells only started logging 2026-07-06)."""
        toks = set()
        try:
            with open(os.path.join(self.here, self.fill_log)) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("side", "BUY") == "BUY" and r.get("token"):
                        toks.add(str(r["token"]))
        except FileNotFoundError:
            pass
        return toks

    def check_book(self, heal_cash=False):
        """The book invariant (HANDOFF proper fix, Option A), asserted after
        every trade and at boot: every my_pos token has a bet record, a conds
        entry, and its cost debited from cash. Records and conds self-correct
        here. The cash leg only heals at boot (heal_cash=True) and only when
        ledger_drift matches ONE candidate's cost+fee — candidates are
        positions with no drained-fill evidence (no ledger BUY line, record
        synthesized) — so a drift that matches nothing stays loudly visible
        instead of being papered over. Callers hold self.lock (webhook path)
        or run before threads start (boot)."""
        st = self.engine.state
        mp = st["my_pos"]
        bets = st.setdefault("bets", {})
        fixed = False
        for tok, pos in list(mp.items()):
            b = bets.get(tok)
            if not b or b.get("status") != "open":
                self._synth_bet(tok, pos)
                log(f"⚠ BOOK: synthesized missing bet record — "
                    f"{(pos.get('title') or '?')[:42]}")
                fixed = True
            if tok not in self.conds:
                if pos.get("cond"):
                    self.conds[tok] = pos["cond"]
                    log(f"⚠ BOOK: backfilled conds from my_pos — "
                        f"{(pos.get('title') or '?')[:42]}")
                    fixed = True
                else:
                    log(f"⚠ BOOK: no conditionId for "
                        f"{(pos.get('title') or '?')[:42]} — can't settle it "
                        f"until a reconcile pass learns the market")
        drift = self.ledger_drift()
        if heal_cash and abs(drift) > 0.01:
            vouched = self._ledger_buy_tokens()
            for tok in list(mp):
                b = bets.get(tok) or {}
                # a real _record_lag record (their_price set) or a ledger BUY
                # line proves the fill was drained — cash side is fine
                if str(tok) in vouched or b.get("their_price") is not None:
                    continue
                gap = (b.get("cost") or mp[tok].get("cost") or 0) + (b.get("fee") or 0)
                if gap > 0 and abs(drift - gap) <= max(0.10, 0.01 * gap):
                    st["cash"] -= gap
                    st["fees_paid"] = st.get("fees_paid", 0.0) + (b.get("fee") or 0)
                    try:                     # the late debit IS the missed drain
                        with open(os.path.join(self.here, self.fill_log), "a") as fh:
                            fh.write(json.dumps({
                                "ts": round(time.time(), 1), "side": "BUY",
                                "healed": True, "token": str(tok),
                                "shares": round(mp[tok].get("shares", 0), 4),
                                "price": b.get("my_price"), "fee": b.get("fee", 0),
                                "mode": "live" if self.engine.ex.live else "paper",
                            }) + "\n")
                    except Exception:
                        pass
                    log(f"⚠ BOOK HEALED: debited ${gap:.2f} never-drained cost — "
                        f"{(b.get('title') or '?')[:42]} (drift was ${drift:+.2f})")
                    drift = self.ledger_drift()
                    fixed = True
        if fixed:
            self.engine.persist()
        if abs(drift) > 0.01:
            log(f"⚠ LEDGER DRIFT ${drift:+.2f} — check_book could not attribute it")
        return drift

    def write_feed(self):
        """Publish the bot's live book to live/copybot_live.json — the feed the
        top of jaxperro.com/trading reads. Reconciles any open bet no longer held
        (mirror-sold) to 'closed'."""
        st = self.engine.state
        bets = st.setdefault("bets", {})
        mp = st["my_pos"]
        # my_pos -> bets: a position can land in my_pos without a bet record when
        # its fill was drained in a context where _record_lag didn't fire (e.g. a
        # second market on the same event processed in one batch). It's cash-
        # correct (open_exposure/open_count read my_pos) but INVISIBLE in the feed
        # table, so the header showed "2 open" while the table showed 1. Synthesize
        # the missing record from my_pos so the counts always match what's shown.
        for tok, p in mp.items():
            b = bets.get(tok)
            if not b or b.get("status") != "open":
                self._synth_bet(tok, p)
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
            # avg_s / avg_slip_pct are the 24h ROLLING view (current execution);
            # n is the lifetime copy count, n24 the fills in the window
            "lag": (lambda t: {"n": lag.get("n", 0), "n24": t[0],
                               "avg_s": round(t[1], 1) if t[1] is not None else None,
                               "avg_slip_pct": round(t[2], 4) if t[2] is not None else None,
                               "window_h": 24})(self.lag_24h()),
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
            unchanged = subprocess.run(
                ["git", "-C", repo, "diff", "--quiet", "--", self.feed_path],
                capture_output=True).returncode == 0
            # a brand-new feed path is UNTRACKED — git diff reports no change
            # for it, which silently suppressed the real-money feed's first
            # publish forever (found 2026-07-09, /live stuck at 404)
            untracked = subprocess.run(
                ["git", "-C", repo, "ls-files", "--error-unmatch", self.feed_path],
                capture_output=True).returncode != 0
            if unchanged and not untracked:
                return
            paths = [p for p in (self.feed_path, self.engine.state_path,
                                 self.fill_log, self.shadow_log)
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
            # a conflicted rebase wedges the repo (UU) and silently kills every
            # future publish (2026-07-08: a boot raced the BOOK RESET push — the
            # stale clone rebased old-book state onto the reset and sat wedged
            # until reboot). Abort, resync to origin, re-commit the RUNNING book
            # on top: the process is the book's single writer, so its memory
            # wins the file; remote state surgery must stop the bot first.
            if subprocess.run(["git", "-C", repo, "rev-parse", "-q", "--verify",
                               "REBASE_HEAD"], capture_output=True).returncode == 0:
                log("⚠ publish rebase conflicted — resyncing to origin and "
                    "re-committing the live book")
                subprocess.run(["git", "-C", repo, "rebase", "--abort"],
                               capture_output=True)
                subprocess.run(["git", "-C", repo, "reset", "--hard", "origin/main"],
                               capture_output=True)
                self.engine.persist()
                self.write_feed()
                subprocess.run(["git", "-C", repo, "add", "-f"] + paths,
                               capture_output=True)
                subprocess.run(["git", "-C", repo, "commit", "-q", "-m",
                                "copybot: live paper feed (resynced after "
                                "conflicted rebase) [skip ci]"], capture_output=True)
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
        n = fresh = 0
        cutoff = time.time() - RECENT_TRADE_WINDOW_S
        for wallet in self.cfg.get("watchlist", []):
            for t in recent_trades(wallet):
                tx = t.get("transactionHash")
                if not tx or tx in self.engine.seen:
                    continue
                # trades YOUNGER than the stale window stay unseen: the first
                # post-boot poll copies them like a late webhook would. Before
                # 2026-07-09 a crash-restart baselined away fresh qualifying
                # trades — two live-executor crashes ate Kruto's 21:21 handicap
                # and badaf's 21:53 Epic entries exactly this way.
                if (t.get("timestamp") or 0) >= cutoff:
                    fresh += 1
                    continue
                self.engine.seen.add(tx)
                n += 1
        self.engine.persist()
        log(f"baseline: {n} historical trades marked seen · {fresh} fresh trades "
            f"left copyable — only NEW trades from now")

    def resolve_pendings(self):
        """Settle state["pending_orders"] — in-play holds the executor handed
        off instead of cancelling (2026-07-10 registry). Each pending either
        ADOPTS its fill (full bookkeeping: spend, position, cash drain,
        ledger row, bet record) or expires at TTL into a cancel + honest
        miss. The exchange's balance diff is the fill arbiter, same as the
        executor's own uncertain path."""
        st = self.engine.state
        pend = st.get("pending_orders") or []
        if not pend or not self.engine.ex.live:
            return
        ex = self.engine.ex
        now = time.time()
        keep = []
        for p in pend:
            tok = p["token"]
            px, status, matched = p["price"], "gone", 0.0
            try:
                o = ex.client.get_order(order_id=p["order_id"])
                matched = float(o.size_matched or 0)
                status = o.status
                if matched > 0:
                    px = float(o.price or px)
            except Exception:
                pass                      # gone from open view — terminal
            expired = now - p["ts"] > p.get("ttl_s", 600)
            if status in ("live", "delayed") and not expired and matched <= 0:
                keep.append(p)            # still held — check again next tick
                continue
            if status in ("live", "delayed"):     # expired: kill the remainder
                try:
                    ex.client.cancel_order(order_id=p["order_id"])
                except Exception:
                    pass
            filled = matched
            try:                          # balance diff is the arbiter
                bal1 = ex._shares_held(tok)
                diff = (bal1 - p["bal0"]) if p["side"] == "BUY" else (p["bal0"] - bal1)
                filled = max(filled, diff)
            except Exception:
                pass
            if filled <= 0.01:
                log(f"pending expired unfilled: {p['outcome']} · {p['title'][:40]}")
                if p["side"] == "BUY" and not p.get("is_add"):
                    self.engine.record_miss(
                        p["wallet"], tok, p.get("cond"), p["title"], p["outcome"],
                        p["price"], p.get("stake", 0),
                        f"in-play hold expired unfilled ({int(now - p['ts'])}s)")
                continue
            spent = filled * px
            if p["side"] == "BUY":
                st["spend"]["usd"] += spent
                mine = st["my_pos"].get(tok)
                if p.get("is_add") and mine:
                    mine["shares"] += filled
                    mine["cost"] += spent
                    if p.get("cond"):
                        mine.setdefault("cond", p["cond"])
                else:
                    st["my_pos"][tok] = {
                        "shares": filled, "cost": spent, "title": p["title"],
                        "outcome": p["outcome"], "event": p.get("event"),
                        "wallet": p["wallet"], "cond": p.get("cond")}
                if p.get("cond"):
                    self.conds[tok] = p["cond"]
                ex.fills.append({"side": "BUY", "token": tok,
                                 "shares": filled, "price": px})
                synth = {"timestamp": p.get("their_ts"), "price": p["their_price"],
                         "outcome": p["outcome"], "title": p["title"]}
                for f in self._drain_fills():
                    self._record_lag(p["wallet"], synth, f)
                self.engine.alert(
                    f"PENDING FILLED · {p['outcome']} · {p['title'][:40]} — "
                    f"buy {filled:.2f} @ {px:.3f} (${spent:.2f}, held "
                    f"{int(now - p['ts'])}s)",
                    discord_text=(f"🟢 **OPEN (in-play hold filled)** [LIVE]\n"
                                  f"{p['outcome']} · {p['title'][:60]}\n"
                                  f"buy {filled:.2f} @ {px:.3f} = **${spent:.2f}** "
                                  f"(held {int(now - p['ts'])}s)"))
            else:                          # SELL adoption: reduce the position
                mine = st["my_pos"].get(tok)
                if mine and mine.get("shares"):
                    frac = min(1.0, filled / mine["shares"])
                    mine["cost"] *= (1 - frac)
                    mine["shares"] -= filled
                    if mine["shares"] <= 0.01:
                        st["my_pos"].pop(tok, None)
                ex.fills.append({"side": "SELL", "token": tok,
                                 "shares": filled, "price": px})
                self._drain_fills()
                self.engine.alert(
                    f"PENDING EXIT FILLED · {p['outcome']} · {p['title'][:40]} — "
                    f"sold {filled:.2f} @ {px:.3f}",
                    discord_text=(f"🔴 **EXIT (in-play hold filled)** [LIVE]\n"
                                  f"{p['outcome']} · {p['title'][:60]}\n"
                                  f"sold {filled:.2f} @ {px:.3f} = "
                                  f"**${filled * px:.2f}**"))
        st["pending_orders"] = keep
        self.engine.persist()

    MAX_EXIT_RETRIES = 10

    def retry_stuck_exits(self):
        """LIVE_ROLLOUT 1.6: re-attempt failed mirror-exits each heartbeat.
        A thin/no-bid book at copy time must not silently turn a scalp into
        a hold-to-resolution — retry up to MAX_EXIT_RETRIES ticks, then page
        Discord (⚠ EXIT STUCK) and let the position ride knowingly."""
        st = self.engine.state
        retries = st.get("exit_retries") or []
        if not retries:
            return
        keep = []
        for r in retries:
            tok = r["token"]
            mine = st["my_pos"].get(tok)
            if not mine or mine.get("shares", 0) <= 0.01:
                continue                    # settled/sold meanwhile — done
            shares = min(r["shares"], mine["shares"])
            price = self.engine._live_price(tok, "sell")
            if price is None:
                res = {"ok": False, "resp": "no bid side"}
            else:
                res = self.engine.ex.sell(tok, shares, price, {})
            if res["ok"]:
                frac = min(1.0, res["filled_shares"] / mine["shares"]) \
                    if mine["shares"] else 1.0
                mine["cost"] *= (1 - frac)
                mine["shares"] -= res["filled_shares"]
                if mine["shares"] <= 0.01:
                    st["my_pos"].pop(tok, None)
                self._drain_fills()
                self.engine.alert(
                    f"EXIT RECOVERED · {r['label'][:46]} — sold "
                    f"{res['filled_shares']:.2f} @ {res['price']:.3f} "
                    f"(attempt {r['attempts'] + 1})",
                    discord_text=(f"🔴 **EXIT recovered** [LIVE]\n{r['label'][:60]}\n"
                                  f"sold {res['filled_shares']:.2f} @ "
                                  f"{res['price']:.3f} on retry "
                                  f"{r['attempts'] + 1}"))
                continue
            if res.get("pending"):            # in-play hold — registry owns it now
                st.setdefault("pending_orders", []).append({
                    **res["pending"], "token": tok, "side": "SELL",
                    "wallet": mine.get("wallet", ""), "title": mine.get("title", ""),
                    "outcome": mine.get("outcome", ""), "event": mine.get("event"),
                    "cond": mine.get("cond"), "their_price": price,
                    "their_ts": None, "price": price, "is_add": False,
                    "stake": shares * price, "ts": time.time(), "ttl_s": 600})
                continue
            r["attempts"] += 1
            if r["attempts"] >= self.MAX_EXIT_RETRIES:
                self.engine.alert(
                    f"⚠ EXIT STUCK · {r['label'][:46]} — {r['attempts']} retries "
                    "failed; position rides to resolution",
                    discord_text=(f"🚨 **EXIT STUCK** [LIVE]\n{r['label'][:60]}\n"
                                  f"{r['attempts']} sell retries failed "
                                  f"(last: {str(res.get('resp'))[:60]}) — "
                                  "position will ride to resolution"))
                continue
            keep.append(r)
        st["exit_retries"] = keep
        self.engine.persist()

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
                # unified SDK: the deposit wallet's pUSD as the exchange counts
                # it (LIVE_ROLLOUT 1.4 anchor — was the emptied legacy proxy
                # via py-clob-client, which alarmed CASH≠CHAIN +24.73)
                r = self.engine.ex.client.get_balance_allowance(
                    asset_type="COLLATERAL")
                bal = r.balance / 1e6
                self._chain_bal = (now, bal)
            except Exception:
                return None
        return None if bal is None else self.engine.state.get("cash", 0) - bal

    def lag_24h(self):
        """(count, avg_lag_s, avg_slip_pct) over fills in the last 24h — the
        CURRENT-execution view. The lifetime average buried push mode's ~3s
        under the retired 60s poll era (a 3s bot read as 48s)."""
        now = time.time()
        rec = [r for r in self.engine.state.get("lag_recent", []) if r[0] >= now - 86400]
        if not rec:
            return 0, None, None
        n = len(rec)
        return n, sum(r[1] for r in rec) / n, sum(r[2] for r in rec) / n

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
        n24, avg_s, avg_slip = self.lag_24h()
        if n24:
            lagstr = (f" · {lag.get('n', 0)} copies · 24h lag {avg_s:.0f}s "
                      f"slip {avg_slip:+.1%} ({n24})")
        elif lag.get("n"):
            lagstr = (f" · {lag['n']} copies avg lag {lag['sum_s']/lag['n']:.0f}s "
                      f"slip {lag['sum_slip_pct']/lag['n']:+.1%}")
        bankstr = f" · banked ${reserve:,.0f}" if reserve else ""
        drift = self.ledger_drift()
        driftstr = f" · ⚠ LEDGER DRIFT ${drift:+.2f}" if abs(drift) > 0.01 else ""
        gap = self.chain_cash_gap()
        if gap is not None and abs(gap) > 1.0:      # ≥$1: beyond fee/rounding float
            driftstr += f" · ⚠ CASH≠CHAIN ${gap:+.2f}"
        rtds = getattr(self, "rtds", None)
        if rtds is not None:
            st = rtds.status()
            driftstr += (f" · rtds {st}" if st.startswith("up")
                         else f" · ⚠ rtds {st}")
        log(f"[{cycle}] open {n} · deployed ${exp:,.0f} · free ${cash:,.0f}/${bank:,.0f}"
            f"{bankstr} · realized ${realized:+,.2f}{lagstr}{driftstr}"
            + (f" · CAN'T OPEN (free < ${stake:,.0f} stake — bets missed)"
               if cash < stake else ""))

    def on_wallet_activity(self, wallet, ignore_stale=False):
        """A watched wallet just transacted — pull its latest trades and route any
        new, recent one through the filter and (if it passes) the engine."""
        name = self.names.get(wallet.lower(), wallet[:10] + "…")
        trades = recent_trades(wallet)
        # ONE conviction bet often arrives as several fills — a sweep through
        # the book or rapid clip entries (gkmg 2026-07-09: a $612 MOUZ entry =
        # 3×$204 same-second rows, every clip sub-floor while the backtest's
        # position-level view takes the bet; he sold +60% five minutes later).
        # Merge same-token BUY rows within 120s into the bet the sharp made;
        # component txs are all marked processed. SELLs stay per-fill — the
        # proportional mirror handles those correctly clip by clip.
        merged, by_tok = [], {}
        for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
            if (t.get("side") != "BUY" or not t.get("asset")
                    or not t.get("transactionHash")
                    or t.get("transactionHash") in self.engine.seen
                    or t.get("transactionHash") in self.skipped):
                merged.append(t)
                continue
            prev = by_tok.get(t["asset"])
            if prev is not None and (t.get("timestamp", 0) or 0) - (prev.get("timestamp", 0) or 0) <= 120:
                sz_p, sz_t = prev.get("size") or 0, t.get("size") or 0
                if sz_p + sz_t:
                    prev["price"] = (((prev.get("price") or 0) * sz_p
                                      + (t.get("price") or 0) * sz_t) / (sz_p + sz_t))
                prev["size"] = sz_p + sz_t
                prev["usdcSize"] = (prev.get("usdcSize") or 0) + (t.get("usdcSize") or 0)
                prev["timestamp"] = max(prev.get("timestamp", 0) or 0, t.get("timestamp", 0) or 0)
                prev.setdefault("_extra_txs", []).append(t["transactionHash"])
            else:
                by_tok[t["asset"]] = t
                merged.append(t)
        # oldest-first so the engine's position math stays causal
        for t in sorted(merged, key=lambda x: x.get("timestamp", 0)):
            tx = t.get("transactionHash")
            extras = t.pop("_extra_txs", [])
            if not tx or tx in self.engine.seen or tx in self.skipped:
                self.skipped.update(extras)
                continue
            # filter FIRST (before the stale gate) so we know whether a trade we
            # were too slow on WOULD have qualified — a qualifying miss is worth
            # recording; a below-floor/out-of-band one is a deliberate skip.
            follow, reason = self.filt.check(wallet, t)
            stale = not ignore_stale and time.time() - t.get("timestamp", 0) > RECENT_TRADE_WINDOW_S
            if follow and stale:
                # a bet we'd have copied but didn't catch in time — webhook missed
                # it, the bot was down, or the market resolved faster than we poll
                # (5-min crypto, in-play). Log it as MISSED so it's visible on the
                # dashboard instead of silently dropped; settle_resolved values it
                # hypothetically like every other miss. record_miss dedups by token
                # so reconcile_entries can't double-count the same position.
                self.skipped.update([tx, *extras])
                if t.get("side") == "BUY" and t.get("asset"):
                    late_m = (time.time() - (t.get("timestamp") or 0)) / 60.0
                    with self.lock:
                        self.engine.record_miss(
                            wallet, t["asset"], t.get("conditionId"),
                            t.get("title") or "", t.get("outcome") or "",
                            t.get("price") or 0, self.engine.stake_usd(wallet),
                            f"too slow to follow ({late_m:.0f}m late)")
                        self.engine.persist()
                    log(f"MISS {name}: {t.get('side')} {t.get('outcome','?')} "
                        f"@ {t.get('price',0):.3f} — too slow ({late_m:.0f}m late)")
                continue
            if not follow:
                self.skipped.update([tx, *extras])
                log(f"skip {name}: {t.get('side')} {t.get('outcome','?')} "
                    f"@ {t.get('price',0):.3f} — {reason}")
                continue
            log(f"FOLLOW {name}: {t.get('side')} {t.get('outcome','?')} "
                f"@ {t.get('price',0):.3f} (${t.get('usdcSize',0):,.0f})")
            with self.lock:
                self.engine.handle_trade(wallet, t)   # sizes, gates, places (paper/live)
                self.engine.seen.update(extras)       # component fills of the merged bet
                tok = t.get("asset")
                if tok in self.engine.state["my_pos"] and tok not in self.conds:
                    self.conds[tok] = t.get("conditionId")   # remember for settling
                for f in self._drain_fills():
                    if f["token"] == tok:                    # the fill from this copy
                        self._record_lag(wallet, t, f)
                    else:
                        # a leftover fill from a handler that died mid-trade —
                        # its cash was just debited above; record it or it
                        # becomes an invisible orphan (the 2026-07-08 drift)
                        self._record_untracked_buy(f)
                self.check_book()

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
                for f in self._drain_fills():       # book the sell's cash + sold-leg
                    self._record_untracked_buy(f)   # (returned buys = leftovers)
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
            # tokens adjudicated 'accumulated via sub-floor clips, not a miss'
            # — persisted so each backstop pass doesn't re-fetch and re-log
            no_miss = set(st.setdefault("no_miss_toks", []))
            missed_toks |= no_miss
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
                    # the live path gates PER TRADE; a position can accumulate
                    # past the floor via sub-floor clips the bot rightly skipped
                    # one by one (2026-07-08: 0xbadaf319's paired YES+NO arb
                    # clips of $6–37 built $40+ positions that read as
                    # 'missed · bot offline' while the bot was UP and skipping
                    # every fill). Only a genuine miss if some SINGLE buy on
                    # this market would have passed the same filter. Market-
                    # filtered /trades = full history for the market (no
                    # pagination window, unlike /activity), usdcSize
                    # synthesized where that endpoint omits it. API failure
                    # falls through to the old behavior (record the miss).
                    cond = p.get("conditionId")
                    trs = sm.get_json("/trades", {"user": w, "market": cond,
                                                  "limit": 100}) if cond else None
                    if trs is not None:
                        for t_ in trs:
                            if not t_.get("usdcSize"):
                                t_["usdcSize"] = (t_.get("size") or 0) * (t_.get("price") or 0)
                        # cluster fills ≤120s apart — the same merge rule the
                        # live path applies: a fill-split conviction entry is
                        # ONE bet, and must count as a real miss
                        buys = sorted((t_ for t_ in trs if t_.get("side") == "BUY"
                                       and str(t_.get("asset")) == str(tok)),
                                      key=lambda x: x.get("timestamp", 0) or 0)
                        best, cur, last_ts = None, None, None
                        for t_ in buys:
                            ts_ = t_.get("timestamp", 0) or 0
                            if cur is not None and ts_ - last_ts <= 120:
                                cur = dict(t_, usdcSize=(cur.get("usdcSize") or 0)
                                           + (t_.get("usdcSize") or 0))
                            else:
                                cur = dict(t_)
                            last_ts = ts_
                            if best is None or (cur.get("usdcSize") or 0) > (best.get("usdcSize") or 0):
                                best = cur
                        if best is None or not self.filt.check(w, best)[0]:
                            log(f"reconcile: {name} accumulated "
                                f"{(p.get('title') or '?')[:38]} via sub-floor "
                                f"clips (${iv:,.0f} total) — not copyable under "
                                f"per-trade rules, not a miss")
                            no_miss.add(tok)
                            st["no_miss_toks"] = sorted(no_miss)
                            missed_toks.add(tok)
                            continue
                    want = self.engine.stake_usd(w, iv)
                    self.engine.record_miss(
                        w, tok, p.get("conditionId"), p.get("title") or "",
                        p.get("outcome") or "", trade["price"], want,
                        "not copied in the detection window (reconciled)")
                    missed_toks.add(tok)
                    log(f"reconcile: {name} entered {(p.get('title') or '?')[:42]} "
                        f"while we weren't listening — recorded as missed")
            # exit-aware missed settling: a missed bet whose sharp FULLY
            # EXITED pre-resolution settles at THEIR exit — the mirror
            # counterfactual — instead of riding to resolution (Kruto's 3c
            # Hive entry 2026-07-09: he banked 16x selling at 48c; the
            # hold-to-resolution valuation would have shown the map result
            # instead). Same three affirmative facts as reconcile_exits —
            # silence never reads as an exit. Entry+exit taker fees charged,
            # like every mirrored sell.
            for m in st.get("missed", []):
                # (B) rows already SETTLED at resolution (won/lost/refund):
                # fast in-play markets can resolve before this 5-min pass sees
                # the sharp's sell, and rows from before 2026-07-09 predate
                # exit-aware settling. One idempotent re-check per row: a
                # redeem prints EXACTLY the payout, so an exit print >2c away
                # means the sharp sold pre-resolution — revalue the
                # counterfactual at their exit (mirror semantics, both fees).
                if (m.get("status") in ("won", "lost", "refund")
                        and not m.get("exit_checked")
                        and m.get("cond") and m.get("wallet") and m.get("token")):
                    cps = sm.get_json("/closed-positions", {"user": m["wallet"],
                                                            "market": m["cond"], "limit": 10})
                    if cps is None:
                        continue                # API blip — retry next pass
                    m["exit_checked"] = True    # examined once, forever
                    row = next((p for p in cps
                                if str(p.get("asset")) == str(m["token"])), None)
                    if row is not None:
                        tb = row.get("totalBought") or 0
                        xp = ((row.get("avgPrice") or 0)
                              + ((row.get("realizedPnl") or 0) / tb if tb else 0))
                        payout = {"won": 1.0, "lost": 0.0, "refund": 0.5}[m["status"]]
                        if abs(xp - payout) > 0.02:
                            p = m.get("price") or 0.5
                            sh = m["stake"] / max(p, 0.001)
                            fee_in = taker_fee(sh, p, self.fee_rate)
                            fee_out = taker_fee(sh, xp, self.fee_rate)
                            old_pnl = m.get("pnl")
                            m.update(status="sold", exit_price=round(xp, 4),
                                     pnl=round(sh * xp - m["stake"] - fee_in - fee_out, 2))
                            log(f"missed-reval: {(m.get('title') or '?')[:38]} — sharp "
                                f"sold @ {xp:.3f} pre-resolution; counterfactual "
                                f"${old_pnl} -> ${m['pnl']:+.2f} (sold)")
                    continue
                if (m.get("status") != "open" or not m.get("cond")
                        or not m.get("wallet") or not m.get("token")):
                    continue
                ps = sm.get_json("/positions", {"user": m["wallet"], "market": m["cond"],
                                                "limit": 10, "sizeThreshold": 0})
                if ps is None:
                    continue
                if any(str(p.get("asset")) == str(m["token"]) and (p.get("size") or 0) > 0
                       for p in ps):
                    continue                    # still held — resolution path waits
                cps = sm.get_json("/closed-positions", {"user": m["wallet"],
                                                        "market": m["cond"], "limit": 10})
                if cps is None:
                    continue
                row = next((p for p in cps if str(p.get("asset")) == str(m["token"])), None)
                if row is None:
                    continue
                mk = _market(m["cond"])
                if not mk or mk.get("closed"):
                    continue                    # resolved -> chain-truth settle path
                tb = row.get("totalBought") or 0
                xp = (row.get("avgPrice") or 0) + ((row.get("realizedPnl") or 0) / tb if tb else 0)
                p = m.get("price") or 0.5
                sh = m["stake"] / max(p, 0.001)
                fee_in = taker_fee(sh, p, self.fee_rate)
                fee_out = taker_fee(sh, xp, self.fee_rate)
                m.update(status="sold", exit_price=round(xp, 4),
                         pnl=round(sh * xp - m["stake"] - fee_in - fee_out, 2),
                         settled=int(time.time()))
                log(f"missed-settle: {m.get('name') or m['wallet'][:8]} exited "
                    f"{(m.get('title') or '?')[:38]} @ {xp:.3f} — "
                    f"hypothetical ${m['pnl']:+.2f} (sold)")
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
        v = (os.environ.get(env) or "").strip()   # secrets often arrive with a
        if v:                                      # trailing newline from paste
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
    # per-trade Discord pings retired for PAPER (daily digest only) — but the
    # LIVE book pings every real placement/exit/settle: eyes on every dollar
    # during the supervised test. Webhook via env secret only (a webhook URL
    # committed to this public repo leaked once already — 2026-07-09).
    engine.webhook = ((os.environ.get("DISCORD_WEBHOOK") or "").strip()
                      if want_live else "")
    filt = FollowFilter(cfg)
    bot = Copybot(cfg, engine, filt, redeemer=redeemer)

    # T0 detection — the RTDS trade stream (~1s, wallet-attributed). Paper
    # runs it by default (the 24h shadow run, 2026-07-10); the REAL-MONEY
    # role stays on the proven Alchemy+poll stack until the shadow validates,
    # then: flyctl secrets set RTDS_DETECT=1 -a wwf-copybot-live.
    rtds_default = "0" if os.environ.get("COPYBOT_ROLE", "paper") == "live" else "1"
    if (os.environ.get("RTDS_DETECT") or rtds_default) == "1":
        bot.rtds = RtdsListener(bot)
        bot.rtds.start()
    else:
        log("rtds: T0 listener disabled for this role (RTDS_DETECT=1 enables)")

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
    guard = cfg.get("price_guard_abs", cfg.get("price_guard_pct", 0.05))
    def _cap(v):
        return "off" if v >= 1e5 else f"${v:,.0f}"
    log(f"bankroll ${cfg['bankroll_usd']:.2f} @ {cfg['bankroll_pct']:.1%}/entry · "
        f"guard +{guard:.2f} abs · "
        f"caps: {_cap(cfg['risk']['max_trade_usd'])}/trade, "
        f"{_cap(cfg['risk']['daily_spend_cap_usd'])}/day, "
        f"{_cap(cfg['risk']['max_total_exposure_usd'])} exposure")
    bot.seed()
    # boot invariant pass: rebuild any missing bet/conds records and — only
    # here, where no trade is in flight — heal a never-debited orphan's cash
    # if the drift matches it exactly (HANDOFF proper fix, Option A)
    bot.check_book(heal_cash=True)

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
                bot.resolve_pendings()                # adopt/expire in-play held orders
                bot.retry_stuck_exits()               # LIVE_ROLLOUT 1.6
                bot.settle_resolved()                 # recycle capital at resolution
                if cycle % 5 == 0:
                    bot.reconcile_exits()
                    bot.reconcile_entries()
                for w in cfg.get("watchlist", []):
                    try:
                        bot.on_wallet_activity(w)
                    except Exception as e:      # parity with the push handler's
                        log(f"poll error {w[:10]}…: {e}")   # guard — never die
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
                bot.resolve_pendings()     # adopt/expire in-play held orders
                bot.retry_stuck_exits()    # LIVE_ROLLOUT 1.6
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
