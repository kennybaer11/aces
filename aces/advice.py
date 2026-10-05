"""Keep aces.advice - the record of the advice the site gave - in step with
each collection. Both collectors (odds.py for Betano, load_chance.py for
Chance.cz) call record() for every market they price.

While the match is still to start, an advised market is written (or
rewritten) with backed = true, and a market that was advised but no longer is
keeps its last advice with backed = false: withdrawn, not forgotten. From
kickoff nothing here can change it - the WHERE below, and behind it the
aces.advice_locked trigger in the database.
"""

import os
import subprocess

_VERSION = None


def code_version():
    """The aces commit the advice came from, so a record can be traced."""
    global _VERSION
    if _VERSION is None:
        try:
            _VERSION = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True,
                check=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ).stdout.strip()
        except Exception:
            _VERSION = ""
    return _VERSION or None


def stake(p: float, odds: float) -> int:
    """The advised stake for one tip, 1-10 tenths of a unit.

    The model decides whether to bet; how much leans on the market too. The
    probability is blended half and half with the bookmaker's (1 / odds)
    before Kelly, so a tip is staked on an edge the model can be partly wrong
    about - otherwise a short price at a confident 81% (Rakhimova, 5 Oct 2026:
    0 aces) draws the biggest stake. Then quarter Kelly, read as 1% of a
    bankroll per tenth, and scaled down above odds of 3.0, where the model's
    tail runs optimistic. Typical tips land at 1-5."""
    p = (p + 1 / odds) / 2
    b = odds - 1
    kelly = (p * odds - 1) / b if b > 0 else 0.0
    quarter = max(kelly, 0.0) / 4 * min(1.0, 3.0 / odds)
    return max(1, min(10, round(quarter * 100)))


def share_stakes(cur, match_key: str):
    """One stake per match, not one per tip.

    Tips on the same match - at any bookmaker - win or lose together with the
    match's length, so together they get no more than the largest of them
    alone, shared in proportion. Placed tips keep the rating they were placed
    at and use up their part of it; only open, unplaced tips are adjusted."""
    cur.execute("""
        SELECT a.source, a.event_id, a.market, a.p_model::float, a.odds::float, a.placed, a.rating
          FROM aces.advice a JOIN aces.event e USING (source, event_id)
         WHERE e.match_key = %s AND a.backed AND a.kickoff > now()""", (match_key,))
    tips = cur.fetchall()
    if not tips:
        return
    own = {(s, e, m): (r if placed and r else stake(p, o)) for s, e, m, p, o, placed, r in tips}
    budget = max(own.values())
    placed_total = sum(own[(s, e, m)] for s, e, m, _p, _o, placed, _r in tips if placed)
    free = [(s, e, m) for s, e, m, _p, _o, placed, _r in tips if not placed]
    room = max(budget - placed_total, len(free))      # at least 1 each
    want = sum(own[k] for k in free)
    for k in free:
        rating = max(1, round(own[k] * min(1.0, room / want))) if want else 1
        cur.execute("""UPDATE aces.advice SET rating = %s
                        WHERE (source, event_id, market) = (%s, %s, %s)
                          AND kickoff > now() AND NOT placed""", (rating, *k))


def record(cur, ev: dict, market: str, advised: bool, line: float = None,
           side: str = None, odds: float = None, p_side: float = None,
           model_mean: float = None):
    """ev: source, event_id, tour, kickoff, league, player ids and names."""
    key = {"source": ev["source"], "event_id": ev["event_id"], "market": market}
    if advised:
        cur.execute("""
            INSERT INTO aces.advice (source, event_id, market, tour, kickoff, league,
                                     player_1_id, player_1, player_2_id, player_2,
                                     line, side, odds, p_model, model_mean, edge, backed,
                                     code_version)
            VALUES (%(source)s, %(event_id)s, %(market)s, %(tour)s, %(kickoff)s, %(league)s,
                    %(player_1_id)s, %(player_1)s, %(player_2_id)s, %(player_2)s,
                    %(line)s, %(side)s, %(odds)s, %(p)s, %(mean)s, %(edge)s, true, %(version)s)
            ON CONFLICT (source, event_id, market) DO UPDATE SET
              kickoff = EXCLUDED.kickoff, line = EXCLUDED.line, side = EXCLUDED.side,
              odds = EXCLUDED.odds, p_model = EXCLUDED.p_model, model_mean = EXCLUDED.model_mean,
              edge = EXCLUDED.edge, backed = true, code_version = EXCLUDED.code_version,
              advised_at = now()
            WHERE aces.advice.kickoff > now() AND NOT aces.advice.placed""",
                    {**key, "tour": ev.get("tour"), "kickoff": ev["kickoff"],
                     "league": ev.get("league"), "player_1_id": ev["player_1_id"],
                     "player_1": ev["name_1"], "player_2_id": ev["player_2_id"],
                     "player_2": ev["name_2"], "line": line, "side": side, "odds": odds,
                     "p": p_side, "mean": model_mean, "edge": p_side * odds - 1,
                     "version": code_version()})
    else:
        # Advised before, not now: keep the last advice, marked withdrawn.
        cur.execute("""
            UPDATE aces.advice SET backed = false, advised_at = now()
             WHERE source = %(source)s AND event_id = %(event_id)s AND market = %(market)s
               AND backed AND kickoff > now() AND NOT placed""", key)
