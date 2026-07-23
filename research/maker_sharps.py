#!/usr/bin/env python3
"""T2 EXPLORATORY (2026-07-23) — maker-sharp selection: mine the maker side
of every match (aux orders_matched, ~6M wallet-attributed rows the screens
have never touched). Do improbably-winning MAKERS exist — the species
farming the crater wall — and are they a distinct, followable cohort?

Method mirrors the taker screen (study_flow.informed_set): per wallet-asset
net maker position (maker BUY = resting bid filled), entry vwap, resolved
via tape proxy (chain-validated 742/742 method); wallet improbability
z = (wins − Σp)/sqrt(Σp(1−p)) on n≥6 resolved bets with net≥5sh, vwap in
[0.05,0.95], pnl>0; z≥2.5 qualifies. Readouts: cohort size, top wallets,
overlap vs the taker informed set + watch_sharps (distinct species?),
pooled per-bet EV of qualifying makers' resolved bets. NOT pre-registered;
a forward table row + copyability (lag) study only if a cohort exists."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SET_MIN_Z, SET_MIN_BETS = 2.5, 6


def main():
    db = tape.connect()
    tape.build_resolved(db)
    rows = db.execute("""
    WITH mk AS (
      SELECT lower(json_extract_string(payload,'$.proxyWallet')) wallet,
             json_extract_string(payload,'$.asset') asset,
             json_extract_string(payload,'$.side') side,
             cast(json_extract(payload,'$.price') AS DOUBLE) price,
             cast(json_extract(payload,'$.size') AS DOUBLE) size,
             any_value(json_extract_string(payload,'$.name')) OVER
               (PARTITION BY lower(json_extract_string(payload,'$.proxyWallet'))) nm
      FROM aux WHERE type = 'orders_matched'
    ), bets AS (
      SELECT wallet, any_value(nm) nm, mk.asset,
             any_value(tk.payout) payout,
             sum(CASE WHEN side='BUY' THEN size ELSE -size END) net,
             sum(CASE WHEN side='BUY' THEN size*price END)
               / nullif(sum(CASE WHEN side='BUY' THEN size END),0) vwap
      FROM mk JOIN res_tok tk ON mk.asset = tk.asset
      GROUP BY wallet, mk.asset
      HAVING net >= 5 AND vwap BETWEEN 0.05 AND 0.95
    )
    SELECT wallet, any_value(nm), count(*) n,
           sum(CASE WHEN payout=1.0 THEN 1 ELSE 0 END) wins,
           sum(vwap) exp_w, sum(vwap*(1-vwap)) var_s,
           sum(net*(payout - vwap)) pnl,
           avg(vwap) avg_entry, sum(net*vwap) staked
    FROM bets GROUP BY wallet
    HAVING n >= ? AND var_s > 0
    """, [SET_MIN_BETS]).fetchall()
    print(f"maker wallets with >= {SET_MIN_BETS} resolved conviction-ish "
          f"positions: {len(rows)}")
    scored = []
    for w, nm, n, wins, exp_w, var_s, pnl, avg_e, staked in rows:
        z = (wins - exp_w) / (var_s ** 0.5)
        if z >= SET_MIN_Z and pnl > 0:
            scored.append((z, w, nm, n, wins, pnl, avg_e, staked))
    scored.sort(reverse=True)
    print(f"qualifying maker-sharps (z>={SET_MIN_Z}, pnl>0): {len(scored)}")
    # overlap with the taker screens
    taker = set()
    try:
        d = json.load(open(os.path.join(HERE, "params", "informed_set.json")))
        taker = {x.lower() for x in d["wallets"]}
    except Exception:
        pass
    watch = set()
    try:
        for r in json.load(open(os.path.join(
                os.path.dirname(HERE), "live", "watch_sharps.json"))):
            watch.add(r["wallet"].lower())
    except Exception:
        pass
    ol_t = sum(1 for z, w, *_ in scored if w in taker)
    ol_w = sum(1 for z, w, *_ in scored if w in watch)
    print(f"overlap: {ol_t} in taker informed set (n={len(taker)}) · "
          f"{ol_w} in watch_sharps (n={len(watch)})")
    pooled_pnl = sum(s[5] for s in scored)
    pooled_n = sum(s[3] for s in scored)
    print(f"cohort pooled: {pooled_n} resolved bets · "
          f"${pooled_pnl:+,.0f} maker pnl\n")
    print(f"{'z':>5} {'name':<20} {'bets':>5} {'wins':>5} {'hit':>5} "
          f"{'pnl':>10} {'avg entry':>9}")
    for z, w, nm, n, wins, pnl, avg_e, staked in scored[:15]:
        print(f"{z:5.1f} {(nm or w[:12]):<20} {n:>5} {int(wins):>5} "
              f"{wins/n:>5.2f} {pnl:>+10,.0f} {avg_e:>9.2f}")


if __name__ == "__main__":
    main()
