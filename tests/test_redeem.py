"""Stub test for the pUSD redeemer's calldata (closes #4 regression pin).
Layout proven on-chain 2026-07-19: a decoded revert ("result not received")
on an unresolved market + clean SUCCESS simulation on a resolved one."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from redeem import redeem_calldata, PUSD, _SEL_REDEEM

COND = "0x" + "ab" * 32
d = redeem_calldata(COND)
fails = []
if not d.startswith("0x" + _SEL_REDEEM): fails.append("selector")
body = d[10:]
words = [body[i:i+64] for i in range(0, len(body), 64)]
if len(words) != 7: fails.append(f"word count {len(words)}")
if words[0][-40:] != PUSD[2:].lower(): fails.append("collateral != pUSD")
if int(words[1], 16) != 0: fails.append("parentCollectionId != 0")
if words[2] != "ab" * 32: fails.append("conditionId")
if int(words[3], 16) != 0x80: fails.append("array offset")
if [int(w, 16) for w in words[4:]] != [2, 1, 2]: fails.append("indexSets")
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
