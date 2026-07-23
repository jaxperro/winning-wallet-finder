#!/usr/bin/env python3
"""T6 EXPLORATORY (2026-07-23) — maker inventory-lean: when a screened
maker-sharp stops being balanced and accumulates a directional net
inventory in one market, is the lean informed (follow it) or forced
(fade it)? Two-sided by design; only symmetric-zero kills.

Walk-forward, no self-selection: for each tape day D, the maker-sharp set
is screened on tape < D only (z>=2.5 on resolved-as-of-D maker positions,
same discipline as informed_set); leans are detected during D from that
set's MAKER fills only (orders_matched — inventory absorbed while
quoting, not their deliberate taker bets) and scored to chain truth
(payouts_for — scorer law).

FROZEN v0 params (declared before the run, not tuned after):
  trigger  first crossing per (wallet, asset, day) of
           |net| * vwap >= $150  AND  |net|/gross >= 0.6
  price    lean-side last print in [0.05, 0.95] at trigger
  score    $100 at trigger print -> chain payout; follow-EV and fade-EV
Kill bar: BOTH directions EV <= 0 at n>=100 leans."""
import sys
import time

sys.path.insert(0, "/Users/jaxmakielski/polymarket-smart-money/research")
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

LEAN_USD = 150.0
NET_GROSS = 0.6
BAND = (0.05, 0.95)
SET_MIN_Z, SET_MIN_BETS = 2.5, 6


def screen_asof(db, t_cut):
    """Maker-sharp wallets using tape strictly before t_cut."""
    tape.build_resolved(db, t_end=t_cut)
    rows = db.execute(f"""
    WITH mk AS (
      SELECT lower(json_extract_string(payload,'$.proxyWallet')) wallet,
             json_extract_string(payload,'$.asset') asset,
             json_extract_string(payload,'$.side') side,
             cast(json_extract(payload,'$.price') AS DOUBLE) price,
             cast(json_extract(payload,'$.size') AS DOUBLE) size
      FROM aux WHERE type = 'orders_matched' AND ts < {t_cut}
    ), bets AS (
      SELECT wallet, mk.asset, any_value(tk.payout) payout,
             sum(CASE WHEN side='BUY' THEN size ELSE -size END) net,
             sum(CASE WHEN side='BUY' THEN size*price END)
               / nullif(sum(CASE WHEN side='BUY' THEN size END),0) vwap
      FROM mk JOIN res_tok tk ON mk.asset = tk.asset
      GROUP BY wallet, mk.asset
      HAVING net >= 5 AND vwap BETWEEN 0.05 AND 0.95
    )
    SELECT wallet, count(*) n,
           sum(CASE WHEN payout=1.0 THEN 1 ELSE 0 END) wins,
           sum(vwap) exp_w, sum(vwap*(1-vwap)) var_s,
           sum(net*(payout - vwap)) pnl
    FROM bets GROUP BY wallet
    HAVING n >= {SET_MIN_BETS} AND var_s > 0""").fetchall()
    out = set()
    for w, n, wins, exp_w, var_s, pnl in rows:
        if pnl > 0 and (wins - exp_w) / (var_s ** 0.5) >= SET_MIN_Z:
            out.add(w)
    return out


def main():
    db = tape.connect()
    t_lo, t_hi = db.execute(
        "SELECT min(ts), max(ts) FROM aux WHERE type='orders_matched'"
    ).fetchone()
    day0 = int(t_lo // 86400 + 2)          # >= 2 days of screening tape
    days = [d * 86400 for d in range(day0, int(t_hi // 86400) + 1)]
    print(f"walk-forward days: {len(days)}", flush=True)
    triggers = []
    for lo in days:
        hi = min(lo + 86400, t_hi)
        sharps = screen_asof(db, lo)
        d_str = time.strftime("%m-%d", time.gmtime(lo))
        if not sharps:
            print(f"{d_str}: 0 screened wallets", flush=True)
            continue
        rows = db.execute("""
          SELECT lower(json_extract_string(payload,'$.proxyWallet')) w,
                 json_extract_string(payload,'$.asset') a,
                 json_extract_string(payload,'$.side') s,
                 cast(json_extract(payload,'$.price') AS DOUBLE) p,
                 cast(json_extract(payload,'$.size') AS DOUBLE) z, ts
          FROM aux WHERE type='orders_matched' AND ts >= ? AND ts < ?
          ORDER BY ts""", [lo, hi]).fetchall()
        book = {}                          # (w,a) -> [net, gross, vwap$]
        fired = set()
        n_day = 0
        for w, a, s, p, z, ts in rows:
            if w not in sharps or (w, a) in fired:
                continue
            st = book.setdefault((w, a), [0.0, 0.0])
            st[0] += z if s == "BUY" else -z
            st[1] += z
            net, gross = st
            if gross < 1e-9:
                continue
            px = db.execute("""SELECT price FROM trades WHERE asset=?
                AND ts<=? ORDER BY ts DESC LIMIT 1""", [a, ts]).fetchone()
            if px is None:
                continue
            px = float(px[0])
            lean_px = px if net > 0 else 1 - px   # lean-side price
            if (abs(net) * px >= LEAN_USD
                    and abs(net) / gross >= NET_GROSS
                    and BAND[0] <= lean_px <= BAND[1]):
                fired.add((w, a))
                n_day += 1
                triggers.append({"w": w, "a": a, "ts": ts, "day": d_str,
                                 "side": 1 if net > 0 else -1,
                                 "lean_usd": abs(net) * px,
                                 "px": px, "lean_px": lean_px})
        print(f"{d_str}: {len(sharps)} screened · {n_day} leans", flush=True)
    print(f"total leans: {len(triggers)}", flush=True)
    pays = fwd.payouts_for(db, [t["a"] for t in triggers])
    graded = [(t, pays.get(t["a"])) for t in triggers]
    graded = [(t, p) for t, p in graded if p is not None and p != 0.5]

    def report(tag, rs):
        if not rs:
            print(f"{tag}: 0 graded")
            return
        n = len(rs)
        fol = fad = 0.0
        wins = 0
        for t, p in rs:
            lean_pay = p if t["side"] > 0 else 1 - p   # lean-side payout
            sh = 100.0 / t["lean_px"]
            fol += sh * (lean_pay - t["lean_px"])
            shf = 100.0 / (1 - t["lean_px"])
            fad += shf * ((1 - lean_pay) - (1 - t["lean_px"]))
            wins += lean_pay == 1.0
        print(f"{tag}: n={n} · lean hit {wins/n:.2f} · avg lean px "
              f"{sum(t['lean_px'] for t,_ in rs)/n:.2f} · "
              f"FOLLOW EV/lean {fol/n:+.2f} · FADE EV/lean {fad/n:+.2f}")

    print(f"chain-graded: {len(graded)}/{len(triggers)}")
    report("ALL", graded)
    for lo_, hi_, tag in [(150, 500, "$150-500"), (500, 2000, "$500-2k"),
                          (2000, 1e9, "$2k+")]:
        report(f"lean {tag}",
               [(t, p) for t, p in graded if lo_ <= t["lean_usd"] < hi_])
    for d in sorted({t["day"] for t, _ in graded}):
        report(f"day {d}", [(t, p) for t, p in graded if t["day"] == d])


if __name__ == "__main__":
    main()
