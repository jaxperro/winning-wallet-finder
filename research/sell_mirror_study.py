#!/usr/bin/env python3
"""Sell-mirror counterfactual (2026-07-23, basis for the hold-through
pre-registration): for every mirrored SELL in both books, chain-true
compare the exit against holding to resolution. Their entries are the
copied signal; are their exits information or just their bankroll ops?

Per sell: delta = (chain_payout − sell_price) × shares. delta>0 = the exit
gave up value (token resolved above our sell); delta<0 = the exit saved us.
Reported pooled and per-wallet (sells attributed to the token's buy-side
wallet). Refunds excluded. Rerunnable any time; grading coverage grows
with the tape/payout cache."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    tok2name = {}
    sells = []
    for path, book in ((os.path.join(ROOT, "copybot_fills.live.jsonl"), "live"),
                       (os.path.join(ROOT, "copybot_fills.jsonl"), "paper")):
        for ln in open(path):
            r = json.loads(ln)
            if r.get("untracked"):
                continue
            if r.get("side") == "SELL":
                r["_book"] = book
                sells.append(r)
            elif r.get("name"):
                tok2name[str(r["token"])] = r["name"]
    db = tape.connect()
    tape.build_resolved(db)
    pays = fwd.payouts_for(db, [str(s["token"]) for s in sells])
    for book in ("live", "paper"):
        ss = [s for s in sells if s["_book"] == book]
        n = unk = refund = right = wrong = 0
        saved = given = 0.0
        for s in ss:
            p = pays.get(str(s["token"]))
            if p is None:
                unk += 1
                continue
            if p == 0.5:
                refund += 1
                continue
            n += 1
            delta = (p - s["price"]) * s.get("shares", 0)
            if delta > 0:
                given += delta
                wrong += 1
            else:
                saved -= delta
                right += 1
        if n:
            print(f"[{book}] {len(ss)} sells · {n} graded ({unk} unresolved, "
                  f"{refund} refunds)")
            print(f"  exit RIGHT: {right} (saved ${saved:.2f}) · exit WRONG: "
                  f"{wrong} (gave up ${given:.2f}) · net of mirroring "
                  f"${saved-given:+.2f}")
    agg = {}
    for s in sells:
        p = pays.get(str(s["token"]))
        if p is None or p == 0.5:
            continue
        nm = tok2name.get(str(s["token"]), "?")
        a = agg.setdefault(nm, dict(n=0, net=0.0, wrong=0))
        a["n"] += 1
        delta = (p - s["price"]) * s.get("shares", 0)
        a["net"] -= delta
        a["wrong"] += delta > 0
    print("\nper-wallet (both books):")
    for nm, a in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        print(f"  {nm:<18} n={a['n']:>3} · exit-wrong {a['wrong']:>3} "
              f"({100*a['wrong']/a['n']:.0f}%) · net of mirroring "
              f"${a['net']:+8.2f}")


if __name__ == "__main__":
    main()
