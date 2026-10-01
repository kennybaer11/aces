"""How many points each player serves in a best-of-three match.

The number of aces a player hits is mostly decided by how many serve points
she gets, and that depends on how evenly the two serves are matched: a
6-1 6-1 rout gives the winner perhaps 30 serve points, a three-set fight 90.
So the count markets are priced off the distribution of serve points, and
that distribution comes from simulating matches point by point.

Only the two serve-point win probabilities matter, so the simulation runs once
on a grid of (pa, pb) and is cached; a match is then looked up, not simulated.
Every set ends in a seven-point tie-break at 6-6, the final set included.
That is the WTA tour rule; Grand Slams play ten points in the final set since
2022, a difference of a point or two in a handful of matches.
"""

from pathlib import Path

import numpy as np

GRID = np.round(np.arange(0.36, 0.801, 0.02), 2)   # serve-point win probability

# Real WTA matches are shorter than independent points make them: on 2024's
# 3,969 matches the simulation gave 80.3 serve points a player against 71.3
# played (2026: 79.9 against 70.6). Wider day-to-day form alone cannot close
# that without making lengths more variable than they are, so the shortfall is
# taken as it is measured - a flat factor, fitted on 2024 and checked on
# 2025-26, which the backtest never saw it fitted on.
LENGTH = 0.888
SIMS = 3000
CACHE = Path(__file__).resolve().parent.parent / "cache" / f"servepoints_{SIMS}.npz"


def _game(p, rng):
    """Points in a service game: (server won, points played)."""
    s = r = 0
    while True:
        if rng.random() < p:
            s += 1
        else:
            r += 1
        if s >= 4 and s - r >= 2:
            return True, s + r
        if r >= 4 and r - s >= 2:
            return False, s + r


def _match(pa, pb, rng):
    """One match; returns (points A served, points B served, sets)."""
    na = nb = 0
    sets_a = sets_b = 0
    a_serves = rng.random() < 0.5
    while sets_a < 2 and sets_b < 2:
        ga = gb = 0
        while True:
            if ga == 6 and gb == 6:
                # Tie-break: the next server serves one, then two each.
                ta = tb = 0
                k = 0
                first = a_serves
                while not ((ta >= 7 or tb >= 7) and abs(ta - tb) >= 2):
                    srv_a = first if k == 0 else (first ^ (((k - 1) // 2) % 2 == 0))
                    if srv_a:
                        na += 1
                        won = rng.random() < pa
                        ta, tb = (ta + 1, tb) if won else (ta, tb + 1)
                    else:
                        nb += 1
                        won = rng.random() < pb
                        ta, tb = (ta, tb + 1) if won else (ta + 1, tb)
                    k += 1
                if ta > tb:
                    ga += 1
                else:
                    gb += 1
                a_serves = not first
                break
            if a_serves:
                held, n = _game(pa, rng)
                na += n
                ga, gb = (ga + 1, gb) if held else (ga, gb + 1)
            else:
                held, n = _game(pb, rng)
                nb += n
                ga, gb = (ga, gb + 1) if held else (ga + 1, gb)
            a_serves = not a_serves
            if (ga >= 6 or gb >= 6) and abs(ga - gb) >= 2:
                break
        if ga > gb:
            sets_a += 1
        else:
            sets_b += 1
    return na, nb, sets_a + sets_b


def _build():
    rng = np.random.default_rng(7)
    k = len(GRID)
    out = np.zeros((k, k, SIMS, 3), dtype=np.int16)
    for i, pa in enumerate(GRID):
        for j, pb in enumerate(GRID):
            if j < i:                      # symmetric: reuse the mirror cell
                out[i, j] = out[j, i][:, [1, 0, 2]]
                continue
            for s in range(SIMS):
                out[i, j, s] = _match(pa, pb, rng)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE, grid=GRID, sims=out)
    return out


_SIMS = None


def serve_points(pa: float, pb: float) -> np.ndarray:
    """SIMS x 3 array of (A's serve points, B's serve points, sets played).

    Serve points are scaled by LENGTH, so they are floats, not counts.
    """
    global _SIMS
    if _SIMS is None:
        _SIMS = np.load(CACHE)["sims"] if CACHE.exists() else _build()
    i = int(np.abs(GRID - np.clip(pa, GRID[0], GRID[-1])).argmin())
    j = int(np.abs(GRID - np.clip(pb, GRID[0], GRID[-1])).argmin())
    out = _SIMS[i, j].astype(float)
    out[:, :2] *= LENGTH
    return out
