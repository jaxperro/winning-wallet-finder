"""Regression tests for the Option A orphan fix (HANDOFF 2026-07-08):
_record_untracked_buy, _synth_bet, check_book (record/conds self-correct +
guarded boot cash heal). Run: python3 tests/test_check_book.py — needs no
network, no cache, no config; stubs the engine.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import copybot  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="copybot_test_")
FILL_LOG = os.path.join(_TMP, "fills_test.jsonl")
FEED = os.path.join(_TMP, "feed_test.json")


class StubEx:
    live = False

    def __init__(self):
        self.fills = []


class StubEngine:
    def __init__(self, state):
        self.state = state
        self.ex = StubEx()
        self.persisted = 0

    def persist(self):
        self.persisted += 1

    def open_exposure(self):
        return sum(p["cost"] for p in self.state["my_pos"].values())


def mkbot(state):
    cfg = {"watch": [{"wallet": "0xabc", "name": "TestSharp"}],
           "bankroll_usd": 1000.0,
           "feed_path": FEED, "fill_log": FILL_LOG}
    eng = StubEngine(state)
    bot = copybot.Copybot(cfg, eng, filt=None)
    bot.here = ""  # fill_log paths are absolute already
    return bot, eng


def base_state():
    return {"my_pos": {}, "their_pos": {}, "bets": {}, "conds": {},
            "adjustments": [], "missed": [], "lag_recent": []}


def approx(a, b, tol=0.02):
    assert abs(a - b) <= tol, f"{a} !~ {b}"


fails = []


def case(name):
    def deco(fn):
        try:
            if os.path.exists(FILL_LOG):
                os.remove(FILL_LOG)
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append(name)
    return deco


@case("drain debits cash; untracked buy gets record+conds+ledger line")
def t1():
    st = base_state()
    bot, eng = mkbot(st)
    # position exists (engine added it), fill drained under ANOTHER trade's loop
    st["my_pos"]["T2"] = {"shares": 80.0, "cost": 40.0, "title": "Orphan Mkt",
                          "outcome": "Yes", "wallet": "0xabc", "cond": "0xcond2"}
    eng.ex.fills.append({"side": "BUY", "token": "T2", "shares": 80.0, "price": 0.5})
    cash0 = st["cash"]
    buys = bot._drain_fills()
    assert len(buys) == 1
    fee = buys[0]["fee"]
    approx(st["cash"], cash0 - 40.0 - fee)
    bot._record_untracked_buy(buys[0])
    b = st["bets"]["T2"]
    assert b["status"] == "open" and b["their_price"] is None
    assert b["name"] == "TestSharp" and b["wallet"] == "0xabc"
    assert st["conds"]["T2"] == "0xcond2"
    lines = [json.loads(l) for l in open(FILL_LOG)]
    assert any(l.get("token") == "T2" and l.get("side") == "BUY" for l in lines)
    approx(bot.ledger_drift(), 0.0)


@case("check_book synthesizes record + backfills conds, drift stays 0")
def t2():
    st = base_state()
    bot, eng = mkbot(st)
    st["my_pos"]["T3"] = {"shares": 100.0, "cost": 45.0, "title": "Synth Mkt",
                          "outcome": "No", "wallet": "0xabc", "cond": "0xcond3"}
    # cash WAS debited (cost + the same estimated fee) — drained-but-not-recorded
    fee_est = copybot.taker_fee(100.0, 0.45, bot.fee_rate)
    st["cash"] -= 45.0 + fee_est
    drift = bot.check_book()
    b = st["bets"]["T3"]
    assert b["status"] == "open" and b["their_price"] is None
    approx(b["fee"], fee_est)
    assert st["conds"]["T3"] == "0xcond3"
    approx(drift, 0.0)
    assert eng.persisted > 0


@case("boot heal: never-debited orphan matched by drift -> cash debited, drift 0")
def t3():
    st = base_state()
    bot, eng = mkbot(st)
    st["my_pos"]["T4"] = {"shares": 72.0, "cost": 36.35, "title": "Hanyi Liu Redux",
                          "outcome": "Yes", "wallet": "0xabc", "cond": "0xcond4"}
    cash0 = st["cash"]           # cash NEVER debited — the crash orphan
    drift = bot.check_book(heal_cash=True)
    fee_est = st["bets"]["T4"]["fee"]
    approx(st["cash"], cash0 - 36.35 - fee_est)
    approx(drift, 0.0)
    lines = [json.loads(l) for l in open(FILL_LOG)]
    assert any(l.get("healed") for l in lines)


@case("boot heal REFUSES a drift that matches nothing")
def t4():
    st = base_state()
    bot, eng = mkbot(st)
    st["my_pos"]["T5"] = {"shares": 80.0, "cost": 40.0, "title": "Innocent Mkt",
                          "outcome": "Yes", "wallet": "0xabc", "cond": "0xcond5"}
    fee_est = copybot.taker_fee(80.0, 0.5, bot.fee_rate)
    st["cash"] -= 40.0 + fee_est     # this position's cash is fine
    st["cash"] += 10.0               # unrelated $10 drift from somewhere else
    cash0 = st["cash"]
    drift = bot.check_book(heal_cash=True)
    assert st["cash"] == cash0, "healed cash it should not have touched"
    approx(drift, 10.0)


@case("real _record_lag record (their_price set) is never a heal candidate")
def t5():
    st = base_state()
    bot, eng = mkbot(st)
    st["my_pos"]["T6"] = {"shares": 80.0, "cost": 40.0, "title": "Lagged Mkt",
                          "outcome": "Yes", "wallet": "0xabc", "cond": "0xcond6"}
    st["bets"]["T6"] = {"token": "T6", "wallet": "0xabc", "name": "TestSharp",
                        "outcome": "Yes", "title": "Lagged Mkt",
                        "their_price": 0.48, "my_price": 0.5, "slippage_pct": 0.04,
                        "shares": 80.0, "cost": 40.0, "fee": 0.6,
                        "opened": 1, "status": "open",
                        "exit_price": None, "pnl": None, "settled": None}
    st["cash"] -= 40.6   # this position's cash was properly drained
    # fabricate an UNRELATED drift that HAPPENS to equal this bet's cost+fee
    st["cash"] += 40.6
    cash0 = st["cash"]
    drift = bot.check_book(heal_cash=True)
    assert st["cash"] == cash0, "debited a vouched position"
    approx(drift, 40.6)


@case("two orphans: both healed iteratively? no — only exact-match heals")
def t6():
    st = base_state()
    bot, eng = mkbot(st)
    # two never-debited orphans of different sizes: drift = sum, matches
    # NEITHER individually -> heal must refuse both (visible drift > 0)
    st["my_pos"]["T7"] = {"shares": 80.0, "cost": 40.0, "title": "Orphan A",
                          "outcome": "Yes", "wallet": "0xabc", "cond": "c7"}
    st["my_pos"]["T8"] = {"shares": 50.0, "cost": 30.0, "title": "Orphan B",
                          "outcome": "No", "wallet": "0xabc", "cond": "c8"}
    cash0 = st["cash"]
    drift = bot.check_book(heal_cash=True)
    assert st["cash"] == cash0, "healed on a non-matching drift"
    assert drift > 70, f"drift should stay visible, got {drift}"


for f in (FILL_LOG, FEED):
    if os.path.exists(f):
        os.remove(f)

print()
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
