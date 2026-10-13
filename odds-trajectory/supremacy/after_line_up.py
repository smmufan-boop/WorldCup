"""After the line moves up: is the water move that follows overheated buying or real conviction?

Matches whose last Royal line change before T-10 moved the line toward the
upper side (upper = giving team at the T-10 line; on a level T-10 line, home).
From the quote just after that change to the T-10 quote:
  water move   upper water at T-10 - upper water just after the change
               (< 0: the upper side keeps being backed at the new line;
                > 0: the new line is not supported, water drifts back)
  mu move      the same in implied supremacy (supremacy.py)
  time         minutes from the change to T-10
  upgrades     number of upward line moves over the whole path

Read-out per group (dev 02-01..07-31 and holdout 08-01..10-12 separately):
  upper win rate (win + half win, push excluded), price baseline (v1style.baseline_fit,
  fitted on dev) and lift, ROI backing the upper and the lower side at the T-10
  quote, and upper goal margin minus mu_up(T-10) with its SE.
  "Overheated" = the upper side does worse than its price (negative margin, negative
  lift); "real conviction" = better than its price.

    python odds-trajectory/supremacy/after_line_up.py
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
sys.path.insert(0, str(HERE.parent / 'research'))
from supremacy import record_mu  # noqa: E402
from v1style import WINDOWS, baseline_fit, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'


def bucket(x, cuts, names):
    for c, n in zip(cuts, names):
        if x <= c:
            return n
    return names[-1]


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows = []
    for r in kept.values():
        s = 1 if r['line'] >= 0 else -1
        recs = r['records']
        lines = np.array([-x['home_handicap'] for x in recs])
        steps = s * np.diff(lines)
        changes = np.nonzero(steps != 0)[0]
        if not len(changes) or steps[changes[-1]] <= 0:
            continue  # no line change, or the last one moved against the upper side
        i = changes[-1] + 1  # first quote on the final line
        times = np.array(r['tickTimes'], float)
        up_water = np.array([x['home_water'] if s == 1 else x['away_water'] for x in recs])
        mu = s * record_mu(r)
        water_move = up_water[-1] - up_water[i]
        rows.append({
            'kickoff': r['kickoff'], 'line': r['line'], 'homeWater': r['homeWater'], 'awayWater': r['awayWater'],
            'score': r['score'], 'result': r['upperResult'], 'up': r['upperProfit'], 'low': r['lowerProfit'],
            'margin': s * (r['score'][0] - r['score'][1]) - mu[-1],
            '升盘后水位变化': bucket(water_move, (-.10, -.03, .03, .10),
                              ('大幅降水 ≤−0.10', '降水 −0.10…−0.03', '水平 ±0.03', '升水 0.03…0.10', '大幅升水 ≥0.10')),
            '升盘后μ变化': bucket(mu[-1] - mu[i], (-.10, -.03, .03, .10), ('大幅回落', '回落', '平稳', '继续上升', '大幅上升')),
            '升盘距T-10': bucket((r['cutoff'] - times[i]) / 60, (30, 120, 360), ('<30 分钟', '30–120 分钟', '2–6 小时', '>6 小时')),
            'T-10上盘水位': bucket(up_water[-1], (.80, .90, 1.0), ('≤0.80', '0.80–0.90', '0.90–1.00', '>1.00')),
            '升盘次数': {1: '1 次', 2: '2 次'}.get(int((steps > 0).sum()), '≥3 次'),
        })
    return rows


def stats(items, p_up):
    if len(items) < 60:
        return None
    dec = [(x, p) for x, p in zip(items, p_up) if x['result'] != 'push']
    w = float(np.mean([x['result'] in ('win', 'half_win') for x, _ in dec]))
    base = float(np.mean([p for _, p in dec]))
    m = np.array([x['margin'] for x in items])
    return {'n': len(items), 'winRate': round(w, 4), 'winSE': round(math.sqrt(w * (1 - w) / len(dec)), 4),
            'baseline': round(base, 4), 'lift': round(w - base, 4),
            'roiUpper': round(float(np.mean([x['up'] for x in items])), 4),
            'roiLower': round(float(np.mean([x['low'] for x in items])), 4),
            'marginVsPrice': round(float(m.mean()), 4), 'marginSE': round(float(m.std() / math.sqrt(len(m))), 4)}


def main():
    rows = build()
    kick = np.array([r['kickoff'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    p_up = baseline_fit(rows, dev)
    windows = {'dev': dev, 'holdout': hold}
    report = {'method': __doc__.split('\n\n')[1:3], 'features': {}}
    total = {w: stats([rows[i] for i in np.nonzero(m)[0]], p_up[m]) for w, m in windows.items()}
    report['all'] = total
    print('最后一次变盘为升盘的比赛：' + ' | '.join(
        f"{w} {v['n']} 场，上盘赢盘率 {v['winRate']:.3f}（基准 {v['baseline']:.3f}），超出价格 {v['marginVsPrice']:+.3f} 球"
        for w, v in total.items()))
    for f in ('升盘后水位变化', '升盘后μ变化', '升盘距T-10', 'T-10上盘水位', '升盘次数'):
        print(f'\n== {f}')
        print('  类别              | 开发：场次 上盘赢盘率(提升) 买上盘ROI 买下盘ROI 超出价格±SE | 留出：同左 | 合并 z')
        block = {}
        for c in sorted({r[f] for r in rows}):
            res = {}
            for w, m in windows.items():
                idx = np.nonzero(m & np.array([r[f] == c for r in rows]))[0]
                res[w] = stats([rows[i] for i in idx], p_up[idx])
            if any(v is None for v in res.values()):
                continue
            z = sum(v['marginVsPrice'] / v['marginSE'] for v in res.values()) / math.sqrt(2)
            same = res['dev']['marginVsPrice'] * res['holdout']['marginVsPrice'] > 0
            verdict = ''
            if same and abs(z) >= 3:
                verdict = '确实看好' if z > 0 else '过热'
            elif same and abs(z) >= 2:
                verdict = '倾向' + ('看好' if z > 0 else '过热') + '（证据弱）'
            block[c] = {**res, 'combinedZ': round(z, 2), 'verdict': verdict}
            cells = ' | '.join(f"{v['n']:5d} {v['winRate']:.3f}({v['lift']:+.3f}) {v['roiUpper']:+.3f} {v['roiLower']:+.3f} "
                               f"{v['marginVsPrice']:+.3f}±{v['marginSE']:.3f}" for v in res.values())
            print(f'  {c:16s} | {cells} | {z:+.1f} {verdict}')
        report['features'][f] = block
    (HERE / 'results-after-line-up.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
