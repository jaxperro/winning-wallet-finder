#!/usr/bin/env python3
"""#24 v1 — per-wallet follow-start dates from the follow config's own git
history (the truth of when each wallet actually entered the book).
Writes live/follow_since.json {wallet_lower: epoch}; wallets present in
the FIRST recorded config carry that first-commit date (left-censored,
same convention as sharp_halflife). Run by daily.sh before the portfolio
replays so --follow-only never replays a wallet before we followed it
(the JuiceFarm class: 8 'unseen' bets that were simply pre-follow)."""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    log = subprocess.run(
        ["git", "log", "--reverse", "--format=%at %H",
         "--", "live/copybot.paper.json"],
        cwd=ROOT, capture_output=True, text=True).stdout.split()
    pairs = list(zip(log[0::2], log[1::2]))
    since = {}
    for ts, sha in pairs:
        try:
            blob = subprocess.run(
                ["git", "show", f"{sha}:live/copybot.paper.json"],
                cwd=ROOT, capture_output=True, text=True).stdout
            cfg = json.loads(blob)
        except Exception:
            continue
        for w in cfg.get("wallets", []):
            a = (w.get("wallet") or "").lower()
            if a and a not in since:
                since[a] = int(ts)
    # wallets in the working tree but not yet committed: follow = now
    try:
        cfg = json.load(open(os.path.join(HERE, "copybot.paper.json")))
        for w in cfg.get("wallets", []):
            a = (w.get("wallet") or "").lower()
            if a and a not in since:
                since[a] = int(time.time())
    except Exception:
        pass
    out = os.path.join(HERE, "follow_since.json")
    json.dump(since, open(out, "w"), indent=1)
    print(f"[follow_since] {len(since)} wallets -> follow_since.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
