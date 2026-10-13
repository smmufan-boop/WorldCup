"""Expected total goals Z from the Royal totals (size) market, for the mu conversion.

From 2026-10-14 every Asian quote is converted to mu at the expected total the
totals market gave at that moment, instead of the fixed 2.7:
  Z(t)       last valid pre-match Royal totals quote (source 3) at or before t,
             turned into the independent-Poisson expected total whose de-margined
             over probability is fair on the quoted line (quarter lines split);
             before the first totals quote, the first one
  fallback   matches without a totals quote (the euro-totals snapshot is partial):
             Z estimated from the Asian price alone, Z = a + b|mu27| + c mu27^2,
             fitted on dev (02-01..07-31) matches that have a T-10 totals quote,
             where mu27 is the same quote converted at 2.7

Paths and the fallback coefficients are cached in research/cache/royal-totals.pkl
(rebuilt automatically when missing). Set SUPREMACY_TOTAL=2.7 to reproduce the
earlier fixed-total results.
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import pickle
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / 'research' / 'cache'
DATA = HERE.parent / 'euro-totals' / 'snapshots' / '2026-10-13-partial' / 'data'
PATHS = CACHE / 'royal-totals.pkl'
LAMBDA = np.round(np.arange(.5, 7.0001, .01), 3)
GOALS = np.arange(0, 21)
DEV = ('2026-02-01', '2026-08-01')


def jsonl(table):
    for part in sorted(glob.glob(str(DATA / f'{table}-part-*.jsonl.gz'))):
        with gzip.open(part, 'rt', encoding='utf-8') as stream:
            for line in stream:
                yield json.loads(line)


def poisson(lam):
    return np.exp(-lam) * lam ** GOALS / np.array([math.factorial(int(g)) for g in GOALS])


def over_parts(line, goals):
    """(win, loss) weight of an over bet on ``line`` for each goal count; quarter lines split."""
    def one(l):
        return (goals > l + 1e-9).astype(float), (goals < l - 1e-9).astype(float)
    if abs(line * 4) % 2 == 1:
        (w1, l1), (w2, l2) = one(line - .25), one(line + .25)
        return (w1 + w2) / 2, (l1 + l2) / 2
    return one(line)


_TABLE = {}


def market_lambda(line, over_hk, under_hk):
    """Expected total that makes the de-margined over probability fair on ``line``."""
    io, iu = 1 / (1 + over_hk), 1 / (1 + under_hk)
    p_over = io / (io + iu)
    if line not in _TABLE:
        w, l = over_parts(line, GOALS)
        probs = np.array([poisson(x) for x in LAMBDA])
        W, L = probs @ w, probs @ l
        _TABLE[line] = W / (W + L)
    return float(np.interp(p_over, _TABLE[line], LAMBDA))


def settle_over(line, over_hk, under_hk, goals):
    """(result for over, profit of over, profit of under) per unit stake at HK water."""
    def one(l):
        return 1 if goals > l + 1e-9 else -1 if goals < l - 1e-9 else 0
    parts = [one(line - .25), one(line + .25)] if abs(line * 4) % 2 == 1 else [one(line)] * 2
    over = sum(over_hk / 2 if p > 0 else -.5 if p < 0 else 0 for p in parts)
    under = sum(under_hk / 2 if p < 0 else -.5 if p > 0 else 0 for p in parts)
    s = sum(parts)
    result = {2: 'win', 1: 'half_win', 0: 'push', -1: 'half_loss', -2: 'loss'}[s]
    return result, over, under


def royal_totals_quotes(match_ids, kickoffs):
    """Valid pre-match Royal totals quotes per match, sorted: {matchId: [(changed_at, line, over_hk, under_hk)]}."""
    tasks = {t[0]: t[1] for t in jsonl('tasks') if t[2] == 'size' and t[3] == '3' and t[1] in match_ids}
    out = {}
    for q in jsonl('quotes'):  # task, order, changed_at, ..., over_hk 8, total_line 9, under_hk 10, valid 11, prematch 12
        mid = tasks.get(q[0])
        if mid is None or q[11] != 1 or q[12] != 1 or q[2] is None or q[2] >= kickoffs[mid]:
            continue
        out.setdefault(mid, []).append((q[2], q[1], float(q[9]), float(q[8]), float(q[10])))
    return {m: [(t, l, o, u) for t, _, l, o, u in sorted(v)] for m, v in out.items()}


def _build():
    from supremacy import supremacy  # local import: supremacy imports this module lazily
    records = pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb'))
    kickoffs = {r['matchId']: r['kickoff'] for r in records}
    quotes = royal_totals_quotes(set(kickoffs), kickoffs)
    paths = {m: (np.array([q[0] for q in qs], float), np.array([market_lambda(*q[1:]) for q in qs]))
             for m, qs in quotes.items()}
    lo, hi = (datetime.fromisoformat(d + 'T00:00:00+08:00').timestamp() for d in DEV)
    mu, z = [], []
    for r in records:
        if r['matchId'] in paths and lo <= r['kickoff'] < hi:
            t, zs = paths[r['matchId']]
            i = np.searchsorted(t, r['kickoff'] - 600, side='right') - 1
            if i >= 0:
                mu.append(supremacy(r['line'], r['homeWater'], r['awayWater'], 2.7))
                z.append(zs[i])
    mu = np.array(mu)
    coef = np.linalg.lstsq(np.column_stack([np.ones_like(mu), np.abs(mu), mu ** 2]), np.array(z), rcond=None)[0]
    data = {'paths': paths, 'fallback': [float(c) for c in coef], 'fallbackFitN': len(mu)}
    pickle.dump(data, open(PATHS, 'wb'))
    return data


_DATA = None


def data():
    global _DATA
    if _DATA is None:
        _DATA = pickle.load(open(PATHS, 'rb')) if PATHS.exists() else _build()
    return _DATA


def z_at(match_id, times):
    """Market expected total at each time, or None when the match has no totals quote."""
    path = data()['paths'].get(match_id)
    if path is None:
        return None
    t, z = path
    return z[np.clip(np.searchsorted(t, np.asarray(times, float), side='right') - 1, 0, None)]


def fallback_z(mu27):
    a, b, c = data()['fallback']
    return np.clip(a + b * np.abs(mu27) + c * np.asarray(mu27) ** 2, 1.2, 5.0)
