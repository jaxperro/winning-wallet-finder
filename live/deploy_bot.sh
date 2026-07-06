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
# the follow set, commits+pushes just the config, redeploys the Railway worker,
# and waits for the fresh boot banner.
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

railway redeploy --service copybot --yes
echo "redeploy triggered — waiting for the new container…"
for i in $(seq 1 15); do
  sleep 20
  tail5=$(railway logs --service copybot 2>/dev/null | tail -5)
  if echo "$tail5" | grep -qE "\[[123]\] "; then
    railway logs --service copybot 2>/dev/null | grep -E "watching|floor\[" | tail -12
    echo "$tail5" | tail -1
    echo "✅ bot rebooted with the new follow set"
    echo "   (push mode? remember: the Alchemy webhook's address list must match"
    echo "    the follow set — update it at dashboard.alchemy.com → Webhooks)"
    exit 0
  fi
done
echo "⚠ no fresh boot banner within 5min — check: railway logs --service copybot"
exit 1
