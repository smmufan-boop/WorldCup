"""Test the data-measurable "gates" from manual write-ups on top of the rule.

Base direction: the no-model rule (rule_calibration.py) at the Royal T-10 quote.
Base sets: A = every rule bet; B = rule bets confirmed by V1 (T-120 agrees and
T-10 does not disagree). Each gate splits a base set into predeclared
categories; for each category we report win rate, lift over the price +
line-type baseline, and N on dev and holdout. A gate is only worth keeping when
a category beats its base set in BOTH windows.

Gates measurable with the Asian-handicap snapshot (all from the rule side's view):
  G1 marketMoveOpenToT10   equivalent handicap moved toward / against / flat since opening
  G2 marketMoveT60ToT10    same, last 50 minutes before the cutoff
  G3 lineMoveOpenToT10     line itself: toward (升盘 for the side) / against (退盘) / unchanged
  G4 ruleSideWater         T-10 water of the rule side: <0.85 / 0.85-0.95 / >0.95
  G5 macauVsRoyal          澳彩 rates the rule side stronger / weaker / equal / no quote
  G6 bet365VsRoyal         same for Bet365
  G7 competitionType       national-team or cup tournament vs league
Not measurable yet: Euro odds (欧指) consistency and the totals (大小球) line,
because the snapshot has the asia market only.

    python odds-trajectory/research/gates.py   (after rule_calibration prerequisites)
"""
from __future__ import annotations

import json
import pickle
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cross_company import macau_quotes, p_home, state_at  # noqa: E402
from rule_calibration import WINDOWS, load_frame  # noqa: E402

CACHE = HERE / 'cache'
K = 4.631
FLAT = .05  # goals on the equivalent-handicap scale
TOURNAMENT = re.compile(r'欧国联|世预|世界杯|欧洲杯|美洲杯|亚洲杯|非洲杯|金杯|国家|友谊|杯')


def equivalent(q):
    if q is None:
        return np.nan
    line, home, away = q
    return line + K * (p_home(home, away) - .5)


def three_way(x, lo='against', mid='flat', hi='toward'):
    return np.where(np.isnan(x), 'none', np.where(x > FLAT, hi, np.where(x < -FLAT, lo, mid)))


def build():
    df = load_frame()
    rows = {r['matchId']: r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r}
    targets = list(rows.values())
    books = {c: macau_quotes(targets, c) for c in ('1', '8')}
    extra = []
    for mid in df.matchId:
        r = rows[mid]
        times = np.array(r['tickTimes'])
        quotes = [(-x['home_handicap'], x['home_water'], x['away_water']) for x in r['records']]
        q10, qo = quotes[-1], quotes[0]
        q60 = state_at(times, quotes, r['cutoff'] - 3000, ordered=True)
        item = {'sR10': equivalent(q10), 'sRo': equivalent(qo), 'sR60': equivalent(q60),
                'L10': q10[0], 'Lo': qo[0], 'competition': r['competition']}
        for c, name in (('1', 'sM'), ('8', 'sB')):
            m = books[c].get(mid)
            item[name] = equivalent(state_at(m[0], m[1], r['cutoff'])) if m else np.nan
        extra.append(item)
    df = pd.concat([df.reset_index(drop=True), pd.DataFrame(extra)], axis=1)

    side_home = np.where(df.ruleUpper, df.line >= 0, df.line < 0)
    sign = np.where(side_home, 1., -1.)
    side_water = np.where(side_home, df.hw, df.aw)
    df['G1_marketMoveOpenToT10'] = three_way(sign * (df.sR10 - df.sRo))
    df['G2_marketMoveT60ToT10'] = three_way(sign * (df.sR10 - df.sR60))
    line_move = sign * (df.L10 - df.Lo)
    df['G3_lineMoveOpenToT10'] = np.where(line_move > 0, 'toward(升盘)', np.where(line_move < 0, 'against(退盘)', 'unchanged'))
    df['G4_ruleSideWater'] = np.where(side_water < .85, '<0.85', np.where(side_water <= .95, '0.85-0.95', '>0.95'))
    df['G5_macauVsRoyal'] = three_way(sign * (df.sM - df.sR10), 'weaker', 'equal', 'stronger')
    df['G6_bet365VsRoyal'] = three_way(sign * (df.sB - df.sR10), 'weaker', 'equal', 'stronger')
    df['G7_competitionType'] = np.where(df.competition.fillna('').str.contains(TOURNAMENT), 'tournament/cup', 'league')
    rule = df.ruleUpper.to_numpy()
    agree120 = df.hasV1_120.to_numpy() & (df.v1Upper120.to_numpy() == rule)
    dis10 = df.hasV1_10.to_numpy() & (df.v1Upper10.to_numpy() != rule)
    df['baseB'] = agree120 & ~dis10
    df['won'] = np.where(rule, df.upWon, 1 - df.upWon)
    df['exp'] = np.where(rule, df.expUpper, 1 - df.expUpper)
    return df


def cell(x):
    x = x[x.won.notna()]
    n = len(x)
    if n == 0:
        return {'N': 0}
    w = float(x.won.mean())
    return {'N': n, 'winRate': round(w, 4), 'winSE': round(float(np.sqrt(w * (1 - w) / n)), 4),
            'lift': round(w - float(x.exp.mean()), 4)}


def main():
    df = build()
    gates = [c for c in df.columns if c.startswith('G')]
    report = {'flatThresholdGoals': FLAT, 'K': K, 'baseSets': {}, 'gates': {}}
    consistent = []
    for base_name, base_mask in (('A_ruleAll', np.ones(len(df), bool)), ('B_ruleV1Confirmed', df.baseB.to_numpy())):
        base = df[base_mask]
        report['baseSets'][base_name] = {w: cell(base[(base.dt >= pd.Timestamp(a)) & (base.dt < pd.Timestamp(b))])
                                         for w, (a, b) in WINDOWS.items()}
        for g in gates:
            out = {}
            for cat, part in base.groupby(g):
                out[cat] = {w: cell(part[(part.dt >= pd.Timestamp(a)) & (part.dt < pd.Timestamp(b))])
                            for w, (a, b) in WINDOWS.items()}
                beats = [out[cat][w].get('N', 0) >= 100 and out[cat][w]['winRate'] > report['baseSets'][base_name][w]['winRate']
                         for w in WINDOWS]
                if all(beats):
                    consistent.append({'base': base_name, 'gate': g, 'category': cat, **out[cat]})
            report['gates'].setdefault(base_name, {})[g] = out
    report['categoriesBeatingBaseInBothWindows'] = consistent
    (HERE / 'results').mkdir(exist_ok=True)
    (HERE / 'results' / 'gates.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')

    for base_name, block in report['gates'].items():
        b = report['baseSets'][base_name]
        print(f"\n===== {base_name}: dev {b['dev']['winRate']:.3f} (N={b['dev']['N']}) | holdout {b['holdout']['winRate']:.3f} (N={b['holdout']['N']})")
        for g, cats in block.items():
            print(' ', g)
            for cat, r in cats.items():
                cells = ' | '.join(f"{w} {r[w]['winRate']:.3f}±{r[w]['winSE']:.3f} lift {r[w]['lift']:+.3f} N={r[w]['N']}"
                                   for w in WINDOWS if r[w].get('N'))
                print(f'    {cat:16s} {cells}')
    print('\nBeat base in both windows (N>=100 each):')
    for c in consistent:
        print(' ', c['base'], c['gate'], c['category'], c['dev']['winRate'], c['holdout']['winRate'], c['dev']['N'], c['holdout']['N'])


if __name__ == '__main__':
    main()
