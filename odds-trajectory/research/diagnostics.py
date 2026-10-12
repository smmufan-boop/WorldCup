"""Diagnostics behind the README conclusions; run after replay_v1.py.

1. Do instance marks predict future agreement, and is that beyond V1's own vote?
2. Does V1 strength add information once the T-10 market price is known?

    python odds-trajectory/research/diagnostics.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from reliability import DEV, frame_for, instance_marks, load  # noqa: E402


def rates(frame, by):
    return {str(k): {'agree': round(float(g.agree.mean()), 4), 'n': int(len(g))}
            for k, g in frame.groupby(by, observed=True)}


def logistic(X, y, steps=25):
    w = np.zeros(X.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-X @ w))
        hessian = X.T @ (X * (p * (1 - p))[:, None]) + 1e-6 * np.eye(len(w))
        w += np.linalg.solve(hessian, X.T @ (y - p))
    return w


def main():
    inputs, targets, pairs, z, buckets = load()
    out = {}

    p = instance_marks(pairs)
    p = p[(p.zt != 0) & (p.zj != 0)].copy()
    p['agree'] = (p.zt == p.zj).astype(float)
    out['pairAgreementOverall'] = {'agree': round(float(p.agree.mean()), 4), 'n': int(len(p))}
    out['agreementBySimilarity'] = rates(p, pd.cut(p.S, [-.1, 50, 70, 85, 95, 100]))
    out['agreementByGroup'] = rates(p, 'group')
    marked = p[(p.good + p.bad) >= 5].copy()
    marked['r'] = marked.good / (marked.good + marked.bad)
    r_bins = pd.cut(marked.r, [-.01, .3, .45, .55, .7, 1.01])
    out['agreementByPastMark'] = rates(marked, r_bins)
    D = targets.set_index('idx').score
    marked['D'] = D.reindex(marked.t).to_numpy()
    marked = marked[marked.D.notna() & ((marked.D - 50).abs() > 1e-8)]
    marked['sameAsV1'] = np.sign(marked.D - 50) == marked.zj
    out['agreementByV1AndMark'] = rates(marked, ['sameAsV1', pd.cut(marked.r, [-.01, .45, .55, 1.01])])
    out['highMarkSameAsV1Share'] = round(float(marked[marked.r > .55].sameAsV1.mean()), 4)
    out['lowMarkSameAsV1Share'] = round(float(marked[marked.r < .45].sameAsV1.mean()), 4)

    targets['dt'] = pd.to_datetime(targets.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    f = frame_for(targets, targets.set_index('idx').score, z)
    f = f[f.bet].merge(targets[['idx', 'effectiveN']], on='idx', suffixes=('', '_t'))
    f['water'] = np.where(f.D > 50, np.where(f.line >= 0, f.homeWater, f.awayWater),
                          np.where(f.line >= 0, f.awayWater, f.homeWater))

    def design(x):
        return np.column_stack([np.ones(len(x)), np.log(x.pPick / (1 - x.pPick)),
                                (x.D - 50).abs() / 50, np.log1p(x.effectiveN)])
    dev = f[(f.dt >= DEV[0]) & (f.dt < DEV[1])]
    holdout = f[f.dt >= DEV[1]]
    fit = dev[dev.won.notna()]
    w = logistic(design(fit), fit.won.to_numpy())
    out['pickWinLogitOnDev'] = dict(zip(['const', 'logitMarketPick', 'v1Strength', 'log1pEffectiveN'],
                                        np.round(w, 4).tolist()))
    filt = {}
    for name, x in (('dev', dev), ('holdout', holdout)):
        ev = 1 / (1 + np.exp(-design(x) @ w)) * (1 + x.water) - 1
        filt[name] = {str(m): {'N': int((ev > m).sum()), 'roi': round(float(x.profit[ev > m].mean()), 4)
                               if (ev > m).any() else None} for m in (-1, 0, .02)}
    out['evFilter'] = filt
    (HERE / 'results').mkdir(exist_ok=True)
    path = HERE / 'results' / 'diagnostics-royal-t10.json'
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
