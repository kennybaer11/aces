"""Price ace and double-fault lines.

Three ingredients, each estimated only from matches played before the one
being priced:

  rate     per serve point, how often a player hits an ace (or a double
           fault), and how often she wins the point. A player's figure is a
           time-weighted ratio of what she did to what an average player
           would have done in the same matches - same surface, same
           opponents - shrunk towards average until there is enough of it.
  opponent the same ratio from the returner's side: some players are aced far
           more than others.
  length   how many serve points each player gets, from sim.py, given the two
           serve-point win probabilities.

A match's count is then Poisson given the serve points, with a gamma-
distributed day-to-day multiplier on the rate (a negative binomial), mixed
over the simulated serve points. Totals convolve the two players' counts
within each simulated match, so the length they share is kept.
"""

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

from . import sim

HALF_LIFE_DAYS = 365
PRIOR = {"ace": 120.0, "df": 120.0, "spw": 300.0}   # shrinkage, in pseudo serve points
PRIOR_RET = {"ace": 400.0, "df": 1500.0, "spw": 600.0}
SURFACE_PRIOR = 250.0           # how much surface-specific history it takes to move off overall
SHAPE = {"ace": 6.0, "df": 8.0}  # gamma shape of the day-to-day multiplier; fitted in backtest
KMAX = 60


def frame(conn, q: str) -> pd.DataFrame:
    """A query's rows as a DataFrame, without pandas' SQLAlchemy warning."""
    with conn.cursor() as cur:
        cur.execute(q)
        return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def load(conn) -> pd.DataFrame:
    """One row per player per completed match, oldest first."""
    q = """
    SELECT m.tour, m.tournament_id, m.year, m.match_id, m.draw, m.round,
           t.name AS tournament, t.level, t.surface, t.indoor,
           COALESCE(m.played_at, t.start_date::timestamptz) AS played_at,
           m.player_a_id, m.player_a, m.player_b_id, m.player_b, m.winner,
           m.sets_played, m.completed,
           sa.aces AS aces_a, sa.double_faults AS df_a, sa.serve_points AS sp_a,
           sa.serve_points_won AS spw_a,
           sb.aces AS aces_b, sb.double_faults AS df_b, sb.serve_points AS sp_b,
           sb.serve_points_won AS spw_b
      FROM aces.match m
      JOIN aces.tournament t USING (tour, tournament_id, year)
      JOIN aces.serve sa ON (sa.tour, sa.tournament_id, sa.year, sa.match_id, sa.set_num, sa.side)
                          = (m.tour, m.tournament_id, m.year, m.match_id, 0, 'a')
      JOIN aces.serve sb ON (sb.tour, sb.tournament_id, sb.year, sb.match_id, sb.set_num, sb.side)
                          = (m.tour, m.tournament_id, m.year, m.match_id, 0, 'b')
     WHERE m.completed AND sa.serve_points > 0 AND sb.serve_points > 0
    """
    df = frame(conn, q)
    df["played_at"] = pd.to_datetime(df["played_at"], utc=True)
    df["surface"] = df["surface"].fillna("Hard").replace({"Carpet": "Hard"})
    return df.sort_values(["played_at", "tournament_id", "match_id"]).reset_index(drop=True)


@dataclass
class _Acc:
    """Time-decayed sums: actual and expected events, and opportunities."""
    act: float = 0.0
    exp: float = 0.0
    when: float = 0.0       # days

    def decay_to(self, t: float):
        if t > self.when:
            f = 0.5 ** ((t - self.when) / HALF_LIFE_DAYS)
            self.act *= f
            self.exp *= f
            self.when = t

    def ratio(self, t: float, prior: float, base: float, toward: float = 1.0) -> float:
        """Actual over expected, with `prior` pseudo serve points at `toward`."""
        self.decay_to(t)
        pe = prior * base
        return (self.act + pe * toward) / (self.exp + pe)


@dataclass
class _Base:
    """League-wide rate per surface, also time-decayed."""
    n: float = 0.0
    k: float = 0.0
    when: float = 0.0

    def add(self, t, k, n):
        if t > self.when:
            f = 0.5 ** ((t - self.when) / (HALF_LIFE_DAYS * 2))
            self.n *= f
            self.k *= f
            self.when = t
        self.n += n
        self.k += k

    def rate(self, default):
        return self.k / self.n if self.n > 500 else default


DEFAULTS = {"ace": 0.035, "df": 0.040, "spw": 0.56}
STATS = ("ace", "df", "spw")


@dataclass
class Ratings:
    base: dict = field(default_factory=dict)        # (stat, surface) -> _Base
    srv: dict = field(default_factory=dict)         # (stat, player) -> _Acc
    srv_surf: dict = field(default_factory=dict)    # (stat, player, surface) -> _Acc
    ret: dict = field(default_factory=dict)         # (stat, player) -> _Acc
    matches: dict = field(default_factory=dict)     # player -> decayed match count

    def _b(self, stat, surface):
        return self.base.setdefault((stat, surface), _Base()).rate(DEFAULTS[stat])

    def rate(self, stat, server, returner, surface, t) -> float:
        """Expected per-point rate for `server` serving to `returner`."""
        b = self._b(stat, surface)
        overall = self.srv.setdefault((stat, server), _Acc()).ratio(t, PRIOR[stat], b)
        surf = self.srv_surf.setdefault((stat, server, surface), _Acc()).ratio(
            t, SURFACE_PRIOR, b, toward=overall)
        opp = self.ret.setdefault((stat, returner), _Acc()).ratio(t, PRIOR_RET[stat], b)
        if stat == "spw":
            # Near a base of 0.56 the factors stay within about +-20%, so the
            # product stays a probability; the clip is a guard, not a model.
            return float(np.clip(b * surf * opp, 0.30, 0.85))
        return b * surf * opp

    def update(self, row, t):
        for s, r in (("a", "b"), ("b", "a")):
            server, returner = row[f"player_{s}_id"], row[f"player_{r}_id"]
            n = row[f"sp_{s}"]
            for stat, col in (("ace", f"aces_{s}"), ("df", f"df_{s}"), ("spw", f"spw_{s}")):
                k = row[col]
                if k is None or (isinstance(k, float) and math.isnan(k)):
                    continue
                b = self._b(stat, row["surface"])
                opp = self.ret.setdefault((stat, returner), _Acc()).ratio(t, PRIOR_RET[stat], b)
                srvr = self.srv.setdefault((stat, server), _Acc()).ratio(t, PRIOR[stat], b)
                for acc, expected in (
                        (self.srv[(stat, server)], b * opp * n),
                        (self.srv_surf.setdefault((stat, server, row["surface"]), _Acc()), b * opp * n),
                        (self.ret[(stat, returner)], b * srvr * n)):
                    acc.decay_to(t)
                    acc.act += k
                    acc.exp += expected
            self.matches[server] = self.matches.get(server, 0) + 1
        for stat, cols in (("ace", ("aces_a", "aces_b")), ("df", ("df_a", "df_b")),
                           ("spw", ("spw_a", "spw_b"))):
            k = sum(row[c] for c in cols)
            self.base.setdefault((stat, row["surface"]), _Base()).add(t, k, row["sp_a"] + row["sp_b"])


def _days(ts) -> float:
    return ts.timestamp() / 86400.0


def _nb_pmf(mu: np.ndarray, shape: float) -> np.ndarray:
    """Rows of negative-binomial pmfs over 0..KMAX for each mean in mu."""
    k = np.arange(KMAX + 1)
    p = shape / (shape + np.maximum(mu, 1e-9))
    return stats.nbinom.pmf(k[None, :], shape, p[:, None])


@dataclass
class Prediction:
    ace_a: np.ndarray       # pmf over 0..KMAX
    ace_b: np.ndarray
    ace_total: np.ndarray
    df_a: np.ndarray
    df_b: np.ndarray
    df_total: np.ndarray
    spw_a: float
    spw_b: float
    rate: dict

    @staticmethod
    def over(pmf: np.ndarray, line: float) -> float:
        return float(pmf[int(math.floor(line)) + 1:].sum())

    def mean(self, key: str) -> float:
        pmf = getattr(self, key)
        return float((np.arange(len(pmf)) * pmf).sum())


def predict(r: Ratings, a: str, b: str, surface: str, t: float,
            shape: dict | None = None) -> Prediction:
    shape = shape or SHAPE
    pa = r.rate("spw", a, b, surface, t)
    pb = r.rate("spw", b, a, surface, t)
    pts = sim.serve_points(pa, pb).astype(float)
    out = {}
    rates = {}
    for stat in ("ace", "df"):
        qa = r.rate(stat, a, b, surface, t)
        qb = r.rate(stat, b, a, surface, t)
        rates[stat] = (qa, qb)
        pa_k = _nb_pmf(qa * pts[:, 0], shape[stat])
        pb_k = _nb_pmf(qb * pts[:, 1], shape[stat])
        # Total within each simulated match, then averaged.
        tot = np.zeros(KMAX + 1)
        for i in range(KMAX + 1):
            tot[i:] += (pa_k[:, i:i + 1] * pb_k[:, : KMAX + 1 - i]).sum(0)
        out[f"{stat}_a"] = pa_k.mean(0)
        out[f"{stat}_b"] = pb_k.mean(0)
        out[f"{stat}_total"] = tot / len(pts)
    return Prediction(ace_a=out["ace_a"], ace_b=out["ace_b"], ace_total=out["ace_total"],
                      df_a=out["df_a"], df_b=out["df_b"], df_total=out["df_total"],
                      spw_a=pa, spw_b=pb, rate=rates)


def walk(df: pd.DataFrame, start=None, shape=None, on_predict=None) -> Ratings:
    """Rate every match in order; from `start` on, predict each before learning from it."""
    r = Ratings()
    for row in df.itertuples(index=False):
        row = row._asdict()
        t = _days(row["played_at"])
        if on_predict is not None and start is not None and row["played_at"] >= start:
            on_predict(row, predict(r, row["player_a_id"], row["player_b_id"],
                                    row["surface"], t, shape), r)
        r.update(row, t)
    return r
