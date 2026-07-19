"""Stub test for Copybot.sweep_dust (2026-07-17): sells only untracked
live-market residue, books an adjustment (not P&L), skips my_pos/pending/
floor/redeemable. Run: python3 tests/test_dust_sweep.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import copybot  # noqa: E402


class Ex:
    live = True

    def __init__(self):
        self.fills, self.sold = [], []

    def _shares_held(self, tok):
        # chain truth for the audit-3.2 gate: DUST really exists on chain
        return {"DUST": 1.0}.get(tok, 0.0)

    def sell(self, tok, shares, price, meta):
        self.sold.append(tok)
        self.fills.append({"side": "SELL", "token": tok, "shares": shares,
                           "price": price})
        return {"ok": True, "filled_shares": shares, "price": price}


class Eng:
    def __init__(self, st):
        self.state, self.ex, self.alerts = st, Ex(), []

    def persist(self):
        pass

    def alert(self, m, discord_text=None):
        self.alerts.append(m)


st = {"my_pos": {"HELD": {}}, "pending_orders": [{"token": "PEND"}],
      "exit_retries": [], "cash": 10.0, "adjustments": [], "bets": {}}
bot = copybot.Copybot.__new__(copybot.Copybot)
bot.engine = Eng(st)
bot.fee_rate = 0.03
import threading
bot.lock = threading.Lock()

POS = [
    {"asset": "HELD", "currentValue": 5.0, "curPrice": 0.5, "size": 10},      # book-open
    {"asset": "PEND", "currentValue": 5.0, "curPrice": 0.5, "size": 10},      # pending
    {"asset": "TINY", "currentValue": 0.05, "curPrice": 0.5, "size": 0.1},    # under floor
    {"asset": "DEAD", "currentValue": 0.0, "curPrice": 0.0, "size": 2,
     "redeemable": True},                                                     # worthless
    {"asset": "DUST", "currentValue": 0.37, "curPrice": 0.37, "size": 1.0,
     "title": "Team Yandex dust"},                                            # sweep me
]
copybot.sm.get_json = lambda path, params=None: POS
copybot.clob_price = lambda tok, side: 0.37

bot.sweep_dust(cycle=5)
fails = []
if bot.engine.ex.sold != ["DUST"]:
    fails.append(f"sold wrong set: {bot.engine.ex.sold}")
fee = copybot.taker_fee(1.0, 0.37, 0.03)
want_cash = 10.0 + 0.37 - fee
if abs(st["cash"] - want_cash) > 1e-9:
    fails.append(f"cash {st['cash']} != {want_cash}")
if not (st["adjustments"] and abs(st["adjustments"][0]["amount"] - (0.37 - fee)) < 1e-9):
    fails.append(f"adjustment wrong: {st['adjustments']}")
# realized invariant: cash delta fully covered by the adjustment
if not bot.engine.alerts:
    fails.append("no alert sent")
# off-schedule cycles do nothing
bot.sweep_dust(cycle=6)
if len(bot.engine.ex.sold) != 1:
    fails.append("swept on an off-schedule cycle")

print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
