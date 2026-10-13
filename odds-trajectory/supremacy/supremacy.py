"""Implied goal supremacy from Asian-handicap quotes, and trajectory features.

Each quote (line, home water, away water) is turned into the expected goal
difference mu (home minus away) at which the de-margined price is fair, under
independent Poisson goals with a fixed expected total. Line changes then no
longer break the series: a deeper line with higher home water maps to about
the same mu as the previous line with lower home water.

Local line sign follows the snapshot convention: + means the home team gives.
"""
from __future__ import annotations

import math

import numpy as np

TOTAL_GOALS = 2.7
MU_GRID = np.round(np.arange(-9.0, 9.0001, 0.005), 4)
LINES = np.round(np.arange(-10.0, 10.0001, 0.25), 2)  # deeper lines are clamped
GRID_MINUTES = (1440, 720, 360, 180, 120, 60, 30, 10)  # as-of points before kickoff
SHAPE_NAMES = ['open→1440'] + [f'{a}→{b}' for a, b in zip(GRID_MINUTES, GRID_MINUTES[1:])]


def _goal_difference(mu, total=TOTAL_GOALS, max_goals=20):
    lh, la = max((total + mu) / 2, 1e-3), max((total - mu) / 2, 1e-3)
    k = np.arange(max_goals + 1)
    ph = np.exp(-lh) * lh ** k / np.array([math.factorial(int(i)) for i in k])
    pa = np.exp(-la) * la ** k / np.array([math.factorial(int(i)) for i in k])
    joint = np.outer(ph, pa)
    diff = np.subtract.outer(k, k)
    return np.array([joint[diff == g].sum() for g in range(-max_goals, max_goals + 1)]), np.arange(-max_goals, max_goals + 1)


def _fair_home_probability(line, prob, goals):
    """De-margined home probability at which a home bet on ``line`` is fair.

    With W the weight of winning outcomes and L of losing ones (pushes and half
    results split), a fair price gives p_home = W / (W + L)."""
    def weights(l):
        margin = goals - l
        return prob[margin > 1e-9].sum(), prob[margin < -1e-9].sum()
    if abs(line * 4) % 2 == 1:  # quarter line: half stake on each neighbour
        (w1, l1), (w2, l2) = weights(line - .25), weights(line + .25)
        w, l = (w1 + w2) / 2, (l1 + l2) / 2
    else:
        w, l = weights(line)
    return w / (w + l)


def _table():
    table = np.empty((len(LINES), len(MU_GRID)))
    for j, mu in enumerate(MU_GRID):
        prob, goals = _goal_difference(mu)
        for i, line in enumerate(LINES):
            table[i, j] = _fair_home_probability(line, prob, goals)
    return table


TABLE = _table()


def supremacy(line, home_water, away_water):
    """Implied mu for one quote (Hong Kong waters)."""
    ih, ia = 1 / (1 + home_water), 1 / (1 + away_water)
    p_home = ih / (ih + ia)
    row = TABLE[min(max(int(round((line - LINES[0]) / .25)), 0), len(LINES) - 1)]
    return float(np.interp(p_home, row, MU_GRID))


def trajectory(record):
    """Features for one match record (research/build_inputs.py format) at its cutoff."""
    lines = [-x['home_handicap'] for x in record['records']]
    mus = np.array([supremacy(l, x['home_water'], x['away_water']) for l, x in zip(lines, record['records'])])
    times = np.array(record['tickTimes'])
    points = [mus[0]]
    for minutes in GRID_MINUTES:
        i = np.searchsorted(times, record['kickoff'] - minutes * 60, side='right') - 1
        points.append(mus[max(i, 0)])
    steps = np.diff(mus)
    changed = np.array([a != b for a, b in zip(lines, lines[1:])], bool)
    line_steps = np.diff(lines)
    return {
        'mu': mus[-1],
        'shape': np.diff(points),                       # 8 changes between as-of points
        'atLineChange': steps[changed].sum() if changed.any() else 0.,
        'withinLine': steps[~changed].sum() if len(steps) else 0.,
        'lineUp': float((line_steps > 0).sum()),           # line moved toward home giving more
        'lineDown': float((line_steps < 0).sum()),
    }
