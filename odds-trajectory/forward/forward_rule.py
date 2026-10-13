"""forward-rule-v1: no-model quarter-line rule, V1 trajectory confirmation, line-move gate.

The rule, gates and evaluation are fixed in rule_spec.json and RULES.md.
Shadow versions V1-四分之一, V1-多阶段 and the water-hold gate G_HOLD (L0-水位门,
L1-水位门, and 水位门否决 = the decisions it vetoes) (shadow_spec.json) are recorded
on the same decisions and evaluated in parallel; they never change v1.
V1 is called unchanged through the research builders (same cleaning, history
pool and parameters as the published V1 package).

Modes
  backtest  check the frozen rule and the shadows on the research caches (retrospective)
  scan      freeze V1 at T-120/T-60/T-30 and issue T-10 decisions into a JSONL ledger
  settle    score ledger decisions with verified 90-minute results

Sources: ``--db trajectory.sqlite3`` (a restored snapshot or the live Pankou
database with the same four tables) or ``--snapshot DIR`` (published CSV parts).

    python odds-trajectory/forward/forward_rule.py backtest
    python odds-trajectory/forward/forward_rule.py scan --db PATH --ledger ledger.jsonl [--now ISO]
    python odds-trajectory/forward/forward_rule.py settle --db PATH --ledger ledger.jsonl [--list V1-四分之一]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import gzip
import json
import math
import pickle
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent / 'research'
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(HERE.parent / 'v1' / 'code'))
import replay_v1  # noqa: E402
from build_inputs import build_record, build_records, collect, kickoff_of, result_of, settle  # noqa: E402
from direction_v1 import configuration  # noqa: E402
from matching_core import BJT  # noqa: E402

SPEC = json.loads((HERE / 'rule_spec.json').read_text('utf-8'))
SHADOW = json.loads((HERE / 'shadow_spec.json').read_text('utf-8'))
VERSION = SPEC['version']
COMPANY = SPEC['company']
DECISION = SPEC['stages']['decisionMinutesBeforeKickoff'] * 60
CONFIRM = SPEC['stages']['confirmationMinutesBeforeKickoff'] * 60
SHADOW_LEADS = tuple(m * 60 for m in SHADOW['extraStagesMinutesBeforeKickoff'])
FREEZE_LEADS = (CONFIRM, *SHADOW_LEADS)  # frozen between their cutoff and the decision
SHADOW_NAMES = tuple(v['name'] for v in SHADOW['versions'])
HOLD = SHADOW['gates']['G_HOLD']['parameters']
TOL = SPEC['v1']['neutralTolerance']
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}
WINS, LOSSES = ('win', 'half_win'), ('half_loss', 'loss')


# ---------------------------------------------------------------- the rule
def upper_team(line):
    return 'home' if line >= 0 else 'away'


def other(team):
    return 'away' if team == 'home' else 'home'


def rule_team(line, home_water, away_water):
    fraction = round(abs(line) * 4) % 4
    up = upper_team(line)
    if fraction == 1:
        return other(up)
    if fraction == 3:
        return up
    up_water, low_water = (home_water, away_water) if up == 'home' else (away_water, home_water)
    return up if up_water <= low_water else other(up)


def v1_team(score, line):
    if score is None or line is None or abs(score - 50) <= TOL:
        return None
    return upper_team(line) if score > 50 else other(upper_team(line))


def line_move(team, line_open, line_now):
    """Signed move of the local line (+ = home gives) from the team's view."""
    delta = (1 if team == 'home' else -1) * (line_now - line_open)
    return 'toward' if delta > 1e-9 else 'against' if delta < -1e-9 else 'unchanged'


def decide(line, home_water, away_water, line_open, v1_t10, v1_t120):
    """v1_t10 / v1_t120: {'score': D or None, 'line': stage line} or None."""
    team = rule_team(line, home_water, away_water)
    t120 = v1_team(*(v1_t120['score'], v1_t120['line'])) if v1_t120 else None
    t10 = v1_team(*(v1_t10['score'], v1_t10['line'])) if v1_t10 else None
    move = line_move(team, line_open, line)
    gate120 = t120 == team
    gate10 = t10 is None or t10 == team
    gate_line = move == 'toward'
    return {'team': team, 'v1TeamT120': t120, 'v1TeamT10': t10, 'lineMove': move,
            'gates': {'G_V1_T120': gate120, 'G_V1_T10': gate10, 'G_LINE': gate_line},
            'levels': {'L0': True, 'L1': gate120 and gate10, 'L2': gate120 and gate10 and gate_line}}


def water_hold(records, line, home_water, away_water):
    """G_HOLD (shadow_spec.json): side the market keeps favouring by water alone, without moving the line.

    records: every valid prematch Royal quote up to the decision cutoff. Returns
    (held team or None, upper water at T-10, whether the line ever changed)."""
    lines = {round(-x['home_handicap'], 2) for x in records}
    changed = len(lines) > 1
    up = upper_team(line)
    up_water = home_water if up == 'home' else away_water
    held = None
    if not changed and up_water <= HOLD['upperLowWaterMax']:
        held = up
    elif not changed and up_water >= HOLD['upperHighWaterMin']:
        held = other(up)
    return held, up_water, changed


def shadow_verdicts(d, line, team_t60, team_t30, hold=(None, None, None)):
    """Shadow versions on top of a v1 decision ``d`` (shadow_spec.json)."""
    quarter = round(abs(line) * 4) % 4 in (1, 3)
    held, up_water, changed = hold
    gate_hold = held is None or held == d['team']
    info = {'heldTeam': held, 'upperWaterT10': up_water, 'lineChanged': changed, 'G_HOLD': gate_hold}
    team = d['team']
    teams = {120: d['v1TeamT120'], 60: team_t60, 30: team_t30, 10: d['v1TeamT10']}
    agree = [s for s in (120, 60, 30) if teams[s] == team]
    against = [s for s in (120, 60, 30, 10) if teams[s] not in (None, team)]
    return {
        'V1-四分之一': {'id': 'v1-quarter', 'bet': d['levels']['L2'] and quarter, 'quarterLine': quarter},
        'V1-多阶段': {'id': 'v1-multistage', 'quarterLine': quarter, 'agreeStages': agree, 'againstStages': against,
                   'bet': quarter and d['gates']['G_LINE'] and len(agree) >= 2 and not against},
        'L0-水位门': {'id': 'l0-water-hold', **info, 'bet': gate_hold},
        'L1-水位门': {'id': 'l1-water-hold', **info, 'bet': d['levels']['L1'] and gate_hold},
        '水位门否决': {'id': 'water-hold-vetoed', **info, 'bet': not gate_hold},
    }


def settle_team(team, line, home_water, away_water, score):
    result, upper_profit, lower_profit = settle(line, home_water, away_water, *score)
    if team == upper_team(line):
        return result, upper_profit
    return FLIP[result], lower_profit


def baseline_expected(team, line, home_water, away_water):
    ih, ia = 1 / (1 + home_water), 1 / (1 + away_water)
    p_home = ih / (ih + ia)
    p_up = p_home if line >= 0 else 1 - p_home
    f = round(abs(line) * 4) % 4
    c = SPEC['baseline']['coefficients']
    p = 1 / (1 + math.exp(-(c['a'][f] + c['b'][f] * math.log(p_up / (1 - p_up)))))
    return p if team == upper_team(line) else 1 - p


def dedupe_fixtures(records):
    """One record per real match (V1 fixture key); the smallest source match ID wins."""
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    return list(kept.values())


def selected(entry, level):
    """Whether a decision (ledger entry or backtest row) bets at a v1 level or a shadow name."""
    if level in ('L0', 'L1', 'L2'):
        return entry['levels'][level]
    return bool(entry.get('shadows', {}).get(level, {}).get('bet'))


# ---------------------------------------------------------------- summaries
def summarise(rows):
    """rows: dicts with result, profit, expected."""
    counts = defaultdict(int)
    for r in rows:
        counts[r['result']] += 1
    decided = [r for r in rows if r['result'] != 'push']
    wins = sum(r['result'] in WINS for r in decided)
    n = len(decided)
    out = {'bets': len(rows), 'counts': {k: counts[k] for k in ('win', 'half_win', 'push', 'half_loss', 'loss')}}
    if n:
        w = wins / n
        out.update(winRate=round(w, 4), winSE=round(math.sqrt(w * (1 - w) / n), 4),
                   baseline=round(sum(r['expected'] for r in decided) / n, 4),
                   lift=round(w - sum(r['expected'] for r in decided) / n, 4))
    if rows:
        out['roi'] = round(sum(r['profit'] for r in rows) / len(rows), 4)
    return out


def by_level(rows):
    return {lv: summarise([r for r in rows if r['levels'][lv]]) for lv in ('L0', 'L1', 'L2')}


def by_shadow(rows):
    """Each shadow with v1 L2 and L0 on the same rows for comparison."""
    out = {name: summarise([r for r in rows if selected(r, name)]) for name in SHADOW_NAMES}
    out.update({'对照 L2': summarise([r for r in rows if r['levels']['L2']]),
                '对照 L0': summarise(rows)})
    return out


def print_levels(title, block):
    print(title)
    for lv, s in block.items():
        if s.get('winRate') is None:
            print(f'  {lv}: no settled bets')
            continue
        print(f"  {lv}: bets {s['bets']:6d}  win rate {s['winRate']:.3f} ±{s['winSE']:.3f}  baseline {s['baseline']:.3f}"
              f"  lift {s['lift']:+.3f}  ROI {s['roi']:+.3f}")


# ---------------------------------------------------------------- backtest
def backtest(args):
    cache = RESEARCH / 'cache'
    inputs = dedupe_fixtures(r for r in pickle.load(open(cache / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r)
    v1 = {}
    for stage in (10, 120, 60, 30):
        for t in pickle.load(open(cache / f'targets-company3-T{stage}.pkl', 'rb')):
            v1.setdefault(t['matchId'], {})[stage] = {'score': t['score'], 'line': t['line']}
    start = datetime.fromisoformat('2026-02-01T00:00:00+08:00').timestamp()
    rows = []
    for r in inputs:
        if r['kickoff'] < start:
            continue
        line_open = -r['records'][0]['home_handicap']
        m = v1.get(r['matchId'], {})
        d = decide(r['line'], r['homeWater'], r['awayWater'], line_open, m.get(10), m.get(120))
        teams = {s: v1_team(m[s]['score'], m[s]['line']) if s in m else None for s in (60, 30)}
        result, profit = settle_team(d['team'], r['line'], r['homeWater'], r['awayWater'], r['score'])
        hold = water_hold(r['records'], r['line'], r['homeWater'], r['awayWater'])
        rows.append({**d, 'shadows': shadow_verdicts(d, r['line'], teams[60], teams[30], hold),
                     'kickoff': r['kickoff'], 'firstTier': r['firstTier'], 'result': result, 'profit': profit,
                     'expected': baseline_expected(d['team'], r['line'], r['homeWater'], r['awayWater'])})
    windows = {'dev 2026-02-01..07-31': ('2026-02-01', '2026-08-01'),
               'holdout 2026-08-01..10-12': ('2026-08-01', '2026-10-13')}
    report = {'version': VERSION, 'mode': 'retrospective backtest on research caches', 'windows': {}}
    shadows = {'mode': 'retrospective backtest on research caches', 'windows': {}}
    for name, (a, b) in windows.items():
        lo = datetime.fromisoformat(a + 'T00:00:00+08:00').timestamp()
        hi = datetime.fromisoformat(b + 'T00:00:00+08:00').timestamp()
        part = [r for r in rows if lo <= r['kickoff'] < hi]
        report['windows'][name] = {'all': by_level(part), 'firstTier': by_level([r for r in part if r['firstTier']])}
        shadows['windows'][name] = {'all': by_shadow(part), 'firstTier': by_shadow([r for r in part if r['firstTier']])}
        print_levels(f'== {name} (all competitions)', report['windows'][name]['all'])
        print_levels(f'== {name} shadows (all competitions)', shadows['windows'][name]['all'])
    months = defaultdict(list)
    for r in rows:
        months[datetime.fromtimestamp(r['kickoff'], BJT).strftime('%Y-%m')].append(r)
    report['monthlyL2'] = {m: summarise([r for r in rs if r['levels']['L2']]) for m, rs in sorted(months.items())}
    shadows['monthly'] = {name: {m: summarise([r for r in rs if selected(r, name)]) for m, rs in sorted(months.items())}
                          for name in SHADOW_NAMES}
    out = HERE / 'backtest-forward-rule-v1.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    out_shadow = HERE / 'backtest-shadows.json'
    out_shadow.write_text(json.dumps(shadows, ensure_ascii=False, indent=1), 'utf-8')
    print('written', out, out_shadow)


# ---------------------------------------------------------------- sources
def sqlite_rows(path, table, where='', params=()):
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        cur = con.execute(f'SELECT * FROM {table} {where}', params)
        cols = [c[0] for c in cur.description]
        for row in cur:
            yield {c: ('' if v is None else str(v)) for c, v in zip(cols, row)}
    finally:
        con.close()


def snapshot_rows(directory, table):
    for part in sorted(glob.glob(str(Path(directory) / 'data' / f'{table}-part-*.csv.gz'))):
        with gzip.open(part, 'rt', encoding='utf-8', newline='') as stream:
            yield from csv.DictReader(stream)


def load_source(args):
    if args.db:
        q = "WHERE company_id = ? AND market_type = 'asia'"
        return collect(sqlite_rows(args.db, 'matches'), sqlite_rows(args.db, 'match_score_aliases'),
                       sqlite_rows(args.db, 'odds_history', q, (COMPANY,)),
                       sqlite_rows(args.db, 'odds_minute_order', q, (COMPANY,)), COMPANY)
    return collect(snapshot_rows(args.snapshot, 'matches'), snapshot_rows(args.snapshot, 'match_score_aliases'),
                   snapshot_rows(args.snapshot, 'odds_history'), snapshot_rows(args.snapshot, 'odds_minute_order'),
                   COMPANY)


# ---------------------------------------------------------------- V1 at a stage
class StageScorer:
    """V1 direction for targets at one stage, history pool built once per process."""

    def __init__(self, data, lead):
        self.lead = lead
        history, _ = build_records(data, lead)
        self.history = [r for r in history if 'score' in r]
        self.pool_size = len(self.history)
        replay_v1.COMPANY, replay_v1.STAGE = COMPANY, lead // 60
        replay_v1.INPUTS = list(self.history)
        self.buckets = replay_v1.build_buckets([i for i, r in enumerate(self.history) if r['teamIds']])

    def score(self, rec):
        replay_v1.COMPANY, replay_v1.STAGE = COMPANY, self.lead // 60
        replay_v1.INPUTS = self.history + [rec]
        replay_v1.BUCKETS = self.buckets
        _, direction, candidates, _ = replay_v1.replay(len(self.history))
        return {'score': direction['score'], 'effectiveN': direction['effectiveN'],
                'sampleCount': direction['sampleCount'], 'candidates': candidates,
                'line': rec['line'], 'featureHash': rec['feature']['hash'],
                'lastQuote': {'ts': stamp(rec['feature']['last']['ts']),
                              **{k: rec['feature']['last'][k] for k in ('line', 'home', 'away')}}}


# ---------------------------------------------------------------- ledger
def read_ledger(path):
    entries = []
    if Path(path).exists():
        for line in Path(path).read_text('utf-8').splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def stamp(ts):
    return datetime.fromtimestamp(ts, BJT).isoformat(timespec='seconds')


class LedgerState:
    """Frozen stage scores and decided matches, read once and kept in step with appends."""

    def __init__(self, path):
        self.path = path
        self.frozen = {lead: {} for lead in FREEZE_LEADS}
        self.decided, self.decided_fixtures = set(), set()
        for e in read_ledger(path):
            if e.get('specVersion') != VERSION:
                continue
            self.add(e)

    def add(self, e):
        if e['kind'] == 'decision':
            self.decided.add(e['matchId'])
            if 'fixtureKey' in e:
                self.decided_fixtures.add(tuple(e['fixtureKey']))
        elif e['kind'].startswith('v1_t'):
            lead = int(e['kind'][4:]) * 60
            if lead in self.frozen:
                self.frozen[lead][e['matchId']] = e

    def append(self, entries):
        if not entries:
            return
        with open(self.path, 'a', encoding='utf-8') as stream:
            for e in entries:
                stream.write(json.dumps(e, ensure_ascii=False) + '\n')
                self.add(e)


def stage_score(data, mid, lead, scorer, state):
    """Frozen V1 score for a stage, else recomputed at the stage cutoff (flagged)."""
    if mid in state.frozen[lead]:
        return state.frozen[lead][mid], 'frozen'
    rec, reason = build_record(data, mid, lead)
    return (scorer.score(rec) if rec else {'score': None, 'line': None, 'unavailable': reason}), 'recomputed_at_decision'


def scan_once(data, now, state, scorers, candidates):
    """candidates: (kickoff, matchId) whose freeze or decision window may contain ``now``."""
    new = []
    for kickoff, mid in sorted(candidates, key=lambda x: x[1]):  # smallest source ID first, as in backtest
        for lead in FREEZE_LEADS:
            if kickoff - lead <= now < kickoff - DECISION and mid not in state.frozen[lead]:
                rec, reason = build_record(data, mid, lead)
                entry = {'kind': f'v1_t{lead // 60}', 'specVersion': VERSION, 'matchId': mid,
                         'kickoff': stamp(kickoff), 'generatedAt': stamp(now)}
                entry.update(scorers[lead].score(rec) if rec else {'score': None, 'unavailable': reason})
                state.frozen[lead][mid] = entry
                new.append(entry)
        if kickoff - DECISION <= now < kickoff + DECISION and mid not in state.decided:
            rec, reason = build_record(data, mid, DECISION)
            if rec is None:
                continue  # no valid T-10 quote path: the rule cannot be applied
            if tuple(rec['fixtureKey']) in state.decided_fixtures:
                continue  # same real match already decided under another source ID
            v10 = scorers[DECISION].score(rec)
            stages = {lead: stage_score(data, mid, lead, scorers[lead], state) for lead in FREEZE_LEADS}
            v120, source = stages[CONFIRM]
            line_open = -rec['records'][0]['home_handicap']
            d = decide(rec['line'], rec['homeWater'], rec['awayWater'], line_open,
                       {'score': v10['score'], 'line': v10['line']},
                       {'score': v120.get('score'), 'line': v120.get('line')} if v120.get('score') is not None else None)
            shadow_teams = [v1_team(stages[lead][0].get('score'), stages[lead][0].get('line')) for lead in SHADOW_LEADS]
            entry = {'kind': 'decision', 'specVersion': VERSION, 'matchId': mid, 'fixtureKey': rec['fixtureKey'],
                     'kickoff': stamp(kickoff),
                     'generatedAt': stamp(now), 'late': now >= kickoff,
                     'competition': rec['competition'], 'firstTier': rec['firstTier'],
                     'home': rec['home'], 'away': rec['away'],
                     'teamName': rec['home'] if d['team'] == 'home' else rec['away'],
                     'frozenQuote': {'ts': stamp(rec['feature']['last']['ts']), 'line': rec['line'],
                                     'homeWater': rec['homeWater'], 'awayWater': rec['awayWater']},
                     'lineOpen': line_open, 'v1T10': v10, 'v1T120': v120, 'v1T120Source': source, **d}
            for lead in SHADOW_LEADS:
                entry[f'v1T{lead // 60}'], entry[f'v1T{lead // 60}Source'] = stages[lead]
            hold = water_hold(rec['records'], rec['line'], rec['homeWater'], rec['awayWater'])
            entry['shadows'] = shadow_verdicts(d, rec['line'], *shadow_teams, hold)
            new.append(entry)
            state.decided.add(mid)
            state.decided_fixtures.add(tuple(rec['fixtureKey']))
    state.append(new)
    return new


def describe(e):
    sign = -1 if e['team'] == 'home' else 1
    water = e['frozenQuote']['homeWater'] if e['team'] == 'home' else e['frozenQuote']['awayWater']
    return (f"{e['kickoff']} {e['competition']} {e['home']} v {e['away']}: {e['teamName']} "
            f"{sign * e['frozenQuote']['line'] + 0.0:+g} @{water}")


def scan(args):
    now = datetime.fromisoformat(args.now).timestamp() if args.now else time.time()
    t0 = time.time()
    data = load_source(args)
    kickoffs = sorted((k, mid) for mid in data['quotes'] for k in (kickoff_of(data, mid),) if k is not None)
    times = [k for k, _ in kickoffs]
    horizon = max(FREEZE_LEADS)
    scorers = {lead: StageScorer(data, lead) for lead in (DECISION, *FREEZE_LEADS)}
    state = LedgerState(args.ledger)
    if args.until:
        end = datetime.fromisoformat(args.until).timestamp()
        steps = []
        while now <= end:
            steps.append(now)
            now += args.step * 60
    else:
        steps = [now]
    total = []
    for at in steps:
        # Freeze windows need kickoff <= at + lead; the decision window needs kickoff > at - DECISION.
        lo, hi = bisect.bisect_right(times, at - DECISION), bisect.bisect_right(times, at + horizon)
        total += scan_once(data, at, state, scorers, kickoffs[lo:hi])
    decisions = [e for e in total if e['kind'] == 'decision']
    summary = {'version': VERSION, 'scans': len(steps), 'newFrozenStages': len(total) - len(decisions),
               'newDecisions': len(decisions), 'newL2': [describe(e) for e in decisions if e['levels']['L2']]}
    for name in SHADOW_NAMES:
        summary[f'new {name}'] = [describe(e) for e in decisions if selected(e, name)]
    summary['seconds'] = round(time.time() - t0, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def settle_ledger(args):
    data = load_source(args)
    starts = {'v1': datetime.fromisoformat(SPEC['prospectiveFrom']).timestamp(),
              'shadow': datetime.fromisoformat(SHADOW['prospectiveFrom']).timestamp()}
    rows, shadow_rows, pending, excluded, listed = [], [], 0, defaultdict(int), []
    list_start = starts['v1' if args.list in (None, 'L0', 'L1', 'L2') else 'shadow']
    for e in read_ledger(args.ledger):
        if e['kind'] != 'decision' or e['specVersion'] != VERSION:
            continue
        if e['late']:
            excluded['late'] += 1
            continue
        kickoff = datetime.fromisoformat(e['kickoff']).timestamp()
        in_v1 = args.retrospective or kickoff >= starts['v1']
        in_shadow = 'shadows' in e and (args.retrospective or kickoff >= starts['shadow'])
        if not in_v1:
            excluded['beforeProspectiveFrom'] += 1
            continue
        score = result_of(data, e['matchId']) if e['matchId'] in data['matches'] else None
        q = e['frozenQuote']
        if args.list and selected(e, args.list) and (args.retrospective or kickoff >= list_start):
            listed.append((e, score, settle_team(e['team'], q['line'], q['homeWater'], q['awayWater'], score)
                           if score else (None, None)))
        if score is None:
            pending += 1
            continue
        result, profit = settle_team(e['team'], q['line'], q['homeWater'], q['awayWater'], score)
        row = {'levels': e['levels'], 'shadows': e.get('shadows', {}), 'firstTier': e['firstTier'],
               'result': result, 'profit': profit,
               'expected': baseline_expected(e['team'], q['line'], q['homeWater'], q['awayWater'])}
        rows.append(row)
        if in_shadow:
            shadow_rows.append(row)
    report = {'version': VERSION, 'settled': len(rows), 'pending': pending, 'excluded': dict(excluded),
              'all': by_level(rows), 'firstTier': by_level([r for r in rows if r['firstTier']]),
              'shadows': {'prospectiveFrom': SHADOW['prospectiveFrom'], 'settled': len(shadow_rows),
                          'all': by_shadow(shadow_rows),
                          'firstTier': by_shadow([r for r in shadow_rows if r['firstTier']])}}
    l2 = report['all']['L2'].get('bets', 0)
    report['nextCheckpoint'] = next((c for c in SPEC['evaluation']['checkpointsL2Settled'] if c > l2), None)
    label = 'RETROSPECTIVE TEST, not evidence' if args.retrospective else 'prospective'
    report['mode'] = label
    print_levels(f"== {VERSION} {label} (settled {len(rows)}, pending {pending}, excluded {dict(excluded)})",
                 report['all'])
    print_levels('== first tier', report['firstTier'])
    start_note = '' if args.retrospective else f", from {SHADOW['prospectiveFrom']}"
    print_levels(f"== 影子版本 shadows {label} (settled {len(shadow_rows)}{start_note}; L2/L0 on the same decisions)",
                 report['shadows']['all'])
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    if args.list:
        print_matches(listed, args.list, args.list_csv)


RESULT_ZH = {'win': '全赢', 'half_win': '赢半', 'push': '走盘', 'half_loss': '输半', 'loss': '全输', None: '待结算'}


def print_matches(listed, level, csv_path=None):
    """One line per decision at ``level``: team-perspective handicap (- gives, + receives)."""
    header = ['开球(北京时间)', '赛事', '主队', '客队', '选择', '盘口', '水位', '初盘',
              'V1 T-120', 'V1 T-60', 'V1 T-30', 'V1 T-10', '比分', '结果', '盈亏']
    lines = []

    def d(e, key):
        value = (e.get(key) or {}).get('score')
        return '-' if value is None else f'{value:.1f}'
    for e, score, (result, profit) in sorted(listed, key=lambda x: (x[0]['kickoff'], x[0]['matchId'])):
        q = e['frozenQuote']
        sign = -1 if e['team'] == 'home' else 1  # local line + = home gives
        water = q['homeWater'] if e['team'] == 'home' else q['awayWater']
        lines.append([e['kickoff'][5:16].replace('T', ' '), e['competition'], e['home'], e['away'], e['teamName'],
                      f"{sign * q['line'] + 0.0:+g}", f'{water:.2f}', f"{sign * e['lineOpen'] + 0.0:+g}",
                      d(e, 'v1T120'), d(e, 'v1T60'), d(e, 'v1T30'), d(e, 'v1T10'),
                      '-' if score is None else f'{score[0]}-{score[1]}', RESULT_ZH[result],
                      '' if profit is None else f'{profit:+.3f}'])
    print(f'\n{level} 比赛（{len(lines)} 场；盘口为所选球队视角，负数为让球）')
    print(' | '.join(header))
    for row in lines:
        print(' | '.join(row))
    if csv_path:
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('backtest')
    for name in ('scan', 'settle'):
        p = sub.add_parser(name)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument('--db', help='SQLite with matches/odds_history/odds_minute_order/match_score_aliases')
        src.add_argument('--snapshot', help='published snapshot directory with data/*.csv.gz')
        p.add_argument('--ledger', required=True)
        if name == 'scan':
            p.add_argument('--now', help='ISO time with offset; default current time')
            p.add_argument('--until', help='simulate repeated scans up to this ISO time (testing)')
            p.add_argument('--step', type=float, default=5, help='minutes between simulated scans')
        else:
            p.add_argument('--output')
            p.add_argument('--retrospective', action='store_true',
                           help='also settle decisions before prospectiveFrom (testing only)')
            p.add_argument('--list', choices=('L0', 'L1', 'L2', *SHADOW_NAMES),
                           help='print every decision at this v1 level or shadow version')
            p.add_argument('--list-csv', help='also write the listed decisions to this CSV')
    args = parser.parse_args()
    if configuration()['companies'][COMPANY] != SPEC['v1']['parameters']:
        raise SystemExit('Published V1 parameters differ from the registered rule')
    {'backtest': backtest, 'scan': scan, 'settle': settle_ledger}[args.mode](args)


if __name__ == '__main__':
    main()
