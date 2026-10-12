"""Walk-forward replay of the unchanged V1 pipeline at T-10, logging every pair.

For each target, the history pool holds only matches whose kickoff + 5400 s is
before the target cutoff, with a verified 90-minute result and both team IDs
(ALGORITHM.md section 2). Candidates are classified by V1 ``classify``, scored by
``compare_segmented_odds`` and aggregated by ``direction_v1.score_company``.
A numpy prefilter only discards candidates V1 would reject anyway (first/last
segment gaps above 0.10 or differing direction keys); it never admits extras.

Outputs (cache/): targets-company{c}-T{stage}.pkl with V1 direction per target,
and pairs-company{c}-T{stage}.pkl with one row per (target, neighbour) comparison.
Each company uses its own published V1 parameters (澳彩 alpha=50, Royal alpha=20).

    python odds-trajectory/research/replay_v1.py [--company 3] [--stage 10] [--from 2026-02-01]
"""
from __future__ import annotations

import argparse
import bisect
import json
import multiprocessing as mp
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'v1' / 'code'))
from direction_v1 import configuration, score_company  # noqa: E402
from matching_core import BJT, classify, movement  # noqa: E402
from odds_segment_similarity import compare_segmented_odds  # noqa: E402

CACHE = HERE / 'cache'
SETTLED_AFTER = 5400
GAP_LIMIT = .10 + 1e-8

INPUTS: list = []
BUCKETS: dict = {}
COMPANY = '3'
STAGE = 10


def bucket_key(feat):
    """Necessary conditions shared by strict and approx matches."""
    first, last = feat['segments'][0], feat['segments'][-1]
    recent = feat['recentDelta']
    return (feat['path'][0], feat['path'][-1], feat['giver'], tuple(first['direction']), tuple(last['direction']),
            None if recent is None else tuple(movement(x) for x in recent))


def gap_vector(feat):
    first, last = feat['segments'][0], feat['segments'][-1]
    values = [s[k][side] for s in (first, last) for k in ('first', 'last') for side in ('home', 'away')]
    values += [s[k] for s in (first, last) for k in ('minHome', 'minAway')]
    return values + list(feat['recentDelta'] or (0., 0.))


def match_payload(rec):
    return {'id': rec['matchId'], 'bookmaker': COMPANY, 'market': 'asian_handicap', 'phase': 'prematch',
            'odds_format': 'hong_kong', 'perspective': 'home', 'window_id': f'first-valid-to-T-{STAGE}',
            'records': rec['records']}


def build_buckets(pool):
    buckets = defaultdict(list)
    for i in pool:
        buckets[bucket_key(INPUTS[i]['feature'])].append(i)
    out = {}
    for key, members in buckets.items():
        members.sort(key=lambda i: INPUTS[i]['kickoff'])
        out[key] = (np.array(members), np.array([INPUTS[i]['kickoff'] for i in members]),
                    np.array([gap_vector(INPUTS[i]['feature']) for i in members]))
    return out


def replay(index):
    target = INPUTS[index]
    feat = target['feature']
    entry = BUCKETS.get(bucket_key(feat))
    found = {}
    if entry is not None:
        members, kickoffs, vectors = entry
        end = bisect.bisect_left(kickoffs, target['cutoff'] - SETTLED_AFTER)
        if end:
            gaps = np.abs(vectors[:end] - np.array(gap_vector(feat))).max(axis=1)
            for j in members[:end][gaps <= GAP_LIMIT]:
                cand = INPUTS[j]
                if cand['fixtureKey'] == target['fixtureKey'] or cand['sourceId'] == target['sourceId']:
                    continue
                verdict = classify(feat, cand['feature'])
                if verdict is None:
                    continue
                key = cand['fixtureKey']
                rank = (verdict['group'] != 'strict', verdict['distance'], cand['matchId'])
                if key not in found or rank < found[key][0]:
                    found[key] = (rank, j, verdict)
    groups = {'strict': {'samples': []}, 'approx': {'samples': []}}
    pairs = []
    payload = match_payload(target)
    for _, j, verdict in found.values():
        cand = INPUTS[j]
        point = compare_segmented_odds(payload, match_payload(cand))
        point = {k: point.get(k) for k in ('rankable', 'direction_score', 'relative_rmse', 'net_change_gap', 'status')}
        groups[verdict['group']]['samples'].append({
            'matchId': cand['matchId'], 'pointScore': point,
            'settlements': {'upper': {'result': cand['upperResult'], 'profit': cand['upperProfit']}}})
        pairs.append((index, j, verdict['group'], verdict['distance'], point['direction_score'],
                      bool(point['rankable']), point['relative_rmse']))
    direction = score_company({'groups': groups}, parameters=configuration()['companies'][COMPANY])
    return index, {k: direction[k] for k in ('score', 'effectiveN', 'sampleCount', 'weightSum', 'groupConflict')}, \
        len(found), pairs


def main():
    global INPUTS, BUCKETS, COMPANY, STAGE
    parser = argparse.ArgumentParser()
    parser.add_argument('--company', default='3', choices=sorted(configuration()['companies']))
    parser.add_argument('--stage', type=int, default=10)
    parser.add_argument('--from', dest='start', default='2026-02-01')
    parser.add_argument('--limit', type=int, default=0, help='debug: only the first N targets')
    args = parser.parse_args()
    COMPANY, STAGE = args.company, args.stage
    tag = f'company{COMPANY}-T{STAGE}'
    with open(CACHE / f'inputs-{tag}.pkl', 'rb') as stream:
        INPUTS = pickle.load(stream)
    pool = [i for i, r in enumerate(INPUTS) if 'score' in r and r['teamIds']]
    BUCKETS = build_buckets(pool)
    start = datetime.fromisoformat(args.start).replace(tzinfo=BJT).timestamp()
    targets = sorted((i for i, r in enumerate(INPUTS) if 'score' in r and r['kickoff'] >= start),
                     key=lambda i: INPUTS[i]['kickoff'])
    if args.limit:
        targets = targets[:args.limit]
    rows, pairs = [], []
    with mp.get_context('fork').Pool() as workers:
        for n, (index, direction, candidates, pair_rows) in enumerate(workers.imap(replay, targets, chunksize=64), 1):
            r = INPUTS[index]
            rows.append({'idx': index, 'matchId': r['matchId'], 'kickoff': r['kickoff'], 'cutoff': r['cutoff'],
                         'competition': r['competition'], 'firstTier': r['firstTier'], 'line': r['line'],
                         'homeWater': r['homeWater'], 'awayWater': r['awayWater'],
                         'upperResult': r['upperResult'], 'upperProfit': r['upperProfit'],
                         'lowerProfit': r['lowerProfit'], 'candidates': candidates, **direction})
            pairs.extend(pair_rows)
            if n % 5000 == 0:
                print(f'{n}/{len(targets)} targets, {len(pairs)} pairs', flush=True)
    suffix = f'-limit{args.limit}' if args.limit else ''
    with open(CACHE / f'targets-{tag}{suffix}.pkl', 'wb') as stream:
        pickle.dump(rows, stream, protocol=pickle.HIGHEST_PROTOCOL)
    with open(CACHE / f'pairs-{tag}{suffix}.pkl', 'wb') as stream:
        pickle.dump(pairs, stream, protocol=pickle.HIGHEST_PROTOCOL)
    scored = [r for r in rows if r['score'] is not None]
    print(json.dumps({'targets': len(rows), 'withDirection': len(scored), 'pairs': len(pairs)}, indent=1))


if __name__ == '__main__':
    main()
