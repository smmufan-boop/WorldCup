"""Does the late part of the supremacy trajectory carry information beyond the T-10 price?

Market error e = actual goal difference - mu at T-10 (the decision price).
If late moves (off-field news) are fully absorbed by T-10, no segment of the
mu path predicts e. Under-reaction gives a positive relation (follow the move),
over-reaction a negative one (fade it).

  1. direct test   per segment: correlation of the mu change with e, mean e by
                   size of move, and follow/fade rules (segment, threshold and
                   direction chosen on dev by win-rate lift, reported on holdout)
  2. segment kNN   shape of mu every 5 minutes inside the late segment (relative
                   to its start) plus the T-10 level, k nearest earlier matches
                   (kickoff + 5400 s before the target cutoff), prediction = mean
                   neighbour e; reported overall and for matches that moved

    python odds-trajectory/supremacy/segments.py
"""
from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from build_inputs import settle  # noqa: E402
from supremacy import record_mu  # noqa: E402
from v1style import WINDOWS, baseline_fit, build, summarise, ts  # noqa: E402

POINTS = {'open': None, 'T-360': 360, 'T-120': 120, 'T-60': 60, 'T-30': 30, 'T-10': 10}
SEGMENTS = [('open', 'T-360'), ('T-360', 'T-120'), ('T-120', 'T-60'), ('T-60', 'T-30'), ('T-30', 'T-10'),
            ('T-60', 'T-10'), ('T-120', 'T-10')]
BINS = [-9, -.15, -.05, -.02, .02, .05, .15, 9]
KNN_SEGMENTS = {'T-60→T-10': list(range(60, 5, -5)), 'T-120→T-10': list(range(120, 5, -5))}
K, LEVEL_WEIGHT, SETTLED_AFTER = 100, 1.0, 5400
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}

SHARED = None


def as_of(times, mus, at):
    i = np.searchsorted(times, at, side='right') - 1
    return mus[max(i, 0)]


def features(rows):
    pts = {k: [] for k in POINTS}
    grids = {k: [] for k in KNN_SEGMENTS}
    for r in rows:
        times = np.array(r['tickTimes'])
        mus = record_mu(r)
        for k, minutes in POINTS.items():
            pts[k].append(mus[0] if minutes is None else as_of(times, mus, r['kickoff'] - minutes * 60))
        for k, grid in KNN_SEGMENTS.items():
            vals = np.array([as_of(times, mus, r['kickoff'] - m * 60) for m in grid])
            grids[k].append(vals[1:] - vals[0])
    return {k: np.array(v) for k, v in pts.items()}, {k: np.array(v) for k, v in grids.items()}


def bet(rows, i, home, p_up):
    r = rows[i]
    take_upper = home == (r['line'] >= 0)
    res, up_profit, low_profit = settle(r['line'], r['homeWater'], r['awayWater'], *r['score'])
    return {'result': res if take_upper else FLIP[res], 'profit': up_profit if take_upper else low_profit,
            'expected': p_up[i] if take_upper else 1 - p_up[i]}


def knn(i):
    seg, grid, level, e, kick, cut = SHARED
    end = np.searchsorted(kick, cut[i] - SETTLED_AFTER, side='left')
    if end < K:
        return i, None
    d = ((grid[:end] - grid[i]) ** 2).sum(axis=1) + LEVEL_WEIGHT * (level[:end] - level[i]) ** 2
    near = np.argpartition(d, K)[:K]
    return i, float(e[:end][near].mean())


def main():
    global SHARED
    rows, _, arr = build()
    pts, grids = features(rows)
    e = arr['gd'] - pts['T-10']
    kick = arr['kickoff']
    win = {name: (kick >= ts(a)) & (kick < ts(b)) for name, (a, b) in WINDOWS.items()}
    p_up = baseline_fit(rows, win['dev'])
    report = {'direct': {}, 'rules': [], 'knn': {}}

    print('== 1. 各片段 μ 变化 与 市场误差（实际净胜球 − T-10 μ）')
    for a, b in SEGMENTS:
        name = f'{a}→{b}'
        move = pts[b] - pts[a]
        block = {}
        for w, m in win.items():
            moved = m & (np.abs(move) > .02)
            means = []
            for lo, hi in zip(BINS, BINS[1:]):
                sel = m & (move > lo) & (move <= hi)
                means.append((round(float(e[sel].mean()), 3) if sel.any() else None, int(sel.sum())))
            block[w] = {'corr': round(float(np.corrcoef(move[m], e[m])[0, 1]), 4),
                        'corrMoved': round(float(np.corrcoef(move[moved], e[moved])[0, 1]), 4),
                        'movedShare': round(float(moved.sum() / m.sum()), 3), 'meanErrorByMove': means}
        report['direct'][name] = block
        print(f"  {name:12s} " + ' | '.join(
            f"{w}: corr {block[w]['corr']:+.4f}, 有变化场次 {block[w]['movedShare']:.0%} 中 corr {block[w]['corrMoved']:+.4f}"
            for w in win))
    print('  按变化幅度分组的平均市场误差（T-60→T-10；区间：<-0.15, -0.15~-0.05, -0.05~-0.02, 持平, 0.02~0.05, 0.05~0.15, >0.15）')
    for w in win:
        print(f"    {w}: " + ', '.join(f'{m}(n={n})' for m, n in report['direct']['T-60→T-10'][w]['meanErrorByMove']))

    # Follow / fade rules: chosen on dev, reported on holdout.
    for a, b in SEGMENTS:
        move = pts[b] - pts[a]
        for t in (.05, .10, .20):
            for mode in ('follow', 'fade'):
                rule = {'segment': f'{a}→{b}', 'threshold': t, 'mode': mode}
                for w, m in win.items():
                    idx = np.nonzero(m & (np.abs(move) > t))[0]
                    items = [bet(rows, i, (move[i] > 0) == (mode == 'follow'), p_up) for i in idx]
                    rule[w] = summarise(items) if items else {'N': 0}
                report['rules'].append(rule)
    eligible = [r for r in report['rules'] if r['dev'].get('N', 0) >= 300 and 'lift' in r['dev']]
    best = max(eligible, key=lambda r: r['dev']['lift'])
    report['selectedRule'] = best
    print('\n== 跟随/反向规则（开发段按提升选出，留出段原样检验）')
    for r in sorted(eligible, key=lambda r: -r['dev']['lift'])[:6]:
        print(f"  {r['segment']:12s} |Δμ|>{r['threshold']:.2f} {r['mode']:6s} dev {r['dev']['winRate']:.3f}({r['dev']['lift']:+.3f}, N={r['dev']['N']})"
              f" | holdout {r['holdout'].get('winRate', float('nan')):.3f}({r['holdout'].get('lift', float('nan')):+.3f}, N={r['holdout']['N']}, ROI {r['holdout'].get('roi', float('nan')):+.3f})")

    print('\n== 2. 片段轨迹近邻（每 5 分钟取点，k=100，加 T-10 水平）')
    level = pts['T-10']
    for seg, grid in grids.items():
        scale = grid[win['dev']].std() or 1
        SHARED = (seg, grid / scale, level / level[win['dev']].std(), e, kick, arr['cutoff'])
        targets = [i for i in range(len(rows)) if kick[i] >= ts(WINDOWS['dev'][0])]
        with mp.get_context('fork').Pool() as pool:
            preds = dict(pool.imap_unordered(knn, targets, chunksize=256))
        moved_any = np.abs(grid).max(axis=1) > .02
        block = {}
        for w, m in win.items():
            idx = [i for i in np.nonzero(m)[0] if preds.get(i) is not None]
            mv = [i for i in idx if moved_any[i]]
            p = np.array([preds[i] for i in idx])
            top = [idx[j] for j in np.argsort(-np.abs(p))[:max(1, len(idx) // 10)]]
            block[w] = {'corr': round(float(np.corrcoef(p, e[idx])[0, 1]), 4),
                        'corrMoved': round(float(np.corrcoef([preds[i] for i in mv], e[mv])[0, 1]), 4),
                        'nMoved': len(mv), 'top10pct': summarise([bet(rows, i, preds[i] > 0, p_up) for i in top])}
        report['knn'][seg] = block
        for w in win:
            t10 = block[w]['top10pct']
            print(f"  {seg:11s} {w:8s} corr {block[w]['corr']:+.4f} | 片段内有变化 {block[w]['nMoved']} 场 corr {block[w]['corrMoved']:+.4f}"
                  f" | 最强 10%: {t10['winRate']:.3f}({t10['lift']:+.3f}) ROI {t10['roi']:+.3f}")
    print(f"\n相关系数的标准误约：开发段 {1 / np.sqrt(win['dev'].sum()):.4f}，留出段 {1 / np.sqrt(win['holdout'].sum()):.4f}")
    (HERE / 'results-segments.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
