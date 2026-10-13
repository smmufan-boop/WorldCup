"""Splitting the expected goals with the totals line: X (upper) + Y (lower) = Z, X - Y = mu.

Two Royal T-10 prices, two unknowns:
  totals  size quote -> expected total Z (independent Poisson; de-margined over
          probability fair on the quoted line; totals_from_asian.market_lambda)
  Asian   line and waters -> expected goal difference mu at that same total Z
so the upper side (giving team; home on a level line) is expected to score
X = (Z + mu) / 2 and the lower side Y = (Z - mu) / 2. The identity
mu = Z - 2Y = 2X - Z holds exactly, so there is no internal deviation; a
deviation appears only against the actual score or against a third price.

  1. total assumption  the margin error with mu at 2.7 vs at the real Z, by level and by Z
  2. per team          actual goals - X and actual goals - Y by expected level; note
                       e_X = (e_total + e_margin) / 2 and e_Y = (e_total - e_margin) / 2
  3. third price       Royal euro 1X2 -> (mu_E, Z_E), hence X_E and Y_E; deviations from
                       (mu, Z) centred on their dev medians, against the actual errors, and
                       backing the Asian side / over-under they point to at the T-10 quotes
  4. draw              euro draw probability minus the Poisson draw probability at (X, Y):
                       actual draw rate and the totals result by quintile (dev cut points)
Windows: dev 02-01..07-31, holdout 08-01..10-12. Totals and euro come from the
euro-totals partial snapshot, which is not a random sample.

    python odds-trajectory/supremacy/team_goals.py
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
from euro_asia import euro_fit, euro_quotes, state_at  # noqa: E402
from supremacy import _goal_difference, supremacy  # noqa: E402
from totals_from_asian import market_lambda, royal_totals_t10, settle_over  # noqa: E402
from v1style import WINDOWS, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}


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
    recs = sorted(kept.values(), key=lambda r: r['kickoff'])
    ids = {r['matchId'] for r in recs}
    totals = royal_totals_t10(ids, {r['matchId']: r['kickoff'] for r in recs})
    euro = euro_quotes(ids)
    rows = []
    for r in recs:
        q = totals.get(r['matchId'])
        if q is None:
            continue
        z = market_lambda(*q)
        s = 1 if r['line'] >= 0 else -1
        mu27 = s * supremacy(r['line'], r['homeWater'], r['awayWater'])
        mu = s * supremacy(r['line'], r['homeWater'], r['awayWater'], z)
        up, low = (r['score'][0], r['score'][1]) if s == 1 else (r['score'][1], r['score'][0])
        x, y = (z + mu) / 2, (z - mu) / 2
        prob, g = _goal_difference(mu, z)
        row = {'kickoff': r['kickoff'], 'Z': z, 'mu27': mu27, 'mu': mu, 'X': x, 'Y': y, 'up': up, 'low': low,
               'eT': up + low - z, 'eM': up - low - mu, 'eM27': up - low - mu27, 'eX': up - x, 'eY': low - y,
               'draw': up == low, 'pDrawPoisson': float(prob[g == 0][0]),
               'result': r['upperResult'], 'upProfit': r['upperProfit'], 'lowProfit': r['lowerProfit'],
               'totals': settle_over(*q, up + low)}
        e10 = state_at(euro.get(r['matchId'], []), r['kickoff'] - 600)
        if e10 is not None:
            mu_e, z_e, _ = euro_fit(*e10)
            inv = 1 / np.array(e10, float)
            row.update({'muE': s * mu_e, 'ZE': z_e, 'pDrawEuro': float(inv[1] / inv.sum())})
        rows.append(row)
    return rows


def mean_se(v):
    v = np.asarray(v, float)
    return float(v.mean()), float(v.std() / math.sqrt(len(v)))


def table(rows, windows, key, cuts, names, cols, title):
    """Mean +/- SE of each error column per category of ``key`` in each window; combined z."""
    print(f'\n  [{title}]  ' + ' | '.join(f'{w}: 场次 ' + ' '.join(c[1] for c in cols) for w in windows))
    out = {}
    for name in names:
        res = {}
        for w, part in windows.items():
            sel = [x for x in part if bucket(x[key], cuts, names) == name]
            if len(sel) < 80:
                break
            res[w] = {'n': len(sel), **{c[0]: mean_se([x[c[0]] for x in sel]) for c in cols}}
        if len(res) < len(windows):
            continue
        out[name] = res
        cells = []
        for w, v in res.items():
            cells.append(f"{v['n']:5d} " + ' '.join(f'{v[c[0]][0]:+.3f}±{v[c[0]][1]:.3f}' for c in cols))
        z = {c[0]: sum(v[c[0]][0] / v[c[0]][1] for v in res.values()) / math.sqrt(len(res)) for c in cols}
        flags = [f'{c[1]} z={z[c[0]]:+.1f}' for c in cols
                 if abs(z[c[0]]) >= 3 and np.sign(res['dev'][c[0]][0]) == np.sign(res['holdout'][c[0]][0])]
        print(f'    {name:12s} ' + ' | '.join(cells) + ('   ← ' + '，'.join(flags) if flags else ''))
    return out


def bets(items):
    dec = [x for x in items if x[0] != 'push']
    if len(dec) < 30:
        return None
    w = sum(x[0] in ('win', 'half_win') for x in dec) / len(dec)
    return {'n': len(items), 'winRate': round(w, 4), 'winSE': round(math.sqrt(w * (1 - w) / len(dec)), 4),
            'roi': round(sum(x[1] for x in items) / len(items), 4)}


def main():
    rows = build()
    windows = {n: [x for x in rows if ts(a) <= x['kickoff'] < ts(b)] for n, (a, b) in WINDOWS.items()}
    report = {'method': __doc__.split('\n\n')[1:4], 'n': {w: len(v) for w, v in windows.items()}}
    print(f"有 Royal 亚盘 + 大小球 T-10 报价且有赛果：开发 {len(windows['dev'])} 场，留出 {len(windows['holdout'])} 场")
    for w, part in windows.items():
        z, x, y = (np.mean([r[k] for r in part]) for k in ('Z', 'X', 'Y'))
        print(f"  {w}: 平均 Z {z:.2f}（上盘 X {x:.2f} + 下盘 Y {y:.2f}）；实际 {np.mean([r['up'] + r['low'] for r in part]):.2f}"
              f"（上盘 {np.mean([r['up'] for r in part]):.2f} + 下盘 {np.mean([r['low'] for r in part]):.2f}）")

    print('\n== 1. 总进球假设：净胜球误差（实际 − μ），μ 按 2.7 换算 vs 按大小球的 Z 换算')
    cols = (('eM27', 'μ@2.7'), ('eM', 'μ@Z'))
    report['totalAssumption'] = {
        'byMu': table(rows, windows, 'mu27', (.25, .5, .75, 1, 1.5), ('μ<0.25', '0.25–0.5', '0.5–0.75', '0.75–1',
                                                                      '1–1.5', '≥1.5'), cols, '按上盘 μ@2.7'),
        'byZ': table(rows, windows, 'Z', (2.3, 2.6, 2.9, 3.2), ('Z≤2.3', '2.3–2.6', '2.6–2.9', '2.9–3.2', '>3.2'),
                     cols, '按大小球 Z')}

    print('\n== 2. 拆到每一方：实际进球 − 预期（e_X 上盘、e_Y 下盘、e_T 总数、e_M 净胜）')
    cols = (('eX', 'e_X'), ('eY', 'e_Y'), ('eT', 'e_T'), ('eM', 'e_M'))
    report['perTeam'] = {
        'byX': table(rows, windows, 'X', (1, 1.25, 1.5, 1.75, 2.25), ('X≤1', '1–1.25', '1.25–1.5', '1.5–1.75',
                                                                     '1.75–2.25', '>2.25'), cols, '按上盘预期 X'),
        'byY': table(rows, windows, 'Y', (.6, .8, 1, 1.2), ('Y≤0.6', '0.6–0.8', '0.8–1', '1–1.2', '>1.2'), cols,
                     '按下盘预期 Y')}
    print('\n  能否下注：按 T-10 报价买上盘亚盘、买小球（分组同上）')
    report['perTeamBets'] = {}
    for key, cuts, names in (('X', (1, 1.25, 1.5, 1.75, 2.25), ('X≤1', '1–1.25', '1.25–1.5', '1.5–1.75', '1.75–2.25', '>2.25')),
                             ('Y', (.6, .8, 1, 1.2), ('Y≤0.6', '0.6–0.8', '0.8–1', '1–1.2', '>1.2'))):
        for name in names:
            res = {}
            for w, part in windows.items():
                sel = [x for x in part if bucket(x[key], cuts, names) == name]
                res[w] = {'upperAsian': bets([(x['result'], x['upProfit']) for x in sel]),
                          'under': bets([(FLIP[x['totals'][0]], x['totals'][2]) for x in sel])}
            if any(v['upperAsian'] is None for v in res.values()):
                continue
            report['perTeamBets'][name] = res
            print(f'    {name:10s} ' + ' | '.join(
                f"{w} 上盘 {v['upperAsian']['winRate']:.3f} ROI {v['upperAsian']['roi']:+.3f}，小球 {v['under']['winRate']:.3f} ROI {v['under']['roi']:+.3f}"
                for w, v in res.items()))
    report['errorCorr'] = {}
    for w, part in windows.items():
        ex, ey = np.array([r['eX'] for r in part]), np.array([r['eY'] for r in part])
        report['errorCorr'][w] = round(float(np.corrcoef(ex, ey)[0, 1]), 4)
    print(f"  e_X 与 e_Y 的相关（独立泊松下应为 0）：开发 {report['errorCorr']['dev']:+.3f}，留出 {report['errorCorr']['holdout']:+.3f}")

    print('\n== 3. 第三个价格：Royal 欧指 → (μ_E, Z_E)，与亚盘 + 大小球拆出的 (μ, Z) 比较')
    euro_w = {w: [x for x in part if 'muE' in x] for w, part in windows.items()}
    med_m = float(np.median([x['muE'] - x['mu'] for x in euro_w['dev']]))
    med_t = float(np.median([x['ZE'] - x['Z'] for x in euro_w['dev']]))
    for part in euro_w.values():
        for x in part:
            x['dM'], x['dT'] = x['muE'] - x['mu'] - med_m, x['ZE'] - x['Z'] - med_t
            x['dX'], x['dY'] = (x['dT'] + x['dM']) / 2, (x['dT'] - x['dM']) / 2
    print(f"  有欧指：开发 {len(euro_w['dev'])} 场，留出 {len(euro_w['holdout'])} 场；开发段中位数 μ_E−μ {med_m:+.3f}，Z_E−Z {med_t:+.3f}（已扣除）")
    report['euro'] = {'n': {w: len(v) for w, v in euro_w.items()}, 'devMedian': {'dM': med_m, 'dT': med_t},
                      'corr': {}, 'asianBets': {}, 'totalsBets': {}}
    for dev_key, err_key, label in (('dM', 'eM', '欧指−亚盘 净胜 vs 净胜误差'), ('dT', 'eT', '欧指−大小球 总数 vs 总数误差'),
                                    ('dX', 'eX', '欧指−拆分 上盘进球 vs 上盘误差'), ('dY', 'eY', '欧指−拆分 下盘进球 vs 下盘误差')):
        cs = {w: round(float(np.corrcoef([x[dev_key] for x in part], [x[err_key] for x in part])[0, 1]), 4)
              for w, part in euro_w.items()}
        report['euro']['corr'][label] = cs
        print(f"  {label}：相关 开发 {cs['dev']:+.3f}（SE {1 / math.sqrt(len(euro_w['dev'])):.3f}），"
              f"留出 {cs['holdout']:+.3f}（SE {1 / math.sqrt(len(euro_w['holdout'])):.3f}）")
    for th in (0., .1, .2, .3):
        a_cells, t_cells = [], []
        for w, part in euro_w.items():
            a = [(x['result'], x['upProfit']) if x['dM'] > 0 else (FLIP[x['result']], x['lowProfit'])
                 for x in part if abs(x['dM']) >= th]
            t = [(x['totals'][0], x['totals'][1]) if x['dT'] > 0 else (FLIP[x['totals'][0]], x['totals'][2])
                 for x in part if abs(x['dT']) >= th]
            report['euro']['asianBets'].setdefault(th, {})[w] = bets(a)
            report['euro']['totalsBets'].setdefault(th, {})[w] = bets(t)
            for cells, b in ((a_cells, bets(a)), (t_cells, bets(t))):
                cells.append(f"{w} {b['n']:5d} 场 {b['winRate']:.3f}±{b['winSE']:.3f} ROI {b['roi']:+.3f}" if b else f'{w} 样本少')
        print(f'  |偏差| ≥ {th:.1f}：按欧指方向买亚盘  ' + ' | '.join(a_cells))
        print('               按欧指方向买大小球 ' + ' | '.join(t_cells))

    print('\n== 4. 平局：欧指平局概率 − 泊松(X, Y) 平局概率，按开发段五分位')
    for part in euro_w.values():
        for x in part:
            x['drawExcess'] = x['pDrawEuro'] - x['pDrawPoisson']
            x['underProfit'] = x['totals'][2]
    cuts = list(np.quantile([x['drawExcess'] for x in euro_w['dev']], [.2, .4, .6, .8]))
    report['draw'] = {'cuts': [round(c, 4) for c in cuts], 'groups': {}}
    print('  组（平局溢价区间）       | 开发：场次 欧指平局 泊松平局 实际平局 e_T 买小ROI | 留出：同左')
    edges = [-1, *cuts, 1]
    for gi in range(5):
        res = {}
        for w, part in euro_w.items():
            sel = [x for x in part if edges[gi] < x['drawExcess'] <= edges[gi + 1]]
            res[w] = {'n': len(sel), 'pDrawEuro': float(np.mean([x['pDrawEuro'] for x in sel])),
                      'pDrawPoisson': float(np.mean([x['pDrawPoisson'] for x in sel])),
                      'drawRate': float(np.mean([x['draw'] for x in sel])), 'eT': mean_se([x['eT'] for x in sel]),
                      'underRoi': float(np.mean([x['underProfit'] for x in sel]))}
        report['draw']['groups'][gi + 1] = res
        print(f'  {gi + 1} ({edges[gi]:+.3f}…{edges[gi + 1]:+.3f}) | ' + ' | '.join(
            f"{v['n']:5d} {v['pDrawEuro']:.3f} {v['pDrawPoisson']:.3f} {v['drawRate']:.3f} {v['eT'][0]:+.2f}±{v['eT'][1]:.2f} {v['underRoi']:+.3f}"
            for v in res.values()))
    (HERE / 'results-team-goals.json').write_text(json.dumps(report, ensure_ascii=False, indent=1, default=float), 'utf-8')


if __name__ == '__main__':
    main()
