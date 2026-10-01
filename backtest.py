#!/usr/bin/env python
"""Walk-forward test: price every match from --start on using only what came
before it, then score the prices against what happened.

    python backtest.py --start 2024-01-01
    python backtest.py --start 2025-01-01 --shape-ace 4 --shape-df 6

Scored per market (player aces, total aces, player DFs, total DFs):
  logloss   minus the mean log probability of the actual count (lower wins)
  mae       of the predicted mean
  calib     for a line at the nearest .5 below the predicted median - the
            line a bookmaker would hang - predicted P(over) against the share
            that went over, in bins
The baseline is what a casual bettor would use: each player's own recent
average, as a negative binomial, ignoring the opponent and match length.
"""

import argparse
import math
import os
from collections import defaultdict

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv
from scipy import stats

from aces import model

MARKETS = {
    "ace_player": ("ace", "player"),
    "ace_total": ("ace", "total"),
    "df_player": ("df", "player"),
    "df_total": ("df", "total"),
}


class Baseline:
    """Each player's decayed average count per match."""

    def __init__(self):
        self.sum = defaultdict(float)
        self.n = defaultdict(float)
        self.when = {}

    def _decay(self, key, t):
        last = self.when.get(key, t)
        f = 0.5 ** ((t - last) / model.HALF_LIFE_DAYS)
        self.sum[key] *= f
        self.n[key] *= f
        self.when[key] = t

    def mean(self, stat, player, t, default):
        k = (stat, player)
        self._decay(k, t)
        return (self.sum[k] + 3 * default) / (self.n[k] + 3)

    def add(self, stat, player, t, x):
        k = (stat, player)
        self._decay(k, t)
        self.sum[k] += x
        self.n[k] += 1


def nb_pmf(mu, shape):
    k = np.arange(model.KMAX + 1)
    return stats.nbinom.pmf(k, shape, shape / (shape + max(mu, 1e-9)))


def median_line(pmf):
    c = np.cumsum(pmf)
    return float(np.searchsorted(c, 0.5)) - 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--shape-ace", type=float, default=model.SHAPE["ace"])
    ap.add_argument("--shape-df", type=float, default=model.SHAPE["df"])
    ap.add_argument("--out", default=None, help="write per-prediction rows to this CSV")
    args = ap.parse_args()
    load_dotenv()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        df = model.load(conn)
    start = pd.Timestamp(args.start, tz="UTC")
    print(f"{len(df)} completed matches with stats, {(df.played_at >= start).sum()} from {args.start}")

    shape = {"ace": args.shape_ace, "df": args.shape_df}
    base = Baseline()
    league = {"ace": 4.5, "df": 3.5}
    rows = []

    def score(row, pred, ratings):
        t = model._days(row["played_at"])
        for stat in ("ace", "df"):
            col = "aces" if stat == "ace" else "df"
            ya, yb = int(row[f"{col}_a"]), int(row[f"{col}_b"])
            ba = base.mean(stat, row["player_a_id"], t, league[stat])
            bb = base.mean(stat, row["player_b_id"], t, league[stat])
            for who, y, pmf, bmu in (("a", ya, getattr(pred, f"{stat}_a"), ba),
                                     ("b", yb, getattr(pred, f"{stat}_b"), bb)):
                rows.append(_row(row, f"{stat}_player", y, pmf, nb_pmf(bmu, shape[stat])))
            tot = getattr(pred, f"{stat}_total")
            # Baseline total: the two baseline means as one negative binomial.
            rows.append(_row(row, f"{stat}_total", ya + yb, tot, nb_pmf(ba + bb, shape[stat])))

    def _row(row, market, y, pmf, bpmf):
        y = min(y, model.KMAX)
        line = median_line(pmf)
        return {
            "played_at": row["played_at"], "level": row["level"], "surface": row["surface"],
            "market": market, "y": y,
            "mean": float((np.arange(len(pmf)) * pmf).sum()),
            "base_mean": float((np.arange(len(bpmf)) * bpmf).sum()),
            "ll": math.log(max(pmf[y], 1e-12)),
            "base_ll": math.log(max(bpmf[y], 1e-12)),
            "line": line,
            "p_over": float(pmf[int(line + 0.5):].sum()),
            "base_p_over": float(bpmf[int(line + 0.5):].sum()),
            "over": int(y > line),
        }

    def learn_baseline(row):
        t = model._days(row["played_at"])
        for stat, col in (("ace", "aces"), ("df", "df")):
            base.add(stat, row["player_a_id"], t, row[f"{col}_a"])
            base.add(stat, row["player_b_id"], t, row[f"{col}_b"])

    r = model.Ratings()
    for row in df.itertuples(index=False):
        row = row._asdict()
        t = model._days(row["played_at"])
        if row["played_at"] >= start:
            score(row, model.predict(r, row["player_a_id"], row["player_b_id"],
                                     row["surface"], t, shape), r)
        r.update(row, t)
        learn_baseline(row)

    res = pd.DataFrame(rows)
    if args.out:
        res.to_csv(args.out, index=False)
    report(res)


def report(res: pd.DataFrame):
    print()
    print(f"{'market':<12}{'n':>7}{'logloss':>10}{'base':>8}{'mae':>8}{'base':>8}"
          f"{'P(over)':>9}{'over%':>7}{'brier':>8}{'base':>8}")
    for m, g in res.groupby("market"):
        print(f"{m:<12}{len(g):>7}{-g.ll.mean():>10.4f}{-g.base_ll.mean():>8.4f}"
              f"{(g['mean'] - g.y).abs().mean():>8.3f}{(g.base_mean - g.y).abs().mean():>8.3f}"
              f"{g.p_over.mean():>9.3f}{g.over.mean():>7.3f}"
              f"{((g.p_over - g.over) ** 2).mean():>8.4f}{((g.base_p_over - g.over) ** 2).mean():>8.4f}")
    print("\nCalibration of P(over) at the median line, all markets:")
    bins = pd.cut(res.p_over, [0, .3, .4, .45, .5, .55, .6, .7, 1])
    for b, g in res.groupby(bins, observed=True):
        print(f"  {str(b):<14} n={len(g):>6}  predicted {g.p_over.mean():.3f}  actual {g.over.mean():.3f}")


if __name__ == "__main__":
    main()
