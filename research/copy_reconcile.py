#!/usr/bin/env python3
"""#24 v0 (2026-07-23) — THREE-BOOK RECONCILIATION: where does the
follow-only backtest's story (+312%) diverge from the bots' reality?
Every backtest bet lands in one bucket per book (paper, live):

  UNSEEN   the bot has no record of the signal at all (fills, misses,
           nothing) — universe/screen gap pooled in v0 (cursor gaps,
           pre-boot signals, filter drift). The backtest replays these
           from the cache as if they were copyable; reality never saw
           them.
  MISSED   the bot detected and skipped/failed (FAK crater, floor,
           depth, cash). The backtest's 100%-fill assumption pays this.
  FILLED   both took it — the FILL-PRICE gap prices the difference
           between the backtest's feeless their-price entry and the
           bot's actual my_price + taker fee.

$100-normalized per signal (sizing paths deliberately excluded in v0 —
sizing/exit/fee-path buckets are v1, per the #24 spec). Chain-graded
(payouts_for — scorer law); refunds excluded. Window: parity era only
(PARITY_T0, same boundary as live/edge.py) so live comparisons are
honest. Companion input: live/portfolio_follow_bets.json (portfolio.py
--follow-only). NOT pre-registered — measurement of our own books."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PARITY_T0 = 1784260140          # keep == live/edge.py
FEE = 0.03


def load_jsonl(path):
    out = []
    try:
        for ln in open(os.path.join(ROOT, path)):
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def bot_views():
    """{book: {"fills": {token: my_price}, "missed": set(token)}}"""
    v = {}
    for book, ff in (("paper", "copybot_fills.jsonl"),
                     ("live", "copybot_fills.live.jsonl")):
        fills = {}
        for r in load_jsonl(ff):
            if (r.get("side") == "SELL" or r.get("untracked")
                    or not r.get("my_price")):
                continue
            fills[str(r["token"])] = r["my_price"]
        v[book] = {"fills": fills, "missed": set()}
    for book, feed in (("paper", "live/copybot_live_full.json"),
                       ("live", "live/copybot_live_real_full.json")):
        try:
            d = json.load(open(os.path.join(ROOT, feed)))
            for m in d.get("missed") or []:
                if m.get("token"):
                    v[book]["missed"].add(str(m["token"]))
        except Exception:
            pass
    for r in load_jsonl("copybot_missed_archive.live.jsonl"):
        if r.get("token"):
            v["live"]["missed"].add(str(r["token"]))
    for r in load_jsonl("copybot_missed_archive.jsonl"):
        if r.get("token"):
            v["paper"]["missed"].add(str(r["token"]))
    return v


def rejects():
    """{book: {token,...}} — the reject ledgers (live since 2026-07-24)
    split UNSEEN into REJECTED (screen: the bot saw and filtered) vs
    truly unseen (universe)."""
    out = {"paper": set(), "live": set()}
    for book, f in (("paper", "copybot_rejects.jsonl"),
                    ("live", "copybot_rejects.live.jsonl")):
        for r in load_jsonl(f):
            if r.get("token"):
                out[book].add(str(r["token"]))
    return out


def main():
    bets = json.load(open(os.path.join(ROOT, "live",
                                       "portfolio_follow_bets.json")))
    try:
        fs = {k.lower(): int(v) for k, v in json.load(open(os.path.join(
            ROOT, "live", "follow_since.json"))).items()}
        nm2w = {w["name"]: w["wallet"].lower() for w in json.load(open(
            os.path.join(ROOT, "live", "copybot.paper.json")))["wallets"]}
    except Exception:
        fs, nm2w = {}, {}
    bets = [b for b in bets if b.get("entry_t", 0) >= PARITY_T0
            and b.get("asset") and b.get("p")
            and b.get("entry_t", 0) >= fs.get(nm2w.get(b["name"], ""), 0)]
    print(f"backtest bets in the parity era with join keys "
          f"(follow-since applied): {len(bets)}", flush=True)
    db = tape.connect()
    tape.build_resolved(db)
    pays = fwd.payouts_for(db, [b["asset"] for b in bets])
    v = bot_views()

    graded = []
    for b in bets:
        wp = pays.get(b["asset"], b.get("wp"))
        if wp is None or wp == 0.5:
            continue
        b["_wp"] = wp
        b["_ev_bt"] = (100.0 / b["p"]) * (wp - b["p"])   # feeless maker conv.
        graded.append(b)
    print(f"chain-graded (refunds/pending excluded): {len(graded)}\n",
          flush=True)

    rej = rejects()
    row = {"date": time.strftime("%Y-%m-%d"), "n": len(graded)}
    for book in ("paper", "live"):
        f, mset = v[book]["fills"], v[book]["missed"]
        buckets = {"FILLED": [], "MISSED": [], "REJECTED": [], "UNSEEN": []}
        pg = 0.0
        for b in graded:
            a = b["asset"]
            if a in f:
                buckets["FILLED"].append(b)
                myp = f[a]
                ev_real = (100.0 / myp) * (b["_wp"] - myp) \
                    - FEE * (100.0 / myp) * min(myp, 1 - myp)
                pg += b["_ev_bt"] - ev_real
            elif a in mset:
                buckets["MISSED"].append(b)
            elif a in rej[book]:
                buckets["REJECTED"].append(b)      # screen gap, now visible
            else:
                buckets["UNSEEN"].append(b)        # universe gap
        tot_bt = sum(b["_ev_bt"] for b in graded)
        row[f"{book}_bt_ev"] = round(tot_bt)
        print(f"== {book.upper()} (n={len(graded)} signals · backtest "
              f"$100-EV {tot_bt:+,.0f}) ==")
        for k in ("FILLED", "MISSED", "REJECTED", "UNSEEN"):
            bs = buckets[k]
            ev = sum(b["_ev_bt"] for b in bs)
            share = 100 * ev / tot_bt if tot_bt else 0
            row[f"{book}_{k.lower()}_n"] = len(bs)
            row[f"{book}_{k.lower()}_ev"] = round(ev)
            print(f"  {k:>8}: n={len(bs):>4} · backtest EV in bucket "
                  f"{ev:+9.0f} ({share:+5.1f}% of story)")
        row[f"{book}_fill_gap"] = round(pg)
        print(f"  fill-price gap on FILLED (their_p feeless -> my_p+fee): "
              f"{pg:+,.0f}")
        un = buckets["UNSEEN"]
        if un:
            byw = {}
            for b in un:
                byw[b["name"]] = byw.get(b["name"], 0) + 1
            top = sorted(byw.items(), key=lambda kv: -kv[1])[:4]
            print(f"  unseen by wallet: {top}")
        print(flush=True)
    # live-bankroll replica residual: the model at the live book's capital
    # vs the live book itself (window-aligned numbers recorded raw)
    try:
        rep = json.load(open(os.path.join(ROOT, "live",
                                          "portfolio_live_replica.json")))
        lv = json.load(open(os.path.join(ROOT, "live",
                                         "copybot_live_real.json")))
        row["replica_pnl"] = round(rep.get("pnl") or 0, 2)
        row["live_realized"] = round(lv.get("realized") or 0, 2)
        print(f"REPLICA: model-at-live-bank 30d P&L {row['replica_pnl']:+.2f}"
              f" vs live book realized {row['live_realized']:+.2f} "
              f"(residual {row['replica_pnl'] - row['live_realized']:+.2f})",
              flush=True)
    except Exception:
        pass
    json.dump(row, open(os.path.join(HERE, "copy_reconcile.json"), "w"))

    if "--csv" in sys.argv:
        import csv as _csv
        path = os.path.join(ROOT, "history", "reconcile.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = []
        try:
            rows = list(_csv.DictReader(open(path)))
        except FileNotFoundError:
            pass
        rows = [r for r in rows if r.get("date") != row["date"]]
        # drift alarm: any bucket-EV share moved >15pp vs the trailing mean
        if len(rows) >= 5:
            import statistics as _st
            for book in ("paper", "live"):
                for k in ("filled", "missed", "rejected", "unseen"):
                    key = f"{book}_{k}_ev"
                    tot_k = f"{book}_bt_ev"
                    hist = [float(r[key]) / max(1, abs(float(r[tot_k])))
                            for r in rows[-7:] if r.get(key) and r.get(tot_k)]
                    if not hist:
                        continue
                    cur = row[key] / max(1, abs(row[tot_k]))
                    if abs(cur - _st.mean(hist)) > 0.15:
                        print(f"⚠ RECONCILE DRIFT {book}.{k}: share "
                              f"{cur:+.0%} vs 7d mean {_st.mean(hist):+.0%}",
                              flush=True)
        rows.append({k: str(vv) for k, vv in row.items()})
        cols = sorted({c for r in rows for c in r}, key=lambda c:
                      (c != "date", c))
        with open(path, "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"[reconcile.csv] {len(rows)} rows -> history/reconcile.csv",
              flush=True)


if __name__ == "__main__":
    main()
