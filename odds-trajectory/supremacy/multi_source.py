"""Turning-point features from Royal euro and 澳彩 Asian paths, added to the Royal Asian ones.

All matches with a verified result; upper side = giving team at the Royal
T-10 Asian line, every path is put on the upper side's implied supremacy:
  Royal Asian   turning_points.features (unchanged)
  Royal euro    de-margined home/draw/away -> (mu, total) of the closest
                independent-Poisson pair on a grid (nearest neighbour on
                P(home), P(draw)), then the same path features; plus the gap
                euro - Royal Asian at T-10 and its change since the opening
  澳彩 Asian     line and waters -> mu (total 2.7), same path features; plus
                the gap 澳彩 - Royal at T-10 and its change since the opening
Missing blocks are zero-filled with an availability flag.

Evaluation: rolling origin. For each month m in Aug, Sep, Oct, models are
trained on everything before m and predict m, so the euro block (mostly
from August on) can be learned. Settings are fixed from turning_points.py
(ridge alpha 100; boosted trees depth 2, 400 per leaf, 150 iterations;
average of standardised predictions). Feature sets are compared on the same
matches.

    python odds-trajectory/supremacy/multi_source.py
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from cross_company import macau_quotes  # noqa: E402
from euro_asia import MU, PD, PH, TOT, euro_quotes  # noqa: E402
from supremacy import supremacy  # noqa: E402
from turning_points import CLIP, PRICE_FEATURES, THRESHOLDS, as_of, features, zigzag  # noqa: E402
from v1style import WINDOWS, baseline_fit, summarise, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}
GRID = NearestNeighbors(n_neighbors=1).fit(np.column_stack([PH.ravel(), PD.ravel()]))


def euro_mu(prices):
    """(mu, total) arrays for an array of (home, draw, away) decimal prices."""
    inv = 1 / np.asarray(prices, float)
    p = inv / inv.sum(axis=1, keepdims=True)
    _, idx = GRID.kneighbors(p[:, :2])
    i, j = np.unravel_index(idx[:, 0], PH.shape)
    return MU[i], TOT[j]


def path_features(times, values, kickoff, cutoff, prefix):
    """Turning-point and back-and-forth features of one upper-side path (same definitions as turning_points)."""
    end, steps = values[-1], np.diff(values)
    tv = float(np.abs(steps).sum())
    minutes_before = lambda i: (kickoff - times[i]) / 60  # noqa: E731
    f = {'net': end - values[0], 'pathLength': tv, 'efficiency': abs(end - values[0]) / tv if tv > 1e-9 else 1.,
         'peakRetreat': values.max() - end, 'troughRecovery': end - values.min(),
         'logMinHigh': math.log1p(max(minutes_before(int(values.argmax())), 0)),
         'logMinLow': math.log1p(max(minutes_before(int(values.argmin())), 0)),
         'early': as_of(times, values, kickoff - 3600) - values[0],
         'late': end - as_of(times, values, kickoff - 3600),
         'last20': end - as_of(times, values, kickoff - 1800),
         'logTicks': math.log(len(values)), 'logAgeMin': math.log1p(max((cutoff - times[-1]) / 60, 0))}
    for h in THRESHOLDS:
        piv, direction, _ = zigzag(values, h)
        pv = values[piv]
        tag = f'{int(h * 100):02d}'
        f[f'swings{tag}'] = float(len(piv) - 1)
        f[f'amplitude{tag}'] = float((np.abs(np.diff(pv)).sum() if len(pv) > 1 else 0.) + abs(end - pv[-1]))
        f[f'lastLeg{tag}'] = end - pv[-1]
        f[f'lastDir{tag}'] = float(direction)
        f[f'logMinSincePivot{tag}'] = math.log1p(max((cutoff - times[piv[-1]]) / 60, 0)) if len(piv) > 1 else 0.
    return {f'{prefix}{k}': v for k, v in f.items()}


def royal_mu_up(r, s):
    return np.array([s * supremacy(-x['home_handicap'], x['home_water'], x['away_water']) for x in r['records']])


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    recs = sorted(kept.values(), key=lambda r: r['kickoff'])
    euro = euro_quotes({r['matchId'] for r in recs})
    macau = macau_quotes(recs, '1')
    rows, blocks = [], {'asian': [], 'euro': [], 'macau': []}
    for r in recs:
        f, s = features(r)
        times_a = np.array(r['tickTimes'], float)
        mu_a = royal_mu_up(r, s)
        blocks['asian'].append(f)
        e = sorted(q for q in euro.get(r['matchId'], []) if q[0] <= r['cutoff'])
        if len(e) >= 2:
            t_e = np.array([q[0] for q in e], float)
            mu_e, tot_e = euro_mu([q[1:] for q in e])
            mu_e = s * mu_e
            fe = path_features(t_e, mu_e, r['kickoff'], r['cutoff'], 'euro_')
            fe.update({'euro_has': 1., 'euro_gapT10': mu_e[-1] - mu_a[-1],
                       'euro_gapChange': (mu_e[-1] - mu_a[-1]) - (mu_e[0] - as_of(times_a, mu_a, t_e[0])),
                       'euro_totalChange': float(tot_e[-1] - tot_e[0])})
        else:
            fe = {'euro_has': 0.}
        blocks['euro'].append(fe)
        m = macau.get(r['matchId'])
        if m is not None and (m[0] <= r['cutoff']).sum() >= 2:
            keep = m[0] <= r['cutoff']
            t_m = m[0][keep]
            mu_m = np.array([s * supremacy(*q) for q, k in zip(m[1], keep) if k])
            fm = path_features(t_m, mu_m, r['kickoff'], r['cutoff'], 'macau_')
            fm.update({'macau_has': 1., 'macau_gapT10': mu_m[-1] - mu_a[-1],
                       'macau_gapChange': (mu_m[-1] - mu_a[-1]) - (mu_m[0] - as_of(times_a, mu_a, t_m[0]))})
        else:
            fm = {'macau_has': 0.}
        blocks['macau'].append(fm)
        margin = s * (r['score'][0] - r['score'][1])
        rows.append({'kickoff': r['kickoff'], 'line': r['line'], 'homeWater': r['homeWater'],
                     'awayWater': r['awayWater'], 'score': r['score'], 'upperResult': r['upperResult'],
                     'upperProfit': r['upperProfit'], 'lowerProfit': r['lowerProfit'],
                     'margin': margin - f['level'], 'profit': (r['upperProfit'] - r['lowerProfit']) / 2})
    matrices, names = {}, {}
    for b, items in blocks.items():
        keys = sorted({k for it in items for k in it})
        if b == 'asian':
            keys = [k for k in items[0] if k not in PRICE_FEATURES]  # trajectory-only, as before
        names[b] = keys
        matrices[b] = np.array([[it.get(k, 0.) for k in keys] for it in items], float)
    return rows, matrices, names


def fit_average(X_train, y_train, X_test):
    scaler = StandardScaler().fit(X_train)
    Xtr, Xte = scaler.transform(X_train), scaler.transform(X_test)
    y_clip = np.clip(y_train, -CLIP, CLIP)
    ridge = Ridge(alpha=100.).fit(Xtr, y_clip)
    gbm = HistGradientBoostingRegressor(max_depth=2, min_samples_leaf=400, learning_rate=.03, max_iter=150,
                                        l2_regularization=1., random_state=0).fit(Xtr, y_clip)
    out = []
    for model in (ridge, gbm):
        p_train, p_test = model.predict(Xtr), model.predict(Xte)
        out.append((p_test - p_train.mean()) / (p_train.std() or 1))
    return (out[0] + out[1]) / 2


def bets(rows, idx, pred, p_up, share):
    order = idx[np.argsort(-np.abs(pred[idx]))][:max(1, int(len(idx) * share))]
    items = []
    for i in order:
        r = rows[i]
        upper = pred[i] > 0
        items.append({'result': r['upperResult'] if upper else FLIP[r['upperResult']],
                      'profit': r['upperProfit'] if upper else r['lowerProfit'],
                      'expected': p_up[i] if upper else 1 - p_up[i]})
    return summarise(items)


def main():
    rows, M, names = build()
    kick = np.array([r['kickoff'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    p_up = baseline_fit(rows, dev)
    has_e, has_m = M['euro'][:, names['euro'].index('euro_has')] > 0, M['macau'][:, names['macau'].index('macau_has')] > 0
    months = [(ts('2026-08-01'), ts('2026-09-01')), (ts('2026-09-01'), ts('2026-10-01')), (ts('2026-10-01'), ts('2026-10-13'))]
    sets = {'只用 Royal 亚盘': ['asian'], '+Royal 欧指': ['asian', 'euro'], '+澳彩亚盘': ['asian', 'macau'],
            '+两者': ['asian', 'euro', 'macau']}
    hold = np.zeros(len(rows), bool)
    for lo, hi in months:
        hold |= (kick >= lo) & (kick < hi)
    report = {'coverage': {'euro': {'dev': int((dev & has_e).sum()), 'holdout': int((hold & has_e).sum())},
                           'macau': {'dev': int((dev & has_m).sum()), 'holdout': int((hold & has_m).sum())}},
              'results': {}}
    print(f"覆盖：Royal 欧指 开发 {report['coverage']['euro']['dev']} / 8–10 月 {report['coverage']['euro']['holdout']} 场；"
          f"澳彩亚盘 开发 {report['coverage']['macau']['dev']} / 8–10 月 {report['coverage']['macau']['holdout']} 场")

    # Single new features on available matches (Aug-Oct and dev), correlation with both targets.
    print('\n新特征单独与目标的相关（开发 / 8–10 月；只算有该数据的比赛）：')
    for b, mask in (('euro', has_e), ('macau', has_m)):
        for j, n in enumerate(names[b]):
            if n.endswith('_has'):
                continue
            cells = []
            for t in ('margin', 'profit'):
                y = np.array([r[t] for r in rows])
                c = [np.corrcoef(M[b][w & mask, j], y[w & mask])[0, 1] for w in (dev, hold)]
                cells.append((t, c))
            if all(c[0] * c[1] > 0 for _, c in cells) and min(abs(v) for _, c in cells for v in c) > .01:
                print(f"  {n:26s} " + '  '.join(f'{t} {c[0]:+.4f}/{c[1]:+.4f}' for t, c in cells))

    for target in ('margin', 'profit'):
        y = np.array([r[target] for r in rows])
        preds = {}
        for label, blocks in sets.items():
            X = np.hstack([M[b] for b in blocks])
            pred = np.full(len(rows), np.nan)
            for lo, hi in months:
                tr, te = kick < lo, (kick >= lo) & (kick < hi)
                pred[te] = fit_average(X[tr], y[tr], X[te])
            preds[label] = pred
        print(f'\n== 目标 {target}（逐月滚动：每月用此前全部数据训练）')
        report['results'][target] = {}
        for subset, mask in (('8–10 月全部', hold), ('有 Royal 欧指', hold & has_e), ('有澳彩亚盘', hold & has_m)):
            idx = np.nonzero(mask)[0]
            print(f'  [{subset}，{len(idx)} 场]')
            block = {}
            for label, pred in preds.items():
                c = float(np.corrcoef(pred[idx], y[idx])[0, 1])
                top10, top20 = bets(rows, idx, pred, p_up, .1), bets(rows, idx, pred, p_up, .2)
                block[label] = {'corr': round(c, 4), 'top10': top10, 'top20': top20}
                print(f"    {label:12s} 相关 {c:+.4f} | 前10% {top10['winRate']:.3f}({top10['lift']:+.3f}) ROI {top10['roi']:+.3f}"
                      f" | 前20% {top20['winRate']:.3f}({top20['lift']:+.3f}) ROI {top20['roi']:+.3f}")
            report['results'][target][subset] = block
    (HERE / 'results-multi-source.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
