"""Turning points and back-and-forth of the supremacy path, combined into one score.

All matches (wins and losses together). Upper side = giving team at the T-10
Royal line; its path mu_up over every tick up to T-10 (supremacy.py).
Targets: margin  upper market error = upper goal margin - mu_up(T-10)
         profit  (upper profit - lower profit) / 2 at the T-10 quote, i.e.
                 what a bet on either side actually returns
Feature sets: all, and trajectory-only (without T-10 level, depth, water and
line type) to see how much comes from turning points and back-and-forth.

Denoising: ZigZag pivots (a turning point is confirmed only after the path
reverses by at least h goals) at h = 0.03 and 0.08, so tick-level jitter
does not count as a swing.

Features per match (upper perspective): swing counts and amplitude, largest
retreat from a peak and recovery from a trough, last leg and time since the
last pivot, efficiency ratio (|net move| / path length), time of high and low,
early / late / last-20-minute moves, line moves, unchanged line with falling
upper water, T-10 level, depth and water, activity.

Amplification: ridge and a strongly regularised gradient-boosted tree model
on a winsorised target (+/-4 goals); hyper-parameters by forward monthly
validation inside dev (Feb-Jul); refit on all data before August; one
evaluation on holdout (Aug-Oct 12). Bets back the side the score favours at
the T-10 quote, strongest predictions first.

    python odds-trajectory/supremacy/turning_points.py
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from supremacy import supremacy  # noqa: E402
from v1style import WINDOWS, baseline_fit, summarise, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
THRESHOLDS = (0.03, 0.08)
CLIP = 4.0
PRICE_FEATURES = {'level', 'depth', 'quarter25', 'quarter75', 'water'}
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}


def zigzag(v, h):
    """Confirmed pivot indices (starting with 0), current leg direction and its extreme index."""
    pivots, direction, ext = [0], 0, 0
    for i in range(1, len(v)):
        if direction == 0:
            if v[i] - v[0] >= h:
                direction, ext = 1, i
            elif v[0] - v[i] >= h:
                direction, ext = -1, i
        elif direction == 1:
            if v[i] >= v[ext]:
                ext = i
            elif v[ext] - v[i] >= h:
                pivots.append(ext)
                direction, ext = -1, i
        else:
            if v[i] <= v[ext]:
                ext = i
            elif v[i] - v[ext] >= h:
                pivots.append(ext)
                direction, ext = 1, i
    return pivots, direction, ext


def as_of(times, values, at):
    return values[max(np.searchsorted(times, at, side='right') - 1, 0)]


def features(r):
    s = 1 if r['line'] >= 0 else -1
    recs = r['records']
    times = np.array(r['tickTimes'], float)
    lines = np.array([-x['home_handicap'] for x in recs])
    mu = np.array([s * supremacy(l, x['home_water'], x['away_water']) for l, x in zip(lines, recs)])
    up_water = np.array([x['home_water'] if s == 1 else x['away_water'] for x in recs])
    cutoff, end = r['cutoff'], mu[-1]
    minutes_before = lambda i: (r['kickoff'] - times[i]) / 60  # noqa: E731
    steps = np.diff(mu)
    tv = float(np.abs(steps).sum())
    f = {
        'level': end, 'depth': abs(r['line']), 'quarter25': float(abs(r['line']) % 1 in (.25,)),
        'quarter75': float(abs(r['line']) % 1 in (.75,)), 'water': up_water[-1],
        'logTicks': math.log(len(mu)), 'logAgeMin': math.log1p(max((cutoff - times[-1]) / 60, 0)),
        'net': end - mu[0], 'pathLength': tv, 'efficiency': abs(end - mu[0]) / tv if tv > 1e-9 else 1.,
        'peakRetreat': mu.max() - end, 'troughRecovery': end - mu.min(),
        'logMinHigh': math.log1p(minutes_before(int(mu.argmax()))),
        'logMinLow': math.log1p(minutes_before(int(mu.argmin()))),
        'early': as_of(times, mu, r['kickoff'] - 3600) - mu[0],
        'late': end - as_of(times, mu, r['kickoff'] - 3600),
        'last20': end - as_of(times, mu, r['kickoff'] - 1800),
        'lineUp': float((s * np.diff(lines) > 0).sum()), 'lineDown': float((s * np.diff(lines) < 0).sum()),
    }
    final_start = len(lines) - 1
    while final_start > 0 and lines[final_start - 1] == lines[-1]:
        final_start -= 1
    water_move = up_water[-1] - up_water[final_start]
    f['finalWaterMove'] = water_move
    f['unchangedLineWaterDrop'] = float(final_start == 0 and water_move <= -.03)
    for h in THRESHOLDS:
        piv, direction, _ = zigzag(mu, h)
        values = mu[piv]
        legs = np.abs(np.diff(values)) if len(values) > 1 else np.array([0.])
        tag = f'{int(h * 100):02d}'
        f[f'swings{tag}'] = float(len(piv) - 1)
        f[f'amplitude{tag}'] = float(legs.sum() + abs(end - values[-1]))
        f[f'lastLeg{tag}'] = end - values[-1]
        f[f'lastDir{tag}'] = float(direction)
        f[f'logMinSincePivot{tag}'] = math.log1p(max((cutoff - times[piv[-1]]) / 60, 0)) if len(piv) > 1 else 0.
    return f, s


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows, feats = [], []
    for r in sorted(kept.values(), key=lambda r: r['kickoff']):
        f, s = features(r)
        margin = s * (r['score'][0] - r['score'][1])
        rows.append({'kickoff': r['kickoff'], 'line': r['line'], 'homeWater': r['homeWater'],
                     'awayWater': r['awayWater'], 'score': r['score'], 'upperResult': r['upperResult'],
                     'upperProfit': r['upperProfit'], 'lowerProfit': r['lowerProfit'],
                     'e': margin - f['level'], 'p': (r['upperProfit'] - r['lowerProfit']) / 2})
        feats.append(f)
    names = list(feats[0])
    return rows, np.array([[f[n] for n in names] for f in feats]), names


def month_start(year, month):
    return datetime.fromisoformat(f'{year}-{month:02d}-01T00:00:00+08:00').timestamp()


def models(params):
    kind, value = params
    if kind == 'ridge':
        return Ridge(alpha=value)
    return HistGradientBoostingRegressor(max_depth=value[0], min_samples_leaf=value[1], learning_rate=.03,
                                         max_iter=value[2], l2_regularization=1.0, random_state=0)


def fit_predict(params, X_train, y_train, X_test):
    scaler = StandardScaler().fit(X_train)
    model = models(params).fit(scaler.transform(X_train), np.clip(y_train, -CLIP, CLIP))
    return model.predict(scaler.transform(X_test)), model, scaler


def bet_summary(rows, idx, pred, p_up, share):
    order = idx[np.argsort(-np.abs(pred[idx]))][:max(1, int(len(idx) * share))]
    items = []
    for i in order:
        r = rows[i]
        upper = pred[i] > 0
        res = r['upperResult'] if upper else FLIP[r['upperResult']]
        items.append({'result': res, 'profit': r['upperProfit'] if upper else r['lowerProfit'],
                      'expected': p_up[i] if upper else 1 - p_up[i]})
    return summarise(items)


def run(rows, X, names, y, kick, p_up, title):
    """Single-feature check, forward validation, holdout evaluation for one target and feature set."""
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    out = {'singleFeatureCorr': {n: {w: round(float(np.corrcoef(X[m, j], y[m])[0, 1]), 4) for w, m in
                                     (('dev', dev), ('holdout', hold))} for j, n in enumerate(names)}}
    grid = [('ridge', a) for a in (1., 100., 3000.)] + \
           [('gbm', (d, leaf, it)) for d in (2, 3) for leaf in (400, 1500) for it in (150, 400)]
    folds = [(month_start(2026, m), month_start(2026, m + 1)) for m in (5, 6, 7)]
    scores = {}
    for params in grid:
        cs = []
        for lo, hi in folds:
            tr, va = kick < lo, (kick >= lo) & (kick < hi)
            pred, _, _ = fit_predict(params, X[tr], y[tr], X[va])
            cs.append(np.corrcoef(pred, y[va])[0, 1])
        scores[str(params)] = round(float(np.mean(cs)), 4)
    best = {kind: max((p for p in grid if p[0] == kind), key=lambda p: scores[str(p)]) for kind in ('ridge', 'gbm')}
    out['forwardValidation'], out['chosen'] = scores, {k: str(v) for k, v in best.items()}
    train = kick < ts(WINDOWS['holdout'][0])
    preds = {}
    for kind, params in best.items():
        preds[kind], model, _ = fit_predict(params, X[train], y[train], X)
        if kind == 'ridge':
            out['ridgeCoefficients'] = dict(sorted(((n, round(float(c), 4)) for n, c in zip(names, model.coef_)),
                                                   key=lambda kv: -abs(kv[1]))[:10])
    z = lambda v: (v - v[train].mean()) / v[train].std()  # noqa: E731
    preds['average'] = (z(preds['ridge']) + z(preds['gbm'])) / 2
    idx = np.nonzero(hold)[0]
    out['holdout'] = {}
    print(f"\n== {title}  （前推验证：ridge {scores[str(best['ridge'])]:+.4f}，gbm {scores[str(best['gbm'])]:+.4f}）")
    for kind, pred in preds.items():
        block = {'corr': round(float(np.corrcoef(pred[idx], y[idx])[0, 1]), 4), 'monthlyCorr': {},
                 **{f'top{int(s * 100)}': bet_summary(rows, idx, pred, p_up, s) for s in (.1, .2, .3, 1.)}}
        for m in (8, 9, 10):
            mm = idx[(kick[idx] >= month_start(2026, m)) & (kick[idx] < month_start(2026, m + 1))]
            block['monthlyCorr'][m] = round(float(np.corrcoef(pred[mm], y[mm])[0, 1]), 4)
        out['holdout'][kind] = block
        cells = ' | '.join(f"前{int(s * 100)}% {block[f'top{int(s * 100)}']['winRate']:.3f}({block[f'top{int(s * 100)}']['lift']:+.3f}) ROI {block[f'top{int(s * 100)}']['roi']:+.3f}"
                           for s in (.1, .2, .3, 1.))
        print(f"  {kind:8s} 留出相关 {block['corr']:+.4f}（8/9/10 月 {block['monthlyCorr'][8]:+.3f}/{block['monthlyCorr'][9]:+.3f}/{block['monthlyCorr'][10]:+.3f}）| {cells}")
    print('  岭回归主要系数：' + ', '.join(f'{k} {v:+.3f}' for k, v in out['ridgeCoefficients'].items()))
    return out


def main():
    rows, X, names = build()
    kick = np.array([r['kickoff'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    p_up = baseline_fit(rows, dev)
    traj = [j for j, n in enumerate(names) if n not in PRICE_FEATURES]
    targets = {'margin': np.array([r['e'] for r in rows]), 'profit': np.array([r['p'] for r in rows])}
    report = {'features': names, 'n': {'dev': int(dev.sum()), 'holdout': int(hold.sum())}, 'runs': {}}
    singles = {}
    for j, n in enumerate(names):
        singles[n] = {f'{t}:{w}': round(float(np.corrcoef(X[m, j], y[m])[0, 1]), 4)
                      for t, y in targets.items() for w, m in (('dev', dev), ('holdout', hold))}
    print('单个特征的相关（两种目标都两段同号的；标准误：开发 %.3f，留出 %.3f）' % (1 / math.sqrt(dev.sum()), 1 / math.sqrt(hold.sum())))
    for n, c in singles.items():
        vals = list(c.values())
        if all(v > 0 for v in vals) or all(v < 0 for v in vals):
            print(f'  {n:24s} ' + '  '.join(f'{k} {v:+.4f}' for k, v in c.items()))
    for t, y in targets.items():
        for fs, cols in (('全部特征', list(range(len(names)))), ('只用轨迹特征', traj)):
            key = f'{t} / {fs}'
            report['runs'][key] = run(rows, X[:, cols], [names[j] for j in cols], y, kick, p_up, f'目标 {t}，{fs}')
    (HERE / 'results-turning-points.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')

if __name__ == '__main__':
    main()
