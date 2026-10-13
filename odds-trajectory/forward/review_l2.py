"""Review L2 decisions in a ledger: per-match diagnostics and handicap-type breakdown.

For every L2 decision: handicap type and the chosen team's role (gives /
receives / level), its water, the opening-to-T-10 line move, V1 strengths, the
Royal move after T-10 up to the last pre-kickoff quote, 澳彩 and Bet365 at T-10
on the equivalent-handicap scale, and the goal margin against the line.
The same categories are tabulated for the historical L2 backtest so a bad run
can be compared with what each category did before.

    python odds-trajectory/forward/review_l2.py --snapshot DIR --ledger ledger.jsonl [--from ISO] [--csv out.csv]
"""
from __future__ import annotations

import argparse
import csv
import math
import pickle
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forward_rule as fr  # noqa: E402
from build_inputs import build_record  # noqa: E402
from cross_company import macau_quotes, p_home, state_at  # noqa: E402

K = 4.631  # goals per unit probability (research/cross_company.py)
RESULT_ZH = fr.RESULT_ZH


def team_line(team, line):
    """Handicap from the team's view: negative gives, positive receives."""
    return (-1 if team == 'home' else 1) * line + 0.0


def line_type(team_view):
    f = round(abs(team_view) * 4) % 4
    depth = abs(team_view)
    if depth == 0:
        return '平手 0'
    name = {0: '整球', 1: '.25', 2: '半球 .5', 3: '.75'}[f]
    role = '让' if team_view < 0 else '受让'
    return f'{name}·{role}' + ('·深盘≥1.5' if depth >= 1.5 else '')


def water_band(w):
    return '<0.85' if w < .85 else '0.85-0.95' if w <= .95 else '>0.95'


def equivalent_team(team, line, home, away):
    """Equivalent handicap strength of the team (+ = market rates it stronger)."""
    s_home = line + K * (p_home(home, away) - .5)
    return s_home if team == 'home' else -s_home


def categories(team, line, hw, aw, line_open, d120, d10):
    tv = team_line(team, line)
    water = hw if team == 'home' else aw
    steps = round(abs(line - line_open) / .25)
    strength = abs(50 + 50 * math.tanh((d120 - 50) / 5) - 50) if d120 is not None else None
    return {'盘口类型': line_type(tv),
            '水位': water_band(water),
            '初盘到T-10升盘档数': f'{steps}档' if steps < 2 else '≥2档',
            'T-10 V1': '有且同向' if d10 is not None and abs(d10 - 50) > 1e-8 else '无/中性',
            'T-120 V1色带': ('green' if strength < 10 else 'orange' if strength < 25 else 'red') if strength is not None else '-'}


def historical_rows():
    cache = fr.RESEARCH / 'cache'
    inputs = fr.dedupe_fixtures(r for r in pickle.load(open(cache / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r)
    v1 = {}
    for stage in (10, 120):
        for t in pickle.load(open(cache / f'targets-company3-T{stage}.pkl', 'rb')):
            v1.setdefault(t['matchId'], {})[stage] = {'score': t['score'], 'line': t['line']}
    start = datetime.fromisoformat('2026-02-01T00:00:00+08:00').timestamp()
    end = datetime.fromisoformat('2026-10-09T12:00:00+08:00').timestamp()  # before the reviewed days
    rows = []
    for r in inputs:
        if not start <= r['kickoff'] < end:
            continue
        line_open = -r['records'][0]['home_handicap']
        m = v1.get(r['matchId'], {})
        d = fr.decide(r['line'], r['homeWater'], r['awayWater'], line_open, m.get(10), m.get(120))
        if not d['levels']['L2']:
            continue
        result, _ = fr.settle_team(d['team'], r['line'], r['homeWater'], r['awayWater'], r['score'])
        rows.append({'result': result, **categories(d['team'], r['line'], r['homeWater'], r['awayWater'], line_open,
                                                    m[120]['score'], (m.get(10) or {}).get('score'))})
    return rows


def rate(rows):
    wins = sum(r['result'] in fr.WINS for r in rows)
    losses = sum(r['result'] in fr.LOSSES for r in rows)
    pushes = sum(r['result'] == 'push' for r in rows)
    return wins, losses, pushes, (wins / (wins + losses) if wins + losses else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--from', dest='start', default='2026-10-09T12:00:00+08:00')
    parser.add_argument('--csv')
    args = parser.parse_args()
    args.db = None
    data = fr.load_source(args)
    start = datetime.fromisoformat(args.start).timestamp()
    decisions = [e for e in fr.read_ledger(args.ledger)
                 if e['kind'] == 'decision' and e['levels']['L2'] and not e['late']
                 and datetime.fromisoformat(e['kickoff']).timestamp() >= start]
    targets = [{'matchId': e['matchId'], 'kickoff': datetime.fromisoformat(e['kickoff']).timestamp()} for e in decisions]
    books = {c: macau_quotes(targets, c) for c in ('1', '8')}

    rows = []
    for e in sorted(decisions, key=lambda x: (x['kickoff'], x['matchId'])):
        q = e['frozenQuote']
        team = e['team']
        score = fr.result_of(data, e['matchId'])
        result, profit = fr.settle_team(team, q['line'], q['homeWater'], q['awayWater'], score) if score else (None, None)
        close, _ = build_record(data, e['matchId'], 0)  # last pre-kickoff Royal quote
        s10 = equivalent_team(team, q['line'], q['homeWater'], q['awayWater'])
        after = None
        if close:
            after = equivalent_team(team, close['line'], close['homeWater'], close['awayWater']) - s10
        cutoff = datetime.fromisoformat(e['kickoff']).timestamp() - fr.DECISION
        others = {}
        for c, name in (('1', '澳彩'), ('8', 'Bet365')):
            m = books[c].get(e['matchId'])
            st = state_at(m[0], m[1], cutoff) if m else None
            others[name] = None if st is None else equivalent_team(team, *st) - s10
        tv = team_line(team, q['line'])
        margin = None
        if score:
            goals = score[0] - score[1] if team == 'home' else score[1] - score[0]
            margin = goals + tv  # > 0 covers, 0 push, -0.25/+0.25 halves
        rows.append({
            'kickoff': e['kickoff'][5:16].replace('T', ' '), 'match': f"{e['home']} v {e['away']}",
            'competition': e['competition'], 'firstTier': e['firstTier'], 'team': e['teamName'],
            'line': f'{tv:+g}', 'water': q['homeWater'] if team == 'home' else q['awayWater'],
            'open': f"{team_line(team, e['lineOpen']):+g}",
            'close': '-' if not close else f"{team_line(team, close['line']):+g}@"
                     f"{close['homeWater'] if team == 'home' else close['awayWater']:.2f}",
            'afterT10': after, 'macau': others['澳彩'], 'bet365': others['Bet365'],
            'score': '-' if not score else f'{score[0]}-{score[1]}', 'margin': margin,
            'result': result, 'profit': profit,
            **categories(team, q['line'], q['homeWater'], q['awayWater'], e['lineOpen'],
                         e['v1T120'].get('score'), e['v1T10'].get('score'))})

    def fmt(x, digits=2):
        return '-' if x is None else f'{x:+.{digits}f}'
    print('开球 | 赛事 | 对阵 | 选择 | 盘口 | 水位 | 初盘 | 收盘 | T-10后走势 | 澳彩差 | 365差 | 比分 | 净胜-盘口 | 结果')
    for r in rows:
        print(' | '.join([r['kickoff'], r['competition'], r['match'], r['team'], r['line'], f"{r['water']:.2f}",
                          r['open'], r['close'], fmt(r['afterT10']), fmt(r['macau']), fmt(r['bet365']),
                          r['score'], fmt(r['margin'], 2), RESULT_ZH[r['result']]]))
    hist = historical_rows()
    settled = [r for r in rows if r['result']]
    for key in ('盘口类型', '水位', '初盘到T-10升盘档数', 'T-10 V1', 'T-120 V1色带'):
        print(f'\n== {key}: 本期 赢-输-走 赢盘率 | 历史L2(02-01..10-09) 赢-输-走 赢盘率')
        groups = sorted({r[key] for r in settled} | {r[key] for r in hist})
        for g in groups:
            a, b = rate([r for r in settled if r[key] == g]), rate([r for r in hist if r[key] == g])
            if a[0] + a[1] + a[2] == 0 and b[0] + b[1] < 20:
                continue
            show = lambda t: f"{t[0]}-{t[1]}-{t[2]} {'-' if t[3] is None else f'{t[3]:.1%}'}"
            print(f'  {g:18s} {show(a):18s} | {show(b)}')
    late = [r for r in settled if r['afterT10'] is not None]
    for label, cond in (('T-10后继续朝所选方向', lambda r: r['afterT10'] > 1e-9),
                        ('T-10后无变化', lambda r: abs(r['afterT10']) <= 1e-9),
                        ('T-10后反向', lambda r: r['afterT10'] < -1e-9)):
        part = [r for r in late if cond(r)]
        print(f'\n{label}: {rate(part)[:3]} 赢盘率 {rate(part)[3]}')
    if args.csv:
        with open(args.csv, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == '__main__':
    main()
