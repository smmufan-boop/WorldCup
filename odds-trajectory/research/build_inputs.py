"""Build per-match T-X inputs (default T-10) for the V1 walk-forward replay.

Reads the published snapshot directly (no SQLite restore needed) and calls the
unchanged V1 ``points``/``feature`` functions, so every match feature equals what
V1 would compute from the same saved quotes. Output: one pickle per company.

    python odds-trajectory/research/build_inputs.py [--company 3] [--stage 10]
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import pickle
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'v1' / 'code'))
from matching_core import clock, feature, fixture_key, points  # noqa: E402

SNAPSHOT = HERE.parent / 'snapshots' / '2026-10-13' / 'data'
CACHE = HERE / 'cache'
FIRST_TIER = set(json.loads((HERE.parent / 'v1' / 'rules' / 'first-tier-competitions.json').read_text('utf-8')))
BLOCKED = re.compile(r'女|青年|少年|后备|後備|预备|預備|友谊|友誼|u\s*-?\s*\d{2}(?!\d)|women|youth|reserve|friendly')


def rows(table):
    for path in sorted(glob.glob(str(SNAPSHOT / f'{table}-part-*.csv.gz'))):
        with gzip.open(path, 'rt', encoding='utf-8', newline='') as stream:
            yield from csv.DictReader(stream)


def first_tier(name):
    label = re.split(r'[·｜|]', unicodedata.normalize('NFKC', name or ''))[0]
    label = ''.join(label.split()).lower()
    return label in FIRST_TIER and not BLOCKED.search(label)


def verified_result(m):
    """Regular-time 90-minute score per DATA_DICTIONARY; None when unverified."""
    found = re.search(r'90分钟\[(\d+)-(\d+)\]', m['score_note'])
    if found:
        return int(found[1]), int(found[2])
    if (not m['score_note'] and m['status'] == 'Played' and m['period'] == 'FT' and m['minute'] == '90'
            and m['score_home'].isdigit() and m['score_away'].isdigit()):
        return int(m['score_home']), int(m['score_away'])
    return None


def settle(line, home_water, away_water, sh, sa):
    """Upper-side (giver; home on level line) result plus upper/lower profit at the T-10 quote."""
    home_margin = sh - sa - line
    upper_margin = home_margin if line >= 0 else -home_margin
    upper_water, lower_water = (home_water, away_water) if line >= 0 else (away_water, home_water)

    def half(d):
        return 1 if d > 1e-9 else -1 if d < -1e-9 else 0
    if abs(line * 4) % 2 == 1:  # quarter line splits across the two neighbours
        parts = (half(upper_margin - .25), half(upper_margin + .25))
    else:
        parts = (half(upper_margin),) * 2
    upper = sum(upper_water if p > 0 else -1. if p < 0 else 0. for p in parts) / 2
    lower = sum(lower_water if p < 0 else -1. if p > 0 else 0. for p in parts) / 2
    result = {2: 'win', 1: 'half_win', 0: 'push', -1: 'half_loss', -2: 'loss'}[sum(parts)]
    return result, upper, lower


def collect(matches_rows, alias_rows, quote_rows, order_rows, company):
    """One pass over the four tables, keeping only ``company`` asia quotes."""
    matches = {m['match_id']: m for m in matches_rows}
    alias = {a['alias_id']: a['source_id'] for a in alias_rows}
    quotes, kickoffs = defaultdict(list), defaultdict(list)
    for q in quote_rows:
        if q['company_id'] == company and q['market_type'] == 'asia':
            quotes[q['match_id']].append(q)
            ts = clock(q['kickoff_local'])
            if ts is not None:
                kickoffs[q['match_id']].append(ts)
    order = defaultdict(lambda: defaultdict(list))
    for o in order_rows:
        if o['company_id'] == company and o['market_type'] == 'asia':
            order[o['match_id']][clock(o['change_time_local'])].append(o['sequence_json'])
    return {'matches': matches, 'alias': alias, 'quotes': quotes, 'kickoffs': kickoffs, 'order': order}


def kickoff_of(data, mid):
    """Earliest known kickoff: protects against postponements leaking in-play quotes."""
    meta = data['matches'].get(mid)
    if meta is None:
        return None
    candidates = data['kickoffs'][mid] + [t for t in (clock(meta['kickoff_local']),) if t is not None]
    return min(candidates) if candidates else None


def result_of(data, mid):
    meta = data['matches'][mid]
    row = meta if verified_result(meta) else data['matches'].get(data['alias'].get(mid, ''), meta)
    return verified_result(row)


def build_record(data, mid, lead):
    """V1 inputs for one match at cutoff = kickoff - lead; (record, None) or (None, reason)."""
    meta = data['matches'].get(mid)
    if meta is None:
        return None, 'no_metadata'
    kickoff = kickoff_of(data, mid)
    cutoff = kickoff - lead
    ticks, error = points(data['quotes'][mid], kickoff, cutoff, data['order'][mid])
    if error:
        return None, 'points:' + error
    feat = feature(ticks, cutoff)
    if feat is None:
        return None, 'feature_none'
    score = result_of(data, mid)
    last = feat['last']
    rec = {
        'matchId': mid, 'kickoff': kickoff, 'cutoff': cutoff,
        'competition': meta['competition_name'], 'firstTier': first_tier(meta['competition_name']),
        'home': meta['team_A_name'], 'away': meta['team_B_name'],
        'teamIds': bool(meta['team_A_id'] and meta['team_B_id']),
        'fixtureKey': fixture_key(meta), 'sourceId': data['alias'].get(mid, mid),
        'feature': feat,
        # V1 point scorer input: home perspective, home_handicap = -line.
        'tickTimes': [p['ts'] for p in ticks],
        'records': [{'home_handicap': -p['line'], 'home_water': p['home'], 'away_water': p['away']}
                    for p in ticks],
        'line': last['line'], 'homeWater': last['home'], 'awayWater': last['away'],
    }
    if score is not None:
        rec['score'] = score
        rec['upperResult'], rec['upperProfit'], rec['lowerProfit'] = settle(
            last['line'], last['home'], last['away'], *score)
    return rec, None


def build_records(data, lead, match_ids=None):
    out, skipped = [], defaultdict(int)
    for mid in (data['quotes'] if match_ids is None else match_ids):
        rec, reason = build_record(data, mid, lead)
        if rec is None:
            skipped[reason] += 1
        else:
            out.append(rec)
    return out, dict(skipped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--company', default='3')
    parser.add_argument('--stage', type=int, default=10, help='decision cutoff, minutes before kickoff')
    args = parser.parse_args()
    data = collect(rows('matches'), rows('match_score_aliases'), rows('odds_history'),
                   rows('odds_minute_order'), args.company)
    out, skipped = build_records(data, args.stage * 60)

    CACHE.mkdir(exist_ok=True)
    path = CACHE / f'inputs-company{args.company}-T{args.stage}.pkl'
    with open(path, 'wb') as stream:
        pickle.dump(out, stream, protocol=pickle.HIGHEST_PROTOCOL)
    print(json.dumps({'company': args.company, 'stage': args.stage, 'matches': len(out),
                      'withResult': sum('score' in r for r in out), 'skipped': skipped,
                      'output': str(path)}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
