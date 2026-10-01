"""Postgres writes. Every table is in the `aces` schema."""

import os
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def connect():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False)
    with conn.cursor() as cur:
        cur.execute(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def known_match_ids(conn, tour: str, tournament_id: str, year: int) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("""SELECT match_id FROM aces.match m
                        WHERE tour=%s AND tournament_id=%s AND year=%s
                          AND EXISTS (SELECT 1 FROM aces.serve s
                                       WHERE (s.tour, s.tournament_id, s.year, s.match_id)
                                           = (m.tour, m.tournament_id, m.year, m.match_id))""",
                    (tour, tournament_id, year))
        return {r[0] for r in cur.fetchall()}


def save_tournament(conn, t: dict):
    cols = ["tour", "tournament_id", "year", "name", "level", "surface", "indoor",
            "city", "country", "start_date", "end_date"]
    _upsert(conn, "tournament", cols, [t], ["tour", "tournament_id", "year"])


MATCH_COLS = ["tour", "tournament_id", "year", "match_id", "draw", "round", "played_at",
              "player_a_id", "player_a", "player_b_id", "player_b", "winner", "score",
              "sets_played", "completed", "duration_s"]
SERVE_COLS = ["tour", "tournament_id", "year", "match_id", "set_num", "side", "player_id",
              "aces", "double_faults", "serve_points", "serve_points_won", "first_in",
              "first_won", "service_games", "bp_faced", "bp_saved"]


def save_match(conn, m: dict, serve: list[dict]):
    _upsert(conn, "match", MATCH_COLS, [m], ["tour", "tournament_id", "year", "match_id"])
    key = {k: m[k] for k in ("tour", "tournament_id", "year", "match_id")}
    _upsert(conn, "serve", SERVE_COLS, [{**key, **r} for r in serve],
            ["tour", "tournament_id", "year", "match_id", "set_num", "side"])


def _upsert(conn, table: str, cols: list[str], rows: list[dict], key: list[str]):
    if not rows:
        return
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in key)
    sql = (f"INSERT INTO aces.{table} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}")
    with conn.cursor() as cur:
        cur.executemany(sql, [[r.get(c) for c in cols] for r in rows])
