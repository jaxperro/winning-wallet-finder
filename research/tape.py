#!/usr/bin/env python3
"""Read-only tape access + ground truth for research/ (see README silo rules).

Resolution method is the one chain-validated 742/742 on 2026-07-20
(live/tape_sharps.py first run): a token is proxy-resolved when its final-
30min VWAP converged (>= 0.97 / <= 0.03) AND it went quiet >= QUIET_H before
the tape end AND no sibling of the same condition disagrees (still trading,
or a second proxy-winner). `chain_overlay()` upgrades any subset to CTF
payout-vector truth via live/payouts.py (append-only shared cache).

Timezones: tape ts are epoch UTC. Polymarket crypto titles quote ET; the
tape era is July 2026 = EDT = UTC-4 (ET_OFF). Sprints/hourlies embed their
window in the title; "on <date>" dailies resolve at 12:00 ET by venue
convention.
"""
import os
import re
import sys
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RTDS = os.path.join(ROOT, "live", "rtds.duckdb")

WIN_T, LOSE_T = 0.97, 0.03
QUIET_H = 2
ET_OFF = 4 * 3600            # EDT (July) = UTC-4
YEAR = 2026                  # tape era; revisit at year roll

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

SYMBOLS = {"bitcoin": "btcusdt", "btc": "btcusdt",
           "ethereum": "ethusdt", "eth": "ethusdt",
           "solana": "solusdt", "sol": "solusdt",
           "xrp": "xrpusdt", "bnb": "bnbusdt", "doge": "dogeusdt",
           "dogecoin": "dogeusdt"}


def connect():
    return duckdb.connect(RTDS, read_only=True)


# ── tape proxy-resolution ───────────────────────────────────────────────────

def build_resolved(db, t_end=None):
    """TEMP tables `res_tok` (asset, cond, last_ts, term_vwap, payout) and
    `res_bad` on this connection. Idempotent per connection."""
    if t_end is None:
        t_end = db.execute("SELECT max(ts) FROM trades").fetchone()[0]
    quiet = t_end - QUIET_H * 3600
    db.execute(f"""
    CREATE OR REPLACE TEMP TABLE _alltok AS
    WITH last AS (
      SELECT asset, any_value(cond) cond, max(ts) last_ts
      FROM trades WHERE cond IS NOT NULL AND cond != ''
        AND ts <= {t_end} GROUP BY asset
    ), term AS (
      SELECT t.asset, sum(t.price * t.size) / nullif(sum(t.size), 0) term_vwap
      FROM trades t JOIN last l ON t.asset = l.asset
      WHERE t.ts >= l.last_ts - 1800 AND t.ts <= {t_end} GROUP BY t.asset
    )
    SELECT l.asset, l.cond, l.last_ts, tm.term_vwap,
           l.last_ts > {quiet} AS alive,
           CASE WHEN tm.term_vwap >= {WIN_T} THEN 1.0
                WHEN tm.term_vwap <= {LOSE_T} THEN 0.0 END AS payout
    FROM last l JOIN term tm ON l.asset = tm.asset""")
    db.execute("""
    CREATE OR REPLACE TEMP TABLE res_bad AS
    SELECT cond FROM _alltok GROUP BY cond
    HAVING bool_or(alive)
        OR sum(CASE WHEN payout = 1.0 THEN 1 ELSE 0 END) > 1""")
    db.execute("""
    CREATE OR REPLACE TEMP TABLE res_tok AS
    SELECT asset, cond, last_ts, term_vwap, payout FROM _alltok
    WHERE NOT alive AND payout IS NOT NULL
      AND cond NOT IN (SELECT cond FROM res_bad)""")
    return t_end


def chain_overlay(pairs):
    """[(cond, asset)] -> {(cond, asset): 1.0/0.0/0.5/None} via payouts.py.
    The only shared write in research/ (append-only resolutions cache)."""
    sys.path.insert(0, os.path.join(ROOT, "live"))
    import payouts
    payouts.ensure(sorted({c for c, _ in pairs}))
    return {(c, a): payouts.truth(c, a) for c, a in pairs}


# ── title parsers ───────────────────────────────────────────────────────────

NICHE_PATTERNS = [
    ("esports", ["lol:", "dota", "cs2", "csgo", "valorant", "esports",
                 "bilibili", "map ", "game 1", "game 2", "game 3"]),
    ("tennis", ["tennis", "atp", "wta", "wimbledon", "set winner"]),
    ("sports", [" vs. ", " vs ", " @ ", "mlb", "nba", "nhl", "ufc",
                "world cup", "f1", "grand prix", "fifa"]),
    ("crypto", ["bitcoin", "btc", "ethereum", "solana", "xrp", "doge",
                "price of", "up or down"]),
    ("politics", ["election", "president", "senate", "governor", "mayor",
                  "nominee", "impeach", "tariff", "fed ", "rate cut"]),
    ("geo", ["iran", "israel", "russia", "ukraine", "china", "taiwan",
             "ceasefire", "strike", "nato"]),
]


def niche(title):
    t = (title or "").lower()
    for label, pats in NICHE_PATTERNS:
        if any(p in t for p in pats):
            return label
    return "other"


def _et(mon, day, hh, mm):
    return time.mktime(time.struct_time(
        (YEAR, mon, day, 0, 0, 0, 0, 0, 0))) - time.timezone + hh * 3600 \
        + mm * 60 + ET_OFF


def _clock(h, m, ap):
    h = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
    return h, int(m or 0)


RE_SPRINT = re.compile(
    r"(?i)^(\w+)\s+up or down\s*-\s*(\w+)\s+(\d+),\s*"
    r"(\d+)(?::(\d+))?(am|pm)-(\d+)(?::(\d+))?(pm|am)\s*et")
RE_HOURLY = re.compile(
    r"(?i)^(\w+)\s+(above|below)\s+([\d,\.]+)\s+on\s+(\w+)\s+(\d+),\s*"
    r"(\d+)(?::(\d+))?\s*(am|pm)\s*et")
RE_DAILY = re.compile(
    r"(?i)price of (\w+) be (above|below|between)\s+\$?([\d,\.]+)"
    r"(?:\s+and\s+\$?([\d,\.]+))?\s+on\s+(\w+)\s+(\d+)")
# NOT parsed on purpose: "dip to / reach $K" one-touch claims are
# path-dependent (barrier, not terminal digital) — the Φ fair value below
# would misprice them. v2 if the terminal edge proves out.


def _num(s):
    return float(s.replace(",", "")) if s else None


def crypto_parse(title):
    """-> dict(sym, kind, k1, k2, t0, t1) or None.
    kind: sprint (S_t1 > S_t0), above/below/between (vs strike at t1).
    t0 only for sprints (window open)."""
    t = title or ""
    m = RE_SPRINT.match(t)
    if m:
        sym = SYMBOLS.get(m.group(1).lower())
        mon = MONTHS.get(m.group(2).lower())
        if not sym or not mon:
            return None
        day = int(m.group(3))
        h0, m0 = _clock(m.group(4), m.group(5), m.group(6))
        h1, m1 = _clock(m.group(7), m.group(8), m.group(9))
        return {"sym": sym, "kind": "sprint", "k1": None, "k2": None,
                "t0": _et(mon, day, h0, m0), "t1": _et(mon, day, h1, m1)}
    m = RE_HOURLY.match(t)
    if m:
        sym, mon = SYMBOLS.get(m.group(1).lower()), MONTHS.get(m.group(4).lower())
        if not sym or not mon:
            return None
        h, mi = _clock(m.group(6), m.group(7), m.group(8))
        return {"sym": sym, "kind": m.group(2).lower(), "k1": _num(m.group(3)),
                "k2": None, "t0": None,
                "t1": _et(mon, int(m.group(5)), h, mi)}
    m = RE_DAILY.search(t)
    if m:
        sym, mon = SYMBOLS.get(m.group(1).lower()), MONTHS.get(m.group(5).lower())
        if not sym or not mon:
            return None
        return {"sym": sym, "kind": m.group(2).lower(), "k1": _num(m.group(3)),
                "k2": _num(m.group(4)), "t0": None,
                "t1": _et(mon, int(m.group(6)), 12, 0)}   # dailies: 12PM ET
    return None


# ── tick series ─────────────────────────────────────────────────────────────

def load_ticks(db, sym):
    """[(ts, price)] sorted — ms feed timestamps preferred over ingest ts."""
    rows = db.execute("""
      SELECT coalesce(cast(json_extract(payload,'$.timestamp') AS DOUBLE)/1000, ts) t,
             cast(json_extract(payload,'$.value') AS DOUBLE) v
      FROM aux WHERE topic = 'crypto_prices'
        AND json_extract_string(payload,'$.symbol') = ?
      ORDER BY 1""", [sym]).fetchall()
    return [(t, v) for t, v in rows if v]
