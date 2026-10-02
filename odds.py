#!/usr/bin/env python
"""Collect Betano's WTA ace and double-fault ladders and price them.

    python odds.py              collect, store, price - no advice
    python odds.py --advise     ... and advise bets that clear EDGE
    python odds.py --dry-run    collect and price, print only

Every run stores every rung of every ladder in aces.odds, timestamped, and
writes one line per match and market to aces.line - the rung with the most
value if it clears EDGE and --advise is given, otherwise the rung nearest
even money, unadvised. Advice stays off until the backtest says the model is
calibrated: until then the lines are a record of model against bookmaker,
not tips.
A match that has started is never touched again: its last pre-match line is
the record. Run collect.py first so the ratings include yesterday.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from aces import betano, db, events, model
from aces.players import Players

log = logging.getLogger("odds")

EDGE = 0.10          # expected value to advise a bet; tails run ~1 point optimistic (tail_check)
# Only aces are advised. Walk-forward on 2025-26 the model beats each player's
# own recent average on aces (log-loss 1.82 v 1.99) but only ties it on double
# faults, so a DF "edge" is not one. DF lines are still priced and stored.
ADVISE_MARKETS = {"aces", "aces:1", "aces:2"}
MIN_HISTORY = 10     # matches of serve stats each player needs before we advise
SLAM_SURFACE = {"Australian Open": "Hard", "Roland Garros": "Clay",
                "Wimbledon": "Grass", "US Open": "Hard"}


def surface_for(conn, ev: dict) -> str:
    """The surface of the event the players are in this week.

    Betano does not say, so it is read from the tour's own data: the
    tournament either player last played in the fortnight before kickoff.
    """
    for slam, surf in SLAM_SURFACE.items():
        if ev["league"].startswith(slam):
            return surf
    ids = [i for i in (ev["player_1_id"], ev["player_2_id"]) if i]
    if ids:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.surface FROM aces.match m
                JOIN aces.tournament t USING (tour, tournament_id, year)
                WHERE (m.player_a_id = ANY(%s) OR m.player_b_id = ANY(%s))
                  AND m.played_at BETWEEN %s AND %s
                ORDER BY m.played_at DESC LIMIT 1""",
                        (ids, ids, ev["kickoff"] - timedelta(days=14), ev["kickoff"]))
            row = cur.fetchone()
        if row and row[0]:
            return "Hard" if row[0] == "Carpet" else row[0]
    return "Hard"


MIN_P = 0.30         # never advise a rung the model gives less than this


def best_line(pmf, rungs: list[tuple[int, float]], can_advise: bool):
    """(at_least, price, p, ev, advised) for the rung this market is shown at.

    Advised: of the rungs the model gives at least MIN_P and at least EDGE of
    value, the one with the best Kelly growth (model.kelly_growth) - not the
    one with the most EV, which on Betano's ladders is nearly always the top
    rung, the long shot where the model's tail is least reliable. Unadvised:
    the rung nearest even money."""
    priced = []
    for n, price in rungs:
        p = model.Prediction.over(pmf, n - 0.5)
        priced.append((n, price, p, p * price - 1))
    ok = [r for r in priced if r[2] >= MIN_P and r[3] >= EDGE]
    if can_advise and ok:
        top = max(ok, key=lambda r: model.kelly_growth(r[2], r[1]))
        return (*top, True)
    even = min(priced, key=lambda r: abs(r[1] - 2.0))
    return (*even, False)


# Tours whose model has passed a walk-forward backtest. Lines of other tours
# are priced and stored - for the comparison page - but never advised.
VALIDATED_TOURS = {"WTA", "ATP"}   # ATP passed its walk-forward backtest 2024-26; on since 2026-10-02


def run(conn, dry_run: bool, advise: bool):
    tours = {}
    for tour in ("WTA", "ATP"):
        hist = model.load(conn, tour)
        if hist.empty:
            continue
        tours[tour] = {
            "players": Players(conn, tour), "ratings": model.walk(hist),
            "seen": hist.player_a_id.value_counts().add(hist.player_b_id.value_counts(), fill_value=0)}
        log.info("%s ratings from %d matches up to %s", tour, len(hist), hist.played_at.max())

    fetched_at = datetime.now(timezone.utc)
    counts = {"events": 0, "unmatched": 0, "priced": 0, "advised": 0}
    for league in betano.leagues():
        T = tours.get(league["tour"])
        for ev in betano.league_events(league):
            counts["events"] += 1
            P = T["players"] if T else None
            ev["player_1_id"] = P.by_full_name(ev["name_1"]) if P else None
            ev["player_2_id"] = P.by_full_name(ev["name_2"]) if P else None
            ev["surface"] = surface_for(conn, ev)
            ev["match_key"] = events.match_key(ev["kickoff"], ev["name_1"], ev["name_2"],
                                               ev["player_1_id"], ev["player_2_id"])
            ladders = betano.event_ladders(ev)
            known = bool(T and ev["player_1_id"] and ev["player_2_id"])
            pred = None
            if known and ladders:
                pred = model.predict(T["ratings"], ev["player_1_id"], ev["player_2_id"], ev["surface"],
                                     model._days(ev["kickoff"]), tour=ev["tour"], level=ev["level"])
            if not dry_run:
                _save_event(conn, ev, fetched_at, ladders, pred)
            if not known:
                counts["unmatched"] += 1
                log.info("no %s history for %s / %s", ev["tour"], ev["name_1"], ev["name_2"])
                continue
            if not ladders:
                continue
            enough = (advise and ev["tour"] in VALIDATED_TOURS
                      and min(T["seen"].get(ev["player_1_id"], 0),
                              T["seen"].get(ev["player_2_id"], 0)) >= MIN_HISTORY)
            rows = []
            for market, rungs in ladders.items():
                pmf = pred.pmf(market)
                n, price, p, gain, advised = best_line(pmf, rungs,
                                                       enough and market in ADVISE_MARKETS)
                rows.append({
                    "date": ev["kickoff"].date(), "player_1_id": ev["player_1_id"],
                    "player_1": ev["name_1"], "player_2_id": ev["player_2_id"],
                    "player_2": ev["name_2"], "market": market, "line": n - 0.5,
                    "surface": ev["surface"], "over_odds": price, "under_odds": None,
                    "p_over": p, "model_mean": float((pmf * range(len(pmf))).sum()),
                    "bet": "over" if advised else None, "source": "betano",
                    "event_id": ev["event_id"], "kickoff": ev["kickoff"]})
                counts["priced"] += 1
                counts["advised"] += advised
                if advised or dry_run:
                    log.info("%s %s v %s  %-6s %d+ @ %.2f  model %.0f%%  EV %+.0f%%%s",
                             ev["kickoff"].strftime("%d.%m %H:%M"), ev["name_1"], ev["name_2"],
                             market, n, price, 100 * p, 100 * gain, "  BET" if advised else "")
            if not dry_run:
                _save_lines(conn, rows)
                conn.commit()
    log.info("done: %s", counts)
    return counts


def _save_event(conn, ev, fetched_at, ladders, pred):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO aces.event (source, event_id, kickoff, league, name_1, name_2,
                                    player_1_id, player_2_id, surface, url, tour, match_key)
            VALUES ('betano', %(event_id)s, %(kickoff)s, %(league)s, %(name_1)s, %(name_2)s,
                    %(player_1_id)s, %(player_2_id)s, %(surface)s, %(url)s, %(tour)s, %(match_key)s)
            ON CONFLICT (source, event_id) DO UPDATE SET kickoff=EXCLUDED.kickoff,
              player_1_id=EXCLUDED.player_1_id, player_2_id=EXCLUDED.player_2_id,
              surface=EXCLUDED.surface, tour=EXCLUDED.tour, match_key=EXCLUDED.match_key,
              last_seen=now()""", ev)
        cur.executemany("""
            INSERT INTO aces.odds (source, event_id, fetched_at, market, at_least, price, side, p_model)
            VALUES ('betano', %s, %s, %s, %s, %s, 'over', %s) ON CONFLICT DO NOTHING""",
                        [(ev["event_id"], fetched_at, m, n, p,
                          model.Prediction.over(pred.pmf(m), n - 0.5) if pred else None)
                         for m, rungs in ladders.items() for n, p in rungs])


def _save_lines(conn, rows):
    with conn.cursor() as cur:
        for r in rows:
            # The rung shown can move between runs; the old one goes, but only
            # while the match is still to start. After kickoff nothing changes.
            cur.execute("""DELETE FROM aces.line
                            WHERE source = 'betano' AND event_id = %(event_id)s
                              AND market = %(market)s AND line <> %(line)s
                              AND kickoff > now() AND actual IS NULL AND NOT placed""", r)
            cur.execute("""
                INSERT INTO aces.line (date, player_1_id, player_1, player_2_id, player_2, market,
                                       line, surface, over_odds, under_odds, p_over, model_mean,
                                       bet, source, event_id, kickoff)
                VALUES (%(date)s, %(player_1_id)s, %(player_1)s, %(player_2_id)s, %(player_2)s,
                        %(market)s, %(line)s, %(surface)s, %(over_odds)s, %(under_odds)s,
                        %(p_over)s, %(model_mean)s, %(bet)s, %(source)s, %(event_id)s, %(kickoff)s)
                ON CONFLICT (date, player_1_id, player_2_id, market, line) DO UPDATE SET
                  over_odds=EXCLUDED.over_odds, p_over=EXCLUDED.p_over,
                  model_mean=EXCLUDED.model_mean, bet=EXCLUDED.bet, surface=EXCLUDED.surface,
                  kickoff=EXCLUDED.kickoff, priced_at=now()
                WHERE aces.line.actual IS NULL AND aces.line.void IS NOT TRUE AND NOT aces.line.placed
                  AND (aces.line.kickoff IS NULL OR aces.line.kickoff > now())""", r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--advise", action="store_true", help="mark bets that clear EDGE")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    load_dotenv()
    conn = db.connect()
    try:
        run(conn, args.dry_run, args.advise)
    except betano.Blocked as e:
        log.error("Betano refused the request (%s). Stopping - not retrying past it.", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
