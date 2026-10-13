"""Similar-trajectory score: Royal Asian path only, betting return as the target, evaluated by score group.

All matches. Upper side = giving team at the T-10 Royal line. Outcome of a
past match, from the upper side: z = (upper profit - lower profit) / 2 at its
T-10 quote, i.e. the return of backing the upper side minus half the vig
asymmetry; z > 0 means backing the upper side paid.

Similarity spaces (Royal Asian trajectory only, no T-10 price level):
  features   the turning-point / back-and-forth features of turning_points.py
             without level, depth, line type and water; standardised on the pool
  weighted   the same, each feature scaled by |corr(feature, z)| on the pool
  path       mu_up at T-360, -240, -180, -120, -90, -60, -45, -30, -20, -15
             minus mu_up at T-10 (shape relative to the final level)
Pool restriction: all lines, or the same |line| as the target.

Score (V1 form): take the k nearest past matches (kickoff + 90 min before the
target's day starts), similarity S = 1 - d / d_(k+1), weight w = S^2,
  D = 50 + 50 * sum(w z) / (alpha + sum(w)),  alpha = 20.
D > 50 backs the upper side at the T-10 quote, D < 50 the lower side.

Groups: cut points fixed on dev (02-01..07-31) -- deciles of D, and strength
bands on |D - 50|: red = strongest 10 %, orange = next 20 %, green = rest.
The configuration (space x restriction x k) is chosen on dev by the mean
monthly correlation of D - 50 with z; holdout (08-01..10-12) is evaluated once.

    python odds-trajectory/supremacy/similar_score.py
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from turning_points import CACHE, FLIP, PRICE_FEATURES, as_of, features, month_start  # noqa: E402
from supremacy import supremacy  # noqa: E402
from v1style import WINDOWS, baseline_fit, summarise, ts  # noqa: E402

OFFSETS = (360, 240, 180, 120, 90, 60, 45, 30, 20, 15)
KS = (100, 400, 1600)
ALPHA = 20.
BUFFER = 5400


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows, feats, paths = [], [], []
    for r in sorted(kept.values(), key=lambda r: r['kickoff']):
        f, s = features(r)
        times = np.array(r['tickTimes'], float)
        mu = np.array([s * supremacy(-x['home_handicap'], x['home_water'], x['away_water']) for x in r['records']])
        paths.append([as_of(times, mu, r['kickoff'] - m * 60) - mu[-1] for m in OFFSETS])
        rows.append({'kickoff': r['kickoff'], 'cutoff': r['cutoff'], 'line': r['line'], 'homeWater': r['homeWater'],
                     'awayWater': r['awayWater'], 'score': r['score'], 'upperResult': r['upperResult'],
                     'upperProfit': r['upperProfit'], 'lowerProfit': r['lowerProfit'],
                     'z': (r['upperProfit'] - r['lowerProfit']) / 2})
        feats.append([v for n, v in f.items() if n not in PRICE_FEATURES])
    return rows, np.array(feats, float), np.array(paths, float)


def scores(rows, spaces, same_line):
    """D for every target from Feb on, for every space and k; neighbours are past matches only."""
    kick = np.array([r['kickoff'] for r in rows])
    cut = np.array([r['cutoff'] for r in rows])
    depth = np.abs(np.array([r['line'] for r in rows]))
    z = np.array([r['z'] for r in rows])
    days = np.floor((kick - ts(WINDOWS['dev'][0])) / 86400).astype(int)
    out = {(sp, k): np.full(len(rows), np.nan) for sp in spaces for k in KS}
    for day in range(0, days.max() + 1):
        tgt = np.nonzero(days == day)[0]
        if not len(tgt):
            continue
        pool = np.nonzero(kick + BUFFER < cut[tgt].min())[0]
        for sp, M in spaces.items():
            P = M[pool]
            mean, std = P.mean(0), P.std(0)
            std[std < 1e-9] = 1
            P, T = (P - mean) / std, (M[tgt] - mean) / std
            if sp == 'weighted':
                zc = z[pool] - z[pool].mean()
                wcol = np.abs(P.T @ zc) / (len(pool) * zc.std())
                P, T = P * wcol, T * wcol
            d2 = (T ** 2).sum(1)[:, None] + (P ** 2).sum(1)[None, :] - 2 * T @ P.T
            d = np.sqrt(np.maximum(d2, 0))
            if same_line:
                d[depth[tgt][:, None] != depth[pool][None, :]] = np.inf
            kmax = min(KS[-1] + 1, len(pool))
            near = np.argpartition(d, kmax - 1, axis=1)[:, :kmax]
            nd = np.take_along_axis(d, near, 1)
            order = np.argsort(nd, 1)
            near, nd = np.take_along_axis(near, order, 1), np.take_along_axis(nd, order, 1)
            nz = z[pool][near]
            for k in KS:
                kk = min(k, kmax - 1)
                ref = nd[:, kk:kk + 1]
                w = np.where(np.isfinite(nd[:, :kk]), (1 - nd[:, :kk] / np.where(np.isfinite(ref), ref, np.inf)) ** 2, 0)
                w = np.nan_to_num(w)
                out[(sp, k)][tgt] = 50 + 50 * (w * nz[:, :kk]).sum(1) / (ALPHA + w.sum(1))
    return out


def side_items(rows, idx, D, p_up):
    items = []
    for i in idx:
        r, upper = rows[i], D[i] > 50
        items.append({'result': r['upperResult'] if upper else FLIP[r['upperResult']],
                      'profit': r['upperProfit'] if upper else r['lowerProfit'],
                      'expected': p_up[i] if upper else 1 - p_up[i]})
    return summarise(items)


def upper_items(rows, idx, p_up):
    return summarise([{'result': rows[i]['upperResult'], 'profit': rows[i]['upperProfit'], 'expected': p_up[i]}
                      for i in idx])


def grouped(rows, D, kick, dev, hold, p_up, report):
    """Decile groups of D and red / orange / green strength bands on |D - 50|; cut points from dev."""
    hold_months = [(month_start(2026, m), month_start(2026, m + 1)) for m in (8, 9, 10)]
    cuts = np.quantile(D[dev], np.linspace(.1, .9, 9))
    strength = np.abs(D - 50)
    red, orange = np.quantile(strength[dev], .9), np.quantile(strength[dev], .7)
    band = np.where(strength >= red, '红', np.where(strength >= orange, '橙', '绿'))
    group = np.searchsorted(cuts, D)
    report.update({'decileCuts': [round(float(c), 3) for c in cuts],
                   'bandCuts': {'red': round(float(red), 3), 'orange': round(float(orange), 3)},
                   'deciles': {}, 'bands': {}})
    windows = {'dev': dev, 'holdout': hold, **{f'{m}月': (kick >= lo) & (kick < hi)
                                                 for m, (lo, hi) in zip((8, 9, 10), hold_months)}}
    print('\n== 按 D 十分位分组（组 1 最看好下盘 … 组 10 最看好上盘）；列为"买上盘"的结果')
    print('   组  D 区间            | 开发：场次 上盘赢盘率(提升) ROI | 留出：场次 上盘赢盘率(提升) ROI')
    edges = [-np.inf, *cuts, np.inf]
    for g in range(10):
        block = {w: upper_items(rows, np.nonzero(m & (group == g))[0], p_up) for w, m in windows.items()}
        report['deciles'][g + 1] = {'range': [float(edges[g]), float(edges[g + 1])], **block}
        cells = ' | '.join(f"{block[w]['N']:5d} {block[w]['winRate']:.3f}({block[w]['lift']:+.3f}) {block[w]['roi']:+.3f}"
                           for w in ('dev', 'holdout'))
        print(f'  {g + 1:3d}  {edges[g]:7.2f} – {edges[g + 1]:7.2f} | {cells}')
    print(f'\n== 按强度分色（红 |D−50| ≥ {red:.2f}，橙 ≥ {orange:.2f}），买评分看好的一方')
    for b in ('红', '橙', '绿'):
        block = {w: side_items(rows, np.nonzero(m & (band == b))[0], D, p_up) for w, m in windows.items()}
        report['bands'][b] = block
        print(f'  {b}  ' + ' | '.join(f"{w} {v['N']:5d} 场 {v['winRate']:.3f}±{v['winSE']:.3f}({v['lift']:+.3f}) ROI {v['roi']:+.3f}"
                                     for w, v in block.items()))


def main():
    rows, F, Pth = build()
    kick = np.array([r['kickoff'] for r in rows])
    z = np.array([r['z'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    p_up = baseline_fit(rows, dev)
    spaces = {'features': F, 'weighted': F, 'path': Pth}
    all_scores = {}
    for same in (False, True):
        for (sp, k), D in scores(rows, spaces, same).items():
            all_scores[(sp, 'sameLine' if same else 'allLines', k)] = D
    dev_months = [(month_start(2026, m), month_start(2026, m + 1)) for m in range(2, 8)]
    hold_months = [(month_start(2026, m), month_start(2026, m + 1)) for m in (8, 9, 10)]

    def monthly(D, months):
        return [float(np.corrcoef(D[(kick >= lo) & (kick < hi)] - 50, z[(kick >= lo) & (kick < hi)])[0, 1])
                for lo, hi in months]

    report = {'method': __doc__.split('\n\n')[1:6], 'configs': {}}
    print('配置（空间 / 邻居范围 / k）：开发段逐月相关均值 | 留出段相关（8/9/10 月）')
    for key, D in all_scores.items():
        dm, hm = monthly(D, dev_months), monthly(D, hold_months)
        hc = float(np.corrcoef(D[hold] - 50, z[hold])[0, 1])
        report['configs']['/'.join(map(str, key))] = {'devMonthly': [round(c, 4) for c in dm],
                                                      'devMean': round(float(np.mean(dm)), 4),
                                                      'holdout': round(hc, 4), 'holdoutMonthly': [round(c, 4) for c in hm]}
        print(f"  {key[0]:8s} {key[1]:8s} k={key[2]:5d}: 开发 {np.mean(dm):+.4f}（{' '.join(f'{c:+.3f}' for c in dm)}）"
              f" | 留出 {hc:+.4f}（{' '.join(f'{c:+.3f}' for c in hm)}）")
    best = max(all_scores, key=lambda k: report['configs']['/'.join(map(str, k))]['devMean'])
    D = all_scores[best]
    report['chosen'] = '/'.join(map(str, best))
    print(f'\n开发段选出：{report["chosen"]}')

    grouped(rows, D, kick, dev, hold, p_up, report)
    (HERE / 'results-similar-score.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
