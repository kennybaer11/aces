-- aces: serve statistics from the WTA tour, for pricing ace and double-fault
-- lines.
--
-- Everything lives in its own schema, so this shares the Neon database with
-- posession and emptynet without colliding.

CREATE SCHEMA IF NOT EXISTS aces;

CREATE TABLE IF NOT EXISTS aces.tournament (
  tour          TEXT    NOT NULL,   -- WTA
  tournament_id TEXT    NOT NULL,   -- the tour's own id; stable across years
  year          INTEGER NOT NULL,
  name          TEXT    NOT NULL,
  level         TEXT,               -- Grand Slam, WTA 1000, WTA 500, ...
  surface       TEXT,               -- Hard, Clay, Grass, Carpet
  indoor        BOOLEAN,
  city          TEXT,
  country       TEXT,
  start_date    DATE,
  end_date      DATE,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tour, tournament_id, year)
);

-- One row per singles match. Player A and B are the feed's own order, which is
-- the draw position, not winner and loser - so nothing here leaks the result
-- into which side a player is on.
CREATE TABLE IF NOT EXISTS aces.match (
  tour          TEXT    NOT NULL,
  tournament_id TEXT    NOT NULL,
  year          INTEGER NOT NULL,
  match_id      TEXT    NOT NULL,   -- unique within a tournament and year
  draw          TEXT,               -- M main draw, Q qualifying
  round         TEXT,
  played_at     TIMESTAMPTZ,        -- when the feed last stamped the match
  player_a_id   TEXT    NOT NULL,
  player_a      TEXT    NOT NULL,
  player_b_id   TEXT    NOT NULL,
  player_b      TEXT    NOT NULL,
  winner        TEXT,               -- a | b
  score         TEXT,               -- from A's side: '6-4,3-6,7-6(5)'
  sets_played   INTEGER,
  completed     BOOLEAN NOT NULL,   -- false for a walkover or a retirement
  duration_s    INTEGER,
  PRIMARY KEY (tour, tournament_id, year, match_id),
  FOREIGN KEY (tour, tournament_id, year)
    REFERENCES aces.tournament (tour, tournament_id, year) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS match_player_a_idx ON aces.match (player_a_id);
CREATE INDEX IF NOT EXISTS match_player_b_idx ON aces.match (player_b_id);

-- Serve statistics for one player in one match. set_num 0 is the whole match,
-- 1..n the individual sets. One row per player, so "aces by X" is a plain
-- filter rather than an a/b case expression.
CREATE TABLE IF NOT EXISTS aces.serve (
  tour            TEXT    NOT NULL,
  tournament_id   TEXT    NOT NULL,
  year            INTEGER NOT NULL,
  match_id        TEXT    NOT NULL,
  set_num         INTEGER NOT NULL,
  side            TEXT    NOT NULL,  -- a | b
  player_id       TEXT    NOT NULL,
  aces            INTEGER,
  double_faults   INTEGER,
  serve_points    INTEGER,           -- points played on own serve
  serve_points_won INTEGER,
  first_in        INTEGER,           -- first serves in
  first_won       INTEGER,
  service_games   INTEGER,
  bp_faced        INTEGER,           -- break points against this server
  bp_saved        INTEGER,
  PRIMARY KEY (tour, tournament_id, year, match_id, set_num, side),
  FOREIGN KEY (tour, tournament_id, year, match_id)
    REFERENCES aces.match (tour, tournament_id, year, match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS serve_player_idx ON aces.serve (player_id);

-- A bookmaker line and what the model made of it when it was priced. Written
-- by price.py (lines typed in by hand) and odds.py (collected from a bookmaker),
-- settled by price.py --settle. Never repriced once its match has started or
-- settled: this is the record, not a backtest.
CREATE TABLE IF NOT EXISTS aces.line (
  date        DATE    NOT NULL,
  player_1_id TEXT    NOT NULL,
  player_1    TEXT    NOT NULL,
  player_2_id TEXT    NOT NULL,
  player_2    TEXT    NOT NULL,
  market      TEXT    NOT NULL,   -- aces, aces:1, aces:2, df, df:1, df:2
  line        NUMERIC NOT NULL,   -- "10+" is stored as 9.5
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
ALTER TABLE aces.line ADD COLUMN IF NOT EXISTS source   TEXT;         -- manual | betano
ALTER TABLE aces.line ADD COLUMN IF NOT EXISTS event_id TEXT;         -- the bookmaker's
ALTER TABLE aces.line ADD COLUMN IF NOT EXISTS kickoff  TIMESTAMPTZ;  -- frozen from then on

-- A bookmaker's match, as it listed it, with the players resolved to tour ids.
CREATE TABLE IF NOT EXISTS aces.event (
  source      TEXT    NOT NULL,   -- betano
  event_id    TEXT    NOT NULL,
  kickoff     TIMESTAMPTZ NOT NULL,
  league      TEXT,
  name_1      TEXT    NOT NULL,   -- as the bookmaker spells them
  name_2      TEXT    NOT NULL,
  player_1_id TEXT,               -- NULL when no tour player matched
  player_2_id TEXT,
  surface     TEXT,
  url         TEXT,
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, event_id)
);

-- Every price seen, every time it was collected: the ladder of "N or more"
-- rungs per market. Kept whole so the model can later be scored against the
-- bookmaker on every rung, not only the ones it advised.
CREATE TABLE IF NOT EXISTS aces.odds (
  source      TEXT    NOT NULL,
  event_id    TEXT    NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL,
  market      TEXT    NOT NULL,   -- aces, aces:1, aces:2, df, df:1, df:2
  at_least    INTEGER NOT NULL,   -- the rung: "10+" is 10
  price       NUMERIC NOT NULL,
  PRIMARY KEY (source, event_id, fetched_at, market, at_least),
  FOREIGN KEY (source, event_id) REFERENCES aces.event (source, event_id) ON DELETE CASCADE
);

-- Two-sided bookmakers (Chance.cz) quote an under as well as an over; Betano
-- quotes overs only. A rung of "at least N" is the over side of the line
-- N - 0.5, so one table holds both, told apart by side.
ALTER TABLE aces.odds ADD COLUMN IF NOT EXISTS side TEXT NOT NULL DEFAULT 'over';  -- over | under
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a
                   ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                  WHERE c.conrelid = 'aces.odds'::regclass AND c.contype = 'p' AND a.attname = 'side') THEN
    ALTER TABLE aces.odds DROP CONSTRAINT odds_pkey;
    ALTER TABLE aces.odds ADD PRIMARY KEY (source, event_id, fetched_at, market, at_least, side);
  END IF;
END $$;
ALTER TABLE aces.event ADD COLUMN IF NOT EXISTS tour TEXT;   -- WTA | ATP

-- For comparing bookmakers: the model's P(count >= at_least) beside every
-- price, written when the rung is priced, and a key that is the same for one
-- match at every bookmaker - the two tour player ids when known, otherwise the
-- two players' name words, with the UTC date either way.
ALTER TABLE aces.odds  ADD COLUMN IF NOT EXISTS p_model NUMERIC;
ALTER TABLE aces.event ADD COLUMN IF NOT EXISTS match_key TEXT;
CREATE INDEX IF NOT EXISTS event_match_key_idx ON aces.event (match_key);

-- Ticked on betken.cz when the admin has actually placed the bet. A placed line
-- is frozen: the collectors neither reprice it nor delete it when the advised
-- rung moves, so it keeps the price that was taken. The side is the one shown
-- on the page: the advised side, or the over (under if only an under is quoted).
ALTER TABLE aces.line ADD COLUMN IF NOT EXISTS placed    BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE aces.line ADD COLUMN IF NOT EXISTS placed_at TIMESTAMPTZ;

-- The advice the site gave, as it stood when the match started - the tennis
-- twin of posession's pl_advice. One row per bookmaker match and market that
-- was ever advised. Rewritten on every collection while the match is still to
-- start; a tip withdrawn before then stays, with backed = false, so the record
-- shows it was given and taken back. From kickoff the row is locked by the
-- trigger below: only the result (actual, void) and the admin's placed tick
-- can still change. aces.line is the working table every priced line lives in;
-- this is the record.
CREATE TABLE IF NOT EXISTS aces.advice (
  source           TEXT    NOT NULL,     -- betano | chance
  event_id         TEXT    NOT NULL,
  market           TEXT    NOT NULL,     -- aces, aces:1, aces:2
  tour             TEXT,
  kickoff          TIMESTAMPTZ NOT NULL,
  league           TEXT,
  player_1_id      TEXT    NOT NULL,
  player_1         TEXT    NOT NULL,
  player_2_id      TEXT    NOT NULL,
  player_2         TEXT    NOT NULL,
  line             NUMERIC NOT NULL,     -- "8+" is 7.5
  side             TEXT    NOT NULL,     -- over | under
  odds             NUMERIC NOT NULL,
  p_model          NUMERIC NOT NULL,     -- the model's chance of the side
  model_mean       NUMERIC,
  edge             NUMERIC NOT NULL,     -- p_model * odds - 1
  backed           BOOLEAN NOT NULL,     -- false: advised, then withdrawn before kickoff
  code_version     TEXT,
  first_advised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  advised_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  actual           INTEGER,
  void             BOOLEAN,
  settled_at       TIMESTAMPTZ,
  placed           BOOLEAN NOT NULL DEFAULT false,
  placed_at        TIMESTAMPTZ,
  PRIMARY KEY (source, event_id, market)
);
CREATE INDEX IF NOT EXISTS advice_kickoff_idx ON aces.advice (kickoff);

CREATE OR REPLACE FUNCTION aces.advice_locked() RETURNS trigger AS $$
BEGIN
  IF OLD.kickoff <= now() AND
     (NEW.line, NEW.side, NEW.odds, NEW.p_model, NEW.edge, NEW.backed, NEW.kickoff)
       IS DISTINCT FROM
     (OLD.line, OLD.side, OLD.odds, OLD.p_model, OLD.edge, OLD.backed, OLD.kickoff) THEN
    RAISE EXCEPTION 'aces.advice %/%/% is locked: the match started at %',
      OLD.source, OLD.event_id, OLD.market, OLD.kickoff;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS advice_locked ON aces.advice;
CREATE TRIGGER advice_locked BEFORE UPDATE ON aces.advice
  FOR EACH ROW EXECUTE FUNCTION aces.advice_locked();
CREATE OR REPLACE RULE advice_no_delete AS ON DELETE TO aces.advice
  WHERE OLD.kickoff <= now() DO INSTEAD NOTHING;
