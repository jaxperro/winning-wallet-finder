#!/usr/bin/env python3
"""Nightly chain-truth grading of the surge measurement harness (A2).

Pulls /data/surge2_state.json AND /data/surge_attempts.jsonl off the box
(read-only — never writes back), re-grades every settled entry with CTF
payout vectors (payouts.truth; provisional CLOB winner flags lie on
operator-resolved markets), and appends one row per settle to
research/surge_meas_ledger.jsonl (keyed asset+ts, idempotent). The pulled
copies are KEPT on disk for surge_book_replay.py (virtual bankroll specs).

v1 history is closed: surge_paper_ledger.jsonl and /data/surge_state.json
are frozen audit artifacts of the halted cash-gated book (2026-07-22)."""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "surge_meas_ledger.jsonl")
STATE_PULL = os.path.join(HERE, ".surge2_state.pull.json")
ATT_PULL = os.path.join(HERE, ".surge_attempts.pull.jsonl")
FLYCTL = shutil.which("flyctl") or "/opt/homebrew/bin/flyctl"


def sftp(remote, local):
    subprocess.run([FLYCTL, "ssh", "sftp", "get", remote, local + ".new",
                    "-a", "wwf-surgebot"], capture_output=True,
                   timeout=600, stdin=subprocess.DEVNULL)
    if os.path.exists(local + ".new") and os.path.getsize(local + ".new") > 0:
        os.replace(local + ".new", local)   # keep last good pull on failure
        return True
    try:
        os.remove(local + ".new")
    except FileNotFoundError:
        pass
    return False


def main():
    ok = sftp("/data/surge2_state.json", STATE_PULL)
    sftp("/data/surge_attempts.jsonl", ATT_PULL)
    sftp("/data/surge_markouts.jsonl",
         os.path.join(HERE, ".surge_markouts.pull.jsonl"))
    if not ok:
        print("[grade_surge] box unreachable or no state yet — skip")
        return 0
    st = json.load(open(STATE_PULL))
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
    total_settles = st.get("counters", {}).get("settled_total", 0)
    if total_settles > ledger_rows + len(settled):
        print(f"[grade_surge] ⚠ TAPE LOSS: {total_settles} lifetime settles "
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
    ev = pnl_sum / total if total else None
    print(f"[grade_surge] +{graded} settles ({flips} provisional flips) · "
          f"measurement: {total} settled {wins}W · chain P&L ${pnl_sum:+.2f}"
          f"{f' (${ev:+.2f}/fill)' if ev is not None else ''} · "
          f"open {len(st.get('open', {}))} · lifetime trig {c.get('triggers')} "
          f"att {c.get('attempts')} fill {c.get('fills')} "
          f"crater {c.get('craters')}")
    print("[grade_surge] capturability read — NOT the #16 verdict "
          "(forward_ledger is); bankroll reads come from surge_book_replay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
