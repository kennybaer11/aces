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
