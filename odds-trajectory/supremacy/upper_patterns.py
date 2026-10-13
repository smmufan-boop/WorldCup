"""Which water-trajectory trends mean the upper side is really favoured, and which are traps?

Upper side = the giving team at the T-10 Royal line (home on a level line).
Labels, all from the upper side:
  weighted cover   win 1 (weight 1), half win 1 (weight 0.5), half loss 0 (weight 0.5),
                   loss 0 (weight 1), push excluded
  price-implied    the same weighted cover rate expected from mu at T-10
                   (independent Poisson, total 2.7), so lift = actual - implied
  margin vs price  upper goal margin minus upper implied supremacy, in goals;
                   > 0 the upper side beat its price (favoured), < 0 a trap

Trends (upper perspective): line move open -> T-10, water move on the final
line, their classic combinations, mu move early (open -> T-60) and late
(T-60 -> T-10), upper water at T-10, line depth. Each category is reported for
dev (02-01..07-31) and holdout (08-01..10-12). A trend is flagged only when the
margin vs price has the same sign in both windows and the combined z >= 3.

    python odds-trajectory/supremacy/upper_patterns.py
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
from supremacy import _goal_difference, final_total, record_mu  # noqa: E402
from v1style import WINDOWS, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
WEIGHT = {'win': (1, 1.), 'half_win': (1, .5), 'half_loss': (0, .5), 'loss': (0, 1.)}
_DIST = {}


def implied_weighted_cover(mu, line, total):
    """Expected weighted cover rate of the upper side on ``line`` (local sign) at supremacy ``mu``."""
    key = (round(mu, 2), round(total, 1))
    if key not in _DIST:
        _DIST[key] = _goal_difference(*key)
    prob, goals = _DIST[key]
    up_sign = 1 if line >= 0 else -1
    margin = up_sign * goals - abs(line)  # upper goal margin minus the handicap it gives
    def part(m):
        return np.sign(np.where(np.abs(m) < 1e-9, 0, m))
    if abs(line * 4) % 2 == 1:
        score = part(margin - .25) + part(margin + .25)
    else:
        score = 2 * part(margin)
    p = {s: prob[score == s].sum() for s in (2, 1, -1, -2)}  # win, half win, half loss, loss
    num = p[2] + .5 * p[1]
    return num / (num + .5 * p[-1] + p[-2])


def as_of(times, values, at):
    i = np.searchsorted(times, at, side='right') - 1
    return values[max(i, 0)]


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
        if r['upperResult'] == 'push':
            continue
        line = r['line']
        s = 1 if line >= 0 else -1  # upper is home when s = 1
        recs = r['records']
        lines = np.array([-x['home_handicap'] for x in recs])
        up_water = np.array([x['home_water'] if s == 1 else x['away_water'] for x in recs])
        mus = record_mu(r)
        times = np.array(r['tickTimes'])
        k = r['kickoff']
        final_start = len(lines) - 1
        while final_start > 0 and lines[final_start - 1] == lines[-1]:
            final_start -= 1
        line_move = s * (lines[-1] - lines[0]) / .25
        water_move = up_water[-1] - up_water[final_start]
        mu10, mu60 = mus[-1], as_of(times, mus, k - 3600)
        early, late = s * (mu60 - mus[0]), s * (mu10 - mu60)
        gd = r['score'][0] - r['score'][1]
        hit, weight = WEIGHT[r['upperResult']]
        line_name = '升盘' if line_move > 0 else '降盘' if line_move < 0 else '盘不变'
        water_name = '降水' if water_move <= -.03 else '升水' if water_move >= .03 else '水平'

        def direction(x):
            return '升' if x >= .03 else '降' if x <= -.03 else '平'
        depth = abs(line)
        rows.append({
            'kickoff': k, 'hit': hit, 'weight': weight, 'profit': r['upperProfit'],
            'rawWin': r['upperResult'] in ('win', 'half_win'),
            'implied': implied_weighted_cover(mu10, line, final_total(r)),
            'margin': s * (gd - mu10),
            '盘口变化': line_name,
            '末段水位(上盘)': water_name,
            '盘水组合': line_name + water_name,
            '早段μ(初盘→T-60)': bucket(early, (-.10, -.03, .03, .10), ('大幅走弱', '走弱', '平稳', '走强', '大幅走强')),
            '临场μ(T-60→T-10)': bucket(late, (-.10, -.03, .03, .10), ('大幅走弱', '走弱', '平稳', '走强', '大幅走强')),
            '早晚组合': f'早{direction(early)}晚{direction(late)}',
            'T-10上盘水位': bucket(up_water[-1], (.80, .90, 1.0), ('<0.80', '0.80–0.90', '0.90–1.00', '≥1.00')),
            '盘口深度': '平手' if depth == 0 else bucket(depth, (.25, .5, .75, 1.0), ('平半', '半球', '半一', '一球', '一球以上')),
        })
    return rows


def stats(items):
    if not items:
        return None
    w = np.array([x['weight'] for x in items])
    hit = np.array([x['hit'] for x in items])
    margin = np.array([x['margin'] for x in items])
    implied = np.array([x['implied'] for x in items])
    return {'n': len(items), 'weightedCover': float((w * hit).sum() / w.sum()), 'implied': float(implied.mean()),
            'winRate': float(np.mean([x['rawWin'] for x in items])),
            'roi': float(np.mean([x['profit'] for x in items])),
            'roiSE': float(np.std([x['profit'] for x in items]) / math.sqrt(len(items))),
            'marginVsPrice': float(margin.mean()), 'marginSE': float(margin.std() / math.sqrt(len(items)))}


def main():
    rows = build()
    windows = {n: [x for x in rows if ts(a) <= x['kickoff'] < ts(b)] for n, (a, b) in WINDOWS.items()}
    features = ['盘口变化', '末段水位(上盘)', '盘水组合', '早段μ(初盘→T-60)', '临场μ(T-60→T-10)', '早晚组合',
                'T-10上盘水位', '盘口深度']
    report = {'labels': __doc__.split('\n\n')[1], 'n': {n: len(v) for n, v in windows.items()}, 'features': {}}
    flagged = []
    for name, part in windows.items():
        s = stats(part)
        print(f"{name}: {s['n']} 场，上盘加权赢盘率 {s['weightedCover']:.3f}，价格隐含 {s['implied']:.3f}，"
              f"上盘净胜超出价格 {s['marginVsPrice']:+.3f} 球")
    for f in features:
        cats = sorted({x[f] for x in rows})
        block = {}
        print(f'\n== {f}')
        print('  类别            | 开发段：场次 赢盘率(隐含) 超出价格±SE        | 留出段：场次 赢盘率(隐含) 超出价格±SE        | 判断')
        for c in cats:
            res = {n: stats([x for x in part if x[f] == c]) for n, part in windows.items()}
            if any(v is None or v['n'] < 100 for v in res.values()):
                continue
            z = [v['marginVsPrice'] / v['marginSE'] for v in res.values()]
            combined = sum(z) / math.sqrt(len(z))
            same_sign = all(v > 0 for v in z) or all(v < 0 for v in z)
            verdict = ''
            if same_sign and abs(combined) >= 3:
                verdict = '看好（上盘打出）' if combined > 0 else '诱盘（上盘不打出）'
                flagged.append({'feature': f, 'category': c, 'verdict': verdict, 'combinedZ': round(combined, 2), **res})
            elif same_sign and abs(combined) >= 2:
                verdict = '倾向' + ('看好' if combined > 0 else '诱盘') + '（证据弱）'
            block[c] = {**res, 'combinedZ': round(combined, 2), 'verdict': verdict}
            cells = ' | '.join(f"{v['n']:5d} {v['weightedCover']:.3f}({v['implied']:.3f}) {v['marginVsPrice']:+.3f}±{v['marginSE']:.3f}"
                               for v in res.values())
            print(f'  {c:14s} | {cells} | z={combined:+.1f} {verdict}')
        report['features'][f] = block
    report['flagged'] = flagged
    (HERE / 'results-upper-patterns.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    print('\n明确的趋势（两段同号且合并 z ≥ 3）：')
    for x in flagged:
        print(f"  {x['feature']} = {x['category']}: {x['verdict']}  z={x['combinedZ']}")
    if not flagged:
        print('  无')
    print('\n按 T-10 Royal 报价买上盘（赢盘率：全赢、赢半都算赢）：')
    for x in flagged:
        cells = ' | '.join(f"{n}: 赢盘率 {x[n]['winRate']:.3f}，ROI {x[n]['roi']:+.3f}±{x[n]['roiSE']:.3f}（{x[n]['n']} 场）"
                           for n in WINDOWS)
        print(f"  {x['category']:8s} {cells}")


if __name__ == '__main__':
    main()
