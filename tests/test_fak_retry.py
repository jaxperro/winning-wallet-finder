"""Tests for the FAK no-match re-quote retry (2026-07-20): a first-attempt
OPEN whose FAK dies unmatched is handed to Copybot.fak_requote_retry instead
of recording a miss; the retry re-enters _handle_their_buy(retry=True) with
their_size=0 once, and only a SECOND rejection records the miss (tagged).
Run: python3 tests/test_fak_retry.py — needs no network, no config.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import copybot    # noqa: E402
import copytrade  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="copybot_test_")
FAK_RESP = "exception: no orders found to match with FAK order. FAK orders..."
WALLET = "0xabc"
TOK = "T1"

fails = []


def case(name):
    def deco(fn):
        cp0, bd0 = copytrade.clob_price, copybot.book_depth
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append(name)
        finally:
            copytrade.clob_price, copybot.book_depth = cp0, bd0
    return deco


class ScriptedEx:
    """Returns the scripted buy responses in order; repeats the last one."""
    live = False

    def __init__(self, *resps):
        self.resps = list(resps)
        self.fills = []
        self.calls = 0

    def buy(self, token_id, shares, price, meta):
        self.calls += 1
        r = self.resps.pop(0) if len(self.resps) > 1 else self.resps[0]
        if r == "fak":
            return {"ok": False, "filled_shares": 0.0, "price": price,
                    "resp": FAK_RESP, "paper": True}
        if r == "other":
            return {"ok": False, "filled_shares": 0.0, "price": price,
                    "resp": "some other rejection", "paper": True}
        self.fills.append({"side": "BUY", "token": token_id,
                           "shares": shares, "price": price})
        return {"ok": True, "filled_shares": shares, "price": price,
                "paper": True}


def mkengine(*resps):
    cfg = {"bankroll_usd": 100.0, "bankroll_pct": 0.04,
           "watch": [{"wallet": WALLET, "name": "TestSharp"}],
           "feed_path": os.path.join(_TMP, "feed.json"),
           "fill_log": os.path.join(_TMP, "fills.jsonl"),
           "risk": {"min_price": 0.02, "max_price": 0.99,
                    "max_open_positions": 50, "min_order_usd": 1.0,
                    "max_trade_usd": 100.0, "daily_spend_cap_usd": 1000.0,
                    "max_total_exposure_usd": 1000.0}}
    state = copytrade.new_state()
    state["cash"] = 100.0
    eng = copytrade.CopyTrader(cfg, state, ScriptedEx(*resps),
                               os.path.join(_TMP, "state.json"))
    copytrade.clob_price = lambda t, s: 0.50
    return eng


def their_buy(eng, retry=False, their_size=100.0):
    eng.state["their_pos"].setdefault(WALLET, {})
    if retry:                       # host passes 0 — their_pos already counts it
        eng.state["their_pos"][WALLET][TOK] = their_size
        their_size = 0.0
    eng._handle_their_buy(WALLET, TOK, their_size, 0.50, "Yes · Test market",
                          "Test market", "Yes", event="ev-1", cond="0xc0nd",
                          their_ts=123, retry=retry)


@case("first FAK reject on an OPEN -> hook fires with the copy ctx, NO miss")
def t1():
    eng = mkengine("fak")
    calls = []
    eng.on_fak_reject = calls.append
    their_buy(eng)
    assert len(calls) == 1, f"hook calls: {len(calls)}"
    ctx = calls[0]
    assert ctx["token"] == TOK and ctx["wallet"] == WALLET
    assert ctx["their_price"] == 0.50 and ctx["cond"] == "0xc0nd"
    assert not eng.state.get("missed"), eng.state.get("missed")
    assert TOK not in eng.state["my_pos"]


@case("retry=True rejection -> miss recorded 'twice (re-quote retry)', no re-schedule")
def t2():
    eng = mkengine("fak")
    calls = []
    eng.on_fak_reject = calls.append
    their_buy(eng, retry=True)
    assert not calls, "retry must never re-schedule"
    m = eng.state["missed"]
    assert len(m) == 1 and "twice (re-quote retry)" in m[0]["reason"], m


@case("no hook installed -> miss recorded at once (pre-change behavior)")
def t3():
    eng = mkengine("fak")
    their_buy(eng)
    m = eng.state["missed"]
    assert len(m) == 1 and m[0]["reason"].startswith("order rejected: "), m


@case("non-FAK rejection -> no hook, miss recorded at once")
def t4():
    eng = mkengine("other")
    calls = []
    eng.on_fak_reject = calls.append
    their_buy(eng)
    assert not calls
    assert len(eng.state["missed"]) == 1


@case("FAK reject on an ADD -> no hook, no miss (adds were never miss-tracked)")
def t5():
    eng = mkengine("fak")
    calls = []
    eng.on_fak_reject = calls.append
    eng.state["my_pos"][TOK] = {"shares": 2.0, "cost": 1.0,
                                "title": "Test market", "outcome": "Yes"}
    eng.state["their_pos"][WALLET] = {TOK: 100.0}
    eng._handle_their_buy(WALLET, TOK, 100.0, 0.50, "Yes · Test market",
                          "Test market", "Yes")
    assert not calls and not eng.state.get("missed")


@case("retry fill sizes off their FULL stake (their_size=0, no double count)")
def t6():
    eng = mkengine("ok")
    their_buy(eng, retry=True, their_size=5.0)   # their whole bet: 5 shares
    pos = eng.state["my_pos"].get(TOK)
    assert pos, "retry fill should open the position"
    # stake = min(4% × $100 equity, their 5) = $4, not capped by a doubled 10
    assert abs(pos["cost"] - 4.0) < 1e-6, pos


@case("end-to-end: Copybot retry thread re-buys, books bet + lag, no miss")
def t7():
    eng = mkengine("fak", "ok")                  # first buy dies, retry fills
    bot = copybot.Copybot(eng.cfg, eng, filt=None)
    bot.here = ""
    bot.check_book = lambda: None                # network-touching, not under test
    copybot.book_depth = lambda t: None          # _record_lag's book snapshot
    bot.fak_retry_niche = {}                     # force the scalar fallback
    bot.fak_retry_s = 0.05
    eng.on_fak_reject = bot.fak_requote_retry
    their_buy(eng)                               # reject -> schedules the retry
    assert TOK not in eng.state["my_pos"]
    time.sleep(1.0)                              # let the daemon thread run
    pos = eng.state["my_pos"].get(TOK)
    assert pos and pos["shares"] > 0, "retry did not fill"
    assert eng.ex.calls == 2, f"buy attempts: {eng.ex.calls}"
    b = eng.state["bets"].get(TOK)
    assert b and b["status"] == "open" and b["their_price"] == 0.50, b
    assert not eng.ex.fills, "fill left undrained"
    assert not eng.state.get("missed"), eng.state["missed"]



@case("per-niche retry waits: measured map, first-match classing, fallback")
def t8():
    eng = mkengine("fak")
    bot = copybot.Copybot(eng.cfg, eng, filt=None)
    assert bot._fak_wait("Ethereum above 1,900 on July 20, 12PM ET?") == 4.0
    assert bot._fak_wait("LoL: G2 Esports vs LYON - Game 2 Winner") == 10.0
    assert bot._fak_wait("Will Argentina win the 2026 FIFA World Cup?") == 25.0
    assert bot._fak_wait("Israel x Iran ceasefire continues through July 20?") == 25.0
    assert bot._fak_wait("Completely unclassifiable market") == 25.0  # other
    assert bot._fak_wait("Wimbledon: Alcaraz set winner") == bot.fak_retry_s  # tennis: no measurement -> fallback
    eng.cfg["fak_retry_niche_s"] = {"crypto": 2}
    bot2 = copybot.Copybot(eng.cfg, eng, filt=None)
    assert bot2._fak_wait("Bitcoin above 63,400 on July 17, 3PM ET?") == 2.0



print()
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
