"""Stub tests for repair_market_meta (#18): a copy seeded from a
metadata-less RTDS row books title/outcome/cond == "" and can never settle —
the 2026-07-20 Odyssey bet sat open while the venue auto-redeemed it,
alarming CASH≠CHAIN until manual surgery. Covers: backfill of my_pos + bets
+ missed records, the falsy-cond overwrite, lookup-failure backoff, the 1h
unsettleable alarm (once), and the end-to-end un-stick (repair -> same-pass
settle). Run: python3 tests/test_meta_repair.py — no network, no config.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import copybot  # noqa: E402

TOK = "35358889366059131445607416132477521541386270328042995107702816540034073998102"
MISS_TOK = "111"
COND = "0xd2a97dd956e70c9c4abda30d2864588ece0c7213013eb3fa664b88509ff829b1"
TITLE = 'Will "The Odyssey" Opening Weekend Box Office be greater than 115m?'


class Ex:
    live = False


class Eng:
    def __init__(self, st):
        self.state, self.ex, self.alerts = st, Ex(), []

    def persist(self):
        pass

    def alert(self, m, discord_text=None):
        self.alerts.append(m)


def mkbot(st):
    bot = copybot.Copybot.__new__(copybot.Copybot)
    bot.engine = Eng(st)
    bot.lock = threading.Lock()
    bot.redeemer = None
    bot.fee_rate = 0.03
    bot.conds = st.setdefault("conds", {})
    bot._meta_fail = {}
    bot._meta_warned = set()
    return bot


def fresh_state():
    return {
        "cash": 10.0, "adjustments": [],
        "my_pos": {TOK: {"shares": 1.694914, "cost": 1.0, "title": "",
                         "outcome": "", "event": None, "wallet": "0xw",
                         "cond": ""}},
        "bets": {TOK: {"token": TOK, "wallet": "0xw", "name": "Bikes",
                       "outcome": "", "title": "", "shares": 1.69,
                       "cost": 1.0, "fee": 0.0123, "opened": 1,
                       "status": "open", "exit_price": None, "pnl": None,
                       "settled": None}},
        "missed": [{"token": MISS_TOK, "cond": "", "status": "open",
                    "outcome": "", "price": 0.5, "stake": 2.0}],
        "conds": {TOK: ""},
    }


calls = []


def meta_ok(token):
    calls.append(token)
    return (TITLE, "Yes", COND) if token == TOK else ("Miss mkt", "No", "0xm")


def meta_fail(token):
    calls.append(token)
    return None


fails = []

# 1) repair backfills my_pos + bets + conds + missed, and logs no alarm
copybot._token_market = meta_ok
st = fresh_state()
bot = mkbot(st)
bot.repair_market_meta()
if st["conds"][TOK] != COND:
    fails.append(f"conds not repaired: {st['conds'][TOK]!r}")
if st["my_pos"][TOK]["title"] != TITLE or st["my_pos"][TOK]["cond"] != COND:
    fails.append(f"my_pos not backfilled: {st['my_pos'][TOK]}")
if st["bets"][TOK]["title"] != TITLE or st["bets"][TOK]["outcome"] != "Yes":
    fails.append(f"bets not backfilled: {st['bets'][TOK]}")
if st["missed"][0]["cond"] != "0xm":
    fails.append(f"missed not repaired: {st['missed'][0]}")
if MISS_TOK in st["conds"]:
    fails.append("missed token leaked into conds (conds is open positions only)")
if bot.engine.alerts:
    fails.append(f"unexpected alarm: {bot.engine.alerts}")

# 2) lookup failure: tracked, backed off, no crash; second pass inside the
#    retry window must NOT re-call the API
copybot._token_market = meta_fail
st = fresh_state()
bot = mkbot(st)
calls.clear()
bot.repair_market_meta()
n_first = len(calls)
bot.repair_market_meta()          # immediate second pass -> backoff, no calls
if n_first != 2:                  # TOK + MISS_TOK
    fails.append(f"expected 2 lookup calls, got {n_first}")
if len(calls) != n_first:
    fails.append(f"backoff violated: {len(calls) - n_first} extra calls")
if st["conds"][TOK] != "":
    fails.append("failed lookup should leave cond empty")

# 3) 1h-old failure alarms ONCE (my_pos only, never for missed)
now = time.time()
bot._meta_fail[TOK] = (now - 3700, now)          # old failure, recent attempt
bot._meta_fail[MISS_TOK] = (now - 3700, now)
bot.repair_market_meta()
bot.repair_market_meta()
alarms = [a for a in bot.engine.alerts if "UNSETTLEABLE" in a]
if len(alarms) != 1:
    fails.append(f"expected exactly 1 unsettleable alarm, got {bot.engine.alerts}")

# 4) recovery after failure: lookup succeeds once the market appears -> alarm
#    bookkeeping cleared
copybot._token_market = meta_ok
bot._meta_fail[TOK] = (now - 3700, 0.0)          # out of the retry window
bot.repair_market_meta()
if st["conds"][TOK] != COND or TOK in bot._meta_fail or TOK in bot._meta_warned:
    fails.append("recovery did not clear fail/warned bookkeeping")

# 5) end-to-end un-stick: repaired cond lets the SAME settle_resolved pass
#    settle the bet (this is the exact Odyssey failure, inverted)
copybot._token_market = meta_ok
copybot.resolution_price = lambda token, cond, outcome=None: 1.0
st = fresh_state()
bot = mkbot(st)
bot._settle_last = 0
bot.settle_resolved()
b = st["bets"][TOK]
if b["status"] != "won" or b["pnl"] is None or TOK in st["my_pos"]:
    fails.append(f"repair+settle did not un-stick the bet: {b}")
if abs(st["cash"] - 11.694914) > 1e-9:
    fails.append(f"settle cash wrong: {st['cash']}")

print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
