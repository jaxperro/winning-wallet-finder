#!/usr/bin/env python3
"""T9 EXPLORATORY (2026-07-23) — same-event lead-lag: when an event's most
-traded market moves hard in-play, do sibling markets carrying THE SAME
OUTCOME NAME reprice with a fillable lag?

Semantic mapping problem solved narrowly: direction is only claimed where
the follower has an outcome with the exact same (lowercased) name as the
leader's moved outcome (team/player name) — moneyline vs map/set/half
winner vs series markets. No claim on O/Us or unrelated props.

v0 method: cond→event + cond→{outcome→asset} from orders_matched. Leader
per event = most prints. Burst = leader outcome's print moving >= 10c
within 120s (in-play), cooldown 600s/event. Follower read at burst t:
last print p0; drift = p(t+300s) − p0 in the leader-move direction;
tradable leg = buy follower at p0, grade to chain (payouts_for).
Kill: drift <= fees (~2c) at n>=300 episodes, or chain EV <= 0."""
import sys
import time

sys.path.insert(0, "/Users/jaxmakielski/polymarket-smart-money/research")
import tape                                    # noqa: E402
import forward as fwd                          # noqa: E402

MOVE_C = 0.10
MOVE_WIN = 120
DRIFT_WIN = 300
COOLDOWN = 600
BAND = (0.05, 0.95)


def main():
    db = tape.connect()
    tape.build_resolved(db)
    print("building event/outcome maps…", flush=True)
    db.execute("""
    CREATE TEMP TABLE om AS
    SELECT json_extract_string(payload,'$.eventSlug') ev,
           json_extract_string(payload,'$.conditionId') cond,
           lower(json_extract_string(payload,'$.outcome')) outc,
           json_extract_string(payload,'$.asset') asset,
           count(*) n
    FROM aux WHERE type='orders_matched'
      AND json_extract_string(payload,'$.eventSlug') IS NOT NULL
    GROUP BY 1,2,3,4""")
    # events with >=2 conds sharing an outcome name (the mappable set)
    pairs = db.execute("""
    WITH x AS (SELECT ev, outc, count(DISTINCT cond) nc, sum(n) vol
               FROM om WHERE outc NOT IN ('yes','no','over','under','')
               GROUP BY 1,2 HAVING count(DISTINCT cond) >= 2)
    SELECT ev, outc FROM x ORDER BY vol DESC LIMIT 400""").fetchall()
    print(f"mappable (event, outcome) groups: {len(pairs)}", flush=True)
    episodes = []
    for gi, (ev, outc) in enumerate(pairs):
        toks = db.execute("""SELECT cond, asset, n FROM om
            WHERE ev=? AND outc=?""", [ev, outc]).fetchall()
        if len(toks) < 2:
            continue
        toks.sort(key=lambda r: -r[2])
        lead_asset = toks[0][1]
        followers = [r[1] for r in toks[1:3]]      # top-2 followers
        prints = db.execute("""SELECT ts, price::DOUBLE FROM trades
            WHERE asset=? ORDER BY ts""", [lead_asset]).fetchall()
        last_ep = 0.0
        for i in range(1, len(prints)):
            ts, p = prints[i]
            if ts - last_ep < COOLDOWN:
                continue
            j = i - 1
            while j >= 0 and ts - prints[j][0] <= MOVE_WIN:
                j -= 1
            if j < 0 or j == i - 1:
                base = prints[max(j, 0)][1]
            else:
                base = prints[j + 1][1]
            mv = p - base
            if abs(mv) < MOVE_C:
                continue
            last_ep = ts
            for fa in followers:
                r0 = db.execute("""SELECT price::DOUBLE FROM trades
                    WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                    [fa, ts]).fetchone()
                r1 = db.execute("""SELECT price::DOUBLE FROM trades
                    WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                    [fa, ts + DRIFT_WIN]).fetchone()
                if not r0 or not r1:
                    continue
                p0, p1 = r0[0], r1[0]
                if not (BAND[0] <= p0 <= BAND[1]):
                    continue
                sgn = 1 if mv > 0 else -1
                episodes.append({"ev": ev, "a": fa, "ts": ts, "sgn": sgn,
                                 "p0": p0, "drift": (p1 - p0) * sgn})
        if (gi + 1) % 100 == 0:
            print(f"  … {gi+1}/{len(pairs)} groups · "
                  f"{len(episodes)} episodes", flush=True)
    print(f"episodes: {len(episodes)}", flush=True)
    if not episodes:
        return
    d = sorted(e["drift"] for e in episodes)
    n = len(d)
    print(f"follower drift(+{DRIFT_WIN}s, leader direction): "
          f"mean {sum(d)/n*100:+.2f}c · p50 {d[n//2]*100:+.2f}c · "
          f"frac>+2c {sum(x > 0.02 for x in d)/n:.0%} · "
          f"frac<-2c {sum(x < -0.02 for x in d)/n:.0%}", flush=True)
    pays = fwd.payouts_for(db, [e["a"] for e in episodes])
    graded = []
    for e in episodes:
        p = pays.get(e["a"])
        if p is None or p == 0.5:
            continue
        side_px = e["p0"] if e["sgn"] > 0 else 1 - e["p0"]
        side_pay = p if e["sgn"] > 0 else 1 - p
        if not (BAND[0] <= side_px <= BAND[1]):
            continue
        graded.append(100.0 / side_px * (side_pay - side_px))
    if graded:
        print(f"tradable leg (buy follower in leader direction, chain): "
              f"n={len(graded)} · EV/$100 "
              f"{sum(graded)/len(graded):+.2f}", flush=True)


if __name__ == "__main__":
    main()
