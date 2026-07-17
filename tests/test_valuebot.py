"""Stub tests for the value paper bot (value/PLAN.md V0): honest FAK fill
model, event cap, cooldown, refund-aware chain settlement. No network.
Run: python3 tests/test_valuebot.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "value"))
import valuebot as vb  # noqa: E402

vb.STATE = "/tmp/vb_test_state.json"
vb.FILLS = "/tmp/vb_test_fills.jsonl"
vb.FEED = "/tmp/vb_test_feed.json"

fails = []


def case(name):
    def deco(fn):
        ba0, op0 = vb.book_asks, vb.onchain_payouts
        for f in (vb.STATE, vb.FILLS, vb.FEED):
            if os.path.exists(f):
                os.remove(f)
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append(name)
        finally:
            vb.book_asks, vb.onchain_payouts = ba0, op0
    return deco


def cand(tok, event="ev-2026-07-17", mark=0.01):
    return {"token": tok, "outcome": "Yes", "mark": mark, "cond": "0xc" + tok,
            "title": f"mkt {tok}", "end": "2026-01-01T00:00:00Z",  # already past
            "cat": "esports", "event": event, "tok_index": 0, "n_outcomes": 2}


@case("fill model: walks the ladder inside the band, honest miss when thin")
def t1():
    # $1 at 1c needs 100 shares; 60 at 0.010 + 50 at 0.0104 (inside 1.05 band)
    sh, px, r = vb.model_fill([(0.010, 60), (0.0104, 50)], 1.0, 0.02)
    assert r is None and abs(sh - (0.6/0.010 + 0.4/0.0104)) < 1e-6
    # thin: only $0.30 inside the band -> MISS
    sh, px, r = vb.model_fill([(0.010, 30), (0.02, 1000)], 1.0, 0.02)
    assert sh is None and "FAK no-match" in r, r
    # best ask above the bucket -> not a candidate
    sh, px, r = vb.model_fill([(0.03, 1000)], 1.0, 0.02)
    assert sh is None and "above" in r
    # empty/failed book
    assert vb.model_fill([], 1.0, 0.02)[2] == "no asks on the book"
    assert vb.model_fill(None, 1.0, 0.02)[2] == "book fetch failed"


@case("band: a 1.9c best ask does NOT walk into 2.5c levels")
def t2():
    sh, px, r = vb.model_fill([(0.019, 30), (0.025, 1000)], 1.0, 0.02)
    assert sh is None, "walked past min(MAX_PX, best*1.05)"


@case("event cap 1: second ticket on the same event is skipped")
def t3():
    st = vb.load_state()
    vb.book_asks = lambda tok: [(0.01, 1000)]
    vb.open_positions(st, [cand("A", "ev-2026-07-17"),
                           cand("B", "ev-2026-07-17"),
                           cand("C", "other-2026-07-18")], budget=10)
    assert set(st["my_pos"]) == {"A", "C"}, set(st["my_pos"])
    assert st["stats"]["fills"] == 2 and st["stats"]["attempts"] == 2


@case("cooldown: a missed token is not re-checked inside COOLDOWN_S")
def t4():
    st = vb.load_state()
    vb.book_asks = lambda tok: [(0.01, 10)]          # thin -> miss
    vb.open_positions(st, [cand("A")], budget=10)
    assert st["stats"]["misses"] == 1
    vb.open_positions(st, [cand("A")], budget=10)    # immediately again
    assert st["stats"]["attempts"] == 1, "re-attempted inside cooldown"


@case("settle: win pays full, refund pays 0.5, loss pays 0 — cash exact")
def t5():
    st = vb.load_state()
    vb.book_asks = lambda tok: [(0.01, 1000)]
    for t_ in ("W", "R", "L"):
        vb.open_positions(st, [cand(t_, event=t_)], budget=10)
    cash_after_open = st["cash"]
    vecs = {"0xcW": [1.0, 0.0], "0xcR": [0.5, 0.5], "0xcL": [0.0, 1.0]}
    vb.onchain_payouts = lambda cond, rpc: vecs[cond]
    vb.settle(st, rpc="stub", budget=10)
    s = st["stats"]
    assert (s["wins"], s["refunds"], s["losses"]) == (1, 1, 1)
    sh = 1.0 / 0.01
    assert abs(st["cash"] - (cash_after_open + sh*1.0 + sh*0.5)) < 1e-6
    assert not st["my_pos"]


@case("unresolved market stays open; feed math consistent")
def t6():
    st = vb.load_state()
    vb.book_asks = lambda tok: [(0.01, 1000)]
    vb.open_positions(st, [cand("A")], budget=10)
    vb.onchain_payouts = lambda cond, rpc: None      # denominator 0
    vb.settle(st, rpc="stub", budget=10)
    assert "A" in st["my_pos"] and st["stats"]["resolved"] == 0
    feed = vb.write_feed(st)
    assert feed["open_count"] == 1 and feed["deployed"] == 1.0
    assert feed["fill_rate"] == 1.0


print()
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
