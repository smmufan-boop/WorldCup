"""How well do expected goals taken from the Asian handicap predict total goals?

The Asian handicap price is one number per match (the cover probability), so
it identifies the expected goal difference mu, not the total; supremacy.py
assumes a total of 2.7. Any information about the total can only come from
the cross-match link between |mu| and goals (stronger favourites, more goals).

  1. all deduped Royal T-10 matches with verified 90-minute scores:
     total goals vs |mu| (Royal T-10 Asian, total 2.7); a total predicted from
     the Asian price alone, a + b|mu| + c mu^2, fitted on dev (02-01..07-31),
     evaluated on holdout (08-01..10-12)
  2. matches that also have a Royal totals (size, source 3) T-10 quote in the
     euro-totals partial snapshot: the market's expected total lambda (independent
     Poisson, de-margined over probability on the quoted line, quarter lines split)
     against the Asian-only prediction -- accuracy, whether the Asian prediction
     adds anything to the market (residual correlation), and backing over or
     under at the T-10 quote when the two disagree

    python odds-trajectory/supremacy/totals_from_asian.py
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
from market_total import jsonl, market_lambda, settle_over  # noqa: E402,F401
from supremacy import supremacy  # noqa: E402
from v1style import WINDOWS, ts  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
GAPS = (0., .1, .2, .3)


def royal_totals_t10(match_ids, kickoffs):
    """Last valid pre-match Royal totals quote at or before T-10: {matchId: (line, over_hk, under_hk)}."""
    tasks = {t[0]: t[1] for t in jsonl('tasks') if t[2] == 'size' and t[3] == '3' and t[1] in match_ids}
    best = {}
    for q in jsonl('quotes'):  # task, order, changed_at, ..., over_hk 8, total_line 9, under_hk 10, valid 11, prematch 12
        mid = tasks.get(q[0])
        if mid is None or q[11] != 1 or q[12] != 1 or q[2] is None or q[2] > kickoffs[mid] - 600:
            continue
        key = (q[2], q[1])
        if mid not in best or key > best[mid][0]:
            best[mid] = (key, (float(q[9]), float(q[8]), float(q[10])))
    return {m: v[1] for m, v in best.items()}


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows = []
    for r in kept.values():
        mu = supremacy(r['line'], r['homeWater'], r['awayWater'], 2.7)  # Asian price alone: fixed total
        rows.append({'matchId': r['matchId'], 'kickoff': r['kickoff'], 'mu': mu, 'goals': sum(r['score'])})
    return rows


def main():
    rows = build()
    kick = np.array([r['kickoff'] for r in rows])
    goals = np.array([r['goals'] for r in rows], float)
    mu = np.array([r['mu'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    X = np.column_stack([np.ones_like(mu), np.abs(mu), mu ** 2])
    coef = np.linalg.lstsq(X[dev], goals[dev], rcond=None)[0]
    asian_total = X @ coef
    report = {'method': __doc__.split('\n\n')[1:4], 'asianOnly': {'coefficients': [round(float(c), 4) for c in coef]}}
    print(f'== 1. 只用亚盘：总进球 ≈ {coef[0]:.3f} + {coef[1]:.3f}·|μ| + {coef[2]:.3f}·μ²（开发段拟合）')
    for name, m in (('dev', dev), ('holdout', hold)):
        c = float(np.corrcoef(asian_total[m], goals[m])[0, 1])
        mae_const = float(np.abs(goals[m] - goals[dev].mean()).mean())
        mae = float(np.abs(goals[m] - asian_total[m]).mean())
        report['asianOnly'][name] = {'n': int(m.sum()), 'corr': round(c, 4), 'r2': round(c * c, 4),
                                     'mae': round(mae, 4), 'maeConstant': round(mae_const, 4)}
        print(f'  {name}: {m.sum()} 场，与实际总进球相关 {c:+.3f}（R² {c * c:.3f}），平均绝对误差 {mae:.3f}（常数预测 {mae_const:.3f}）')
    print('  按 |μ| 分组的平均总进球（开发 / 留出）：')
    for lo, hi in ((0, .25), (.25, .5), (.5, .75), (.75, 1), (1, 1.5), (1.5, 9)):
        sel = (np.abs(mu) >= lo) & (np.abs(mu) < hi)
        print(f'    |μ| {lo:.2f}–{hi:.2f}: {goals[sel & dev].mean():.2f} / {goals[sel & hold].mean():.2f}（{(sel & dev).sum()} / {(sel & hold).sum()} 场）')

    ids = {r['matchId'] for r in rows}
    quotes = royal_totals_t10(ids, {r['matchId']: r['kickoff'] for r in rows})
    sub = [i for i, r in enumerate(rows) if r['matchId'] in quotes]
    print(f'\n== 2. 同时有 Royal 大小球 T-10 报价的比赛：{len(sub)} 场（开发 {int(dev[sub].sum())}，留出 {int(hold[sub].sum())}）')
    lam = np.full(len(rows), np.nan)
    for i in sub:
        lam[i] = market_lambda(*quotes[rows[i]['matchId']])
    report['withTotals'] = {}
    for name, m in (('dev', dev), ('holdout', hold), ('all', dev | hold)):
        idx = [i for i in sub if m[i]]
        if len(idx) < 100:
            continue
        g, a, k = goals[idx], asian_total[idx], lam[idx]
        resid = np.corrcoef(a - k, g - k)[0, 1]
        coef2 = np.linalg.lstsq(np.column_stack([np.ones(len(idx)), k, a]), g, rcond=None)[0]
        block = {'n': len(idx), 'corrMarket': round(float(np.corrcoef(k, g)[0, 1]), 4),
                 'corrAsian': round(float(np.corrcoef(a, g)[0, 1]), 4),
                 'maeMarket': round(float(np.abs(g - k).mean()), 4), 'maeAsian': round(float(np.abs(g - a).mean()), 4),
                 'corrAsianMinusMarketWithError': round(float(resid), 4),
                 'jointRegression': {'const': round(float(coef2[0]), 3), 'market': round(float(coef2[1]), 3),
                                     'asian': round(float(coef2[2]), 3)}, 'bets': {}}
        print(f"  [{name}] {len(idx)} 场：与实际总进球相关 大小球盘 {block['corrMarket']:+.3f} / 亚盘推算 {block['corrAsian']:+.3f}；"
              f"平均绝对误差 {block['maeMarket']:.3f} / {block['maeAsian']:.3f}")
        print(f"    亚盘推算−大小球盘 与 实际−大小球盘 的相关 {resid:+.3f}（标准误约 {1 / math.sqrt(len(idx)):.3f}）；"
              f"联合回归 总进球 = {coef2[0]:.2f} + {coef2[1]:.2f}×大小球盘 + {coef2[2]:.2f}×亚盘推算")
        for th in GAPS:
            items = []
            for i in idx:
                gap = asian_total[i] - lam[i]
                if abs(gap) < th:
                    continue
                result, over, under = settle_over(*quotes[rows[i]['matchId']], goals[i])
                if gap > 0:
                    items.append((result, over))
                else:
                    flip = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}
                    items.append((flip[result], under))
            dec = [x for x in items if x[0] != 'push']
            if len(dec) < 30:
                continue
            w = sum(x[0] in ('win', 'half_win') for x in dec) / len(dec)
            roi = sum(x[1] for x in items) / len(items)
            block['bets'][th] = {'n': len(items), 'winRate': round(w, 4), 'roi': round(roi, 4)}
            print(f'    |亚盘推算−大小球盘| ≥ {th:.1f} 球：{len(items):5d} 场，按亚盘推算方向买大/小 赢盘率 {w:.3f} '
                  f'±{math.sqrt(w * (1 - w) / len(dec)):.3f}，ROI {roi:+.3f}')
        report['withTotals'][name] = block
    (HERE / 'results-totals-from-asian.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
