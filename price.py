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
import os
import re
import sys
import unicodedata

import pandas as pd
import psycopg
from dotenv import load_dotenv

from aces import model

EDGE = 0.05     # expected value to flag a bet

DDL = """
CREATE TABLE IF NOT EXISTS aces.line (
  date        DATE    NOT NULL,
  player_1_id TEXT    NOT NULL,
  player_1    TEXT    NOT NULL,
  player_2_id TEXT    NOT NULL,
  player_2    TEXT    NOT NULL,
  market      TEXT    NOT NULL,   -- aces, aces:1, aces:2, df, df:1, df:2
  line        NUMERIC NOT NULL,
  surface     TEXT    NOT NULL,
  over_odds   NUMERIC,
  under_odds  NUMERIC,
  p_over      NUMERIC NOT NULL,   -- the model's, when the line was priced
  model_mean  NUMERIC NOT NULL,
  bet         TEXT,               -- over | under | NULL: no edge
  actual      INTEGER,            -- filled by --settle
  void        BOOLEAN,            -- retirement or walkover
  priced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (date, player_1_id, player_2_id, market, line)
);
"""


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", s.lower().replace("_", " ").replace("-", " ")).strip()


class Players:
    def __init__(self, conn):
        q = """SELECT id, name, max(played_at) AS last FROM (
                 SELECT player_a_id id, player_a name, played_at FROM aces.match
                 UNION ALL SELECT player_b_id, player_b, played_at FROM aces.match) x
               GROUP BY id, name"""
        self.df = model.frame(conn, q).sort_values("last", ascending=False).drop_duplicates("id")
        self.df["n"] = self.df["name"].map(norm)

    def find(self, token: str) -> tuple[str, str]:
        surname, _, first = token.partition(".")
        s, f = norm(surname), norm(first)
        hits = self.df[self.df.n.str.endswith(" " + s) | (self.df.n == s)]
        if f:
            hits = hits[hits.n.str.startswith(f)]
        if hits.empty:
            raise SystemExit(f"no player matches {token!r}")
        if len(hits) > 1:
            names = ", ".join(hits.name.head(5))
            raise SystemExit(f"{token!r} is ambiguous ({names}); add an initial, e.g. {surname}.X")
        h = hits.iloc[0]
        return h.id, h["name"]


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


def pmf_for(pred, market):
    stat, _, who = market.partition(":")
    key = "ace" if stat == "aces" else "df"
    return getattr(pred, f"{key}_{ {'': 'total', '1': 'a', '2': 'b'}[who] }")


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
        id1, n1 = players.find(r["p1"])
        id2, n2 = players.find(r["p2"])
        t = model._days(pd.Timestamp(r["date"], tz="UTC"))
        pred = model.predict(ratings, id1, id2, r["surface"], t)
        pmf = pmf_for(pred, r["market"])
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
                    "player_2": n2, "p_over": p, "model_mean": mean, "bet": bet})
    if args.dry_run:
        return
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.executemany("""
            INSERT INTO aces.line (date, player_1_id, player_1, player_2_id, player_2, market,
                                   line, surface, over_odds, under_odds, p_over, model_mean, bet)
            VALUES (%(date)s, %(player_1_id)s, %(player_1)s, %(player_2_id)s, %(player_2)s,
                    %(market)s, %(line)s, %(surface)s, %(over_odds)s, %(under_odds)s,
                    %(p_over)s, %(model_mean)s, %(bet)s)
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
    its date - and record the count, or void it if the match did not finish."""
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("""
        WITH m AS (
          SELECT l.date, l.player_1_id, l.player_2_id, l.market, l.line, mt.completed,
                 s1.aces a1, s2.aces a2, s1.double_faults d1, s2.double_faults d2,
                 row_number() OVER (PARTITION BY l.date, l.player_1_id, l.player_2_id,
                                    l.market, l.line ORDER BY abs(mt.played_at::date - l.date)) rn
            FROM aces.line l
            JOIN aces.match mt ON ((l.player_1_id, l.player_2_id) = (mt.player_a_id, mt.player_b_id)
                                 OR (l.player_1_id, l.player_2_id) = (mt.player_b_id, mt.player_a_id))
                              AND mt.played_at::date BETWEEN l.date - 1 AND l.date + 3
            LEFT JOIN aces.serve s1 ON (s1.tour, s1.tournament_id, s1.year, s1.match_id, s1.set_num)
                 = (mt.tour, mt.tournament_id, mt.year, mt.match_id, 0) AND s1.player_id = l.player_1_id
            LEFT JOIN aces.serve s2 ON (s2.tour, s2.tournament_id, s2.year, s2.match_id, s2.set_num)
                 = (mt.tour, mt.tournament_id, mt.year, mt.match_id, 0) AND s2.player_id = l.player_2_id
           WHERE l.actual IS NULL AND l.void IS NOT TRUE)
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
        cur.execute("""
        SELECT count(*) FILTER (WHERE bet IS NOT NULL),
               count(*) FILTER (WHERE bet = 'over'  AND actual > line OR bet = 'under' AND actual < line),
               sum(CASE WHEN bet = 'over'  AND actual > line THEN over_odds - 1
                        WHEN bet = 'under' AND actual < line THEN under_odds - 1
                        WHEN bet IS NOT NULL THEN -1 END)
          FROM aces.line WHERE actual IS NOT NULL""")
        bets, won, profit = cur.fetchone()
    conn.commit()
    print(f"settled {n} lines")
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
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.settle:
            settle(conn)
        elif args.file:
            price(args, conn)
        else:
            ap.error("give a lines file or --settle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
