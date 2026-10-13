"""Upper-side winners: which point of the water trajectory came closest to the actual result?

Upper side = giving team at the T-10 Royal line (home on a level line). For
each match, per handicap group, at every as-of point t (opening, T-1440 ...
T-10) the trajectory gives an expected goal margin for the upper side,
mu_up(t). The "fluctuation" at t is actual upper goal margin - mu_up(t).

  1. descriptive   for upper wins (win 1, half win 0.5 weight) and, as the
                   control, upper losses (loss 1, half loss 0.5): mean
                   actual - expected at T-10, mean |actual - mu_up(t)| per t,
                   and the share of matches whose closest point is t
  2. predictive    the only usable form: does mu_up(t) - mu_up(T-10), known
                   before the decision, predict the upper market error
                   (actual - mu_up(T-10))? correlation per t and handicap
                   group, dev and holdout separately

    python odds-trajectory/supremacy/upper_wins_volatility.py
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from supremacy import record_mu  # noqa: E402
from v1style import WINDOWS, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
POINTS = [('初盘', None), ('T-1440', 1440), ('T-720', 720), ('T-360', 360), ('T-180', 180), ('T-120', 120),
          ('T-60', 60), ('T-30', 30), ('T-10', 10)]
WIN_W = {'win': 1., 'half_win': .5}
LOSS_W = {'loss': 1., 'half_loss': .5}


def depth_group(line):
    d = abs(line)
    if d == 0:
        return '平手'
    return {0.25: '平半', 0.5: '半球', 0.75: '半一', 1.0: '一球'}.get(d, '一球以上')


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows = []
    for r in kept.values():
        s = 1 if r['line'] >= 0 else -1
        times = np.array(r['tickTimes'])
        mus = s * record_mu(r)
        path = []
        for _, minutes in POINTS:
            if minutes is None:
                path.append(mus[0])
            else:
                i = np.searchsorted(times, r['kickoff'] - minutes * 60, side='right') - 1
                path.append(mus[max(i, 0)])
        rows.append({'kickoff': r['kickoff'], 'group': depth_group(r['line']), 'result': r['upperResult'],
                     'actual': s * (r['score'][0] - r['score'][1]), 'path': np.array(path)})
    return rows


def describe(rows, weights):
    sel = [x for x in rows if x['result'] in weights]
    if not sel:
        return None
    w = np.array([weights[x['result']] for x in sel])
    actual = np.array([x['actual'] for x in sel])
    paths = np.array([x['path'] for x in sel])
    dev = np.abs(actual[:, None] - paths)
    closest = dev.argmin(axis=1)
    return {'n': len(sel),
            'actualMinusExpectedT10': float((w * (actual - paths[:, -1])).sum() / w.sum()),
            'meanAbsDeviation': [round(float((w * dev[:, k]).sum() / w.sum()), 3) for k in range(len(POINTS))],
            'closestShare': [round(float(w[closest == k].sum() / w.sum()), 3) for k in range(len(POINTS))]}


def main():
    rows = build()
    groups = ['平手', '平半', '半球', '半一', '一球', '一球以上']
    names = [p for p, _ in POINTS]
    report = {'points': names, 'descriptive': {}, 'predictive': {}}
    print('== 1. 描述：上盘赢（对照：上盘输）时，各时点的预期净胜球离实际结果有多远')
    for g in groups + ['全部']:
        part = rows if g == '全部' else [x for x in rows if x['group'] == g]
        win, loss = describe(part, WIN_W), describe(part, LOSS_W)
        report['descriptive'][g] = {'upperWin': win, 'upperLoss': loss}
        print(f"\n  [{g}] 上盘赢 {win['n']} 场：实际−T-10预期 {win['actualMinusExpectedT10']:+.2f} 球 | "
              f"上盘输 {loss['n']} 场：{loss['actualMinusExpectedT10']:+.2f} 球")
        print('    时点           ' + ' '.join(f'{n:>7s}' for n in names))
        print('    赢·平均偏差     ' + ' '.join(f'{v:7.3f}' for v in win['meanAbsDeviation']))
        print('    赢·最接近占比   ' + ' '.join(f'{v:7.1%}' for v in win['closestShare']))
        print('    输·最接近占比   ' + ' '.join(f'{v:7.1%}' for v in loss['closestShare']))

    print('\n== 2. 能否反过来用：mu_up(t) − mu_up(T-10)（下注前已知）与上盘市场误差的相关')
    print('    （标准误约 1/√n；只看有变化的比赛）')
    for g in groups + ['全部']:
        part = rows if g == '全部' else [x for x in rows if x['group'] == g]
        line = []
        block = {}
        for n, (a, b) in WINDOWS.items():
            sub = [x for x in part if ts(a) <= x['kickoff'] < ts(b)]
            paths = np.array([x['path'] for x in sub])
            err = np.array([x['actual'] for x in sub]) - paths[:, -1]
            corrs = []
            for k in range(len(POINTS) - 1):
                diff = paths[:, k] - paths[:, -1]
                moved = np.abs(diff) > .02
                corrs.append(round(float(np.corrcoef(diff[moved], err[moved])[0, 1]), 3) if moved.sum() > 50 else None)
            block[n] = {'n': len(sub), 'corr': corrs}
            line.append(f"{n}(n={len(sub)}, SE≈{1 / math.sqrt(len(sub)):.3f}): " +
                        ' '.join('   -  ' if c is None else f'{c:+.3f}' for c in corrs))
        report['predictive'][g] = block
        print(f'  [{g}] 时点 ' + ' '.join(f'{n:>6s}' for n in names[:-1]))
        for x in line:
            print('     ' + x)
    (HERE / 'results-upper-wins-volatility.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
