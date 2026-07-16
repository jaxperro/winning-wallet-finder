"""Stub tests for the paper FAK no-match model (HANDOFF 2026-07-15):
PaperExecutor.buy rejects when no ask sits inside the live protected band,
reuses the depth gate's book snapshot, fails open on a dead book fetch, and
LedgerPaperExecutor records no fill row for a rejection. Run:
python3 tests/test_paper_fak.py — needs no network, no config.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import copytrade  # noqa: E402
import copybot    # noqa: E402

fails = []


def case(name):
    def deco(fn):
        bd0 = copytrade.book_depth
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append(name)
        finally:
            copytrade.book_depth = bd0
    return deco


def boom(token_id):
    raise AssertionError("book_depth called — meta['book'] should be reused")


@case("ask inside the band -> fills, and the meta book is reused (no refetch)")
def t1():
    copytrade.book_depth = boom
    ex = copytrade.PaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"book": {"ba": 0.82, "ask5c": 200.0}})
    assert r["ok"] and r["filled_shares"] == 10.0 and r["price"] == 0.80


@case("best ask above quote*(1+5%) -> FAK no-match rejection")
def t2():
    copytrade.book_depth = boom
    ex = copytrade.PaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"book": {"ba": 0.90, "ask5c": 200.0}})
    assert not r["ok"] and r["filled_shares"] == 0.0
    assert "no orders found to match" in r["resp"], r["resp"]


@case("no asks at all -> FAK no-match rejection")
def t3():
    copytrade.book_depth = boom
    ex = copytrade.PaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"book": {"ba": None, "ask5c": None}})
    assert not r["ok"] and "no asks" in r["resp"], r["resp"]


@case("boundary: ask exactly at the cap still fills")
def t4():
    copytrade.book_depth = boom
    ex = copytrade.PaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"book": {"ba": 0.84, "ask5c": 200.0}})
    assert r["ok"], r


@case("no meta book + dead book fetch -> fail OPEN (fills, today's behavior)")
def t5():
    copytrade.book_depth = lambda token_id: None
    ex = copytrade.PaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"title": "x", "book": None})
    assert r["ok"] and r["filled_shares"] == 10.0


@case("no meta book -> fetches its own and applies the band")
def t6():
    copytrade.book_depth = lambda token_id: {"ba": 0.95, "ask5c": 200.0}
    ex = copytrade.PaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"title": "x"})
    assert not r["ok"] and "no orders found to match" in r["resp"]


@case("SELLs stay optimistic-fill (unchanged)")
def t7():
    copytrade.book_depth = boom
    ex = copytrade.PaperExecutor()
    r = ex.sell("T", 10.0, 0.80, {"book": {"ba": None}})
    assert r["ok"] and r["filled_shares"] == 10.0


@case("LedgerPaperExecutor: rejection appends NO fill row; success appends")
def t8():
    copytrade.book_depth = boom
    ex = copybot.LedgerPaperExecutor()
    r = ex.buy("T", 10.0, 0.80, {"book": {"ba": 0.95, "ask5c": 200.0}})
    assert not r["ok"] and ex.fills == [], ex.fills
    r = ex.buy("T", 10.0, 0.80, {"book": {"ba": 0.81, "ask5c": 200.0}})
    assert r["ok"] and len(ex.fills) == 1 and ex.fills[0]["shares"] == 10.0


print()
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
