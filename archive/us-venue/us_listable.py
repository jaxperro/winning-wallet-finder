#!/usr/bin/env python3
"""Which of the replayed bets' markets were ALSO listed on Polymarket US?

Polymarket US (gateway.polymarket.us, public/no-auth) settles off-chain — no
wallets to copy — but it lists the same game markets, and it RETAINS closed
historical listings (back to Oct 2025). So we can check, for every bet in the
portfolio.py replay stream, whether a real US listing existed for that market:

  tier 1  deterministic slug: US slug = "aec-"/"tec-" + intl slug
          (holds for esports/team sports; batched via the repeatable ?slug= param)
  tier 2  name+date+line match: tennis abbreviates differently per venue
          (US trimcc/pedsak vs intl mccormi/sakamo; itfme-/itfwo- vs itf-), so
          match full names from the intl question against a US window of the
          bet date's moneyline/totals/spreads listings.

Sub-market winners ("Set 1 Winner: …") name-match the WRONG (full-match) US
market, so anything with a set/map/half qualifier is tier-1 only.

Output -> us_listable.json {cond: {listed, us_slug, how, title}}. Replay just
the listable subset with:
    ONLY_CONDS=us_listable.json PORTFOLIO_OUT=/tmp/pf_us.json python3 portfolio.py
"""
import json
import os
import re
import ssl
import time
import unicodedata
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import portfolio as pf

_SSL = ssl._create_unverified_context()
GATEWAY = "https://gateway.polymarket.us/v1/markets"
HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "us_listable.json")


def get(url):
    for attempt in range(3):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30, context=_SSL)
            return json.loads(r.read())
        except Exception:
            time.sleep(1 + attempt)
    return {}


def norm(s):
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def name_in(name, hay):
    ws = [w for w in name.split() if len(w) > 1]
    return bool(ws) and all(w in hay for w in ws)


# sub-market qualifiers: a "Set 1 Winner"/"First Map" market would name-match
# the full-match US moneyline — a false positive — so these skip tier 2
QUAL = re.compile(r"\b(set \d|1st|first|2nd|second|3rd|third|map \d|game \d|handicap|"
                  r"to score|half|quarter|period|race to|双|correct score|doubles|"
                  r"corners|cards|btts|total sets)\b", re.I)


def parse_title(t):
    """intl question -> {a, b, kind, line} or None (None = tier-1 only)."""
    s = str(t or "")
    ou = re.search(r"O/U\s+(\d+(?:\.\d+)?)", s, re.I)
    hc = re.search(r"\([+-](\d+(?:\.\d+)?)\)", s)
    kind = "totals" if ou else "spreads" if hc else "moneyline"
    line = ou.group(1) if ou else (hc.group(1) if hc else None)
    if QUAL.search(s):
        return None
    s = re.sub(r"\(BO\d+\)", " ", s, flags=re.I)
    s = re.sub(r"\([+-]?\d+(?:\.\d+)?\)", " ", s)
    parts = re.split(r"\svs\.?\s", s, flags=re.I)
    if len(parts) < 2:
        return None
    a = parts[0].split(":", 1)[1] if ":" in parts[0] else parts[0]
    # esports format "Counter-Strike: A vs B (BO3) - IEM Cologne Playoffs":
    # drop the " - Tournament" tail and any ": qualifier" tail from side b
    b = parts[1].split(":")[0].split(" - ")[0]
    a, b = norm(a), norm(b)
    if not a or not b:
        return None
    return {"a": a, "b": b, "kind": kind, "line": line}


_WINDOWS = {}      # (kind, date) -> [{slug, hay}]


def us_window(kind, date):
    """All US <kind> markets listed around <date> (startDate ≈ listing time,
    day-of/day-before for game markets), closed ones included."""
    key = (kind, date)
    if key in _WINDOWS:
        return _WINDOWS[key]
    t = time.mktime(time.strptime(date, "%Y-%m-%d"))
    iso = lambda x: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x))
    lo, hi = iso(t - 2 * 86400), iso(t + 2 * 86400)
    out = []
    # Wimbledon-week windows run past 2k moneylines — never truncate silently
    for off in range(0, 20000, 100):
        d = get(f"{GATEWAY}?marketTypes={kind}&startDateMin={lo}&startDateMax={hi}"
                f"&limit=100&offset={off}")
        ms = d.get("markets", [])
        for m in ms:
            out.append({"slug": m["slug"],
                        "hay": norm(m.get("question", "")) + " " + norm(m["slug"])})
        if len(ms) < 100:
            break
    _WINDOWS[key] = out
    return out


def main():
    # the exact bet stream portfolio.py replays (same dedupe: first entry per market)
    resolved = pf.window_bets()
    opened = pf.open_bets()
    by = {}
    for b in resolved + opened:
        if b.get("cond") and (b["cond"] not in by or b["entry_t"] < by[b["cond"]]["entry_t"]):
            by[b["cond"]] = b
    conds = list(by)
    print(f"[us] {len(conds)} unique markets in the {pf.DAYS}d replay stream", flush=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(pf.market_meta, conds))
    meta = {c: pf.market_meta(c) for c in conds}

    result = {}

    # ---- tier 1: deterministic prefixed slugs, batched --------------------
    cand = {}                                     # us_slug -> cond
    for c in conds:
        slug = meta[c]["slug"]
        if slug:
            cand["aec-" + slug] = c
            cand["tec-" + slug] = c
    slugs = list(cand)
    for i in range(0, len(slugs), 25):
        qs = "&".join("slug=" + s for s in slugs[i:i + 25])
        for m in get(f"{GATEWAY}?{qs}&limit=100").get("markets", []):
            c = cand.get(m["slug"])
            if c and c not in result:
                result[c] = {"listed": True, "us_slug": m["slug"], "how": "slug",
                             "title": meta[c]["title"]}
    print(f"[us] tier 1 (direct slug): {len(result)} listed", flush=True)

    # ---- tier 2: name + date + line ---------------------------------------
    t2 = 0
    for c in conds:
        if c in result:
            continue
        title, slug = meta[c]["title"], meta[c]["slug"]
        p = parse_title(title)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", slug or "")
        if not p or not dm:
            result[c] = {"listed": False, "how": "no-parse", "title": title}
            continue
        hit = None
        for m in us_window(p["kind"], dm.group(1)):
            if not (name_in(p["a"], m["hay"]) and name_in(p["b"], m["hay"])):
                continue
            if p["line"] and norm(p["line"]) not in m["hay"]:
                continue
            hit = m
            break
        if hit:
            t2 += 1
            result[c] = {"listed": True, "us_slug": hit["slug"], "how": "name",
                         "title": title}
        else:
            result[c] = {"listed": False, "how": "unmatched", "title": title}
    print(f"[us] tier 2 (name+date): {t2} more listed "
          f"({len(_WINDOWS)} US windows fetched)", flush=True)

    json.dump(result, open(OUT, "w"), indent=1)
    n_listed = sum(1 for v in result.values() if v["listed"])
    print(f"[us] TOTAL: {n_listed}/{len(conds)} replayed markets had a US listing "
          f"-> {os.path.basename(OUT)}", flush=True)

    # breakdown by intl slug prefix so we can see WHERE coverage holds
    pre = lambda s: (s or "?").split("-")[0]
    for label, keep in [("listed", True), ("unlisted", False)]:
        ctr = Counter(pre(meta[c]["slug"]) for c, v in result.items() if v["listed"] == keep)
        print(f"[us] {label:9s} " + "  ".join(f"{k}:{n}" for k, n in ctr.most_common(14)),
              flush=True)


if __name__ == "__main__":
    main()
