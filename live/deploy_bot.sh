#!/bin/bash
# One-command deploy of the paper copybot's follow set.
#
#   1. edit live/copybot.paper.json — add/remove/reclass entries in "wallets":
#        {"wallet": "0x…", "name": "…", "class": "volume"|"whale", "floor": 123}
#      ("floor" optional: volume wallets without one get an auto p80 at boot;
#       whales ignore floors — they're followed on every trade)
#   2. ./live/deploy_bot.sh
#
# Validates the config (a malformed JSON = crash-looping container), previews
# the follow set, commits+pushes just the config, restarts the Fly.io worker
# (wwf-copybot, Stockholm — start.sh clones main at boot, so a restart IS the
# deploy for config changes; `flyctl deploy` only needed when host/ or
# fly.toml change), and waits for the fresh boot banner.
set -e
cd "$(dirname "$0")"

python3 - <<'PY'
import json, sys
c = json.load(open("copybot.paper.json"))
ws = c.get("wallets") or []
if not ws:
    sys.exit("copybot.paper.json has no wallets[] — nothing to follow")
seen = set()
for w in ws:
    a = w.get("wallet", "")
    assert a.startswith("0x") and len(a) == 42, f"bad address: {w}"
    assert a.lower() not in seen, f"duplicate wallet: {a}"
    seen.add(a.lower())
    assert w.get("class", "volume") in ("volume", "whale"), f"bad class: {w}"
    if w.get("floor") is not None:
        assert float(w["floor"]) >= 0, f"bad floor: {w}"
pct = (c.get("follow") or {}).get("class_pct") or {}
print(f"config OK · {len(ws)} wallets:")
for w in ws:
    cls = w.get("class", "volume")
    fl = ("follow-all" if cls == "whale"
          else f"floor ${w['floor']:,.0f}" if w.get("floor") is not None
          else "floor auto (p80 at boot)")
    print(f"  {w.get('name', w['wallet'][:10]):<18} {cls:<7} "
          f"{pct.get(cls, 0.04)*100:.0f}%/bet · {fl}")
PY

git add copybot.paper.json
if git diff --cached --quiet; then
  echo "no config changes — redeploying anyway (picks up current main)"
else
  git commit -m "copybot: follow-set update (deploy_bot.sh)"
  git pull --rebase --autostash -q
  git push -q
  echo "pushed."
fi

# push mode: keep the Alchemy webhook's address list matched to the follow set
python3 sync_webhook.py || echo "⚠ webhook sync failed — update the address list manually"

flyctl apps restart wwf-copybot
echo "restart triggered — waiting for the new container…"
python3 - <<'PY'
# wait for a boot banner logged AFTER the restart (fly log lines are ANSI-
# colored and ISO-stamped; strip + parse here rather than in bash)
import calendar, re, subprocess, sys, time
t0 = time.time() - 30                      # restart just happened
for _ in range(15):
    time.sleep(20)
    raw = subprocess.run(["flyctl", "logs", "--app", "wwf-copybot", "--no-tail"],
                         capture_output=True, text=True).stdout
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in raw.splitlines()]
    for l in reversed(lines):
        if "watching" in l and "wallets" in l:
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", l)
            # log stamps are UTC — timegm, NOT mktime (which assumes local)
            ts = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else 0
            if ts >= t0:
                print("\n".join(x for x in lines[-40:]
                                if re.search(r"watching|floor\[|push mode|VERDICT", x)))
                print("✅ bot rebooted with the new follow set")
                print("   (Alchemy address list synced above — if it warned, update"
                      " manually at dashboard.alchemy.com → Webhooks)")
                sys.exit(0)
            break
print("⚠ no fresh boot banner within 5min — check: flyctl logs --app wwf-copybot")
sys.exit(1)
PY
