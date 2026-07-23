#!/usr/bin/env python3
"""T12 EXPLORATORY (2026-07-23) — maker-sharp UNWIND leans: the sell side
of T6/Study C. When a screened maker-sharp builds a directional inventory
(>= $150 one-sided) and then takes back >= half of it, is the unwind an
exit signal (the market should be faded / a Study-C hold should exit) or
bankroll ops like taker-sharp exits (#21: exits are noise)?

Walk-forward, no self-selection: per tape day D the maker-sharp set is
screened on tape < D only (maker_lean.screen_asof — same discipline as
T6). During D, per (wallet, asset): running net/gross from orders_matched
MAKER fills; an unwind fires ONCE when peak |net|*px >= $150 (a T6-grade
lean existed) AND |net| has dropped to <= 50% of peak with the same sign.

Scored at the unwind print, chain-true (payouts_for — scorer law):
  STAY  $100 on the ORIGINAL lean side from the unwind print — what a
        no-exit-rule Study C book experiences from this moment on.
        STAY >= 0  => unwinds are noise, hold through (mirrors #21).
  FADE  $100 against the original lean — is the unwind actively
        informative in reverse?
Comparator for STAY is literally 0 (exiting at the unwind print).

FROZEN v0 params (declared before the run, not tuned after):
  build   |net|*px >= $150 AND |net|/gross >= 0.6 at peak (T6 trigger)
  unwind  |net| <= 0.5 * peak|net|, same sign, first per (w,a,day)
  price   lean-side last print in [0.05, 0.95] at unwind
Kill bar for the idea: STAY EV/unwind >= 0 at n >= 100 (exit rule adds
nothing); informative if STAY <= -$3/unwind at n >= 100."""
import json
import os
import sys
import time

sys.path.insert(0, "/Users/jaxmakielski/polymarket-smart-money/research")
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402
import maker_lean as ml                        # noqa: E402

LEAN_USD = 150.0        # FROZEN — must equal maker_lean.py
NET_GROSS = 0.6         # FROZEN — must equal maker_lean.py
UNWIND_FRAC = 0.5
BAND = (0.05, 0.95)


def day_unwinds(db, lo, hi, sharps):
    rows = db.execute("""
      SELECT lower(json_extract_string(payload,'$.proxyWallet')) w,
             json_extract_string(payload,'$.asset') a,
             json_extract_string(payload,'$.side') s,
             cast(json_extract(payload,'$.price') AS DOUBLE) p,
             cast(json_extract(payload,'$.size') AS DOUBLE) z, ts
      FROM aux WHERE type='orders_matched' AND ts >= ? AND ts < ?
      ORDER BY ts""", [lo, hi]).fetchall()
    book, fired, out = {}, set(), []
    for w, a, s_, p, z, ts in rows:
        if w not in sharps or (w, a) in fired:
            continue
        st = book.setdefault((w, a), [0.0, 0.0, 0.0, False])
        #      [net, gross, peak_net_abs_at_qualifying_lean, lean_armed]
        st[0] += z if s_ == "BUY" else -z
        st[1] += z
        net, gross = st[0], st[1]
        if gross < 1e-9:
            continue
        # arm (or re-peak) the lean state at each new extreme
        if abs(net) > st[2]:
            px_row = db.execute("""SELECT price FROM trades WHERE asset=?
                AND ts<=? ORDER BY ts DESC LIMIT 1""", [a, ts]).fetchone()
            if px_row is not None:
                px_now = float(px_row[0])
                if (abs(net) * px_now >= LEAN_USD
                        and abs(net) / gross >= NET_GROSS):
                    st[2] = abs(net)
                    st[3] = net > 0          # sign of the armed lean
        # unwind: armed lean and net back to <= half the peak, same sign
        if st[2] > 0 and (net > 0) == st[3] \
                and abs(net) <= UNWIND_FRAC * st[2]:
            px_row = db.execute("""SELECT price FROM trades WHERE asset=?
                AND ts<=? ORDER BY ts DESC LIMIT 1""", [a, ts]).fetchone()
            if px_row is None:
                continue
            px = float(px_row[0])
            lean_px = px if st[3] else 1 - px
            if not (BAND[0] <= lean_px <= BAND[1]):
                continue
            fired.add((w, a))
            out.append({"w": w, "a": a, "ts": ts,
                        "side": 1 if st[3] else -1,
                        "peak_usd": st[2] * px, "px": px,
                        "lean_px": lean_px})
    return out


def main():
    dump = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".maker_unwind_triggers.json")
    db = tape.connect()
    if "--grade-only" in sys.argv:
        # walk already done; grade the dumped triggers (lets a re-grade
        # queue behind another process's cache.duckdb write lock)
        triggers = json.load(open(dump))
        print(f"grade-only: {len(triggers)} dumped triggers", flush=True)
    else:
        t_lo, t_hi = db.execute(
            "SELECT min(ts), max(ts) FROM aux WHERE type='orders_matched'"
        ).fetchone()
        day0 = int(t_lo // 86400 + 2)
        days = [d * 86400 for d in range(day0, int(t_hi // 86400) + 1)]
        print(f"walk-forward days: {len(days)}", flush=True)
        triggers = []
        for lo in days:
            hi = min(lo + 86400, t_hi)
            sharps = ml.screen_asof(db, lo)
            d_str = time.strftime("%m-%d", time.gmtime(lo))
            if not sharps:
                print(f"{d_str}: 0 screened wallets", flush=True)
                continue
            found = day_unwinds(db, lo, hi, sharps)
            for t in found:
                t["day"] = d_str
            triggers.extend(found)
            print(f"{d_str}: {len(sharps)} screened · {len(found)} unwinds",
                  flush=True)
        print(f"total unwinds: {len(triggers)}", flush=True)
        json.dump(triggers, open(dump, "w"))
    pays = fwd.payouts_for(db, [t["a"] for t in triggers])
    graded = [(t, pays.get(t["a"])) for t in triggers]
    graded = [(t, p) for t, p in graded if p is not None and p != 0.5]

    def report(tag, rs):
        if not rs:
            print(f"{tag}: 0 graded")
            return
        n = len(rs)
        stay = fade = 0.0
        holds = 0
        for t, p in rs:
            lean_pay = p if t["side"] > 0 else 1 - p
            sh = 100.0 / t["lean_px"]
            stay += sh * (lean_pay - t["lean_px"])
            shf = 100.0 / (1 - t["lean_px"])
            fade += shf * ((1 - lean_pay) - (1 - t["lean_px"]))
            holds += lean_pay == 1.0
        print(f"{tag}: n={n} · lean-side hit {holds/n:.2f} · avg lean px "
              f"{sum(t['lean_px'] for t, _ in rs)/n:.2f} · "
              f"STAY EV/unwind {stay/n:+.2f} · FADE EV/unwind {fade/n:+.2f}")

    print(f"chain-graded: {len(graded)}/{len(triggers)}")
    report("ALL", graded)
    for lo_, hi_, tag in [(150, 500, "$150-500"), (500, 2000, "$500-2k"),
                          (2000, 1e9, "$2k+")]:
        report(f"peak {tag}",
               [(t, p) for t, p in graded if lo_ <= t["peak_usd"] < hi_])
    for d in sorted({t["day"] for t, _ in graded}):
        report(f"day {d}", [(t, p) for t, p in graded if t["day"] == d])
    # event concentration (the #22 fade-arm lesson): top-asset share
    by_a = {}
    for t, p in graded:
        lean_pay = p if t["side"] > 0 else 1 - p
        sh = 100.0 / t["lean_px"]
        by_a[t["a"]] = by_a.get(t["a"], 0.0) + sh * (lean_pay - t["lean_px"])
    if by_a:
        tot = sum(by_a.values())
        top = sorted(by_a.items(), key=lambda kv: -abs(kv[1]))[:5]
        print(f"STAY concentration: total {tot:+.0f} · top-5 assets "
              f"{[round(v) for _, v in top]}")


if __name__ == "__main__":
    main()
