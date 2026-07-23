#!/usr/bin/env python3
"""T11 EXPLORATORY (2026-07-23) — the PATIENCE CURVE for maker copy
entries: #20 rests at the sharp's price for 60s because that is what T3
tested, not because 60s is optimal. Same universe and fill convention as
T3 (copy_maker_entry.py — fill = later tape print <= our bid; queue
optimism stated), extended to the full TTL grid, with the two numbers T3
did not produce:

  1. time-to-touch distribution among fills (median/p75/p90 seconds) —
     where the fills actually live on the clock;
  2. HYBRID EV/signal — maker fill inside the TTL, else taker fallback at
     the first print AFTER TTL expiry (within 10 min; approximates the
     then-current ask from the tape; miss if no print) — the policy a bot
     with a fallback would actually run. #20 as deployed has NO fallback
     (pure-maker column is the deployed comparator).

Chain-true grading (payouts_for — scorer law), refunds excluded.
Adverse-selection split (fill rate among eventual winners vs losers) per
TTL — T3's signature stat — decides where patience turns toxic.
NOT pre-registered — exploration to tune #20's maker_ttl_s knob.

VERDICT (2026-07-23 run, n=58 chain-graded): 60s IS ALREADY OPTIMAL.
Touch median 1-2s, p90 3-7s; fill rate 90% @60s -> 97% @5m and FLAT
thereafter (zero touches after 5m). The marginal 60s->5m fills LOWER
EV/signal (+16.62 @60s vs +15.36 @5m+) — at 5m+ the loser fill-rate is
100% (every eventual loser returns to the bid; the 3% never-filled are
winners running away). Hybrid taker-fallback adds nothing (<=4 events).
Taker baseline +12.58. #20's maker_ttl_s=60 stands; patience past 60s
only harvests adverse selection."""
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TTLS = [(60, "60s"), (300, "5m"), (1800, "30m"), (14400, "4h"),
        (86400, "24h"), (None, "to-res")]
FEE = 0.03
FALLBACK_S = 600


def main():
    db = tape.connect()
    t_lo, t_hi = db.execute("SELECT min(ts), max(ts) FROM trades").fetchone()
    tape.build_resolved(db)
    fills = []
    for path, book in ((os.path.join(ROOT, "copybot_fills.jsonl"), "paper"),
                       (os.path.join(ROOT, "copybot_fills.live.jsonl"),
                        "live")):
        for ln in open(path):
            r = json.loads(ln)
            if (r.get("side") == "SELL" or r.get("untracked")
                    or not r.get("their_price") or not r.get("my_price")):
                continue
            sig_ts = r["ts"] - (r.get("detect_lag_s") or 0)
            if not (t_lo + 60 <= sig_ts <= t_hi - 1800):
                continue
            fills.append({"book": book, "token": str(r["token"]),
                          "sig_ts": sig_ts, "p": r["their_price"],
                          "my_px": r["my_price"]})
    print(f"copy signals with tape coverage: {len(fills)}", flush=True)
    pays = fwd.payouts_for(db, [f["token"] for f in fills])
    graded = [f for f in fills if pays.get(f["token"]) is not None
              and pays.get(f["token"]) != 0.5]
    print(f"chain-graded (refunds excluded): {len(graded)}", flush=True)

    tk_pnl = 0.0
    for f in graded:
        pay = pays[f["token"]]
        sh = 100.0 / f["my_px"]
        tk_pnl += sh * (pay - f["my_px"]) \
            - FEE * sh * min(f["my_px"], 1 - f["my_px"])
    print(f"TAKER baseline @$100/signal: n={len(graded)} · "
          f"EV/signal {tk_pnl/len(graded):+.2f}\n", flush=True)

    # one touch-time query per signal covers every TTL (first touch ever)
    touch = {}
    for i, f in enumerate(graded):
        r = db.execute("""SELECT min(ts) FROM trades WHERE asset = ?
            AND ts > ? AND price <= ?""",
            [f["token"], f["sig_ts"], f["p"]]).fetchone()
        touch[i] = r[0]
        if i % 200 == 0:
            print(f"  touch scan {i}/{len(graded)}", flush=True)

    for ttl_s, tag in TTLS:
        mk = hy = 0.0
        n_fill = n_fb = n_miss = 0
        waits = []
        win_fill = lose_fill = win_all = lose_all = 0
        for i, f in enumerate(graded):
            pay = pays[f["token"]]
            win_all += pay == 1
            lose_all += pay == 0
            t_t = touch[i]
            hi = f["sig_ts"] + ttl_s if ttl_s else t_hi
            if t_t is not None and t_t <= hi:
                n_fill += 1
                waits.append(t_t - f["sig_ts"])
                sh = 100.0 / f["p"]
                pnl = sh * (pay - f["p"])          # maker: no taker fee
                mk += pnl
                hy += pnl
                win_fill += pay == 1
                lose_fill += pay == 0
            else:
                # hybrid: taker fallback at first print after TTL expiry
                if ttl_s:
                    fb = db.execute("""SELECT price FROM trades
                        WHERE asset = ? AND ts > ? AND ts <= ?
                        ORDER BY ts LIMIT 1""",
                        [f["token"], hi, hi + FALLBACK_S]).fetchone()
                    if fb is not None:
                        fp = float(fb[0])
                        if 0.01 <= fp <= 0.99:
                            sh = 100.0 / fp
                            hy += sh * (pay - fp) \
                                - FEE * sh * min(fp, 1 - fp)
                            n_fb += 1
                        else:
                            n_miss += 1
                    else:
                        n_miss += 1
                else:
                    n_miss += 1
        n = len(graded)
        fr = n_fill / n
        med = int(st.median(waits)) if waits else 0
        p90 = int(sorted(waits)[int(0.9 * len(waits))]) if waits else 0
        fr_w = win_fill / max(win_all, 1)
        fr_l = lose_fill / max(lose_all, 1)
        print(f"TTL {tag:>6}: fill {fr:5.0%} ({n_fill}) · "
              f"touch med {med}s p90 {p90}s · "
              f"MAKER EV/sig {mk/n:+6.2f} · "
              f"HYBRID EV/sig {hy/n:+6.2f} (fb {n_fb}, miss {n_miss}) · "
              f"fill-rate W {fr_w:.0%} vs L {fr_l:.0%}"
              f"{'  <- adverse' if fr_l > fr_w + 0.1 else ''}", flush=True)


if __name__ == "__main__":
    main()
