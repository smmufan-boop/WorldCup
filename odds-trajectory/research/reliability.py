"""Time-ordered reliability marks on top of the V1 replay, and their evaluation.

Instance marks (the user's proposal): every rankable V1 pair (target t,
neighbour j) whose two upper results are known adds weight w=(S/100)^p to a
"same result" or "different result" tally of BOTH matches. A mark becomes usable
only after the target is settled (kickoff + 5400 s), so a target never sees marks
created by itself or by any later match.

    r_j = (agree_j + a) / (agree_j + disagree_j + 2a)          (Beta(a, a) shrinkage)
    w'_j = w_j * (r_j / (1 - r_j)) ** gamma                    (odds-ratio reweighting)
    prune: drop j when the Wilson upper bound of r_j is below 0.5

Pattern marks: V1's own hit rate in the same matching bucket (first/last line,
giver, first/last segment direction, recent-delta direction), again using only
targets settled before the current cutoff. A gate keeps a call only when the
shrunk bucket hit rate clears a threshold.

Hyper-parameters are chosen on the development window only and then reported
unchanged on the holdout window.

    python odds-trajectory/research/reliability.py
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
from replay_v1 import SETTLED_AFTER, bucket_key  # noqa: E402

CACHE = HERE / 'cache'
POWER, ALPHA = 2.0, 20.0  # published Royal V1 parameters
Z = {'win': 1, 'half_win': 1, 'loss': -1, 'half_loss': -1, 'push': 0}
DEV = ('2026-02-01', '2026-08-01')
HOLDOUT = ('2026-08-01', '2026-10-13')


def load(tag='company3-T10'):
    inputs = pickle.load(open(CACHE / f'inputs-{tag}.pkl', 'rb'))
    targets = pd.DataFrame(pickle.load(open(CACHE / f'targets-{tag}.pkl', 'rb')))
    pairs = pd.DataFrame(pickle.load(open(CACHE / f'pairs-{tag}.pkl', 'rb')),
                         columns=['t', 'j', 'group', 'distance', 'S', 'rankable', 'rmse'])
    z = np.array([Z.get(r.get('upperResult'), 0) for r in inputs])
    kickoff = np.array([r['kickoff'] for r in inputs])
    cutoff = np.array([r['cutoff'] for r in inputs])
    pairs = pairs[pairs.rankable & pairs.S.notna()].copy()
    pairs['w'] = (pairs.S / 100) ** POWER
    pairs['zt'], pairs['zj'] = z[pairs.t], z[pairs.j]
    pairs['cutoff'] = cutoff[pairs.t]
    pairs['known'] = kickoff[pairs.t] + SETTLED_AFTER
    buckets = {i: bucket_key(r['feature']) for i, r in enumerate(inputs)}
    return inputs, targets, pairs, z, buckets


def instance_marks(pairs):
    """Agree/disagree weight available to each query pair strictly before its cutoff."""
    marked = pairs[(pairs.zt != 0) & (pairs.zj != 0)]
    agree = (marked.zt == marked.zj).to_numpy()
    events = pd.DataFrame({
        'entity': np.concatenate([marked.t, marked.j]),
        'time': np.concatenate([marked.known, marked.known]),
        'good': np.tile(np.where(agree, marked.w, 0.), 2),
        'bad': np.tile(np.where(agree, 0., marked.w), 2),
    }).sort_values(['entity', 'time'], kind='stable')
    key = events.entity.to_numpy(np.int64) * 10**10 + events.time.to_numpy(np.int64)
    good, bad = np.concatenate([[0.], events.good.cumsum()]), np.concatenate([[0.], events.bad.cumsum()])
    starts = pd.Series(np.arange(len(events)), index=events.entity.to_numpy()).groupby(level=0).min()
    query_key = pairs.j.to_numpy(np.int64) * 10**10 + (pairs.cutoff.to_numpy(np.int64))
    pos = np.searchsorted(key, query_key, side='left')  # strictly earlier marks only
    first = starts.reindex(pairs.j.to_numpy()).fillna(-1).to_numpy(np.int64)
    has = first >= 0
    base = np.where(has, first, 0)
    pairs = pairs.copy()
    pairs['good'] = np.where(has, good[pos] - good[base], 0.)
    pairs['bad'] = np.where(has, bad[pos] - bad[base], 0.)
    return pairs


def wilson_upper(p, n, zval=1.645):
    n = np.maximum(n, 1e-9)
    centre = p + zval**2 / (2 * n)
    margin = zval * np.sqrt(p * (1 - p) / n + zval**2 / (4 * n**2))
    return (centre + margin) / (1 + zval**2 / n)


def direction(pairs, prior, gamma, prune):
    r = (pairs.good + prior) / (pairs.good + pairs.bad + 2 * prior)
    weight = pairs.w * (r / (1 - r)) ** gamma
    if prune:
        n = pairs.good + pairs.bad
        raw = np.where(n > 0, pairs.good / np.maximum(n, 1e-9), .5)
        weight = np.where((n >= 3) & (wilson_upper(raw, n) < .5), 0., weight)
    frame = pd.DataFrame({'t': pairs.t, 'w': weight, 'wz': weight * pairs.zj})
    frame = frame[pairs.zj != 0]  # push excluded from weights, as in V1
    agg = frame.groupby('t').agg(W=('w', 'sum'), WZ=('wz', 'sum'))
    return 50 + 50 * agg.WZ / (ALPHA + agg.W)


def pattern_gate(targets, buckets, z, prior):
    """Shrunk V1 hit rate in the same bucket, from targets settled before each cutoff."""
    calls = targets[targets.score.notna() & ((targets.score - 50).abs() > 1e-8)].sort_values('cutoff')
    calls = calls.assign(bucket=calls.idx.map(buckets),
                         correct=(np.sign(calls.score - 50) == z[calls.idx]).astype(float),
                         known=calls.kickoff + SETTLED_AFTER)
    calls = calls[z[calls.idx] != 0]
    rate = {}
    for bucket, group in calls.groupby('bucket'):
        known = group.sort_values('known')
        times, hits = known.known.to_numpy(), np.concatenate([[0.], known.correct.cumsum()])
        for idx, cut in zip(group.idx, group.cutoff):
            n = np.searchsorted(times, cut, side='left')
            rate[idx] = ((hits[n] + prior) / (n + 2 * prior), n)
    return rate


def evaluate(frame, start, end):
    window = frame[(frame.dt >= start) & (frame.dt < end)]
    bets = window[window.bet]
    won = bets.won.dropna()
    return {'N': int(len(bets)), 'winRate': round(float(won.mean()), 4) if len(won) else None,
            'marketImplied': round(float(bets.pPick.mean()), 4) if len(bets) else None,
            'roi': round(float(bets.profit.mean()), 4) if len(bets) else None,
            'roiSE': round(float(bets.profit.std() / np.sqrt(len(bets))), 4) if len(bets) > 1 else None}


def frame_for(targets, scores, z, gate=None):
    f = targets.copy()
    f['D'] = scores.reindex(f.idx).to_numpy()
    f['bet'] = f.D.notna() & ((f.D - 50).abs() > 1e-8)
    if gate is not None:
        f['bet'] &= f.idx.map(gate).fillna(False).astype(bool)
    up = f.D > 50
    f['profit'] = np.where(up, f.upperProfit, f.lowerProfit)
    up_w = np.where(f.line >= 0, f.homeWater, f.awayWater)
    lo_w = np.where(f.line >= 0, f.awayWater, f.homeWater)
    p_up = (1 / (1 + up_w)) / (1 / (1 + up_w) + 1 / (1 + lo_w))
    f['pPick'] = np.where(up, p_up, 1 - p_up)
    zt = z[f.idx]
    f['won'] = np.where(zt == 0, np.nan, ((zt > 0) == up).astype(float))
    return f


def main():
    inputs, targets, pairs, z, buckets = load()
    targets['dt'] = pd.to_datetime(targets.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    pairs = instance_marks(pairs)
    windows = {'dev': DEV, 'holdout': HOLDOUT}
    report = {'windows': windows, 'variants': []}

    def run(name, scores, gate=None, params=None):
        frame = frame_for(targets, scores, z, gate)
        row = {'name': name, 'params': params or {}}
        for label, (a, b) in windows.items():
            row[label] = evaluate(frame, pd.Timestamp(a), pd.Timestamp(b))
            row[label + 'FirstTier'] = evaluate(frame[frame.firstTier], pd.Timestamp(a), pd.Timestamp(b))
        report['variants'].append(row)
        return row

    v1 = direction(pairs, prior=1e12, gamma=0, prune=False)  # gamma=0 reproduces V1 exactly
    check = targets.set_index('idx').score.dropna()
    gap = (v1.reindex(check.index) - check).abs().max()
    report['v1ReproductionMaxAbsDiff'] = float(gap)
    run('V1 (replay)', v1)

    # Instance reliability grid, selected on dev ROI.
    grid = []
    for prior, gamma, prune in itertools.product((5, 10, 20), (0.5, 1, 2), (False, True)):
        grid.append(run('V1 + instance marks', direction(pairs, prior, gamma, prune),
                        params={'prior': prior, 'gamma': gamma, 'prune': prune}))
    best_instance = max(grid, key=lambda r: r['dev']['roi'] if r['dev']['N'] >= 1000 else -9)

    # Pattern gate grid on top of plain V1 and on top of the best instance variant.
    gates = []
    for prior, threshold, base_name in itertools.product((10, 30), (.52, .55, .58), ('V1', 'instance')):
        rate = pattern_gate(targets, buckets, z, prior)
        gate = {k: v[0] >= threshold for k, v in rate.items()}
        p = best_instance['params']
        scores = v1 if base_name == 'V1' else direction(pairs, p['prior'], p['gamma'], p['prune'])
        gates.append(run(f'{base_name} + pattern gate', scores, gate,
                         params={'gatePrior': prior, 'threshold': threshold, **({} if base_name == 'V1' else p)}))
    best_gate = max(gates, key=lambda r: r['dev']['roi'] if r['dev']['N'] >= 300 else -9)
    report['selectedOnDev'] = {'instance': best_instance, 'gate': best_gate}
    report['pairsUsed'] = int(len(pairs))
    (HERE / 'results').mkdir(exist_ok=True)
    out = HERE / 'results' / 'reliability-royal-t10.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'v1ReproductionMaxAbsDiff': gap, 'selectedOnDev': report['selectedOnDev'],
                      'v1': report['variants'][0]}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
