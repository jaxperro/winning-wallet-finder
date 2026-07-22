#!/usr/bin/env python3
"""Nightly chain-truth grading of the oracle paper harness (wwf-oraclebot).

Pulls /data/oracle_state.json off the box (read-only — never writes back),
re-grades every settled entry with CTF payout vectors (payouts.truth; the
harness's provisional settles — own-feed ticks and CLOB winner flags — can
both be wrong on operator-resolved/refunded markets), and appends one row
per settle to research/oracle_paper_ledger.jsonl (keyed asset+ts,
idempotent). The per-E-cell rollup printed here is the CAPTURABILITY read —
it is NOT the #17 verdict (forward_ledger.jsonl is)."""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "oracle_paper_ledger.jsonl")
TMP = os.path.join(HERE, ".oracle_state.pull.json")
FLYCTL = shutil.which("flyctl") or "/opt/homebrew/bin/flyctl"
EDGE_GRID = (0.04, 0.07, 0.10)     # cells keyed str(E): "0.04"/"0.07"/"0.1"


def main():
    r = subprocess.run([FLYCTL, "ssh", "sftp", "get", "/data/oracle_state.json",
                        TMP, "-a", "wwf-oraclebot"], capture_output=True,
                       timeout=300, stdin=subprocess.DEVNULL)
    if not os.path.exists(TMP) or os.path.getsize(TMP) == 0:
        print("[grade_oracle] box unreachable or no state yet — skip")
        return 0
    st = json.load(open(TMP))
    os.remove(TMP)
    sys.path.insert(0, os.path.join(HERE, "..", "live"))
    import payouts
    have = set()
    ledger_rows = 0
    try:
        for ln in open(LEDGER):
            try:
                d = json.loads(ln)
                have.add((d["asset"], d["ts"]))
                ledger_rows += 1
            except Exception:
                pass
    except FileNotFoundError:
        pass
    settled = st.get("settled", [])
    # rotation-loss guard: SETTLED_TRIM must never rotate a settle out of
    # state before a nightly grades it
    total_settles = st.get("counters", {}).get("settled_total", 0)
    if total_settles > ledger_rows + len(settled):
        print(f"[grade_oracle] ⚠ TAPE LOSS: {total_settles} lifetime settles "
              f"but only {ledger_rows} graded + {len(settled)} in state — "
              f"raise SETTLED_TRIM or grade more often")
    new = [s for s in settled if (s["asset"], s["ts"]) not in have]
    if new:
        payouts.ensure(sorted({s["cond"] for s in new if s.get("cond")}))
    graded = flips = 0
    with open(LEDGER, "a") as fh:
        for s in new:
            t = payouts.truth(s.get("cond"), s.get("asset")) if s.get("cond") else None
            pay = s["payout"] if t is None else t
            pnl = round(s["shares"] * pay - s["cost"] - s["fee"], 2)
            if t is not None and abs(t - s["payout"]) > 1e-9:
                flips += 1
            fh.write(json.dumps({**s, "chain_payout": pay, "chain_pnl": pnl,
                                 "flip": t is not None
                                 and abs(t - s["payout"]) > 1e-9}) + "\n")
            graded += 1
    # per-E-cell rollup from the LEDGER (chain truth): row in cell iff
    # edge >= E — same bucketing as forward.py
    cells = {str(E): dict(fills=0, wins=0, refunds=0, pnl=0.0)
             for E in EDGE_GRID}
    try:
        for ln in open(LEDGER):
            d = json.loads(ln)
            for E in EDGE_GRID:
                if d.get("edge", 0) >= E:
                    g = cells[str(E)]
                    g["fills"] += 1
                    g["wins"] += d["chain_payout"] == 1.0
                    g["refunds"] += d["chain_payout"] == 0.5
                    g["pnl"] += d["chain_pnl"]
    except FileNotFoundError:
        pass
    c = st.get("counters", {})
    print(f"[grade_oracle] +{graded} settles ({flips} provisional flips) · "
          f"realized ${st.get('pnl_realized', 0):+.2f} · "
          f"open {len(st.get('open', {}))} · lifetime ev {c.get('events')} "
          f"att {c.get('attempts')} fill {c.get('fills')} "
          f"crater {c.get('craters')}")
    for E in EDGE_GRID:
        g = cells[str(E)]
        ev = g["pnl"] / g["fills"] if g["fills"] else None
        hit = g["wins"] / g["fills"] if g["fills"] else None
        print(f"[grade_oracle]   E>={E:.2f}: {g['fills']} fills · "
              f"EV/fill {'—' if ev is None else f'${ev:+.2f}'} · "
              f"hit {'—' if hit is None else f'{hit:.3f}'} · "
              f"pnl ${g['pnl']:+.2f}"
              + (f" · {g['refunds']} refunds" if g["refunds"] else ""))
    print("[grade_oracle] capturability read — NOT the #17 verdict "
          "(forward_ledger is; bar: first cell to 30 fwd fills, "
          "PASS EV>=+$6 & hit>=0.55, KILL EV<=0 at 50)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
