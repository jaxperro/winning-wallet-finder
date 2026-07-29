#!/usr/bin/env python3
"""#26 follow-up — DO THE TAPE AND THE HARNESS NAME THE SAME EVENT?

Study C scored +$2.17/lean on tape and −$2.26/fill at a real book, with
the entry price ruled out (median ask premium +1.0c, 72% fill rate). The
remaining suspect is the SIGNAL DEFINITION itself: the harness builds
each (wallet, token) inventory from the live stream and starts EMPTY at
boot, while maker_lean.day_leans() sees the wallet's whole day. If those
two disagree about WHEN net inventory crosses $150, they are measuring
different events under one name — and #22's tape number was never
reachable by construction rather than by execution.

Method: take the harness's own attempts (its declared crossings, after
the band/event-cap/print filters), recompute tape crossings over the SAME
wallets and window with the frozen method, and ask three questions:
  1. agreement — what fraction of harness attempts have a tape crossing
     for the same (wallet, asset, day)?
  2. tape-only  — crossings the tape names that the harness never saw
  3. size drift — for matched pairs, how far apart are the lean_usd the
     two computed? (the direct read on whole-day vs from-boot inventory)
Read-only; no verdict authority — it explains a KILL that already fired.
"""
import json
import os
import sys
import time
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tape                                    # noqa: E402
import maker_lean as ml                        # noqa: E402

ATT = os.path.join(HERE, ".lean_attempts.pull.jsonl")


def main():
    att = []
    for ln in open(ATT):
        try:
            att.append(json.loads(ln))
        except Exception:
            pass
    if not att:
        raise SystemExit("no attempts pulled")
    lo_ts = min(a["ts"] for a in att)
    hi_ts = max(a["ts"] for a in att)
    print(f"harness attempts: {len(att)} · window "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(lo_ts))} -> "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(hi_ts))}Z", flush=True)

    db = tape.connect()
    # the harness screens off params/maker_set.json (published nightly);
    # use the SAME set so any gap is about crossing detection, not the set
    sharps = set(json.load(open(os.path.join(
        HERE, "params", "maker_set.json")))["wallets"])
    sharps = {w.lower() for w in sharps}
    print(f"screened set: {len(sharps)} wallets (same file the box fetches)",
          flush=True)

    # tape crossings over the harness's own window, frozen method
    leans = ml.day_leans(db, int(lo_ts), int(hi_ts) + 1, sharps)
    db.close()
    band = [t for t in leans
            if ml.LEAN_USD <= t["lean_usd"] < 500.0]
    print(f"tape crossings in window: {len(leans)} · in the $150-500 "
          f"follow band: {len(band)}", flush=True)

    def key(w, a, ts):
        return ((w or "").lower(), str(a), int(ts // 86400))

    # the harness's crossing is on the SOURCE token it watched (src_asset);
    # day_leans keys on the same wallet+asset it saw the inventory build in
    h_keys = {key(a.get("wallet"), a.get("src_asset") or a.get("asset"),
                  a["ts"]): a for a in att}
    t_keys = {key(t["w"], t["a"], t["ts"]): t for t in band}
    both = set(h_keys) & set(t_keys)
    h_only = set(h_keys) - set(t_keys)
    t_only = set(t_keys) - set(h_keys)

    print("\n== AGREEMENT ==")
    print(f"  harness attempts matched by a tape crossing: "
          f"{len(both)}/{len(h_keys)} = {100*len(both)/max(1,len(h_keys)):.0f}%")
    print(f"  harness-only (tape never named it): {len(h_only)}")
    print(f"  tape-only  (harness never saw it):  {len(t_only)}"
          f"  [of {len(t_keys)} tape crossings = "
          f"{100*len(t_only)/max(1,len(t_keys)):.0f}% missed]")

    if both:
        drift = []
        for k in both:
            drift.append((h_keys[k]["lean_usd"], t_keys[k]["lean_usd"]))
        rel = sorted(abs(h - t) / max(t, 1) for h, t in drift)
        agree = sum(1 for h, t in drift if abs(h - t) / max(t, 1) < 0.10)
        print("\n== SIZE DRIFT on matched pairs ==")
        print(f"  within 10% of each other: {agree}/{len(drift)} = "
              f"{100*agree/len(drift):.0f}%")
        print(f"  median |gap|: {100*rel[len(rel)//2]:.0f}% · "
              f"p90 {100*rel[int(.9*len(rel))]:.0f}%")
        for h, t in drift[:5]:
            print(f"    harness ${h:>7.0f} vs tape ${t:>7.0f}")

    if t_only:
        by = collections.Counter(k[0][:10] for k in t_only)
        print(f"\n  tape-only by wallet (top): {dict(by.most_common(4))}")
    if h_only:
        by = collections.Counter(k[0][:10] for k in h_only)
        print(f"  harness-only by wallet (top): {dict(by.most_common(4))}")

    print("\nREAD: high agreement => the definitions match and the KILL is "
          "about the market, not the method. Low agreement or large size "
          "drift => the two systems name different events and #22's tape "
          "number was unreachable by construction.")


if __name__ == "__main__":
    main()
