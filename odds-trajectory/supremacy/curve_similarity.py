"""Curve-to-curve similarity of the Royal Asian supremacy path, scored by goal margin minus prediction.

The line is already inside the curve: every Royal quote becomes the upper
side's implied supremacy mu_up (supremacy.py), so a line change is a step of
the same curve, not a break. Curve = mu_up sampled every 10 minutes from T-360
to T-10 (36 points, last quote before each point), absolute level kept.

Outcome of a past match (upper side = giving team at its T-10 line):
  vs mu     e = upper goal margin - mu_up(T-10)   (actual minus implied prediction)
  vs line   e = upper goal margin - |line|          (actual minus the handicap)
clipped to +/-4 goals. e > 0: the upper side beat the prediction, so the
upper side was "right"; e < 0: the lower side was right.

Similarity measures (time-series literature):
  euclid    Euclidean distance on the absolute curve
  shape     Euclidean on the curve minus its T-10 value (shape only)
  dtw       dynamic time warping, Sakoe-Chiba band 3 points (+/-30 min), absolute curve
  ddtw      derivative DTW (Keogh & Pazzani 2001): DTW on the slope estimate, plus the
            T-10 level difference so the level is not lost
  sbd       shape-based distance of k-Shape (Paparrizos & Gravano 2015):
            1 - max normalised cross-correlation, shifts up to +/-3 points
Candidates: the 2,000 nearest past matches by euclid (prefilter, like
LB-based pruning), then each measure re-ranks them.

Score: k nearest by the measure, similarity S = 1 - d / d_(k+1), weight
w = S^p (higher similarity counts much more for larger p),
  score = sum(w e) / (alpha + sum(w)), alpha = 10;  D = 50 + 12.5 * score.
D > 50 backs the upper side at the T-10 quote, D < 50 the lower side.
Pool for a target: matches with kickoff + 90 min before the target's day.

Selection: measure x k {50, 200, 800} x p {2, 6} x outcome {mu, line} on dev
(02-01..07-31) by the mean monthly correlation of the score with e (vs mu);
the chosen one is grouped by deciles and green / orange / red bands with dev
cut points (similar_score.grouped) and evaluated once on holdout (08-01..10-12).

    python odds-trajectory/supremacy/curve_similarity.py
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
from numba import njit, prange

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from similar_score import BUFFER, grouped  # noqa: E402
from supremacy import supremacy  # noqa: E402
from turning_points import CACHE, as_of, month_start  # noqa: E402
from v1style import WINDOWS, baseline_fit, ts  # noqa: E402

GRID = np.arange(360, 0, -10)  # minutes before kickoff: 360, 350, ..., 10
PREFILTER = 2000
BAND = 3
KS = (50, 200, 800)
POWERS = (2, 6)
ALPHA = 10.
CLIP = 4.
MEASURES = ('euclid', 'shape', 'dtw', 'ddtw', 'sbd')


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows, curves = [], []
    for r in sorted(kept.values(), key=lambda r: r['kickoff']):
        s = 1 if r['line'] >= 0 else -1
        times = np.array(r['tickTimes'], float)
        mu = np.array([s * supremacy(-x['home_handicap'], x['home_water'], x['away_water']) for x in r['records']])
        curve = np.array([as_of(times, mu, r['kickoff'] - m * 60) for m in GRID])
        margin = s * (r['score'][0] - r['score'][1])
        rows.append({'kickoff': r['kickoff'], 'cutoff': r['cutoff'], 'line': r['line'],
                     'homeWater': r['homeWater'], 'awayWater': r['awayWater'], 'score': r['score'],
                     'upperResult': r['upperResult'], 'upperProfit': r['upperProfit'],
                     'lowerProfit': r['lowerProfit'], 'mu': np.clip(margin - mu[-1], -CLIP, CLIP),
                     'lineE': np.clip(margin - abs(r['line']), -CLIP, CLIP)})
        curves.append(curve)
    return rows, np.array(curves)


@njit(cache=True)
def _dtw(a, b, band):
    n = len(a)
    D = np.full((n + 1, n + 1), np.inf)
    D[0, 0] = 0.
    for i in range(1, n + 1):
        for j in range(max(1, i - band), min(n, i + band) + 1):
            c = (a[i - 1] - b[j - 1]) ** 2
            D[i, j] = c + min(D[i - 1, j], D[i, j - 1], D[i - 1, j - 1])
    return np.sqrt(D[n, n])


@njit(cache=True)
def _sbd(a, b, shift):
    na, nb = np.sqrt((a * a).sum()), np.sqrt((b * b).sum())
    if na < 1e-12 and nb < 1e-12:
        return 0.
    if na < 1e-12 or nb < 1e-12:
        return 1.
    n, best = len(a), -1.
    for s in range(-shift, shift + 1):
        acc = 0.
        for i in range(n):
            j = i + s
            if 0 <= j < n:
                acc += a[i] * b[j]
        best = max(best, acc / (na * nb))
    return 1. - best


@njit(parallel=True, cache=True)
def _measures(T, P, cand, band):
    """Distances of each target curve to its candidates under dtw, ddtw and sbd."""
    m, c = cand.shape
    out = np.empty((3, m, c))
    n = T.shape[1]
    for t in prange(m):
        a = T[t]
        da = np.empty(n - 2)
        for i in range(1, n - 1):
            da[i - 1] = ((a[i] - a[i - 1]) + (a[i + 1] - a[i - 1]) / 2) / 2
        za = a - a.mean()
        sa = za.std()
        if sa > 1e-9:
            za = za / sa
        else:
            za = za * 0.
        for q in range(c):
            b = P[cand[t, q]]
            out[0, t, q] = _dtw(a, b, band)
            db = np.empty(n - 2)
            for i in range(1, n - 1):
                db[i - 1] = ((b[i] - b[i - 1]) + (b[i + 1] - b[i - 1]) / 2) / 2
            out[1, t, q] = np.sqrt(_dtw(da, db, band) ** 2 + (a[-1] - b[-1]) ** 2)
            zb = b - b.mean()
            sb = zb.std()
            if sb > 1e-9:
                zb = zb / sb
            else:
                zb = zb * 0.
            out[2, t, q] = _sbd(za, zb, band)
    return out


def neighbour_scores(rows, C):
    """Score for every target from Feb on: measure x k x power x outcome."""
    kick = np.array([r['kickoff'] for r in rows])
    cut = np.array([r['cutoff'] for r in rows])
    outcome = {'mu': np.array([r['mu'] for r in rows]), 'line': np.array([r['lineE'] for r in rows])}
    days = np.floor((kick - ts(WINDOWS['dev'][0])) / 86400).astype(int)
    shape = C - C[:, -1:]
    out = {(ms, k, p, o): np.full(len(rows), np.nan) for ms in MEASURES for k in KS for p in POWERS for o in outcome}
    for day in range(days.max() + 1):
        tgt = np.nonzero(days == day)[0]
        if not len(tgt):
            continue
        pool = np.nonzero(kick + BUFFER < cut[tgt].min())[0]
        T, P = C[tgt], C[pool]
        d2 = (T ** 2).sum(1)[:, None] + (P ** 2).sum(1)[None, :] - 2 * T @ P.T
        m = min(PREFILTER, len(pool))
        cand = np.argpartition(d2, m - 1, axis=1)[:, :m]
        euclid = np.sqrt(np.maximum(np.take_along_axis(d2, cand, 1), 0))
        shape_d = np.sqrt(((shape[tgt][:, None, :] - shape[pool][cand]) ** 2).sum(2))
        dtw, ddtw, sbd = _measures(T, P, cand, BAND)
        for ms, d in zip(MEASURES, (euclid, shape_d, dtw, ddtw, sbd)):
            order = np.argsort(d, 1, kind='stable')
            nd = np.take_along_axis(d, order, 1)
            near = pool[np.take_along_axis(cand, order, 1)]
            for k in KS:
                kk = min(k, m - 1)
                ref = np.maximum(nd[:, kk:kk + 1], 1e-12)
                sim = np.clip(1 - nd[:, :kk] / ref, 0, 1)
                for p in POWERS:
                    w = sim ** p
                    for o, e in outcome.items():
                        score = (w * e[near[:, :kk]]).sum(1) / (ALPHA + w.sum(1))
                        out[(ms, k, p, o)][tgt] = 50 + 12.5 * score
    return out


def main():
    rows, C = build()
    kick = np.array([r['kickoff'] for r in rows])
    e = np.array([r['mu'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    p_up = baseline_fit(rows, dev)
    scores = neighbour_scores(rows, C)
    dev_months = [(month_start(2026, m), month_start(2026, m + 1)) for m in range(2, 8)]
    hold_months = [(month_start(2026, m), month_start(2026, m + 1)) for m in (8, 9, 10)]

    def corr(D, mask):
        return float(np.corrcoef(D[mask], e[mask])[0, 1])

    report = {'method': __doc__.split('\n\n')[1:6], 'configs': {}}
    print('配置（度量 / k / 权重幂 / 结果口径）：开发段逐月相关均值（与"实际净胜球 − T-10 μ"）| 留出段相关（8/9/10 月）')
    for key, D in scores.items():
        dm = [corr(D, (kick >= lo) & (kick < hi)) for lo, hi in dev_months]
        hm = [corr(D, (kick >= lo) & (kick < hi)) for lo, hi in hold_months]
        report['configs']['/'.join(map(str, key))] = {'devMonthly': [round(c, 4) for c in dm], 'devMean': round(float(np.mean(dm)), 4),
                                                      'holdout': round(corr(D, hold), 4), 'holdoutMonthly': [round(c, 4) for c in hm]}
        print(f"  {key[0]:6s} k={key[1]:3d} p={key[2]} {key[3]:4s}: 开发 {np.mean(dm):+.4f}（{' '.join(f'{c:+.3f}' for c in dm)}）"
              f" | 留出 {corr(D, hold):+.4f}（{' '.join(f'{c:+.3f}' for c in hm)}）")
    print('\n各度量在开发段的最好配置：')
    for ms in MEASURES:
        k = max((k for k in scores if k[0] == ms), key=lambda k: report['configs']['/'.join(map(str, k))]['devMean'])
        c = report['configs']['/'.join(map(str, k))]
        print(f"  {ms:6s} {'/'.join(map(str, k))}: 开发 {c['devMean']:+.4f}，留出 {c['holdout']:+.4f}")
    best = max(scores, key=lambda k: report['configs']['/'.join(map(str, k))]['devMean'])
    report['chosen'] = '/'.join(map(str, best))
    print(f"\n开发段选出：{report['chosen']}")
    grouped(rows, scores[best], kick, dev, hold, p_up, report)
    (HERE / 'results-curve-similarity.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
