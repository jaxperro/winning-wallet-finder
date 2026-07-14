"""Stub tests for the chain-seed path (HANDOFF 2026-07-14): fills_from_tx
receipt decoding, forged-event rejection, addresses_in_payload tx hints, and
the on_wallet_activity hint funnel. Run: python3 tests/test_chainseed.py —
needs no network, no cache, no config; stubs the RPC and the engine.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import copybot  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="copybot_test_")
WALLET = "0xbadaf319415c17f28824a43ae0cd912b9d84d874"
OTHER = "0x30fb41b5a08fff5d3dd983f6323e3343931a7db4"
EXCH = "0xe111180000d2663c0091e4f400237545b87b996b"
TOK = "113883491847873555347283937250168614361193102079209733484211339732062843429513"


def topic_addr(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def word(v):
    return hex(v)[2:].rjust(64, "0")


def order_filled_log(maker, cls, token, given, taken, address=EXCH,
                     topic0=copybot._ORDER_FILLED):
    return {"address": address,
            "topics": [topic0, "0x" + "ab" * 32,
                       topic_addr(maker), topic_addr(EXCH)],
            "data": "0x" + word(cls) + word(int(token))
                    + word(given) + word(taken) + word(0) + word(0) + word(0)}


def stub_rpc(receipt_logs, block_ts):
    def _rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"blockNumber": "0x100", "logs": receipt_logs}
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(block_ts)}
        raise AssertionError(f"unexpected rpc {method}")
    return _rpc


fails = []


def case(name):
    def deco(fn):
        rpc0, url0, meta0 = copybot._rpc, copybot._RPC_URL, copybot._token_market
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append(name)
        finally:
            copybot._rpc, copybot._RPC_URL = rpc0, url0
            copybot._token_market = meta0
    return deco


def with_meta():
    copybot._token_market = lambda tok: ("Test Market?", "Yes", "0xcond1")


@case("BUY decodes: cash given -> size/price/usd from the maker row")
def t1():
    copybot._RPC_URL = "stub"
    ts = int(time.time()) - 3
    copybot._rpc = stub_rpc([
        # counterparty's own row — must be ignored (maker != wallet)
        order_filled_log(OTHER, 0, TOK, 17_000_000, 20_000_000),
        # the wallet's row: gave $3.00 cash, took 20 tokens -> BUY 20 @ 0.15
        order_filled_log(WALLET, 0, TOK, 3_000_000, 20_000_000),
    ], ts)
    with_meta()
    fills = copybot.fills_from_tx("0xt1", WALLET)
    assert len(fills) == 1, f"expected 1 fill, got {len(fills)}"
    f = fills[0]
    assert f["side"] == "BUY" and f["asset"] == TOK
    assert abs(f["size"] - 20.0) < 1e-9 and abs(f["price"] - 0.15) < 1e-9
    assert abs(f["usdcSize"] - 3.0) < 1e-9 and f["timestamp"] == ts
    assert f["title"] == "Test Market?" and f["conditionId"] == "0xcond1"


@case("SELL decodes: tokens given -> reversed amounts")
def t2():
    copybot._RPC_URL = "stub"
    copybot._rpc = stub_rpc([
        # gave 10.68 tokens, took $5.1264 -> SELL 10.68 @ 0.48
        order_filled_log(WALLET, 1, TOK, 10_680_000, 5_126_400),
    ], 1)
    with_meta()
    f = copybot.fills_from_tx("0xt2", WALLET)[0]
    assert f["side"] == "SELL"
    assert abs(f["size"] - 10.68) < 1e-9 and abs(f["price"] - 0.48) < 1e-9


@case("forged events rejected: wrong emitter, wrong topic")
def t3():
    copybot._RPC_URL = "stub"
    copybot._rpc = stub_rpc([
        # right shape, but emitted by a contract that is NOT a known exchange
        order_filled_log(WALLET, 0, TOK, 3_000_000, 20_000_000,
                         address="0x" + "66" * 20),
        # right emitter, wrong event signature
        order_filled_log(WALLET, 0, TOK, 3_000_000, 20_000_000,
                         topic0="0x" + "77" * 32),
    ], 1)
    with_meta()
    assert copybot.fills_from_tx("0xt3", WALLET) == []


@case("fail-open: no RPC configured / metadata lookup down -> []")
def t4():
    copybot._RPC_URL = None
    assert copybot.fills_from_tx("0xt4", WALLET) == []
    copybot._RPC_URL = "stub"
    copybot._rpc = stub_rpc([
        order_filled_log(WALLET, 0, TOK, 3_000_000, 20_000_000)], 1)
    copybot._token_market = lambda tok: None            # gamma down
    assert copybot.fills_from_tx("0xt4", WALLET) == []


@case("addresses_in_payload maps watched wallets to their tx hashes")
def t5():
    payload = {"event": {"activity": [
        {"fromAddress": WALLET, "toAddress": EXCH, "hash": "0xaa"},
        {"fromAddress": EXCH, "toAddress": WALLET, "hash": "0xaa"},  # dup tx
        {"fromAddress": WALLET, "toAddress": EXCH, "hash": "0xbb"},
        {"fromAddress": OTHER, "toAddress": EXCH, "hash": "0xcc"},   # unwatched
    ]}}
    got = copybot.addresses_in_payload(payload, {WALLET.lower()})
    assert got == {WALLET.lower(): ["0xaa", "0xbb"]}, got


@case("hint funnel: unseen hash seeds handle_trade; seen/skipped hashes don't")
def t6():
    calls = []

    class Eng:
        seen = {"0xseen"}
        state = {"my_pos": {}, "trade_cursor": {}}

        def handle_trade(self, wallet, t):
            calls.append(t["transactionHash"])
            self.seen.add(t["transactionHash"])

    class Bot(copybot.Copybot):
        def __init__(self):                             # bare wiring only
            self.engine = Eng()
            self.names = {}
            self.skipped = {"0xskip"}
            self.conds = {}
            self.lock = __import__("threading").Lock()

        def _fetch_since_cursor(self, wallet):
            return []                                   # the indexer is lagging

        def _drain_fills(self):
            return []

        def check_book(self):
            return 0.0

    class Filt:
        def check(self, wallet, t):
            return True, None

    bot = Bot()
    bot.filt = Filt()
    fresh = {"transactionHash": "0xnew", "asset": TOK, "side": "BUY",
             "size": 20.0, "price": 0.15, "usdcSize": 3.0,
             "title": "Test Market?", "outcome": "Yes",
             "conditionId": "0xcond1", "timestamp": int(time.time())}
    decoded = {"0xnew": [fresh]}
    real = copybot.fills_from_tx
    copybot.fills_from_tx = lambda tx, w: decoded.get(tx, [])
    try:
        bot.on_wallet_activity(WALLET, hint_txs=["0xseen", "0xskip", "0xnew"])
    finally:
        copybot.fills_from_tx = real
    assert calls == ["0xnew"], calls


print()
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
