#!/usr/bin/env python
"""Price bookmaker ace and double-fault lines with the model.

    python price.py lines.txt           print prices, store the lines
    python price.py lines.txt --dry-run print only
    python price.py --settle            fill in results for stored lines

The ratings are rebuilt from every completed match in the database, so run
collect.py first to bring them up to date. A line is stored once per
(date, players, market, line); re-running updates its prices and prediction
until it settles, and never after.
"""

import argparse
import datetime as dt
import re
import sys

import pandas as pd
from dotenv import load_dotenv

from aces import db, model
from aces.players import Players

EDGE = 0.05     # expected value to flag a bet



def parse(path):
    surface = None
    for n, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].lower() == "surface":
            surface = parts[1].capitalize()
            continue
        if len(parts) < 5:
            raise SystemExit(f"{path}:{n}: expected date player player market line [O..] [U..]")
        if surface is None:
            raise SystemExit(f"{path}:{n}: no 'surface' line above this row")
        date, p1, p2, market, ln = parts[:5]
        odds = {"O": None, "U": None}
        for tok in parts[5:]:
            m = re.fullmatch(r"([OUou])(\d+(?:[.,]\d+)?)", tok)
            if m:
                odds[m.group(1).upper()] = float(m.group(2).replace(",", "."))
            elif tok != "-":
                raise SystemExit(f"{path}:{n}: untagged price {tok!r} - write O{tok} or U{tok}")
        if market not in ("aces", "aces:1", "aces:2", "df", "df:1", "df:2"):
            raise SystemExit(f"{path}:{n}: unknown market {market!r}")
        yield {"date": dt.date.fromisoformat(date), "p1": p1, "p2": p2, "market": market,
               "line": float(ln.replace(",", ".")), "surface": surface,
               "over_odds": odds["O"], "under_odds": odds["U"]}



def price(args, conn):
    players = Players(conn)
    df = model.load(conn)
    ratings = model.walk(df)
    rows = list(parse(args.file))
    out = []
    print(f"ratings from {len(df)} matches up to {df.played_at.max():%Y-%m-%d}\n")
    print(f"{'date':<11}{'match':<36}{'market':<8}{'line':>6}{'mean':>7}{'P(o)':>7}"
          f"{'fair O':>8}{'fair U':>8}{'odds O':>8}{'odds U':>8}{'EV':>7}  bet")
    for r in rows:
        id1, n1 = players.by_surname(r["p1"])
        id2, n2 = players.by_surname(r["p2"])
        t = model._days(pd.Timestamp(r["date"], tz="UTC"))
        pred = model.predict(ratings, id1, id2, r["surface"], t)
        pmf = pred.pmf(r["market"])
        p = model.Prediction.over(pmf, r["line"])
        mean = float((pmf * range(len(pmf))).sum())
        ev_o = p * r["over_odds"] - 1 if r["over_odds"] else None
        ev_u = (1 - p) * r["under_odds"] - 1 if r["under_odds"] else None
        best = max([(e, s) for e, s in ((ev_o, "over"), (ev_u, "under")) if e is not None],
                   default=(None, None))
        bet = best[1] if best[0] is not None and best[0] >= EDGE else None
        fmt = lambda x: f"{x:.2f}" if x else "-"
        print(f"{r['date']!s:<11}{(n1 + ' v ' + n2)[:35]:<36}{r['market']:<8}{r['line']:>6}"
              f"{mean:>7.2f}{p:>7.3f}{1 / max(p, 1e-6):>8.2f}{1 / max(1 - p, 1e-6):>8.2f}"
              f"{fmt(r['over_odds']):>8}{fmt(r['under_odds']):>8}"
              f"{(f'{best[0]:+.1%}' if best[0] is not None else '-'):>7}  {bet or ''}")
        out.append({**r, "player_1_id": id1, "player_1": n1, "player_2_id": id2,
                    "player_2": n2, "p_over": p, "model_mean": mean, "bet": bet,
                    "source": "manual"})
    if args.dry_run:
        return
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO aces.line (date, player_1_id, player_1, player_2_id, player_2, market,
                                   line, surface, over_odds, under_odds, p_over, model_mean, bet,
                                   source)
            VALUES (%(date)s, %(player_1_id)s, %(player_1)s, %(player_2_id)s, %(player_2)s,
                    %(market)s, %(line)s, %(surface)s, %(over_odds)s, %(under_odds)s,
                    %(p_over)s, %(model_mean)s, %(bet)s, %(source)s)
            ON CONFLICT (date, player_1_id, player_2_id, market, line) DO UPDATE SET
              surface=EXCLUDED.surface, over_odds=EXCLUDED.over_odds,
              under_odds=EXCLUDED.under_odds, p_over=EXCLUDED.p_over,
              model_mean=EXCLUDED.model_mean, bet=EXCLUDED.bet, priced_at=now()
            -- A settled line is history: repricing it would rewrite the record.
            WHERE aces.line.actual IS NULL AND aces.line.void IS NOT TRUE""", out)
    conn.commit()
    print(f"\nstored {len(out)} lines")


def settle(conn):
    """Find each open line's match - the same two players within three days of
    its date - and record the count, or void it if the match did not finish.

    Players are compared by tour id, so a Grand Slam match - stored under the
    Slam's own ids - is first mapped through model.aliases.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS alias (id TEXT PRIMARY KEY, tour_id TEXT)")
        cur.execute("TRUNCATE alias")
        cur.executemany("INSERT INTO alias VALUES (%s, %s)", list(model.aliases(conn).items()))
        cur.execute("""
        WITH mm AS (
          SELECT mt.*, COALESCE(xa.tour_id, mt.player_a_id) AS ca,
                       COALESCE(xb.tour_id, mt.player_b_id) AS cb
            FROM aces.match mt
            LEFT JOIN alias xa ON xa.id = mt.player_a_id
            LEFT JOIN alias xb ON xb.id = mt.player_b_id),
        m AS (
          SELECT l.date, l.player_1_id, l.player_2_id, l.market, l.line, mm.completed,
                 CASE WHEN mm.ca = l.player_1_id THEN sa.aces ELSE sb.aces END AS a1,
                 CASE WHEN mm.ca = l.player_1_id THEN sb.aces ELSE sa.aces END AS a2,
                 CASE WHEN mm.ca = l.player_1_id THEN sa.double_faults ELSE sb.double_faults END AS d1,
                 CASE WHEN mm.ca = l.player_1_id THEN sb.double_faults ELSE sa.double_faults END AS d2,
                 row_number() OVER (PARTITION BY l.date, l.player_1_id, l.player_2_id,
                                    l.market, l.line ORDER BY abs(mm.played_at::date - l.date)) rn
            FROM aces.line l
            JOIN mm ON ((l.player_1_id, l.player_2_id) = (mm.ca, mm.cb)
                     OR (l.player_1_id, l.player_2_id) = (mm.cb, mm.ca))
                   AND mm.played_at::date BETWEEN l.date - 1 AND l.date + 3
            LEFT JOIN aces.serve sa ON (sa.tour, sa.tournament_id, sa.year, sa.match_id, sa.set_num, sa.side)
                 = (mm.tour, mm.tournament_id, mm.year, mm.match_id, 0, 'a')
            LEFT JOIN aces.serve sb ON (sb.tour, sb.tournament_id, sb.year, sb.match_id, sb.set_num, sb.side)
                 = (mm.tour, mm.tournament_id, mm.year, mm.match_id, 0, 'b')
           WHERE l.actual IS NULL AND l.void IS NOT TRUE
             AND (l.kickoff IS NULL OR l.kickoff < now()))
        UPDATE aces.line l SET
          void = NOT m.completed,
          actual = CASE WHEN NOT m.completed THEN NULL
                        WHEN l.market = 'aces'   THEN m.a1 + m.a2
                        WHEN l.market = 'aces:1' THEN m.a1
                        WHEN l.market = 'aces:2' THEN m.a2
                        WHEN l.market = 'df'     THEN m.d1 + m.d2
                        WHEN l.market = 'df:1'   THEN m.d1
                        WHEN l.market = 'df:2'   THEN m.d2 END
          FROM m
         WHERE m.rn = 1 AND (l.date, l.player_1_id, l.player_2_id, l.market, l.line)
             = (m.date, m.player_1_id, m.player_2_id, m.market, m.line)""")
        n = cur.rowcount
        # The advice record, matched the same way: its kickoff date stands in
        # for the line's date. Only the result columns are written - the
        # aces.advice_locked trigger refuses anything else after kickoff.
        cur.execute("""
        WITH mm AS (
          SELECT mt.*, COALESCE(xa.tour_id, mt.player_a_id) AS ca,
                       COALESCE(xb.tour_id, mt.player_b_id) AS cb
            FROM aces.match mt
            LEFT JOIN alias xa ON xa.id = mt.player_a_id
            LEFT JOIN alias xb ON xb.id = mt.player_b_id),
        m AS (
          SELECT a.source, a.event_id, a.market, mm.completed,
                 CASE WHEN mm.ca = a.player_1_id THEN sa.aces ELSE sb.aces END AS a1,
                 CASE WHEN mm.ca = a.player_1_id THEN sb.aces ELSE sa.aces END AS a2,
                 row_number() OVER (PARTITION BY a.source, a.event_id, a.market
                                    ORDER BY abs(mm.played_at::date - a.kickoff::date)) rn
            FROM aces.advice a
            JOIN mm ON ((a.player_1_id, a.player_2_id) = (mm.ca, mm.cb)
                     OR (a.player_1_id, a.player_2_id) = (mm.cb, mm.ca))
                   AND mm.played_at::date BETWEEN a.kickoff::date - 1 AND a.kickoff::date + 3
            LEFT JOIN aces.serve sa ON (sa.tour, sa.tournament_id, sa.year, sa.match_id, sa.set_num, sa.side)
                 = (mm.tour, mm.tournament_id, mm.year, mm.match_id, 0, 'a')
            LEFT JOIN aces.serve sb ON (sb.tour, sb.tournament_id, sb.year, sb.match_id, sb.set_num, sb.side)
                 = (mm.tour, mm.tournament_id, mm.year, mm.match_id, 0, 'b')
           WHERE a.actual IS NULL AND a.void IS NOT TRUE AND a.kickoff < now())
        UPDATE aces.advice a SET
          void = NOT m.completed, settled_at = now(),
          actual = CASE WHEN NOT m.completed THEN NULL
                        WHEN a.market = 'aces'   THEN m.a1 + m.a2
                        WHEN a.market = 'aces:1' THEN m.a1
                        WHEN a.market = 'aces:2' THEN m.a2 END
          FROM m
         WHERE m.rn = 1 AND (a.source, a.event_id, a.market) = (m.source, m.event_id, m.market)""")
        n_advice = cur.rowcount
        cur.execute("""
        SELECT count(*) FILTER (WHERE bet IS NOT NULL),
               count(*) FILTER (WHERE bet = 'over'  AND actual > line OR bet = 'under' AND actual < line),
               sum(CASE WHEN bet = 'over'  AND actual > line THEN over_odds - 1
                        WHEN bet = 'under' AND actual < line THEN under_odds - 1
                        WHEN bet IS NOT NULL THEN -1 END)
          FROM aces.line WHERE actual IS NOT NULL""")
        bets, won, profit = cur.fetchone()
    conn.commit()
    print(f"settled {n} lines, {n_advice} advice records")
    if bets:
        print(f"bets {bets}, won {won}, profit {float(profit):+.2f} units "
              f"({float(profit) / bets:+.1%} per bet)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--settle", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    with db.connect() as conn:
        if args.settle:
            settle(conn)
        elif args.file:
            price(args, conn)
        else:
            ap.error("give a lines file or --settle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
