"""Where else could the win rate of forward-rule-v1 come from? Exploratory, not registered.

Builds one feature row per rule bet (L0) from the research caches, then
  1. splits L2 by candidate refinements (more V1 stages, move timing, water,
     data age, competition class, 澳彩 gap, T-120 band),
  2. tries a multi-stage V1 vote in place of the single T-120 gate,
  3. fits a logistic ranking model on dev and reports the top of the ranking
     on holdout and the most recent days, next to L2 at the same volume.
Windows: dev 02-01..07-31 (discovery), holdout 08-01..10-09 12:00, recent
10-09 12:00..10-13. Win rate counts win and half win; pushes excluded.

    python odds-trajectory/research/improve.py
"""
from __future__ import annotations

import math
import pickle
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'forward'))
import forward_rule as fr  # noqa: E402
from cross_company import logistic  # noqa: E402

CACHE = HERE / 'cache'
BLOCKED = re.compile(r'女|青年|少年|后备|後備|预备|預備|友谊|友誼|u\s*-?\s*\d{2}(?!\d)|women|youth|reserve|friendly', re.I)
STAGES = (120, 60, 30, 10)
WINDOWS = {'dev': ('2026-02-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00'),
           'holdout': ('2026-08-01T00:00:00+08:00', '2026-10-09T12:00:00+08:00'),
           'recent': ('2026-10-09T12:00:00+08:00', '2026-10-14T00:00:00+08:00')}
K = 4.631


def ts(s):
    return pd.Timestamp(s).timestamp()


def display_distance(d):
    return abs(round(50 + 50 * math.tanh((d - 50) / 5), 1) - 50)


def team_prob(team, line, hw, aw):
    ih, ia = 1 / (1 + hw), 1 / (1 + aw)
    p_home = ih / (ih + ia)
    return p_home if team == 'home' else 1 - p_home


def build():
    inputs = fr.dedupe_fixtures(r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r)
    v1 = {s: {t['matchId']: t for t in pickle.load(open(CACHE / f'targets-company3-T{s}.pkl', 'rb'))} for s in STAGES}
    line120 = {r['matchId']: r['line'] for r in pickle.load(open(CACHE / 'inputs-company3-T120.pkl', 'rb'))}
    cross = pd.read_pickle(CACHE / 'cross-T10.pkl').set_index('matchId').gap.to_dict()
    rows = []
    for r in inputs:
        if r['kickoff'] < ts(WINDOWS['dev'][0]):
            continue
        mid = r['matchId']
        line_open = -r['records'][0]['home_handicap']
        stage = {s: v1[s].get(mid) for s in STAGES}
        d = fr.decide(r['line'], r['homeWater'], r['awayWater'], line_open,
                      {'score': stage[10]['score'], 'line': stage[10]['line']} if stage[10] else None,
                      {'score': stage[120]['score'], 'line': stage[120]['line']} if stage[120] else None)
        team = d['team']
        sign = 1 if team == 'home' else -1
        tv = -sign * r['line'] + 0.0  # team view: negative gives
        frac = round(abs(tv) * 4) % 4
        ftype = 'level' if tv == 0 else {1: '.25', 3: '.75', 2: 'half', 0: 'whole'}[frac]
        result, profit = fr.settle_team(team, r['line'], r['homeWater'], r['awayWater'], r['score'])
        row = {'matchId': mid, 'kickoff': r['kickoff'], 'L1': d['levels']['L1'], 'L2': d['levels']['L2'],
               'G120': d['gates']['G_V1_T120'], 'G10': d['gates']['G_V1_T10'], 'GLINE': d['gates']['G_LINE'],
               'ftype': ftype, 'quarter': frac in (1, 3), 'deep': abs(tv) >= 1.5,
               'water': r['homeWater'] if team == 'home' else r['awayWater'],
               'pTeam': team_prob(team, r['line'], r['homeWater'], r['awayWater']),
               'expected': fr.baseline_expected(team, r['line'], r['homeWater'], r['awayWater']),
               'moveTotal': sign * (r['line'] - line_open) / .25,
               'moveLate': sign * (r['line'] - line120[mid]) / .25 if mid in line120 else np.nan,
               'moveEarly': sign * (line120[mid] - line_open) / .25 if mid in line120 else np.nan,
               'age': r['feature']['dataAgeMinutes'], 'nQuotes': r['feature']['n'],
               'blocked': bool(BLOCKED.search(r['competition'] or '')), 'firstTier': r['firstTier'],
               'macauGap': (sign * cross[mid]) if mid in cross and cross[mid] == cross[mid] else np.nan,
               'result': result, 'profit': profit,
               'won': np.nan if result == 'push' else float(result in fr.WINS)}
        for s in STAGES:
            t = stage[s]
            vt = fr.v1_team(t['score'], t['line']) if t else None
            row[f'a{s}'] = 0 if vt is None else (1 if vt == team else -1)
            row[f'dist{s}'] = display_distance(t['score']) if t and vt else 0.
        rows.append(row)
    df = pd.DataFrame(rows)
    df['window'] = None
    for name, (a, b) in WINDOWS.items():
        df.loc[(df.kickoff >= ts(a)) & (df.kickoff < ts(b)), 'window'] = name
    return df


def stat(x):
    y = x[x.won.notna()]
    if len(y) == 0:
        return '      -      '
    w = y.won.mean()
    return f"{w:5.1%}±{math.sqrt(w * (1 - w) / len(y)):4.1%} n={len(y):<4d} 提升{w - y.expected.mean():+5.1%} ROI{x.profit.mean():+6.1%}"


def table(title, df, groups):
    print(f'\n## {title}')
    for name, mask in groups:
        cells = ' | '.join(f'{w}: {stat(df[mask & (df.window == w)])}' for w in WINDOWS)
        print(f'  {name:26s} {cells}')


def main():
    df = build()
    L2 = df.L2
    table('L2 现状', df, [('L2', L2), ('L2 只保留四分之一盘', L2 & df.quarter)])

    table('L2 内：T-60、T-30 的 V1 是否同向', df, [
        ('T-60 同向', L2 & (df.a60 == 1)), ('T-60 无方向', L2 & (df.a60 == 0)), ('T-60 反向', L2 & (df.a60 == -1)),
        ('T-30 同向', L2 & (df.a30 == 1)), ('T-30 无方向', L2 & (df.a30 == 0)), ('T-30 反向', L2 & (df.a30 == -1)),
        ('T-60/T-30 都不反向', L2 & (df.a60 >= 0) & (df.a30 >= 0)),
        ('T-60/T-30 有一个反向', L2 & ((df.a60 == -1) | (df.a30 == -1)))])

    table('L2 内：升盘发生的时间', df, [
        ('只在 T-120 之前升', L2 & (df.moveEarly > 0) & (df.moveLate == 0)),
        ('T-120 之后才升', L2 & (df.moveEarly <= 0) & (df.moveLate > 0)),
        ('前后都升', L2 & (df.moveEarly > 0) & (df.moveLate > 0)),
        ('升 1 档', L2 & (df.moveTotal == 1)), ('升 ≥2 档', L2 & (df.moveTotal >= 2))])

    table('L2 内：水位与数据新鲜度', df, [
        ('水位 <0.80', L2 & (df.water < .80)), ('0.80–0.90', L2 & (df.water >= .80) & (df.water < .90)),
        ('0.90–1.00', L2 & (df.water >= .90) & (df.water < 1.0)), ('≥1.00', L2 & (df.water >= 1.0)),
        ('末报价 ≤10 分钟前', L2 & (df.age <= 10)), ('10–60 分钟前', L2 & (df.age > 10) & (df.age <= 60)),
        ('>60 分钟前', L2 & (df.age > 60))])

    table('L2 内：赛事类型、澳彩、T-120 色带', df, [
        ('青年/女足/预备/友谊', L2 & df.blocked), ('其余', L2 & ~df.blocked),
        ('一级赛事', L2 & df.firstTier), ('非一级（且非青年等）', L2 & ~df.firstTier & ~df.blocked),
        ('澳彩更不看好所选队', L2 & (df.macauGap < -.05)), ('澳彩持平', L2 & (df.macauGap.abs() <= .05)),
        ('澳彩更看好', L2 & (df.macauGap > .05)), ('无澳彩', L2 & df.macauGap.isna()),
        ('T-120 绿', L2 & (df.dist120 < 10)), ('T-120 橙', L2 & (df.dist120 >= 10) & (df.dist120 < 25)),
        ('T-120 红', L2 & (df.dist120 >= 25))])

    votes = (df[['a120', 'a60', 'a30']] == 1).sum(axis=1)
    against = (df[['a120', 'a60', 'a30', 'a10']] == -1).sum(axis=1)
    table('多阶段 V1 投票代替单一 T-120 门（都要求升盘）', df, [
        ('L2（对照）', L2),
        ('≥1 阶段同向且无反向', df.GLINE & (votes >= 1) & (against == 0)),
        ('≥2 阶段同向且无反向', df.GLINE & (votes >= 2) & (against == 0)),
        ('≥2 阶段同向且无反向·四分之一盘', df.GLINE & (votes >= 2) & (against == 0) & df.quarter)])

    # Ranking model: dev fit, applied unchanged later.
    X = pd.DataFrame({
        'const': 1.,
        **{f't_{t}': (df.ftype == t).astype(float) for t in ('.25', '.75', 'half', 'whole')},
        'deep': df.deep.astype(float),
        'logitP': np.log(df.pTeam / (1 - df.pTeam)),
        **{f'agree{s}': (df[f'a{s}'] == 1).astype(float) for s in STAGES},
        **{f'against{s}': (df[f'a{s}'] == -1).astype(float) for s in STAGES},
        'dist120': df.dist120 / 50,
        'moveTotal': df.moveTotal.clip(-3, 3), 'moveLate': df.moveLate.fillna(0).clip(-3, 3),
        'macauGap': df.macauGap.fillna(0).clip(-.5, .5), 'macauMissing': df.macauGap.isna().astype(float),
        'blocked': df.blocked.astype(float), 'firstTier': df.firstTier.astype(float),
        'logAge': np.log1p(df.age.clip(lower=0)),
    })
    fit = (df.window == 'dev') & df.won.notna()
    w = logistic(X[fit].to_numpy(), df.won[fit].to_numpy(), l2=1.0)
    df['score'] = 1 / (1 + np.exp(-X.to_numpy() @ w))
    print('\n## 排序模型（开发段拟合）系数')
    print('  ' + ', '.join(f'{k} {v:+.2f}' for k, v in zip(X.columns, w)))
    print('\n## 排序模型：每段按模型分数取前 N 注，与同段 L2 比较')
    for win in WINDOWS:
        part = df[df.window == win]
        n_l2 = int(part.L2.sum())
        for label, n in (('与 L2 同量', n_l2), ('前 2%', int(len(part) * .02)), ('前 5%', int(len(part) * .05))):
            top = part.nlargest(max(n, 1), 'score')
            print(f'  {win:8s} {label:8s} N={n:<5d} {stat(top)} | 其中四分之一盘 {top.quarter.mean():.0%}，平均水位 {top.water.mean():.2f}')
        print(f'  {win:8s} L2 对照          {stat(part[part.L2])}')
    df.to_pickle(CACHE / 'improve-features.pkl')


if __name__ == '__main__':
    main()
