#!/usr/bin/env python3
"""The bankroll-decision number: parity-era per-signal edge vs the fee hurdle.

One row per run -> history/edge.csv, one verdict line -> edge_verdict.txt
(daily.sh rides it into the Discord digest footer) and stdout. Run ad-hoc any
time: python3 live/edge.py

What it measures (FINDINGS "The calibration experiment", HANDOFF rev 13):
- EDGE: net per-signal ROI of the PAPER book's resolved copies opened after
  PARITY_T0 (2026-07-16 03:49Z, the paper FAK-parity boot — before that the
  paper book pretended to fill thin books, so its ROI was flattered). This is
  the honest estimate of what the strategy earns per signal after modeled
  fees/slippage.
- HURDLE: the LIVE book's realized round-trip fee drag (fees paid / dollars
  staked, doubled for the exit side when a position hasn't closed yet is NOT
  modeled — we use actual fees over actual stakes, both sides included as
  they were really paid). The taker fee is proportional, so this hurdle does
  NOT shrink with stake size.
- DRAG: matched-token live-vs-paper ROI gap (execution reality check; the
  $1-stake rounding component of this shrinks as stakes grow, the fee
  component doesn't).

Decision rule (agreed 2026-07-16): sizing up needs EDGE comfortably above
HURDLE on n>=30 parity-era resolved signals. Feeds are read from GitHub raw
(freshest truth — the bots commit their own state; a local checkout can lag),
falling back to the local files offline.
"""
import csv
import json
import os
import ssl
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = "https://raw.githubusercontent.com/jaxperro/winning-wallet-finder/main/live"
PARITY_T0 = 1784260140          # 2026-07-16 03:49Z — paper booted on 646139d
MIN_N = 30                      # below this, the verdict is "insufficient data"


def _feed(name):
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(f"{RAW}/{name}", timeout=30, context=ctx) as r:
            return json.loads(r.read())
    except Exception:
        return json.load(open(os.path.join(HERE, name)))


def main():
    paper = _feed("copybot_live.json")
    live = _feed("copybot_live_real.json")

    # EDGE: parity-era resolved paper copies
    era = [b for b in paper.get("bets", [])
           if (b.get("opened") or 0) >= PARITY_T0
           and b.get("pnl") is not None and b.get("cost")]
    n = len(era)
    staked = sum(b["cost"] for b in era)
    pnl = sum(b["pnl"] for b in era)
    edge = pnl / staked if staked else 0.0

    # HURDLE: live's actual fee drag over everything it ever staked
    lbets = [b for b in live.get("bets", []) if b.get("cost")]
    lstaked = sum(b["cost"] for b in lbets)
    hurdle = (live.get("fees_paid") or 0) / lstaked if lstaked else 0.0

    # DRAG: matched resolved tokens on both books
    def by_tok(feed):
        return {b["token"]: b for b in feed.get("bets", [])
                if b.get("token") and b.get("pnl") is not None and b.get("cost")}
    lb, pb = by_tok(live), by_tok(paper)
    common = set(lb) & set(pb)
    m_ls = sum(lb[t]["cost"] for t in common)
    m_ps = sum(pb[t]["cost"] for t in common)
    drag = ((sum(pb[t]["pnl"] for t in common) / m_ps if m_ps else 0.0)
            - (sum(lb[t]["pnl"] for t in common) / m_ls if m_ls else 0.0))

    if n < MIN_N:
        verdict = f"MEASURING ({n}/{MIN_N} parity-era signals)"
    elif edge > hurdle + 0.02:
        verdict = "CLEARS the fee hurdle — sizing up is defensible"
    elif edge > 0:
        verdict = "positive but BELOW the hurdle — live can't compound"
    else:
        verdict = "NEGATIVE edge — keep money out"

    line = (f"edge {edge:+.1%}/signal (n={n}, ${staked:,.0f} staked) · "
            f"fee hurdle {hurdle:.1%} · live-paper drag {drag*100:.1f}pp "
            f"({len(common)} matched) · {verdict}")
    print(f"[edge] {line}")
    with open(os.path.join(HERE, "edge_verdict.txt"), "w") as fh:
        fh.write(line + "\n")

    row = {"date": time.strftime("%F"), "n": n, "staked": round(staked, 2),
           "pnl": round(pnl, 2), "edge_pct": round(edge * 100, 2),
           "hurdle_pct": round(hurdle * 100, 2),
           "drag_pp": round(drag * 100, 2), "matched_n": len(common),
           "verdict": verdict}
    os.makedirs(os.path.join(HERE, "history"), exist_ok=True)
    path = os.path.join(HERE, "history", "edge.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


if __name__ == "__main__":
    main()
