"""The WTA's own API (api.wtatennis.com), the feed behind wtatennis.com.

    /tennis/tournaments/?from=&to=&page=&pageSize=   the calendar, 100 a page
    /tennis/tournaments/{id}/{year}/matches          every match of an event
    /tennis/tournaments/{id}/{year}/matches/{mid}/stats
                                                     serve stats; setnum 0 is
                                                     the match, 1..n the sets

Covers Grand Slams, WTA 1000/500/250 and WTA 125 back to at least 2016. ITF
events are listed in the calendar but have no matches here.

Two quirks of the match list:
  Winner is "2" when player A won and "3" when player B did.
  A walkover has an empty score; a retirement has a score that stops short.

Break points in the stats are counted from the returner's side:
breakptsplayeda is the chances A had on B's serve. So the break points A
faced as server are B's fields.
"""

import datetime as dt
import re

from .fetch import get_json

BASE = "https://api.wtatennis.com/tennis"
LEVELS = {"Grand Slam", "WTA 1000", "WTA 500", "WTA 250", "WTA 125", "125K",
          "Premier", "Premier 5", "Premier Mandatory", "International",
          "WTA Finals", "Elite Trophy"}


def season_tournaments(year: int) -> list[dict]:
    """Every tour-level event that starts in `year`."""
    out, page = [], 0
    current = year >= dt.date.today().year
    while True:
        d = get_json(f"{BASE}/tournaments/?page={page}&pageSize=100"
                     f"&from={year}-01-01&to={year}-12-31", cache=not current)
        content = (d or {}).get("content") or []
        out.extend(content)
        if len(content) < 100:
            break
        page += 1

    events = []
    for t in out:
        level = t.get("level") or t["tournamentGroup"].get("level")
        if level not in LEVELS or t.get("year") != year:
            continue
        events.append({
            "tour": "WTA",
            "tournament_id": str(t["tournamentGroup"]["id"]),
            "year": year,
            "name": t["tournamentGroup"]["name"],
            "level": level,
            "surface": t.get("surface"),
            "indoor": {"I": True, "O": False}.get(t.get("inOutdoor")),
            "city": t.get("city"),
            "country": t.get("country"),
            "start_date": t.get("startDate"),
            "end_date": t.get("endDate"),
            "finished": t.get("status") == "past",
        })
    return events


def _sets(score: str) -> list[tuple[int, int]]:
    """'6-4,6(5)-7,7-5' -> [(6,4),(6,7),(7,5)], from A's side."""
    out = []
    for s in filter(None, (score or "").split(",")):
        m = re.match(r"(\d+)(?:\(\d+\))?-(\d+)", s.strip())
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def _won_set(a: int, b: int) -> int:
    """1 if A took the set, -1 if B did, 0 if it was unfinished."""
    hi, lo = max(a, b), min(a, b)
    if (hi == 6 and lo <= 4) or (hi == 7 and lo in (5, 6)) or (hi > 7 and hi - lo == 2):
        return 1 if a > b else -1
    # Match tie-break in place of a third set, or a long final set
    # (Wimbledon to 2018, US Open never): treat any two-clear lead as done.
    if hi >= 10 and hi - lo >= 2:
        return 1 if a > b else -1
    return 0


def _seconds(hms: str | None) -> int | None:
    if not hms:
        return None
    try:
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return None


def event_matches(event: dict) -> list[dict]:
    """Finished singles matches of an event, without their stats."""
    d = get_json(f"{BASE}/tournaments/{event['tournament_id']}/{event['year']}/matches",
                 cache=event["finished"])
    out = []
    for m in (d or {}).get("matches") or []:
        if m.get("DrawMatchType") != "S" or m.get("MatchState") != "F":
            continue
        if not m.get("PlayerIDA") or not m.get("PlayerIDB"):
            continue
        winner = {"2": "a", "3": "b"}.get(str(m.get("Winner")))
        sets = _sets(m.get("ScoreString"))
        tally = [_won_set(a, b) for a, b in sets]
        # Every women's match is best of three, Grand Slams included.
        completed = (winner is not None and
                     max(tally.count(1), tally.count(-1)) >= 2 and 0 not in tally)
        out.append({
            "tour": "WTA",
            "tournament_id": event["tournament_id"],
            "year": event["year"],
            "match_id": m["MatchID"],
            "draw": m.get("DrawLevelType"),
            "round": m.get("RoundID"),
            "played_at": m.get("MatchTimeStamp") or m.get("LastUpdated"),
            "player_a_id": str(m["PlayerIDA"]),
            "player_a": f"{m.get('PlayerNameFirstA', '')} {m.get('PlayerNameLastA', '')}".strip(),
            "player_b_id": str(m["PlayerIDB"]),
            "player_b": f"{m.get('PlayerNameFirstB', '')} {m.get('PlayerNameLastB', '')}".strip(),
            "winner": winner,
            "score": m.get("ScoreString") or None,
            "sets_played": len(sets),
            "completed": completed,
            "duration_s": _seconds(m.get("MatchTimeTotal")),
        })
    return out


def match_serve(match: dict) -> list[dict]:
    """Serve rows for both players, for the match (set 0) and each set."""
    d = get_json(f"{BASE}/tournaments/{match['tournament_id']}/{match['year']}"
                 f"/matches/{match['match_id']}/stats")
    rows = []
    for st in d or []:
        for side, other in (("a", "b"), ("b", "a")):
            first_in = st.get(f"ptsplayed1stserv{side}")
            first_won = st.get(f"ptswon1stserv{side}")
            bp_faced = st.get(f"breakptsplayed{other}")
            bp_lost = st.get(f"breakptsconv{other}")
            rows.append({
                "set_num": int(st.get("setnum") or 0),
                "side": side,
                "player_id": match[f"player_{side}_id"],
                "aces": st.get(f"aces{side}"),
                "double_faults": st.get(f"dblflt{side}"),
                "serve_points": st.get(f"totservplayed{side}"),
                "serve_points_won": st.get(f"ptstotwonserv{side}"),
                "first_in": first_in,
                "first_won": first_won,
                "service_games": st.get(f"servgamesplayed{side}"),
                "bp_faced": bp_faced,
                "bp_saved": (bp_faced - bp_lost) if bp_faced is not None and bp_lost is not None else None,
            })
    return rows
