"""How much does the original V1 T-10 signal correct the rule's direction?

For each universe (L0 = every rule bet; P2 = rule + G_V1_T120 + G_LINE, i.e. L2
before the T-10 veto) the V1 T-10 signal is classed as agree / none / disagree.
Two signal definitions:
  any      : any non-neutral V1 direction at T-10
  original : the Royal T-10 auto-registration conditions (T10_RULES.md):
             first-tier target, non-neutral, orange band requires effectiveN >= 5
Reported per class: rule-side win rate. For the disagree class also the
"correction" view: P(rule side loses | T-10 disagrees) versus the base loss
rate, the share of all rule losses caught, and the share of rule wins vetoed.

    python odds-trajectory/forward/t10_correction.py [--ledger replay.jsonl --snapshot DIR]
"""
from __future__ import annotations

import argparse
import math
import pickle
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forward_rule as fr  # noqa: E402


def band(d):
    dist = abs(round(50 + 50 * math.tanh((d - 50) / 5), 1) - 50)
    return 'green' if dist < 10 else 'orange' if dist < 25 else 'red'


def t10_class(rule_team, d10, line10, eff_n, first_tier, definition):
    team = fr.v1_team(d10, line10)
    if team is None:
        return 'none'
    if definition == 'original' and (not first_tier or (band(d10) == 'orange' and not (eff_n or 0) >= 5)):
        return 'none'
    return 'agree' if team == rule_team else 'disagree'


def history(start, end):
    cache = fr.RESEARCH / 'cache'
    inputs = fr.dedupe_fixtures(r for r in pickle.load(open(cache / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r)
    v1 = {}
    for stage in (10, 120):
        for t in pickle.load(open(cache / f'targets-company3-T{stage}.pkl', 'rb')):
            v1.setdefault(t['matchId'], {})[stage] = t
    rows = []
    for r in inputs:
        if not start <= r['kickoff'] < end:
            continue
        m = v1.get(r['matchId'], {})
        t10, t120 = m.get(10), m.get(120)
        d = fr.decide(r['line'], r['homeWater'], r['awayWater'], -r['records'][0]['home_handicap'],
                      {'score': t10['score'], 'line': t10['line']} if t10 else None,
                      {'score': t120['score'], 'line': t120['line']} if t120 else None)
        result, _ = fr.settle_team(d['team'], r['line'], r['homeWater'], r['awayWater'], r['score'])
        rows.append({'d': d, 'result': result, 'firstTier': r['firstTier'],
                     'd10': t10['score'] if t10 else None, 'line10': t10['line'] if t10 else None,
                     'effN10': t10['effectiveN'] if t10 else None})
    return rows


def replay(ledger, snapshot):
    args = argparse.Namespace(db=None, snapshot=snapshot)
    data = fr.load_source(args)
    rows = []
    for e in fr.read_ledger(ledger):
        if e['kind'] != 'decision' or e['late']:
            continue
        score = fr.result_of(data, e['matchId'])
        if score is None:
            continue
        q = e['frozenQuote']
        result, _ = fr.settle_team(e['team'], q['line'], q['homeWater'], q['awayWater'], score)
        rows.append({'d': e, 'result': result, 'firstTier': e['firstTier'], 'd10': e['v1T10'].get('score'),
                     'line10': e['v1T10'].get('line'), 'effN10': e['v1T10'].get('effectiveN')})
    return rows


def report(title, rows):
    print(f'\n==== {title}')
    for universe, keep in (('L0 全部规则注', lambda r: True),
                           ('P2 = 规则+T-120同向+升盘（T-10否决前）',
                            lambda r: r['d']['gates']['G_V1_T120'] and r['d']['gates']['G_LINE'])):
        base = [r for r in rows if keep(r) and r['result'] != 'push']
        if not base:
            continue
        lose = lambda xs: sum(x['result'] in fr.LOSSES for x in xs)
        print(f'  [{universe}] 有输赢 {len(base)} 注，规则方赢盘率 {1 - lose(base) / len(base):.1%}')
        for definition in ('any', 'original'):
            cls = {c: [r for r in base if t10_class(r['d']['team'], r['d10'], r['line10'], r['effN10'],
                                                       r['firstTier'], definition) == c]
                   for c in ('agree', 'none', 'disagree')}
            parts = []
            for c, xs in cls.items():
                if xs:
                    w = 1 - lose(xs) / len(xs)
                    parts.append(f'{c} {len(xs)}注 赢盘率{w:.1%}±{math.sqrt(w * (1 - w) / len(xs)):.1%}')
            dis = cls['disagree']
            caught = lose(dis) / max(lose(base), 1)
            vetoed_wins = (len(dis) - lose(dis)) / max(len(base) - lose(base), 1)
            label = '任意T-10方向' if definition == 'any' else '原自动登记口径'
            print(f'    {label}: ' + ' | '.join(parts))
            if dis:
                print(f'      反向时规则方输的比例 {lose(dis) / len(dis):.1%}（全体 {lose(base) / len(base):.1%}）；'
                      f'捕获全部输盘的 {caught:.1%}，同时误挡全部赢盘的 {vetoed_wins:.1%}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger')
    parser.add_argument('--snapshot')
    args = parser.parse_args()
    ts = lambda s: datetime.fromisoformat(s).timestamp()
    report('开发段 02-01..07-31', history(ts('2026-02-01T00:00:00+08:00'), ts('2026-08-01T00:00:00+08:00')))
    report('留出段 08-01..10-09', history(ts('2026-08-01T00:00:00+08:00'), ts('2026-10-09T12:00:00+08:00')))
    if args.ledger:
        report('本期回放 10-09..10-12', replay(args.ledger, args.snapshot))


if __name__ == '__main__':
    main()
