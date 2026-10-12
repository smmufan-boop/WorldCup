"""Use the no-model quarter-line rule as the base direction, calibrate it with V1.

Rule (T-10 Royal quote): x.25 line -> receiver (a draw is a half win),
x.75 line -> giver (winning by exactly one is a half win), otherwise the
lower-water side (upper on a tie).

V1 trajectory information (T-10 and, optionally, T-120 replay) then
  agree   : bet the rule side only when V1 points the same way,
  skip    : bet the rule side unless V1 disagrees with display distance >= t,
  override: take V1's side when it disagrees with display distance >= t.
Display distance is |E - 50| with E = 50 + 50 tanh((D - 50) / 5), the same
quantity behind the green/orange/red bands (10 and 25). Thresholds are picked
on dev and reported unchanged on holdout.

Win rate = (win + half_win) / (win + half_win + half_loss + loss), push excluded.

    python odds-trajectory/research/rule_calibration.py
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from reliability import DEV, HOLDOUT  # noqa: E402
from winrate import Baseline  # noqa: E402

CACHE = HERE / 'cache'
WINDOWS = {'dev': DEV, 'holdout': HOLDOUT}
THRESHOLDS = (0, 10, 25)


def load_frame():
    rows = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    df = pd.DataFrame({'matchId': [r['matchId'] for r in rows], 'kickoff': [r['kickoff'] for r in rows],
                       'firstTier': [r['firstTier'] for r in rows], 'line': [r['line'] for r in rows],
                       'hw': [r['homeWater'] for r in rows], 'aw': [r['awayWater'] for r in rows],
                       'res': [r['upperResult'] for r in rows],
                       'upP': [r['upperProfit'] for r in rows], 'loP': [r['lowerProfit'] for r in rows]})
    df['dt'] = pd.to_datetime(df.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    frac = np.round(df.line.abs() * 4).astype(int) % 4
    df['lineType'] = frac.map({0: 'whole', 1: 'quarter25', 2: 'half', 3: 'quarter75'})
    up_w = np.where(df.line >= 0, df.hw, df.aw)
    lo_w = np.where(df.line >= 0, df.aw, df.hw)
    df['ruleUpper'] = np.where(frac == 1, False, np.where(frac == 3, True, up_w <= lo_w))
    df['upWon'] = np.where(df.res == 'push', np.nan, df.res.isin(['win', 'half_win']).astype(float))
    df['expUpper'] = Baseline(10).p_upper(df.line, df.hw, df.aw)  # price + line-type baseline
    for stage in (10, 120):
        t = pd.DataFrame(pickle.load(open(CACHE / f'targets-company3-T{stage}.pkl', 'rb')))
        t = t[t.score.notna() & ((t.score - 50).abs() > 1e-8)].set_index('matchId')
        d = df.matchId.map(t.score)
        df[f'v1Upper{stage}'] = d > 50
        df[f'v1Dist{stage}'] = (50 + 50 * np.tanh((d - 50) / 5)).round(1).sub(50).abs()
        df[f'hasV1_{stage}'] = d.notna()
    return df


def outcome(df, take_upper):
    won = np.where(take_upper, df.upWon, 1 - df.upWon)
    profit = np.where(take_upper, df.upP, df.loP)
    return won, profit


def stats(df, take_upper):
    won, profit = outcome(df, take_upper)
    expected = np.where(take_upper, df.expUpper, 1 - df.expUpper)
    ok = ~np.isnan(won)
    n = int(ok.sum())
    if n == 0:
        return {'N': 0}
    w = won[ok].mean()
    return {'N': n, 'winRate': round(float(w), 4), 'winSE': round(float(np.sqrt(w * (1 - w) / n)), 4),
            'baseline': round(float(expected[ok].mean()), 4), 'lift': round(float(w - expected[ok].mean()), 4),
            'roi': round(float(profit.mean()), 4)}


def windows(df, take_upper, mask=None):
    mask = np.ones(len(df), bool) if mask is None else np.asarray(mask)
    out = {}
    for label, (a, b) in WINDOWS.items():
        m = mask & ((df.dt >= pd.Timestamp(a)) & (df.dt < pd.Timestamp(b))).to_numpy()
        out[label] = stats(df[m], np.asarray(take_upper)[m])
        f = m & df.firstTier.to_numpy()
        out[label + 'FirstTier'] = stats(df[f], np.asarray(take_upper)[f])
    return out


def main():
    df = load_frame()
    report = {'rule': 'x.25 receiver, x.75 giver, else lower water', 'thresholdsOnDisplayDistance': THRESHOLDS}
    rule = df.ruleUpper.to_numpy()
    report['ruleAll'] = windows(df, rule)
    report['ruleByLineType'] = {lt: windows(df, rule, (df.lineType == lt).to_numpy())
                                for lt in ('whole', 'quarter25', 'half', 'quarter75')}

    for stage in (10, 120):
        has = df[f'hasV1_{stage}'].to_numpy()
        v1 = df[f'v1Upper{stage}'].to_numpy()
        dist = df[f'v1Dist{stage}'].to_numpy()
        agree = has & (v1 == rule)
        disagree = has & (v1 != rule)
        block = {'ruleOnV1Subset': windows(df, rule, has), 'agreeShare': {}}
        for label, (a, b) in WINDOWS.items():
            m = has & ((df.dt >= pd.Timestamp(a)) & (df.dt < pd.Timestamp(b))).to_numpy()
            block['agreeShare'][label] = round(float(agree[m].sum() / max(m.sum(), 1)), 4)
        block['agree_ruleSide'] = windows(df, rule, agree)
        # On disagreements: who is right, by V1 strength.
        block['disagree'] = {f'dist>={t}': {'ruleSide': windows(df, rule, disagree & (dist >= t)),
                                             'v1Side': windows(df, v1, disagree & (dist >= t))}
                             for t in THRESHOLDS}
        block['skip'] = {f'dist>={t}': windows(df, rule, has & ~(disagree & (dist >= t))) for t in THRESHOLDS}
        block['override'] = {f'dist>={t}': windows(df, np.where(disagree & (dist >= t), v1, rule), has)
                             for t in THRESHOLDS}
        by_type = {}
        for lt in ('whole', 'quarter25', 'half', 'quarter75'):
            m = (df.lineType == lt).to_numpy()
            by_type[lt] = {'agree_ruleSide': windows(df, rule, agree & m),
                           'disagree_ruleSide': windows(df, rule, disagree & m),
                           'disagree_v1Side': windows(df, v1, disagree & m)}
        block['byLineType'] = by_type
        report[f'V1_T{stage}'] = block

    # Confirmation filters: V1 only removes rule bets, it never flips them.
    has10, has120 = df.hasV1_10.to_numpy(), df.hasV1_120.to_numpy()
    agree10 = has10 & (df.v1Upper10.to_numpy() == rule)
    dis10 = has10 & (df.v1Upper10.to_numpy() != rule)
    agree120 = has120 & (df.v1Upper120.to_numpy() == rule)
    report['confirmationFilters'] = {
        'T10_agree': windows(df, rule, agree10),
        'T120_agree': windows(df, rule, agree120),
        'both_agree': windows(df, rule, agree10 & agree120),
        # Chosen after the holdout had been viewed: must be confirmed prospectively.
        'T120_agree_and_T10_not_disagree': windows(df, rule, agree120 & ~dis10),
    }

    (HERE / 'results').mkdir(exist_ok=True)
    (HERE / 'results' / 'rule-calibration.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')

    def fmt(r):
        return ' | '.join(f"{w}: {r[w]['winRate']:.3f}±{r[w]['winSE']:.3f} lift {r[w]['lift']:+.3f} ROI {r[w]['roi']:+.3f} N={r[w]['N']}"
                          for w in ('dev', 'holdout', 'holdoutFirstTier') if r[w].get('N'))
    print('rule all                 ', fmt(report['ruleAll']))
    for lt, r in report['ruleByLineType'].items():
        print(f'  rule {lt:10s}          ', fmt(r))
    print()
    for name, r in report['confirmationFilters'].items():
        print(f'filter {name:34s}', fmt(r))
    for stage in (10, 120):
        b = report[f'V1_T{stage}']
        print(f'\n=== V1 T-{stage}  agree share {b["agreeShare"]}')
        print('rule on V1 subset        ', fmt(b['ruleOnV1Subset']))
        print('agree → rule side        ', fmt(b['agree_ruleSide']))
        for t in THRESHOLDS:
            d = b['disagree'][f'dist>={t}']
            print(f'disagree dist>={t:2d} rule  ', fmt(d['ruleSide']))
            print(f'disagree dist>={t:2d} V1    ', fmt(d['v1Side']))
        for t in THRESHOLDS:
            print(f'skip     dist>={t:2d}       ', fmt(b['skip'][f'dist>={t}']))
            print(f'override dist>={t:2d}       ', fmt(b['override'][f'dist>={t}']))
        for lt, r in b['byLineType'].items():
            print(f'  {lt:10s} agree→rule    ', fmt(r['agree_ruleSide']))
            print(f'  {lt:10s} disagree→rule ', fmt(r['disagree_ruleSide']))


if __name__ == '__main__':
    main()
