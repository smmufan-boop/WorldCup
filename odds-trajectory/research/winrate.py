"""Re-score every studied variant by win rate, the user's evaluation criterion.

Win rate = (win + half_win) / (win + half_win + half_loss + loss); push excluded.
Because a half win counts as a full win, quarter lines alone move win rate:
taking the receiver on x.25 lines and the giver on x.75 lines wins ~55%
without any model. Every strategy is therefore compared with a price-only
baseline: P(upper wins) from logit(de-margined upper probability) with a
separate intercept and slope for each line fraction (0, .25, .5, .75), fitted on
the dev window of all matches at that stage. Lift = actual - baseline expected.
Variants are selected by dev lift and reported unchanged on holdout.

    python odds-trajectory/research/winrate.py   (after the other research scripts)
"""
from __future__ import annotations

import itertools
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cross_company import logistic, p_home  # noqa: E402
from reliability import DEV, HOLDOUT, direction, frame_for, instance_marks, load, pattern_gate  # noqa: E402

CACHE = HERE / 'cache'
WINDOWS = {'dev': DEV, 'holdout': HOLDOUT}
FRACTIONS = (0, 1, 2, 3)  # quarter-goal remainder of |line|: 0 whole, 1 .25, 2 .5, 3 .75


def upper_features(line, home_water, away_water):
    line = np.asarray(line, float)
    up_home = line >= 0
    p_up = np.where(up_home, p_home(home_water, away_water), 1 - p_home(home_water, away_water))
    frac = np.round(np.abs(line) * 4).astype(int) % 4
    logit = np.log(p_up / (1 - p_up))
    return np.column_stack([*(frac == f for f in FRACTIONS), *((frac == f) * logit for f in FRACTIONS)]).astype(float)


class Baseline:
    """Price + line-fraction model of P(upper side wins), fitted on dev."""

    def __init__(self, stage):
        rows = [r for r in pickle.load(open(CACHE / f'inputs-company3-T{stage}.pkl', 'rb'))
                if 'score' in r and r['upperResult'] != 'push']
        df = pd.DataFrame({'kickoff': [r['kickoff'] for r in rows], 'line': [r['line'] for r in rows],
                           'hw': [r['homeWater'] for r in rows], 'aw': [r['awayWater'] for r in rows],
                           'y': [float(r['upperResult'] in ('win', 'half_win')) for r in rows]})
        dt = pd.to_datetime(df.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
        fit = df[(dt >= pd.Timestamp(DEV[0])) & (dt < pd.Timestamp(DEV[1]))]
        self.w = logistic(upper_features(fit.line, fit.hw, fit.aw), fit.y.to_numpy())

    def p_upper(self, line, home_water, away_water):
        return 1 / (1 + np.exp(-upper_features(line, home_water, away_water) @ self.w))


def score(won, expected):
    ok = ~np.isnan(won)
    won, expected = won[ok], expected[ok]
    n = len(won)
    if n == 0:
        return {'N': 0}
    lift = won - expected
    return {'N': int(n), 'winRate': round(float(won.mean()), 4), 'baseline': round(float(expected.mean()), 4),
            'lift': round(float(lift.mean()), 4), 'liftSE': round(float(lift.std() / np.sqrt(n)), 4) if n > 1 else None}


def by_window(frame, won_col, exp_col):
    out = {}
    for label, (a, b) in WINDOWS.items():
        x = frame[(frame.dt >= pd.Timestamp(a)) & (frame.dt < pd.Timestamp(b))]
        out[label] = score(x[won_col].to_numpy(float), x[exp_col].to_numpy(float))
        f = x[x.firstTier]
        out[label + 'FirstTier'] = score(f[won_col].to_numpy(float), f[exp_col].to_numpy(float))
    return out


def picks_from(frame, base):
    """frame from reliability.frame_for: add baseline expected win of the picked side."""
    x = frame[frame.bet].copy()
    p_up = base.p_upper(x.line, x.homeWater, x.awayWater)
    x['expected'] = np.where(x.D > 50, p_up, 1 - p_up)
    return x


def main():
    report = {'definition': '(win+half_win)/(win+half_win+half_loss+loss), push excluded',
              'baseline': 'logit P(upper) ~ per line fraction intercept + slope on logit(de-margined upper prob), dev fit'}
    base10 = Baseline(10)

    # Trivial rules: sanity check that the baseline absorbs line-type effects.
    rows = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    t = pd.DataFrame({'kickoff': [r['kickoff'] for r in rows], 'line': [r['line'] for r in rows],
                      'homeWater': [r['homeWater'] for r in rows], 'awayWater': [r['awayWater'] for r in rows],
                      'firstTier': [r['firstTier'] for r in rows], 'res': [r['upperResult'] for r in rows]})
    t['dt'] = pd.to_datetime(t.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    t = t[t.res != 'push']
    up_won = t.res.isin(['win', 'half_win']).to_numpy(float)
    p_up = base10.p_upper(t.line, t.homeWater, t.awayWater)
    frac = np.round(t.line.abs() * 4).astype(int) % 4
    up_w = np.where(t.line >= 0, t.homeWater, t.awayWater)
    lo_w = np.where(t.line >= 0, t.awayWater, t.homeWater)
    trivial = {}
    for name, take_up in (('alwaysUpper', np.ones(len(t), bool)), ('alwaysLower', np.zeros(len(t), bool)),
                          ('lowerWaterSide', up_w <= lo_w),
                          ('quarterHalfWinSide', np.where(frac == 1, False, np.where(frac == 3, True, up_w <= lo_w)))):
        t['won'] = np.where(take_up, up_won, 1 - up_won)
        t['exp'] = np.where(take_up, p_up, 1 - p_up)
        trivial[name] = by_window(t, 'won', 'exp')
    report['trivialRules'] = trivial

    # V1, instance marks and pattern gate at T-10.
    inputs, targets, pairs, z, buckets = load('company3-T10')
    targets['dt'] = pd.to_datetime(targets.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    pairs = instance_marks(pairs)
    v1 = targets.set_index('idx').score.dropna()
    report['V1_T10'] = by_window(picks_from(frame_for(targets, v1, z), base10), 'won', 'expected')

    grid = []
    for prior, gamma, prune in itertools.product((5, 10, 20), (0.5, 1, 2), (False, True)):
        res = by_window(picks_from(frame_for(targets, direction(pairs, prior, gamma, prune), z), base10), 'won', 'expected')
        grid.append({'params': {'prior': prior, 'gamma': gamma, 'prune': prune}, **res})
    report['instanceMarksSelectedByDevLift'] = max(grid, key=lambda g: g['dev']['lift'])
    report['instanceMarksGrid'] = grid

    gates = []
    for prior, threshold in itertools.product((10, 30), (.52, .55, .58)):
        rate = pattern_gate(targets, buckets, z, prior)
        gate = {k: v[0] >= threshold for k, v in rate.items()}
        res = by_window(picks_from(frame_for(targets, v1, z, gate), base10), 'won', 'expected')
        gates.append({'params': {'gatePrior': prior, 'threshold': threshold}, **res})
    report['patternGateSelectedByDevLift'] = max(gates, key=lambda g: g['dev']['lift'] if g['dev']['N'] >= 300 else -9)
    report['patternGateGrid'] = gates

    # Cross-company at T-10: follow / fade 澳彩 when the equivalent-handicap gap is large.
    cross = pd.read_pickle(CACHE / 'cross-T10.pkl')
    cross['dt'] = pd.to_datetime(cross.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    p_up = base10.p_upper(cross.line, cross.homeWater, cross.awayWater)
    p_home_model = np.where(cross.line >= 0, p_up, 1 - p_up)
    cc = {}
    for lo in (.05, .10, .20):
        for name, take_home in (('followMacau', cross.gap > 0), ('fadeMacau', cross.gap < 0)):
            x = cross.assign(won=np.where(take_home, cross.homeWon, 1 - cross.homeWon),
                             exp=np.where(take_home, p_home_model, 1 - p_home_model))
            cc[f'{name}|gap|>{lo:.2f}'] = by_window(x[x.gap.abs() > lo], 'won', 'exp')
    report['crossCompanyT10'] = cc

    # Earlier stages with their own baseline.
    stages = {}
    for stage in (120, 60, 30):
        base = Baseline(stage)
        _, tg, _, zz, _ = load(f'company3-T{stage}')
        tg['dt'] = pd.to_datetime(tg.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
        stages[f'V1_T{stage}'] = by_window(picks_from(frame_for(tg, tg.set_index('idx').score.dropna(), zz), base),
                                           'won', 'expected')
    report['earlierStages'] = stages

    (HERE / 'results').mkdir(exist_ok=True)
    path = HERE / 'results' / 'winrate-royal.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')

    def line(name, res):
        cells = [f"{w}: {res[w]['winRate']:.3f} vs {res[w]['baseline']:.3f} → {res[w]['lift']:+.3f}±{res[w]['liftSE']:.3f} (N={res[w]['N']})"
                 for w in ('dev', 'holdout', 'holdoutFirstTier') if res[w].get('N')]
        print(f'{name:44s} ' + ' | '.join(cells))
    for k, v in trivial.items():
        line('trivial ' + k, v)
    line('V1 T-10', report['V1_T10'])
    line('instance ' + str(report['instanceMarksSelectedByDevLift']['params']), report['instanceMarksSelectedByDevLift'])
    line('gate ' + str(report['patternGateSelectedByDevLift']['params']), report['patternGateSelectedByDevLift'])
    for k, v in cc.items():
        line(k, v)
    for k, v in stages.items():
        line(k, v)


if __name__ == '__main__':
    main()
