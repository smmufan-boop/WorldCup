"""Implied goal supremacy from Asian-handicap quotes, and trajectory features.

Each quote (line, home water, away water) is turned into the expected goal
difference mu (home minus away) at which the de-margined price is fair, under
independent Poisson goals with a fixed expected total. Line changes then no
longer break the series: a deeper line with higher home water maps to about
the same mu as the previous line with lower home water.

Local line sign follows the snapshot convention: + means the home team gives.

From 2026-10-14 the record helpers (record_mu, quotes_mu) convert at the Royal
totals market's expected total (market_total.py) instead of 2.7; set
SUPREMACY_TOTAL=2.7 to reproduce the earlier results. supremacy() itself keeps
its explicit ``total`` argument (default 2.7).
"""
from __future__ import annotations

import math
import os

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


def _table(total=TOTAL_GOALS):
    """Fair de-margined home probability for every (line, mu): p_home = W / (W + L).

    W and L are the weights of winning and losing outcomes for a home bet on
    the line (pushes excluded; a quarter line splits the stake over its two
    neighbours)."""
    table = np.empty((len(LINES), len(MU_GRID)))
    halves = np.stack([LINES - .25, LINES + .25])
    quarter = np.abs(LINES * 4) % 2 == 1
    for j, mu in enumerate(MU_GRID):
        prob, goals = _goal_difference(mu, total)
        def wl(lines):
            margin = goals[None, :] - lines[..., None]
            return (prob * (margin > 1e-9)).sum(-1), (prob * (margin < -1e-9)).sum(-1)
        w, l = wl(LINES)
        wq, lq = wl(halves)
        w = np.where(quarter, wq.mean(0), w)
        l = np.where(quarter, lq.mean(0), l)
        table[:, j] = w / (w + l)
    return table


TABLE = _table()
_TABLES = {round(TOTAL_GOALS, 2): TABLE}


def _row(table, line):
    return table[min(max(int(round((line - LINES[0]) / .25)), 0), len(LINES) - 1)]


def supremacy(line, home_water, away_water, total=None):
    """Implied mu for one quote (Hong Kong waters); ``total`` defaults to TOTAL_GOALS.

    Other totals use tables on a 0.2-goal grid, interpolated linearly."""
    ih, ia = 1 / (1 + home_water), 1 / (1 + away_water)
    p_home = ih / (ih + ia)
    if total is None or abs(total - TOTAL_GOALS) < 1e-12:
        return float(np.interp(p_home, _row(TABLE, line), MU_GRID))
    lo = math.floor(total / .2) * .2
    values = []
    for t in (lo, lo + .2):
        key = round(t, 2)
        if key not in _TABLES:
            _TABLES[key] = _table(key)
        values.append(float(np.interp(p_home, _row(_TABLES[key], line), MU_GRID)))
    frac = (total - lo) / .2
    return values[0] * (1 - frac) + values[1] * frac


def trajectory(record):
    """Features for one match record (research/build_inputs.py format) at its cutoff."""
    lines = [-x['home_handicap'] for x in record['records']]
    mus = record_mu(record)
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


# ---------------------------------------------------------------- market total (from 2026-10-14)
TOTAL_MODE = os.environ.get('SUPREMACY_TOTAL', 'market')  # 'market' (default) or '2.7'


def _table_for(key):
    key = round(key, 2)
    if key not in _TABLES:
        _TABLES[key] = _table(key)
    return _TABLES[key]


def _interp_many(lines, p_home, table):
    rows = np.clip(np.round((lines - LINES[0]) / .25).astype(int), 0, len(LINES) - 1)
    out = np.empty(len(p_home))
    for row in np.unique(rows):
        m = rows == row
        out[m] = np.interp(p_home[m], table[row], MU_GRID)
    return out


def supremacy_many(lines, home_waters, away_waters, totals=None):
    """Vectorised supremacy(); same 0.2-goal table grid and linear interpolation."""
    lines, hw, aw = (np.asarray(v, float) for v in (lines, home_waters, away_waters))
    ih, ia = 1 / (1 + hw), 1 / (1 + aw)
    p_home = ih / (ih + ia)
    if totals is None:
        return _interp_many(lines, p_home, TABLE)
    totals = np.asarray(totals, float)
    lo = np.round(np.floor(totals / .2 + 1e-9) * .2, 2)
    frac = (totals - lo) / .2
    out = np.zeros(len(p_home))
    for offset, weight in ((0., 1 - frac), (.2, frac)):
        keys = np.round(lo + offset, 2)
        for key in np.unique(keys):
            m = keys == key
            out[m] += weight[m] * _interp_many(lines[m], p_home[m], _table_for(key))
    return out


def quotes_total(match_id, times, quotes):
    """Expected total used for each quote (see quotes_mu), rounded to 0.01."""
    if TOTAL_MODE == '2.7':
        return np.full(len(quotes), TOTAL_GOALS)
    import market_total
    z = market_total.z_at(match_id, times)
    if z is None:
        q = np.array(quotes, float)
        z = market_total.fallback_z(supremacy_many(q[:, 0], q[:, 1], q[:, 2]))
    return np.round(np.clip(z, 1.2, 5.0), 2)


def quotes_mu(match_id, times, quotes):
    """Home-perspective mu for quotes [(line, home water, away water)] at ``times``.

    TOTAL_MODE 'market': each quote at the Royal totals market's expected total at
    that time (market_total.z_at), or the dev-fitted Asian-only estimate when the
    match has no totals quote; '2.7': the fixed total."""
    q = np.array(quotes, float)
    if TOTAL_MODE == '2.7':
        return supremacy_many(q[:, 0], q[:, 1], q[:, 2])
    return supremacy_many(q[:, 0], q[:, 1], q[:, 2], quotes_total(match_id, times, quotes))


def record_mu(record):
    """Home-perspective mu at every tick of a research/build_inputs.py record."""
    return quotes_mu(record['matchId'], record['tickTimes'],
                     [(-x['home_handicap'], x['home_water'], x['away_water']) for x in record['records']])


def final_total(record):
    """Expected total used for the record's final (T-10) quote."""
    x = record['records'][-1]
    return float(quotes_total(record['matchId'], record['tickTimes'][-1:],
                              [(-x['home_handicap'], x['home_water'], x['away_water'])])[0])
