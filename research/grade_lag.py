#!/usr/bin/env python3
"""Nightly chain-truth grading of the lead-lag harness (wwf-lagbot).

Pulls /data/lag_state.json + lag_attempts.jsonl + lag_settles.jsonl off
the box (read-only), re-grades every settle with CTF payout vectors,
appends to research/lag_paper_ledger.jsonl (keyed asset+ts, idempotent),
and prints the two pre-registered readouts: paper-leg EV/episode and the
OBSERVATIONAL kill-switch — the median ask premium over the stale print
(>= 8c over 3 days = the edge was a mirage, no paper sample needed)."""
import json
import os
import shutil
import statistics as st
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "lag_paper_ledger.jsonl")
STATE_PULL = os.path.join(HERE, ".lag_state.pull.json")
ATT_PULL = os.path.join(HERE, ".lag_attempts.pull.jsonl")
SET_PULL = os.path.join(HERE, ".lag_settles.pull.jsonl")
FLYCTL = shutil.which("flyctl") or "/opt/homebrew/bin/flyctl"


def sftp(remote, local):
    subprocess.run([FLYCTL, "ssh", "sftp", "get", remote, local + ".new",
                    "-a", "wwf-lagbot"], capture_output=True,
                   timeout=600, stdin=subprocess.DEVNULL)
    if os.path.exists(local + ".new") and os.path.getsize(local + ".new") > 0:
        os.replace(local + ".new", local)
        return True
    try:
        os.remove(local + ".new")
    except FileNotFoundError:
        pass
    return False


def main():
    ok = sftp("/data/lag_state.json", STATE_PULL)
    sftp("/data/lag_attempts.jsonl", ATT_PULL)
    sftp("/data/lag_settles.jsonl", SET_PULL)
    if not ok:
        print("[grade_lag] box unreachable or no state yet — skip")
        return 0
    stt = json.load(open(STATE_PULL))
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
    settled = stt.get("settled", [])
    if os.path.exists(SET_PULL):       # durable log beats trimmed state
        seen = {(s["asset"], s["ts"]) for s in settled}
        for ln in open(SET_PULL):
            try:
                s = json.loads(ln)
                if (s["asset"], s["ts"]) not in seen:
                    settled.append(s)
            except Exception:
                pass
    new = [s for s in settled if (s["asset"], s["ts"]) not in have]
    if new:
        payouts.ensure(sorted({s["cond"] for s in new if s.get("cond")}))
    graded = flips = 0
    with open(LEDGER, "a") as fh:
        for s in new:
            t = payouts.truth(s.get("cond"), s.get("asset")) \
                if s.get("cond") else None
            pay = s["payout"] if t is None else t
            pnl = round(s["shares"] * pay - s["cost"] - s["fee"], 2)
            if t is not None and abs(t - s["payout"]) > 1e-9:
                flips += 1
            fh.write(json.dumps({**s, "chain_payout": pay, "chain_pnl": pnl,
                                 "flip": t is not None
                                 and abs(t - s["payout"]) > 1e-9}) + "\n")
            graded += 1
    total = wins = 0
    pnl_sum = staked = 0.0
    try:
        for ln in open(LEDGER):
            d = json.loads(ln)
            total += 1
            wins += d["chain_payout"] == 1.0
            pnl_sum += d["chain_pnl"]
            staked += d.get("cost", 0)
    except FileNotFoundError:
        pass
    # observational kill-switch: median premium across ALL attempts with a
    # visible ask (fills, premium-skips, craters alike)
    prem = []
    try:
        for ln in open(ATT_PULL):
            try:
                a = json.loads(ln)
                if a.get("premium") is not None:
                    prem.append(a["premium"])
            except Exception:
                pass
    except FileNotFoundError:
        pass
    c = stt.get("counters", {})
    ev = pnl_sum / total if total else None
    print(f"[grade_lag] +{graded} settles ({flips} flips) · paper leg: "
          f"{total} settled {wins}W · chain P&L ${pnl_sum:+.2f}"
          f"{f' (${ev:+.2f}/ep · {pnl_sum/staked*100:+.1f}% of staked)' if ev is not None and staked else ''} · "
          f"lifetime bursts {c.get('bursts')} att {c.get('attempts')} "
          f"fill {c.get('fills')} prem-skip {c.get('premium_skips')}")
    if prem:
        prem.sort()
        print(f"[grade_lag] OBSERVATIONAL: median ask premium "
              f"{st.median(prem)*100:+.1f}c over {len(prem)} attempts "
              f"(kill-switch: >= +8c across 3 days)")
    print("[grade_lag] verdict binds to the Study D pre-registration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
