#!/usr/bin/env python3
"""T3 EXPLORATORY (2026-07-23) — maker-entry copy execution: instead of
FAK-taking a copy signal (pays the ~1.9% taker fee + slippage, always
fills), rest a bid at the sharp's own print price. Fill = a later tape
print at <= that price (the book crossed through our level; queue-position
optimism stated). Missed = no fill in the window (EV 0, capital free).

The trade-off being measured: fee+slippage savings + better entries vs
fill-rate loss and ADVERSE SELECTION (bids fill preferentially on the way
DOWN — winners run away unfilled, losers come to you). The split of fill
rate by eventual outcome is THE number.

Universe: every non-untracked BUY fill in both books with their_price and
tape coverage. Windows 60s/5m/30m/to-resolution. Chain-true grading
(payouts_for). NOT pre-registered — exploration for a possible execution
change behind the mirror-exactly discipline."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = [(60, "60s"), (300, "5m"), (1800, "30m"), (None, "to-res")]
FEE = 0.03


def main():
    db = tape.connect()
    t_lo, t_hi = db.execute("SELECT min(ts), max(ts) FROM trades").fetchone()
    tape.build_resolved(db)
    fills = []
    for path, book in ((os.path.join(ROOT, "copybot_fills.jsonl"), "paper"),
                       (os.path.join(ROOT, "copybot_fills.live.jsonl"), "live")):
        for ln in open(path):
            r = json.loads(ln)
            if (r.get("side") == "SELL" or r.get("untracked")
                    or not r.get("their_price") or not r.get("my_price")):
                continue
            sig_ts = r["ts"] - (r.get("detect_lag_s") or 0)
            if not (t_lo + 60 <= sig_ts <= t_hi - 1800):
                continue                      # need tape around the signal
            fills.append({"book": book, "token": str(r["token"]),
                          "sig_ts": sig_ts, "p": r["their_price"],
                          "my_px": r["my_price"], "shares": r.get("shares", 0),
                          "fee": r.get("fee") or 0,
                          "cost": r.get("cost") or r["my_price"] * r.get("shares", 0)})
    print(f"copy signals with tape coverage: {len(fills)}")
    pays = fwd.payouts_for(db, [f["token"] for f in fills])

    graded = [f for f in fills if pays.get(f["token"]) is not None
              and pays.get(f["token"]) != 0.5]
    print(f"chain-graded (refunds excluded): {len(graded)}")
    # taker baseline: what actually happened, normalized to $100 stakes
    tk_pnl = 0.0
    for f in graded:
        pay = pays[f["token"]]
        sh = 100.0 / f["my_px"]
        tk_pnl += sh * (pay - f["my_px"]) - FEE * sh * min(f["my_px"], 1 - f["my_px"])
    print(f"\nTAKER baseline @$100/signal: {len(graded)} fills · "
          f"EV/signal {tk_pnl/len(graded):+.2f}")

    for win_s, tag in WINDOWS:
        mk_pnl = 0.0
        n_fill = 0
        win_fill = lose_fill = win_all = lose_all = 0
        for f in graded:
            pay = pays[f["token"]]
            (win_all, lose_all) = (win_all + (pay == 1), lose_all + (pay == 0))
            hi = f["sig_ts"] + win_s if win_s else t_hi
            r = db.execute("""SELECT min(ts) FROM trades WHERE asset = ?
                AND ts > ? AND ts <= ? AND price <= ?""",
                [f["token"], f["sig_ts"], hi, f["p"]]).fetchone()
            if r[0] is None:
                continue                      # bid never touched — no fill
            n_fill += 1
            sh = 100.0 / f["p"]
            mk_pnl += sh * (pay - f["p"])     # maker: no taker fee
            if pay == 1:
                win_fill += 1
            else:
                lose_fill += 1
        fr = n_fill / len(graded)
        fr_w = win_fill / max(win_all, 1)
        fr_l = lose_fill / max(lose_all, 1)
        print(f"MAKER bid@their_px, window {tag:>6}: fill {fr:5.0%} "
              f"({n_fill}) · EV/signal {mk_pnl/len(graded):+6.2f} · "
              f"EV/fill {mk_pnl/max(n_fill,1):+6.2f} · "
              f"fill-rate winners {fr_w:.0%} vs losers {fr_l:.0%}"
              f"{'  ⚠ adverse' if fr_l > fr_w + 0.1 else ''}")


if __name__ == "__main__":
    main()
