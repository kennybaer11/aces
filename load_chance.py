#!/usr/bin/env python
"""Store Chance.cz ace and double-fault lines collected by chance_collect.js,
and price the ones the model can.

    python load_chance.py cache/chance/2026-10-01.json
    python load_chance.py cache/chance/2026-10-01.json --advise

Chance.cz quotes both sides of one line per market - "under 10.5" and "over
10.5" - so each line is stored as an over and an under rung in aces.odds and
priced as one aces.line row on the side with more value. Its ace lines are on
ATP matches; they are priced once the ATP history is loaded, and until then
only stored. Like odds.py, nothing is advised without --advise, and only aces.
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from aces import advice, db, events, model, venues
from aces.players import Players, norm

EDGE = 0.10           # tails run ~1 point optimistic (tail_check), so a higher bar than 5%
MIN_P = 0.30          # never advise a side the model gives less than this
MAX_EDGE = 0.50       # nor one with more value than this: see odds.py
ADVISE_MARKETS = {"aces", "aces:1", "aces:2"}
VALIDATED_TOURS = {"WTA", "ATP"}   # see odds.py
SURFACES = {"tvrdý p.": "Hard", "antuka": "Clay", "tráva": "Grass", "koberec": "Hard"}


def surface_of(comp: str) -> str:
    for k, v in SURFACES.items():
        if k in comp:
            return v
    return "Hard"


def which_player(box: str, home: str, away: str) -> str | None:
    """'A.Molčan' or 'Yunchaokete Bu' -> '1' or '2', by shared name words."""
    b = set(norm(box.replace(".", " ")).split())
    h, a = set(norm(home).split()), set(norm(away).split())
    sh, sa = len(b & h), len(b & a)
    return "1" if sh > sa else "2" if sa > sh else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--advise", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    rows = json.load(open(args.file, encoding="utf-8"))
    conn = db.connect()
    fetched_at = datetime.now(timezone.utc)

    matches = {}
    for (mid, start, comp, name, home, away, mk, box, line, over, under) in rows:
        ev = matches.setdefault(str(mid), {
            "event_id": str(mid), "kickoff": datetime.fromisoformat(start), "league": comp,
            "name_1": home, "name_2": away, "tour": "WTA" if comp.startswith("WTA") else "ATP",
            "surface": surface_of(comp), "lines": []})
        if mk.endswith(":p"):
            who = which_player(box, home, away)
            if not who:
                continue
            mk = mk[:-1] + who
        prices = {s: p for s, p in (("over", over), ("under", under)) if p and p > 1.01}
        if prices:
            ev["lines"].append((mk, float(line), prices))

    ratings, players, seen, V = {}, {}, {}, {}
    for tour in {e["tour"] for e in matches.values()}:
        players[tour] = Players(conn, tour)
        V[tour] = venues.Venues(conn, tour)
        hist = model.load(conn, tour)
        if len(hist):
            ratings[tour] = model.walk(hist)
            seen[tour] = hist.player_a_id.value_counts().add(hist.player_b_id.value_counts(), fill_value=0)

    stored = priced = advised = 0
    with conn.cursor() as cur:
        for ev in matches.values():
            P = players[ev["tour"]]
            ev["player_1_id"] = P.by_full_name(ev["name_1"])
            ev["player_2_id"] = P.by_full_name(ev["name_2"])
            ev["match_key"] = events.match_key(ev["kickoff"], ev["name_1"], ev["name_2"],
                                               ev["player_1_id"], ev["player_2_id"])
            r = ratings.get(ev["tour"])
            pred = None
            if r and ev["player_1_id"] and ev["player_2_id"]:
                level = "Grand Slam" if any(s in ev["league"] for s in ("Australian", "Roland", "Wimbledon", "US Open")) else None
                pred = model.predict(r, ev["player_1_id"], ev["player_2_id"], ev["surface"],
                                     model._days(ev["kickoff"]), tour=ev["tour"], level=level,
                                     venue=V[ev["tour"]].key(ev["league"]))
            cur.execute("""
                INSERT INTO aces.event (source, event_id, kickoff, league, name_1, name_2,
                                        player_1_id, player_2_id, surface, tour, match_key)
                VALUES ('chance', %(event_id)s, %(kickoff)s, %(league)s, %(name_1)s, %(name_2)s,
                        %(player_1_id)s, %(player_2_id)s, %(surface)s, %(tour)s, %(match_key)s)
                ON CONFLICT (source, event_id) DO UPDATE SET kickoff=EXCLUDED.kickoff,
                  player_1_id=EXCLUDED.player_1_id, player_2_id=EXCLUDED.player_2_id,
                  tour=EXCLUDED.tour, match_key=EXCLUDED.match_key, last_seen=now()""", ev)
            for mk, line, prices in ev["lines"]:
                p_rung = (model.Prediction.over(pred.pmf(mk), line)
                          if pred and mk != "games" else None)
                for side, price in prices.items():
                    cur.execute("""INSERT INTO aces.odds (source, event_id, fetched_at, market, at_least,
                                                          price, side, p_model)
                                   VALUES ('chance', %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                (ev["event_id"], fetched_at, mk, math.floor(line) + 1, price, side, p_rung))
                    stored += 1
            if pred is None or ev["kickoff"] <= fetched_at:
                continue
            s = seen[ev["tour"]]
            enough = (ev["tour"] in VALIDATED_TOURS
                      and min(s.get(ev["player_1_id"], 0), s.get(ev["player_2_id"], 0)) >= 10)
            # Chance can quote several lines in one market (aces 8.5, 9.5, ...):
            # each is priced and stored, and the market's advice is the best of
            # them by Kelly growth - one record per market, as on Betano.
            best, means, offered = {}, {}, {}
            for mk, line, prices in ev["lines"]:
                if mk == "games":
                    continue                 # stored above for the length model, not priced
                pmf = pred.pmf(mk)
                p = model.Prediction.over(pmf, line)
                means[mk] = float((pmf * range(len(pmf))).sum())
                offered.setdefault(mk, []).append(line)
                ev_over = p * prices["over"] - 1 if "over" in prices else None
                ev_under = (1 - p) * prices["under"] - 1 if "under" in prices else None
                # Same rule as odds.py: the side with the best Kelly growth among
                # those with EDGE to MAX_EDGE of value and a model chance of at least MIN_P.
                sides = [(model.kelly_growth(pr, prices[sd]), sd, pr) for sd, pr, e in
                         (("over", p, ev_over), ("under", 1 - p, ev_under))
                         if e is not None and EDGE <= e <= MAX_EDGE and pr >= MIN_P]
                pick = max(sides) if sides and args.advise and enough and mk in ADVISE_MARKETS else None
                if pick and (mk not in best or pick[0] > best[mk][0]):
                    best[mk] = (pick[0], pick[1], pick[2], line, prices[pick[1]])
                cur.execute("""
                    INSERT INTO aces.line (date, player_1_id, player_1, player_2_id, player_2, market, line,
                                           surface, over_odds, under_odds, p_over, model_mean, bet,
                                           source, event_id, kickoff)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'chance', %s, %s)
                    ON CONFLICT (date, player_1_id, player_2_id, market, line) DO UPDATE SET
                      over_odds=EXCLUDED.over_odds, under_odds=EXCLUDED.under_odds, p_over=EXCLUDED.p_over,
                      model_mean=EXCLUDED.model_mean, bet=EXCLUDED.bet, priced_at=now()
                    WHERE aces.line.actual IS NULL AND aces.line.void IS NOT TRUE AND NOT aces.line.placed
                      AND (aces.line.kickoff IS NULL OR aces.line.kickoff > now())""",
                            (ev["kickoff"].date(), ev["player_1_id"], ev["name_1"], ev["player_2_id"], ev["name_2"],
                             mk, line, ev["surface"], prices.get("over"), prices.get("under"), p,
                             means[mk], pick[1] if pick else None, ev["event_id"], ev["kickoff"]))
                priced += 1
            for mk, lines in offered.items():
                # Lines no longer offered go; only the market's best keeps its bet.
                cur.execute("""
                    DELETE FROM aces.line WHERE source = 'chance' AND event_id = %s AND market = %s
                       AND NOT (line = ANY(%s)) AND kickoff > now() AND actual IS NULL AND NOT placed""",
                            (ev["event_id"], mk, lines))
                b = best.get(mk)
                cur.execute("""
                    UPDATE aces.line SET bet = NULL WHERE source = 'chance' AND event_id = %s AND market = %s
                       AND line <> %s AND kickoff > now() AND actual IS NULL AND NOT placed""",
                            (ev["event_id"], mk, b[3] if b else -1))
                advice.record(cur, {**ev, "source": "chance"}, mk, b is not None,
                              line=b[3] if b else None, side=b[1] if b else None,
                              odds=b[4] if b else None, p_side=b[2] if b else None,
                              model_mean=means[mk])
                advised += b is not None
            if pred is not None:
                advice.share_stakes(cur, ev["match_key"])
    conn.commit()
    unmatched = sum(1 for e in matches.values() if not (e["player_1_id"] and e["player_2_id"]))
    print(f"{len(matches)} matches, {stored} prices stored, {priced} lines priced, "
          f"{advised} advised, {unmatched} matches without both players in the {'/'.join(players)} data")


if __name__ == "__main__":
    sys.exit(main())
