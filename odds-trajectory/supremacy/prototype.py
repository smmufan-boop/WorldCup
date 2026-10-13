"""Supremacy-trajectory prototype: time-ordered nearest neighbours on implied mu paths.

For each Royal match at T-10 (decision point):
  x = [8 shape changes of mu between as-of points] + [mu level at T-10]
      + [line-change correction: mu moved at line changes, mu moved within
         a line, number of line moves up / down]
  label e = actual goal difference - mu at T-10  (how wrong the market was)
For a target, neighbours come only from matches with kickoff + 5400 s before
its cutoff. The prediction is the mean e of the k nearest neighbours; bet the
side the market underrates (home if > 0) at the target's T-10 Royal quote.

Configurations (k, level weight, correction weight, share of strongest
predictions bet) are chosen on dev by win-rate lift over a price + line-type
baseline fitted on dev, then reported unchanged on holdout. A control with
random neighbours from the same eligible pool is reported alongside.

    python odds-trajectory/supremacy/prototype.py   (needs research/cache/inputs-company3-T10.pkl)
"""
from __future__ import annotations

import itertools
import json
import multiprocessing as mp
import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from build_inputs import settle  # noqa: E402
from supremacy import SHAPE_NAMES, trajectory  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
SETTLED_AFTER = 5400
WINDOWS = {'dev': ('2026-02-01', '2026-08-01'), 'holdout': ('2026-08-01', '2026-10-13')}
KS = (50, 200)
LEVEL_WEIGHTS = (0., 1., 3.)
CORRECTION_WEIGHTS = (0., 1.)
SHARES = (1.0, 0.3, 0.1)

X = None  # globals shared with forked workers
META = None


def ts(day):
    return datetime.fromisoformat(day + 'T00:00:00+08:00').timestamp()


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):  # one record per real match
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows, feats = [], []
    for r in sorted(kept.values(), key=lambda r: r['kickoff']):
        t = trajectory(r)
        sh, sa = r['score']
        upper_res, upper_profit, lower_profit = settle(r['line'], r['homeWater'], r['awayWater'], sh, sa)
        home_is_upper = r['line'] >= 0
        rows.append({'matchId': r['matchId'], 'kickoff': r['kickoff'], 'cutoff': r['cutoff'],
                     'line': r['line'], 'hw': r['homeWater'], 'aw': r['awayWater'], 'mu': t['mu'],
                     'e': (sh - sa) - t['mu'], 'firstTier': r['firstTier'],
                     'upperResult': upper_res, 'homeIsUpper': home_is_upper,
                     'homeProfit': upper_profit if home_is_upper else lower_profit,
                     'awayProfit': lower_profit if home_is_upper else upper_profit})
        feats.append(np.concatenate([t['shape'], [t['mu']],
                                     [t['atLineChange'], t['withinLine'], t['lineUp'], t['lineDown']]]))
    return rows, np.array(feats)


def neighbours(i):
    """Predictions for target i under every (k, level, correction) plus a random control."""
    meta, x = META, X
    end = np.searchsorted(meta['kickoff'], meta['cutoff'][i] - SETTLED_AFTER, side='left')
    if end < max(KS):
        return i, None
    d_shape = ((x['shape'][:end] - x['shape'][i]) ** 2).sum(axis=1)
    d_level = (x['level'][:end] - x['level'][i]) ** 2
    d_corr = ((x['corr'][:end] - x['corr'][i]) ** 2).sum(axis=1)
    e = meta['e'][:end]
    out = {}
    for wl, wc in itertools.product(LEVEL_WEIGHTS, CORRECTION_WEIGHTS):
        dist = d_shape + wl * d_level + wc * d_corr
        order = np.argpartition(dist, max(KS))[:max(KS)]
        order = order[np.argsort(dist[order])]
        for k in KS:
            out[(k, wl, wc)] = float(e[order[:k]].mean())
    rng = np.random.default_rng(i)
    out['random'] = float(e[rng.choice(end, max(KS), replace=False)].mean())
    return i, out


class Baseline:
    """P(upper side wins incl. half wins) from price and line fraction, fitted on dev."""

    def __init__(self, rows, mask):
        self.w = self._fit(rows, mask)

    @staticmethod
    def design(rows):
        line = np.array([r['line'] for r in rows])
        hw, aw = np.array([r['hw'] for r in rows]), np.array([r['aw'] for r in rows])
        ih, ia = 1 / (1 + hw), 1 / (1 + aw)
        p_home = ih / (ih + ia)
        p_up = np.where(line >= 0, p_home, 1 - p_home)
        frac = np.round(np.abs(line) * 4).astype(int) % 4
        logit = np.log(p_up / (1 - p_up))
        return np.column_stack([*(frac == f for f in range(4)), *((frac == f) * logit for f in range(4))]).astype(float)

    def _fit(self, rows, mask):
        X_ = self.design(rows)[mask]
        y = np.array([r['upperResult'] in ('win', 'half_win') for r in rows], float)[mask]
        w = np.zeros(X_.shape[1])
        for _ in range(30):
            p = 1 / (1 + np.exp(-X_ @ w))
            w += np.linalg.solve(X_.T @ (X_ * (p * (1 - p))[:, None]) + 1e-6 * np.eye(len(w)), X_.T @ (y - p))
        return w

    def p_upper(self, rows):
        return 1 / (1 + np.exp(-self.design(rows) @ self.w))


def evaluate(rows, preds, idx, share, p_up):
    """Bet the side the market underrates on the `share` strongest predictions among idx."""
    idx = [i for i in idx if preds[i] is not None]
    if not idx:
        return {'N': 0}
    strength = np.array([abs(preds[i]) for i in idx])
    cut = np.quantile(strength, 1 - share) if share < 1 else -1
    picked = [i for i, s in zip(idx, strength) if s > cut]
    won, expected, profit = [], [], []
    for i in picked:
        r = rows[i]
        home = preds[i] > 0
        take_upper = home == r['homeIsUpper']
        res = r['upperResult']
        if res != 'push':
            upper_won = res in ('win', 'half_win')
            won.append(upper_won if take_upper else not upper_won)
            expected.append(p_up[i] if take_upper else 1 - p_up[i])
        profit.append(r['homeProfit'] if home else r['awayProfit'])
    n = len(won)
    w = float(np.mean(won)) if n else float('nan')
    return {'N': len(picked), 'winRate': round(w, 4), 'winSE': round(float(np.sqrt(w * (1 - w) / max(n, 1))), 4),
            'baseline': round(float(np.mean(expected)), 4), 'lift': round(w - float(np.mean(expected)), 4),
            'roi': round(float(np.mean(profit)), 4)}


def decomposition(rows, preds_all, windows, level_k=200):
    """Is the signal the trajectory, or only the market's bias at a given mu level?

    level-only: mean market error of the level_k eligible matches nearest in mu.
    Reported: correlations of level-only, shape-only (k=200, no level, no
    correction) and the dev-selected style config (k=50, level 3, correction 1)
    with the market error, and of the trajectory predictors with the residual
    left after the level-only predictor."""
    kick = np.array([r['kickoff'] for r in rows])
    cut = np.array([r['cutoff'] for r in rows])
    mu = np.array([r['mu'] for r in rows])
    e = np.array([r['e'] for r in rows])
    out = {}
    for name, idx in windows.items():
        idx = [i for i in idx if preds_all.get(i)]
        level = []
        for i in idx:
            end = np.searchsorted(kick, cut[i] - SETTLED_AFTER, side='left')
            near = np.argpartition(np.abs(mu[:end] - mu[i]), level_k)[:level_k]
            level.append(e[:end][near].mean())
        level, y = np.array(level), e[idx]
        shape = np.array([preds_all[i][(200, 0., 0.)] for i in idx])
        combo = np.array([preds_all[i][(50, 3., 1.)] for i in idx])
        X_ = np.column_stack([np.ones(len(y)), level])
        residual = y - X_ @ np.linalg.lstsq(X_, y, rcond=None)[0]
        c = lambda a, b: round(float(np.corrcoef(a, b)[0, 1]), 4)  # noqa: E731
        out[name] = {'levelOnly': c(level, y), 'shapeOnly': c(shape, y), 'levelShapeCorrection': c(combo, y),
                     'shapeOnlyVsResidual': c(shape, residual), 'levelShapeCorrectionVsResidual': c(combo, residual),
                     'n': len(y), 'correlationSE': round(1 / np.sqrt(len(y)), 4)}
    return out


def main():
    global X, META
    rows, feats = build()
    kick = np.array([r['kickoff'] for r in rows])
    dev_mask = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    # Scale features by dev standard deviations (fixed before any holdout look).
    scale = feats[dev_mask].std(axis=0)
    scale[scale == 0] = 1
    z = feats / scale
    X = {'shape': z[:, :8], 'level': z[:, 8], 'corr': z[:, 9:]}
    META = {'kickoff': kick, 'cutoff': np.array([r['cutoff'] for r in rows]), 'e': np.array([r['e'] for r in rows])}
    targets = [i for i, r in enumerate(rows) if r['kickoff'] >= ts(WINDOWS['dev'][0])]
    preds_all = {}
    with mp.get_context('fork').Pool() as pool:
        for i, out in pool.imap_unordered(neighbours, targets, chunksize=256):
            preds_all[i] = out
    with open(CACHE / 'supremacy-predictions.pkl', 'wb') as stream:
        pickle.dump({'rows': rows, 'preds': preds_all}, stream)
    base = Baseline(rows, dev_mask)
    p_up = base.p_upper(rows)
    windows = {name: [i for i in targets if ts(a) <= rows[i]['kickoff'] < ts(b)] for name, (a, b) in WINDOWS.items()}

    report = {'totalGoals': 2.7, 'features': SHAPE_NAMES + ['mu@T-10', 'atLineChange', 'withinLine', 'lineUp', 'lineDown'],
              'targets': {k: len(v) for k, v in windows.items()}, 'configs': []}
    configs = list(itertools.product(KS, LEVEL_WEIGHTS, CORRECTION_WEIGHTS))
    for cfg in configs + ['random']:
        preds = {i: (o[cfg] if o else None) for i, o in preds_all.items()}
        row = {'config': cfg if cfg == 'random' else dict(zip(('k', 'levelWeight', 'correctionWeight'), cfg))}
        for name, idx in windows.items():
            ok = [i for i in idx if preds[i] is not None]
            row[name] = {'corrWithMarketError': round(float(np.corrcoef([preds[i] for i in ok],
                                                                         [rows[i]['e'] for i in ok])[0, 1]), 4),
                         **{f'share{s}': evaluate(rows, preds, idx, s, p_up) for s in SHARES}}
        report['configs'].append(row)
    candidates = [(c, s) for c in report['configs'] if c['config'] != 'random' for s in SHARES]
    best = max(candidates, key=lambda cs: cs[0]['dev'][f'share{cs[1]}']['lift'])
    report['selectedOnDev'] = {'config': best[0]['config'], 'share': best[1],
                               'dev': best[0]['dev'][f'share{best[1]}'], 'holdout': best[0]['holdout'][f'share{best[1]}'],
                               'holdoutCorr': best[0]['holdout']['corrWithMarketError']}
    report['decomposition'] = decomposition(rows, preds_all, windows)
    out = HERE / 'results-prototype.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')

    print(f"targets: {report['targets']}")
    print('config                                  | dev corr  holdout corr | dev all / top30% / top10% win(lift)          | holdout all / top30% / top10% win(lift)')
    for c in report['configs']:
        cells = []
        for w in WINDOWS:
            cells.append(' / '.join(f"{c[w][f'share{s}']['winRate']:.3f}({c[w][f'share{s}']['lift']:+.3f})" for s in SHARES))
        name = c['config'] if c['config'] == 'random' else f"k={c['config']['k']} level={c['config']['levelWeight']} corr={c['config']['correctionWeight']}"
        print(f"{name:39s} | {c['dev']['corrWithMarketError']:+.4f}   {c['holdout']['corrWithMarketError']:+.4f}    | {cells[0]} | {cells[1]}")
    print('\nselected on dev:', json.dumps(report['selectedOnDev'], ensure_ascii=False))
    print('\ndecomposition (correlation with market error):')
    for w, block in report['decomposition'].items():
        print(f'  {w}: ' + ', '.join(f'{k} {v:+.4f}' for k, v in block.items()))


if __name__ == '__main__':
    main()
