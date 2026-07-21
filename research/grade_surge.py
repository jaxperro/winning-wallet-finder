#!/usr/bin/env python3
"""Nightly chain-truth grading of the surge paper harness (wwf-surgebot).

Pulls /data/surge_state.json off the box (read-only — never writes back),
re-grades every settled entry with CTF payout vectors (payouts.truth; the
harness's provisional CLOB winner flags lie on operator-resolved markets,
[[polymarket-resolution-truth]]), and appends one row per settle to
research/surge_paper_ledger.jsonl (keyed asset+ts, idempotent). Also emits
the running paper-book summary the #16 sprint decision reads."""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "surge_paper_ledger.jsonl")
TMP = os.path.join(HERE, ".surge_state.pull.json")
FLYCTL = shutil.which("flyctl") or "/opt/homebrew/bin/flyctl"


def main():
    r = subprocess.run([FLYCTL, "ssh", "sftp", "get", "/data/surge_state.json",
                        TMP, "-a", "wwf-surgebot"], capture_output=True,
                       timeout=300, stdin=subprocess.DEVNULL)
    if not os.path.exists(TMP) or os.path.getsize(TMP) == 0:
        print("[grade_surge] box unreachable or no state yet — skip")
        return 0
    st = json.load(open(TMP))
    os.remove(TMP)
    sys.path.insert(0, os.path.join(HERE, "..", "live"))
    import payouts
    have = set()
    try:
        for ln in open(LEDGER):
            try:
                d = json.loads(ln)
                have.add((d["asset"], d["ts"]))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    settled = st.get("settled", [])
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
    total = wins = 0
    pnl_sum = 0.0
    try:
        for ln in open(LEDGER):
            d = json.loads(ln)
            total += 1
            wins += d["chain_payout"] == 1.0
            pnl_sum += d["chain_pnl"]
    except FileNotFoundError:
        pass
    c = st.get("counters", {})
    print(f"[grade_surge] +{graded} settles ({flips} provisional flips) · "
          f"book: {total} settled {wins}W · chain P&L ${pnl_sum:+.2f} · "
          f"cash ${st.get('cash', 0):.2f} · open {len(st.get('open', {}))} · "
          f"lifetime trig {c.get('triggers')} fill {c.get('fills')} "
          f"crater {c.get('craters')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
