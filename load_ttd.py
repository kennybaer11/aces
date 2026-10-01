#!/usr/bin/env python
"""Load ATP matches collected from tennistourdata.com into the aces tables.

    python load_ttd.py ttd_atp_2023_2026.json

tennistourdata.com (its owner gave permission on 2026-10-01) serves its
tables only to a browser, so the rows are collected in a browser tab and saved
as one JSON file; this loads that file. Each row is one player's side of a
match:

    loc, job year, page, row class, Type, Surface, Level, Location, Round,
    Year, Date, Player, set 1..5, Serves, 1st Serve, 2nd Serve, Aces, DF,
    1st Serve %, 1st Won %, 2nd Won %, BP Saved, Odds

"Serves" is serve points: first serves in plus second-serve points. The two
rows of a match are consecutive, the second carrying ttd-match-end, and the
winner's row the class Winner. The site has no player ids, so a player is
keyed by the name as it writes it ("Machac T."), prefixed "ttd:".
"""

import hashlib
import json
import re
import sys
from datetime import date

from dotenv import load_dotenv

from aces import db

MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}
LEVELS = {"2000": "Grand Slam", "1000": "ATP 1000", "500": "ATP 500",
          "250": "ATP 250", "0": "ATP Finals"}


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _pct(s):
    m = re.match(r"(\d+(?:\.\d+)?)%", s or "")
    return float(m.group(1)) / 100 if m else None


def side(row: list) -> dict:
    (_, _, _, cls, typ, surface, level, location, rnd, year, day, player,
     s1, s2, s3, s4, s5, serves, first, second, aces, dfs, _first_pct,
     first_won_pct, second_won_pct, bp, odds) = row[:27]
    d, mon = re.match(r"(\d+)\.\s*(\w{3})", day).groups()
    sp, fi, se = _int(serves), _int(first), _int(second)
    fw, sw = _pct(first_won_pct), _pct(second_won_pct)
    won = (round(fi * fw) + round(se * sw)) if None not in (fi, se, fw, sw) else None
    bp_m = re.match(r"(\d+)/(\d+)", bp or "")
    return {
        "cls": cls, "surface": surface, "level": LEVELS.get(level, level),
        "location": location, "round": rnd, "year": int(year),
        "date": date(int(year), MONTHS[mon], int(d)), "player": player,
        "games": [_int(x) for x in (s1, s2, s3, s4, s5)],
        "serve_points": sp, "first_in": fi, "first_won": round(fi * fw) if fi is not None and fw is not None else None,
        "serve_points_won": won, "aces": _int(aces), "double_faults": _int(dfs),
        "bp_saved": _int(bp_m.group(1)) if bp_m else None,
        "bp_faced": _int(bp_m.group(2)) if bp_m else None,
        "winner": "Winner" in cls.split(),
        "retired": bool(re.search(r"Retired|ttd-ret|Walkover|ttd-wo", cls)),
    }


def matches(rows: list):
    """Pair consecutive rows into matches."""
    pending = None
    for row in rows:
        s = side(row)
        if pending is None:
            pending = s
            if "ttd-match-end" in s["cls"].split():      # a lone row: no opponent
                pending = None
            continue
        a, b = pending, s
        pending = None
        if (a["location"], a["date"], a["round"]) != (b["location"], b["date"], b["round"]):
            continue                                     # pairing slipped: drop rather than guess
        yield a, b


def main(path):
    load_dotenv()
    data = json.load(open(path, encoding="utf-8"))
    rows = data["rows"] if isinstance(data, dict) else data
    conn = db.connect()
    seen, n = set(), 0
    with conn.cursor() as cur:
        for a, b in matches(rows):
            tid = a["location"]
            key = (tid, a["year"], a["date"], a["round"], a["player"], b["player"])
            if key in seen:
                continue                                 # the same page read twice
            seen.add(key)
            mid = hashlib.sha1("|".join(map(str, key)).encode()).hexdigest()[:16]
            sets = sum(1 for g in a["games"] if g is not None)
            completed = not (a["retired"] or b["retired"]) and a["serve_points"] and b["serve_points"]
            cur.execute("""
                INSERT INTO aces.tournament (tour, tournament_id, year, name, level, surface, indoor, start_date)
                VALUES ('ATP', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tour, tournament_id, year) DO UPDATE SET
                  start_date = LEAST(aces.tournament.start_date, EXCLUDED.start_date)""",
                        (tid, a["year"], tid, a["level"],
                         "Hard" if a["surface"] == "Indoor" else a["surface"],
                         a["surface"] == "Indoor", a["date"]))
            score = ",".join(f"{x}-{y}" for x, y in zip(a["games"], b["games"]) if x is not None)
            cur.execute("""
                INSERT INTO aces.match (tour, tournament_id, year, match_id, draw, round, played_at,
                                        player_a_id, player_a, player_b_id, player_b, winner,
                                        score, sets_played, completed)
                VALUES ('ATP', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tour, tournament_id, year, match_id) DO NOTHING""",
                        (tid, a["year"], mid, "Q" if a["round"].startswith("Q") else "M", a["round"],
                         a["date"], "ttd:" + a["player"], a["player"], "ttd:" + b["player"], b["player"],
                         "a" if a["winner"] else "b" if b["winner"] else None,
                         score, sets, bool(completed)))
            for sd, s in (("a", a), ("b", b)):
                cur.execute("""
                    INSERT INTO aces.serve (tour, tournament_id, year, match_id, set_num, side, player_id,
                                            aces, double_faults, serve_points, serve_points_won,
                                            first_in, first_won, bp_faced, bp_saved)
                    VALUES ('ATP', %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING""",
                            (tid, a["year"], mid, sd, "ttd:" + s["player"], s["aces"], s["double_faults"],
                             s["serve_points"], s["serve_points_won"], s["first_in"], s["first_won"],
                             s["bp_faced"], s["bp_saved"]))
            n += 1
    conn.commit()
    print(f"{n} ATP matches loaded from {len(rows)} rows")


if __name__ == "__main__":
    main(sys.argv[1])
