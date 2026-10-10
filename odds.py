#!/usr/bin/env python
"""Collect Betano's ace and double-fault ladders and over/under lines, and price them.

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
import math
import logging
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from aces import advice, betano, db, events, model, venues
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
# Never advise above this either. An edge that large against a bookmaker
# almost always means the model is missing something - on 3 Oct 2026 Kraus's
# last ten matches averaged 0.9 aces against the model's 2.0, and Betano's
# 6.20 for her 3+ was right where the model's "+98%" was not.
MAX_EDGE = 0.50


def best_bet(pmf, rungs: list[tuple[int, float]], two_way: list, can_advise: bool) -> dict:
    """The line this market is shown at, from the ladder's over rungs and the
    two-way over/under lines together. Advised: of the sides the model gives
    at least MIN_P and EDGE to MAX_EDGE of value, the one with the best Kelly
    growth (model.kelly_growth) - not the most EV, which on a ladder is nearly
    always the top rung, the long shot where the model's tail is least
    reliable - so an under can win. Unadvised: the ladder's rung nearest
    even money, or the two-way line when there is no ladder."""
    cands = []
    for n, price in rungs:
        p = model.Prediction.over(pmf, n - 0.5)
        cands.append({"line": n - 0.5, "side": "over", "price": price, "p": p, "p_over": p,
                      "over_odds": price, "under_odds": None, "ladder": True})
    for ln, over, under in two_way:
        p = model.Prediction.over(pmf, ln)
        for side, price, ps in (("over", over, p), ("under", under, 1 - p)):
            if price and price > 1.01:
                cands.append({"line": ln, "side": side, "price": price, "p": ps, "p_over": p,
                              "over_odds": over, "under_odds": under, "ladder": False})
    for c in cands:
        c["ev"] = c["p"] * c["price"] - 1
    ok = [c for c in cands if c["p"] >= MIN_P and EDGE <= c["ev"] <= MAX_EDGE]
    if can_advise and ok:
        return {**max(ok, key=lambda c: model.kelly_growth(c["p"], c["price"])), "advised": True}
    pool = [c for c in cands if c["ladder"]] or cands
    return {**min(pool, key=lambda c: abs(c["price"] - 2.0)), "advised": False}


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
            "players": Players(conn, tour), "ratings": model.walk(hist), "venues": venues.Venues(conn, tour),
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
            games = ladders.pop("games", [])       # for the length model, not priced
            two_way = ladders.pop("two_way", {})    # over/under lines: the unders
            known = bool(T and ev["player_1_id"] and ev["player_2_id"])
            pred = None
            if known and ladders:
                pred = model.predict(T["ratings"], ev["player_1_id"], ev["player_2_id"], ev["surface"],
                                     model._days(ev["kickoff"]), tour=ev["tour"], level=ev["level"],
                                     venue=T["venues"].key(ev["league"]))
            if not dry_run:
                _save_event(conn, ev, fetched_at, ladders, pred, games, two_way)
            if not known:
                counts["unmatched"] += 1
                log.info("no %s history for %s / %s", ev["tour"], ev["name_1"], ev["name_2"])
                continue
            if not ladders and not two_way:
                continue
            enough = (advise and ev["tour"] in VALIDATED_TOURS
                      and min(T["seen"].get(ev["player_1_id"], 0),
                              T["seen"].get(ev["player_2_id"], 0)) >= MIN_HISTORY)
            rows = []
            for market in sorted(set(ladders) | set(two_way)):
                pmf = pred.pmf(market)
                b = best_bet(pmf, ladders.get(market, []), two_way.get(market, []),
                             enough and market in ADVISE_MARKETS)
                advised = b["advised"]
                rows.append({
                    "date": ev["kickoff"].date(), "player_1_id": ev["player_1_id"],
                    "player_1": ev["name_1"], "player_2_id": ev["player_2_id"],
                    "player_2": ev["name_2"], "market": market, "line": b["line"],
                    "surface": ev["surface"], "over_odds": b["over_odds"],
                    "under_odds": b["under_odds"], "p_over": b["p_over"],
                    "model_mean": float((pmf * range(len(pmf))).sum()),
                    "bet": b["side"] if advised else None, "side": b["side"], "price": b["price"],
                    "p_side": b["p"], "source": "betano",
                    "event_id": ev["event_id"], "kickoff": ev["kickoff"]})
                counts["priced"] += 1
                counts["advised"] += advised
                if advised or dry_run:
                    shown = (f"{b['line'] + 0.5:.0f}+" if b["side"] == "over" and b["ladder"]
                             else f"{b['side']} {b['line']}")
                    log.info("%s %s v %s  %-6s %s @ %.2f  model %.0f%%  EV %+.0f%%%s",
                             ev["kickoff"].strftime("%d.%m %H:%M"), ev["name_1"], ev["name_2"],
                             market, shown, b["price"], 100 * b["p"], 100 * b["ev"],
                             "  BET" if advised else "")
            if not dry_run:
                _save_lines(conn, rows)
                with conn.cursor() as cur:
                    for r in rows:
                        advice.record(cur, {**ev, "source": "betano"}, r["market"], r["bet"] is not None,
                                      line=r["line"], side=r["side"], odds=r["price"],
                                      p_side=r["p_side"], model_mean=r["model_mean"])
                    advice.share_stakes(cur, ev["match_key"])
                conn.commit()
    log.info("done: %s", counts)
    return counts


def _save_event(conn, ev, fetched_at, ladders, pred, games=(), two_way=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO aces.event (source, event_id, kickoff, league, name_1, name_2,
                                    player_1_id, player_2_id, surface, url, tour, match_key)
            VALUES ('betano', %(event_id)s, %(kickoff)s, %(league)s, %(name_1)s, %(name_2)s,
                    %(player_1_id)s, %(player_2_id)s, %(surface)s, %(url)s, %(tour)s, %(match_key)s)
            ON CONFLICT (source, event_id) DO UPDATE SET kickoff=EXCLUDED.kickoff,
              player_1_id=EXCLUDED.player_1_id, player_2_id=EXCLUDED.player_2_id,
              surface=EXCLUDED.surface, tour=EXCLUDED.tour, match_key=EXCLUDED.match_key,
              url=EXCLUDED.url, last_seen=now()""", ev)
        cur.executemany("""
            INSERT INTO aces.odds (source, event_id, fetched_at, market, at_least, price, side, p_model)
            VALUES ('betano', %s, %s, %s, %s, %s, 'over', %s) ON CONFLICT DO NOTHING""",
                        [(ev["event_id"], fetched_at, m, n, p,
                          model.Prediction.over(pred.pmf(m), n - 0.5) if pred else None)
                         for m, rungs in ladders.items() for n, p in rungs])
        # Over/under lines, both sides, as Chance's are stored. An over that
        # duplicates a ladder rung (over 12.5 = 13+) keeps the rung's price.
        cur.executemany("""
            INSERT INTO aces.odds (source, event_id, fetched_at, market, at_least, price, side, p_model)
            VALUES ('betano', %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        [(ev["event_id"], fetched_at, m, math.floor(ln) + 1, price, side,
                          model.Prediction.over(pred.pmf(m), ln) if pred else None)
                         for m, lines in (two_way or {}).items() for ln, over, under in lines
                         for side, price in (("over", over), ("under", under)) if price])
        # Total-games lines, both sides: what the bookmaker expects the
        # match's length to be. Kept for the match-length model.
        cur.executemany("""
            INSERT INTO aces.odds (source, event_id, fetched_at, market, at_least, price, side)
            VALUES ('betano', %s, %s, 'games', %s, %s, %s) ON CONFLICT DO NOTHING""",
                        [(ev["event_id"], fetched_at, math.floor(ln) + 1, price, side)
                         for ln, over, under in games
                         for side, price in (("over", over), ("under", under)) if price])


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
                  over_odds=EXCLUDED.over_odds, under_odds=EXCLUDED.under_odds, p_over=EXCLUDED.p_over,
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
