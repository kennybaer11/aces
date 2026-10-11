#!/usr/bin/env python
"""Walk-forward test of the "most aces" market (Betano 4083: player 1, draw,
player 2) from the model's two ace forecasts.

    python most_aces_bt.py --tour ATP --start 2025-01-01 --every 3

The two counts are taken as independent given the forecasts - the model
shares match length between them, so this slightly understates the draw.
Scored against a baseline that uses each player's own decayed average ace
count (backtest.Baseline) the same way. Prints log loss, Brier, and the
calibration of the favourite's probability.
"""

import argparse
import math
import os
import zlib

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv

from aces import model
from backtest import Baseline, nb_pmf


def three_way(pa, pb):
    """P(a > b), P(a = b), P(a < b) for independent counts."""
    n = min(len(pa), len(pb))
    pa, pb = pa[:n], pb[:n]
    cb = np.cumsum(pb)
    win = float((pa[1:] * cb[:-1]).sum())
    draw = float((pa * pb).sum())
    return win, draw, max(0.0, 1 - win - draw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tour", default="WTA", choices=("WTA", "ATP"))
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--every", type=int, default=3)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        df = model.load(conn, args.tour)
    start = pd.Timestamp(args.start, tz="UTC")
    base, r, rows = Baseline(), model.Ratings(), []
    for row in df.itertuples(index=False):
        row = row._asdict()
        t = model._days(row["played_at"])
        if row["played_at"] >= start and zlib.crc32((row["match_id"] + row["tournament_id"]).encode()) % args.every == 0:
            pred = model.predict(r, row["player_a_id"], row["player_b_id"], row["surface"], t,
                                 None, args.tour, row["level"], row["draw"], model.venue_key(row))
            m = three_way(pred.ace_a, pred.ace_b)
            ba = base.mean("ace", row["player_a_id"], t, 4.5)
            bb = base.mean("ace", row["player_b_id"], t, 4.5)
            b = three_way(nb_pmf(ba, model.SHAPE["ace"]), nb_pmf(bb, model.SHAPE["ace"]))
            ya, yb = int(row["aces_a"]), int(row["aces_b"])
            y = 0 if ya > yb else 1 if ya == yb else 2
            rows.append({"played_at": row["played_at"], "y": y, "pa": m[0], "pd": m[1], "pb": m[2],
                         "ba": b[0], "bd": b[1], "bb": b[2]})
        r.update(row, t)
        base.add("ace", row["player_a_id"], t, row["aces_a"])
        base.add("ace", row["player_b_id"], t, row["aces_b"])
    res = pd.DataFrame(rows)
    if args.out:
        res.to_csv(args.out, index=False)
    P, B = res[["pa", "pd", "pb"]].to_numpy(), res[["ba", "bd", "bb"]].to_numpy()
    Y = np.eye(3)[res.y.to_numpy()]
    ll = lambda Q: -np.mean(np.log(np.clip((Q * Y).sum(1), 1e-9, 1)))
    br = lambda Q: np.mean(((Q - Y) ** 2).sum(1))
    print(f"{args.tour}: {len(res)} matches from {args.start}")
    print(f"  log loss  model {ll(P):.4f}  baseline {ll(B):.4f}")
    print(f"  Brier     model {br(P):.4f}  baseline {br(B):.4f}")
    print(f"  draw      predicted {res.pd.mean():.3f}  actual {(res.y == 1).mean():.3f}")
    # Calibration on each outcome's probability, all three pooled.
    flat = pd.DataFrame({"p": P.ravel(), "hit": Y.ravel()})
    for lo, hi in ((0, .1), (.1, .2), (.2, .3), (.3, .4), (.4, .5), (.5, .6), (.6, .7), (.7, .8), (.8, 1)):
        g = flat[(flat.p > lo) & (flat.p <= hi)]
        if len(g):
            print(f"  ({lo:.1f}, {hi:.1f}]  n={len(g):>6}  predicted {g.p.mean():.3f}  actual {g.hit.mean():.3f}")


if __name__ == "__main__":
    main()
