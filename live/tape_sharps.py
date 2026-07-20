#!/usr/bin/env python3
"""Tape-era sharp screen over the RTDS firehose (issue #2, first flow study).

The tape (rtds.duckdb `trades`, every CLOB fill since 2026-07-17) sees ALL
fills — including wallets that lose and vanish — so unlike the data-api
leaderboards there is NO survivorship bias to unwind ([[polymarket-smart-money]]:
displayed win rates ~92% collapse to ~50% once hidden losers count). The
screen is insider.py's improbability methodology transplanted onto tape:

  1. RESOLVE FROM THE TAPE. A token is proxy-resolved when its final-30min
     VWAP converged (>= 0.97 won / <= 0.03 lost) AND it stopped trading
     QUIET_H before tape end AND no sibling token of the same condition
     disagrees (two "winners" in one cond = still-live or bad data -> drop).
     Chain truth (payouts.py) then overlays the shortlist's conditions, so
     50/50 refunds and operator quirks can't survive to the report
     ([[polymarket-resolution-truth]]).
  2. HELD NET POSITIONS, not trades. Per wallet-token: net = buys - sells;
     only |net| that survives to the token's last tape print is a bet (a
     scalper who round-trips out is not "holding" the resolution). Entry is
     the buy VWAP; P&L = net * (payout - vwap).
  3. IMPROBABILITY z. Over a wallet's proxy-resolved held bets: each entry
     at price p wins with prob p under the null; z = (W - Σp)/sqrt(Σp(1-p)).
     The tape's 3-day window means n is small — z >= 2 with n >= MIN_BETS
     and positive P&L is a CANDIDATE, not a verdict; the bench forward
     window is the verdict (bench review, issue #13).
  4. CONTEXT COLUMNS for triage: niche mix (title keywords), notional,
     price band, burst flag (wallet's first tape print < 48h before its
     biggest bet — fresh-or-returning wallet swinging immediately, the
     insider fingerprint), and the follow/bench overlap (excluded from
     candidates, printed as benchmark rows so the screen can be sanity
     checked against wallets we already believe in).

Outputs live/tape_sharps.json (full scored rows) and a console report.

    python3 tape_sharps.py                  # screen + chain-validate top 25
    python3 tape_sharps.py --no-chain       # tape-only (no RPC)
    python3 tape_sharps.py --min-bets 6     # loosen for exploration
"""
import argparse
import json
import math
import os
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "rtds.duckdb")
OUT = os.path.join(HERE, "tape_sharps.json")

# proxy-resolution + bet thresholds (tunable; defaults are the 2026-07-20
# first-run calibration — see FINDINGS addendum in the issue)
WIN_T, LOSE_T = 0.97, 0.03    # final-30min VWAP convergence
QUIET_H = 2                   # token must be quiet this long before tape end
MIN_NET_SH = 5.0              # net shares that count as a held bet
MIN_NET_USD = 20.0            # ...and its buy notional (dust bots out)
P_LO, P_HI = 0.05, 0.95       # entry band for the z math (insider.py clamps)
BURST_H = 48                  # "fresh in tape" window for the burst flag

NICHES = [  # (label, TITLE ILIKE patterns) — first match wins
    ("esports", ["%lol:%", "%dota%", "%cs2%", "%csgo%", "%valorant%", "%esports%",
                 "% vs %game %", "%bilibili%", "%map %winner%"]),
    ("tennis", ["%tennis%", "%atp%", "%wta%", "%wimbledon%", "%open (m)%", "%set %winner%"]),
    ("sports", ["% vs. %", "% vs %", "% @ %", "%mlb%", "%nba%", "%nhl%", "%ufc%",
                "%world cup%", "%f1%", "%grand prix%"]),
    ("crypto", ["%bitcoin%", "%btc%", "%ethereum%", "%eth %", "%solana%", "%xrp%",
                "%price of%", "%above%on july%", "%above%on august%"]),
    ("politics", ["%election%", "%president%", "%senate%", "%governor%", "%mayor%",
                  "%nominee%", "%impeach%", "%tariff%", "%fed %", "%rate cut%"]),
    ("geo", ["%iran%", "%israel%", "%russia%", "%ukraine%", "%china%", "%taiwan%",
             "%ceasefire%", "%strike%", "%nato%"]),
]


def known_wallets():
    """follow set + benches + the live bot itself -> {addr_lower: label}."""
    out = {"0x455e252e45ee46d6c4cc1c8fadd3899d68f245a1": "OUR-BOT"}
    try:
        for w in json.load(open(os.path.join(HERE, "copybot.paper.json")))["wallets"]:
            out[w["wallet"].lower()] = f"follow:{w.get('name', w['wallet'][:8])}"
    except Exception:
        pass
    for fn, tag in (("watch_sharps.json", "bench"), ("watch_skilled.json", "skilled")):
        try:
            for w in json.load(open(os.path.join(HERE, fn))):
                out.setdefault(w["wallet"].lower(), f"{tag}:{w.get('name', '?')}")
        except Exception:
            pass
    return out


def niche_case():
    whens = []
    for label, pats in NICHES:
        ors = " OR ".join(f"lower(title) LIKE '{p}'" for p in pats)
        whens.append(f"WHEN ({ors}) THEN '{label}'")
    return "CASE " + " ".join(whens) + " ELSE 'other' END"


def screen(db, min_bets):
    """One SQL pass: proxy-resolve tokens, net held positions, wallet rollup."""
    t_end = db.execute("SELECT max(ts) FROM trades").fetchone()[0]
    quiet = t_end - QUIET_H * 3600
    db.execute(f"""
    CREATE TEMP TABLE alltok AS
    WITH last AS (
      SELECT asset, any_value(cond) cond, max(ts) last_ts
      FROM trades WHERE cond IS NOT NULL AND cond != '' GROUP BY asset
    ), term AS (                       -- final-30min VWAP per token
      SELECT t.asset,
             sum(t.price * t.size) / nullif(sum(t.size), 0) term_vwap
      FROM trades t JOIN last l ON t.asset = l.asset
      WHERE t.ts >= l.last_ts - 1800 GROUP BY t.asset
    )
    SELECT l.asset, l.cond, l.last_ts, tm.term_vwap,
           l.last_ts > {quiet} AS alive,
           CASE WHEN tm.term_vwap >= {WIN_T} THEN 1.0
                WHEN tm.term_vwap <= {LOSE_T} THEN 0.0 END AS payout
    FROM last l JOIN term tm ON l.asset = tm.asset
    """)
    # cond veto: any sibling still trading (in-play comebacks exist), or two
    # proxy-winners (multi-outcome not actually settled) -> drop the cond
    db.execute("""
    CREATE TEMP TABLE badcond AS
    SELECT cond FROM alltok GROUP BY cond
    HAVING bool_or(alive)
        OR sum(CASE WHEN payout = 1.0 THEN 1 ELSE 0 END) > 1
    """)
    db.execute("""
    CREATE TEMP TABLE tok AS
    SELECT * FROM alltok WHERE NOT alive AND payout IS NOT NULL
    """)
    db.execute(f"""
    CREATE TEMP TABLE bets AS
    SELECT tr.wallet, tr.asset, any_value(tk.cond) cond,
           any_value(tk.payout) payout,
           any_value(tk.term_vwap) term_vwap,
           sum(CASE WHEN tr.side = 'BUY' THEN tr.size ELSE -tr.size END) net,
           sum(CASE WHEN tr.side = 'BUY' THEN tr.size * tr.price END)
             / nullif(sum(CASE WHEN tr.side = 'BUY' THEN tr.size END), 0) vwap,
           sum(CASE WHEN tr.side = 'BUY' THEN tr.size * tr.price END) buy_usd,
           count(*) n_fills,
           any_value({niche_case()}) niche,
           min(tr.ts) first_ts
    FROM trades tr
    JOIN tok tk ON tr.asset = tk.asset
    WHERE tk.payout IS NOT NULL
      AND tk.cond NOT IN (SELECT cond FROM badcond)
      AND tr.ts <= tk.last_ts            -- nothing after the last print
    GROUP BY tr.wallet, tr.asset
    HAVING net >= {MIN_NET_SH}
       AND buy_usd >= {MIN_NET_USD}
       AND vwap BETWEEN {P_LO} AND {P_HI}
    """)
    rows = db.execute(f"""
    WITH w AS (
      SELECT wallet,
             count(*) n,
             sum(CASE WHEN payout = 1.0 THEN 1 ELSE 0 END) wins,
             sum(vwap) exp_wins,
             sum(vwap * (1 - vwap)) var_sum,
             sum(net * (payout - vwap)) pnl,
             sum(buy_usd) notional,
             avg(vwap) avg_p,
             avg(n_fills) avg_fills,
             median(buy_usd) med_bet,
             mode(niche) top_niche,
             count(DISTINCT niche) niches,
             min(first_ts) first_seen
      FROM bets GROUP BY wallet HAVING n >= {min_bets}
    ),
    life AS (SELECT wallet, min(ts) tape_first FROM trades GROUP BY wallet)
    SELECT w.*, life.tape_first FROM w JOIN life USING (wallet)
    """).fetchall()
    cols = ["wallet", "n", "wins", "exp_wins", "var_sum", "pnl", "notional",
            "avg_p", "avg_fills", "med_bet", "top_niche", "niches",
            "first_seen", "tape_first"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["z"] = ((d["wins"] - d["exp_wins"]) / math.sqrt(d["var_sum"])
                  if d["var_sum"] > 0 else 0.0)
        d["win_pct"] = 100.0 * d["wins"] / d["n"]
        out.append(d)
    return out, t_end


def chain_validate(cands, db):
    """Overlay payouts.truth on each candidate bet; rescore. Refund (0.5)
    counts as neither win nor loss; unknown keeps the tape proxy."""
    import payouts
    conds = sorted({c for w in cands for c in w["_conds"]})
    payouts.ensure(conds)
    for w in cands:
        n = wins = exp = var = pnl = 0.0
        flip = 0
        for (cond, asset, net, vwap, proxy) in w["_bets"]:
            t = payouts.truth(cond, asset)
            pay = proxy if t is None else t
            if pay == 0.5:            # refund: stake back, no win/no loss
                pnl += 0.0
                continue
            if t is not None and t != proxy:
                flip += 1
            n += 1
            wins += pay
            exp += vwap
            var += vwap * (1 - vwap)
            pnl += net * (pay - vwap)
        w.update(n_chain=int(n), wins_chain=int(wins), pnl_chain=round(pnl, 2),
                 flips=flip,
                 z_chain=round((wins - exp) / math.sqrt(var), 2) if var > 0 else 0.0)
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-bets", type=int, default=8)
    ap.add_argument("--top", type=int, default=25, help="chain-validate this many")
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--max-bets", type=int, default=90, help="copyable cadence cap")
    ap.add_argument("--max-fills", type=float, default=6.0, help="avg fills/bet cap")
    ap.add_argument("--min-med-usd", type=float, default=50.0, help="median bet floor")
    args = ap.parse_args()

    db = duckdb.connect(DB, read_only=True)
    known = known_wallets()
    scored, t_end = screen(db, args.min_bets)
    scored.sort(key=lambda d: -d["z"])

    # COPYABLE means the bot could actually mirror it at 3-17s lag: discrete
    # entries (few fills per bet, not continuous flow), human cadence (<=
    # max-bets held bets in the 3-day tape), conviction-sized (median bet
    # clears the $25 follow floor with margin). Everything else that scores
    # is ALGO FLOW — real edge, uncopyable execution (they ARE the crater).
    def copyable(d):
        return (d["avg_fills"] <= args.max_fills and d["n"] <= args.max_bets
                and d["med_bet"] >= args.min_med_usd)

    fresh = [d for d in scored if d["z"] >= 2.0 and d["pnl"] > 0
             and d["wallet"].lower() not in known]
    interesting = [d for d in fresh if copyable(d)][:args.top]
    algos = [d for d in fresh if not copyable(d)][:8]
    for d in interesting:
        rows = db.execute("""
          SELECT cond, asset, net, vwap, payout FROM bets WHERE wallet = ?
        """, [d["wallet"]]).fetchall()
        d["_conds"] = [r[0] for r in rows]
        d["_bets"] = rows
    if interesting and not args.no_chain:
        chain_validate(interesting, db)
    for d in interesting:
        d.pop("_conds", None); d.pop("_bets", None)

    bench = [d for d in scored if d["wallet"].lower() in known]
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t_end))

    def fmt(d):
        age_h = (t_end - d["tape_first"]) / 3600
        burst = "BURST" if age_h <= BURST_H else f"{age_h:.0f}h"
        chain = (f" · chain z={d['z_chain']} {d['wins_chain']}/{d['n_chain']} "
                 f"${d['pnl_chain']:+,.0f} ({d['flips']} flips)"
                 if "z_chain" in d else "")
        return (f"{d['wallet'][:10]}…  z={d['z']:+.2f}  {d['wins']}/{d['n']} "
                f"({d['win_pct']:.0f}%) avg_p {d['avg_p']:.2f} "
                f"med ${d['med_bet']:,.0f} f/b {d['avg_fills']:.1f} "
                f"pnl ${d['pnl']:+,.0f} vol ${d['notional']:,.0f} "
                f"{d['top_niche']}({d['niches']}) {burst}{chain}")

    print(f"tape through {stamp} · {len(scored)} wallets with >= "
          f"{args.min_bets} proxy-resolved held bets\n")
    print(f"— COPYABLE candidates (z >= 2, pnl > 0, <= {args.max_bets} bets, "
          f"<= {args.max_fills:.0f} fills/bet, med >= ${args.min_med_usd:.0f}) —")
    for d in interesting:
        print(fmt(d))
    print(f"\n— algo flow (scores, but uncopyable execution; {len(algos)} of "
          f"{len(fresh) - len(interesting)}) —")
    for d in algos:
        print(fmt(d))
    print("\n— benchmark: known wallets through the same screen —")
    for d in sorted(bench, key=lambda x: -x["z"])[:15]:
        print(f"[{known[d['wallet'].lower()]}] {fmt(d)}")

    json.dump({"tape_end": t_end, "params": {
                   "min_bets": args.min_bets, "win_t": WIN_T, "lose_t": LOSE_T,
                   "quiet_h": QUIET_H, "min_net_sh": MIN_NET_SH,
                   "min_net_usd": MIN_NET_USD, "p_band": [P_LO, P_HI]},
               "candidates": interesting, "benchmark": bench,
               "screened": len(scored)},
              open(OUT, "w"), indent=1, default=float)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
