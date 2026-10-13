"""V1-style comparison and scoring on the continuous implied-supremacy (mu) series.

Carries the V1 pipeline over from per-line water channels to one continuous
channel, mu (supremacy.py), so no line segmentation is needed:

  candidates   mu at opening and T-10, lowest and highest mu, and the mu change
               over the last 15 minutes (from the last point at or before
               cutoff - 900 s) all within 0.04 (strict) / 0.08 (approx); the
               overall and recent movement directions must match (+/-0.02)
  similarity   V1 point-progress direction agreement S (0-100) on the mu
               series, consecutive duplicates removed, >= 5 points to rank
  direction    V1 aggregation D = 50 + 50 * sum(w z) / (alpha + sum w),
               w = (S/100)^2, alpha = 20, with three outcome values z per neighbour:
                 binary   how its actual score settles on the target's T-10 line
                          for the home side (+1 win or half win, -1 loss or half
                          loss, push excluded)
                 margin   (its goal difference - target's T-10 line) / sigma
                 residual (its goal difference - its own T-10 mu) / sigma
               sigma = dev standard deviation of (goal difference - mu)
  bands        E = 50 + 50 tanh((D - 50) / 5); green |E-50| < 10, orange 10-25, red >= 25

Time order: neighbours only from matches with kickoff + 5400 s before the
target's cutoff. Settlement at the target's T-10 Royal quote.

    python odds-trajectory/supremacy/v1style.py
"""
from __future__ import annotations

import json
import math
import multiprocessing as mp
import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from build_inputs import settle  # noqa: E402
from supremacy import record_mu  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
SETTLED_AFTER, RECENT = 5400, 900
STRICT, APPROX, MOVE = .04, .08, .02
POWER, ALPHA = 2.0, 20.0
WINDOWS = {'dev': ('2026-02-01', '2026-08-01'), 'holdout': ('2026-08-01', '2026-10-13')}
EPS = 1e-9

MODES = ('binary', 'margin', 'residual')
DATA = None  # shared with forked workers


def ts(day):
    return datetime.fromisoformat(day + 'T00:00:00+08:00').timestamp()


def movement(x):
    return 1 if x >= MOVE else -1 if x <= -MOVE else 0


# ---------------------------------------------------------------- V1 direction agreement
def direction_score(a, b):
    """V1 point-progress direction agreement (odds_point_similarity._side_metrics) for one channel.

    Points sit at progress i/(n-1); intervals of the union of both breakpoint
    sets are classed same / opposite / one flat / both flat by the slope sign of
    the segment each series is on. S = 100 * same / (1 - both_flat)."""
    na, nb = len(a), len(b)
    breaks = np.union1d(np.linspace(0, 1, na), np.linspace(0, 1, nb))
    mid = (breaks[:-1] + breaks[1:]) / 2
    h = np.diff(breaks)
    sa = np.sign(np.where(np.abs(d := np.diff(a)) > EPS, d, 0))[np.minimum((mid * (na - 1)).astype(int), na - 2)]
    sb = np.sign(np.where(np.abs(d := np.diff(b)) > EPS, d, 0))[np.minimum((mid * (nb - 1)).astype(int), nb - 2)]
    both_flat = h[(sa == 0) & (sb == 0)].sum()
    active = 1 - both_flat
    if active <= EPS:
        return None
    return 100 * h[(sa != 0) & (sa == sb)].sum() / active


# ---------------------------------------------------------------- data
def series(record):
    mus = [round(float(v), 6) for v in record_mu(record)]
    times = record['tickTimes']
    clean = [m for k, m in enumerate(mus) if k == 0 or m != mus[k - 1]]
    anchor = next((m for t, m in zip(reversed(times), reversed(mus)) if t <= record['cutoff'] - RECENT), None)
    return np.array(clean), {'open': mus[0], 'end': mus[-1], 'min': min(mus), 'max': max(mus),
                             'recent': None if anchor is None else mus[-1] - anchor}


def build():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    rows = sorted(kept.values(), key=lambda r: r['kickoff'])
    paths, keys = [], []
    for r in rows:
        path, key = series(r)
        paths.append(path)
        keys.append(key)
    arr = {k: np.array([key[k] for key in keys], float) for k in ('open', 'end', 'min', 'max')}
    arr['recent'] = np.array([np.nan if key['recent'] is None else key['recent'] for key in keys])
    arr['moveAll'] = np.array([movement(key['end'] - key['open']) for key in keys])
    arr['moveRecent'] = np.array([9 if key['recent'] is None else movement(key['recent']) for key in keys])
    arr['kickoff'] = np.array([r['kickoff'] for r in rows])
    arr['cutoff'] = np.array([r['cutoff'] for r in rows])
    arr['gd'] = np.array([r['score'][0] - r['score'][1] for r in rows], float)
    return rows, paths, arr


def home_z(goal_difference, line):
    """+1 / -1 / 0 for the home side on ``line`` (local sign, + = home gives), half results count fully."""
    margin = goal_difference - line
    if abs(line * 4) % 2 == 1:
        parts = np.sign(margin - .25) + np.sign(margin + .25)
    else:
        parts = 2 * np.sign(margin)
    return np.sign(parts)


def score_target(i):
    rows, paths, a = DATA
    end = np.searchsorted(a['kickoff'], a['cutoff'][i] - SETTLED_AFTER, side='left')
    if end == 0:
        return i, None
    gap = np.maximum.reduce([np.abs(a[k][:end] - a[k][i]) for k in ('open', 'end', 'min', 'max')])
    if np.isnan(a['recent'][i]):
        same_recent = np.isnan(a['recent'][:end])
    else:
        same_recent = ~np.isnan(a['recent'][:end])
        gap = np.maximum(gap, np.nan_to_num(np.abs(a['recent'][:end] - a['recent'][i]), nan=9))
    ok = (gap <= APPROX + 1e-9) & same_recent & (a['moveAll'][:end] == a['moveAll'][i]) \
        & (a['moveRecent'][:end] == a['moveRecent'][i])
    cand = np.nonzero(ok)[0]
    target = paths[i]
    line = rows[i]['line']
    weights, zs, strict = [], {m: [] for m in MODES}, 0
    rankable_target = len(target) >= 5
    for j in cand:
        if not rankable_target or len(paths[j]) < 5:
            continue
        s = direction_score(target, paths[j])
        if s is None:
            continue
        weights.append((s / 100) ** POWER)
        zs['binary'].append(home_z(a['gd'][j], line))
        zs['margin'].append((a['gd'][j] - line) / a['sigma'])
        zs['residual'].append((a['gd'][j] - a['end'][j]) / a['sigma'])
        strict += gap[j] <= STRICT + 1e-9
    if not weights:
        return i, {'candidates': int(len(cand)), **{m: None for m in MODES}}
    w = np.array(weights)
    out = {'candidates': int(len(cand)), 'used': int(len(w)), 'strict': int(strict)}
    for m in MODES:
        z = np.array(zs[m])
        keep = z != 0 if m == 'binary' else np.ones(len(z), bool)  # push excluded from binary weights
        total = w[keep].sum()
        out[m] = 50 + 50 * (w[keep] * z[keep]).sum() / (ALPHA + total) if total else None
        if m == 'binary':
            out['effectiveN'] = float(total ** 2 / (w[keep] ** 2).sum()) if total else 0.
    return i, out


def band(d):
    e = 50 + 50 * math.tanh((d - 50) / 5)
    dist = abs(float(format(e, '.1f')) - 50)
    return 'green' if dist < 10 else 'orange' if dist < 25 else 'red'


def baseline_fit(rows, mask):
    def design(rs):
        line = np.array([r['line'] for r in rs])
        ih, ia = 1 / (1 + np.array([r['homeWater'] for r in rs])), 1 / (1 + np.array([r['awayWater'] for r in rs]))
        p_up = np.where(line >= 0, ih / (ih + ia), ia / (ih + ia))
        frac = np.round(np.abs(line) * 4).astype(int) % 4
        lg = np.log(p_up / (1 - p_up))
        return np.column_stack([*(frac == f for f in range(4)), *((frac == f) * lg for f in range(4))]).astype(float)
    X = design(rows)
    y = np.array([settle(r['line'], r['homeWater'], r['awayWater'], *r['score'])[0] in ('win', 'half_win')
                  for r in rows], float)
    w = np.zeros(X.shape[1])
    for _ in range(30):
        p = 1 / (1 + np.exp(-X[mask] @ w))
        w += np.linalg.solve(X[mask].T @ (X[mask] * (p * (1 - p))[:, None]) + 1e-6 * np.eye(8), X[mask].T @ (y[mask] - p))
    return 1 / (1 + np.exp(-X @ w))


def summarise(items):
    decided = [x for x in items if x['result'] != 'push']
    n = len(decided)
    if not n:
        return {'N': len(items)}
    w = sum(x['result'] in ('win', 'half_win') for x in decided) / n
    exp = sum(x['expected'] for x in decided) / n
    return {'N': len(items), 'winRate': round(w, 4), 'winSE': round(math.sqrt(w * (1 - w) / n), 4),
            'baseline': round(exp, 4), 'lift': round(w - exp, 4),
            'roi': round(sum(x['profit'] for x in items) / len(items), 4)}


def main():
    global DATA
    rows, paths, arr = build()
    dev = (arr['kickoff'] >= ts(WINDOWS['dev'][0])) & (arr['kickoff'] < ts(WINDOWS['dev'][1]))
    arr['sigma'] = float(np.std(arr['gd'][dev] - arr['end'][dev]))
    DATA = (rows, paths, arr)
    targets = [i for i, r in enumerate(rows) if r['kickoff'] >= ts(WINDOWS['dev'][0])]
    out = {}
    with mp.get_context('fork').Pool() as pool:
        for i, res in pool.imap_unordered(score_target, targets, chunksize=128):
            out[i] = res
    kick = arr['kickoff']
    p_up = baseline_fit(rows, (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1])))
    report = {'method': __doc__.split('\n\n')[1], 'sigma': arr['sigma'], 'windows': {}}
    flip = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}
    for name, (lo, hi) in WINDOWS.items():
        idx = [i for i in targets if ts(lo) <= kick[i] < ts(hi)]
        block = {'targets': len(idx)}
        for m in MODES:
            scored = [i for i in idx if out[i] and out[i][m] is not None and abs(out[i][m] - 50) > 1e-8]
            items = {'all': [], 'green': [], 'orange': [], 'red': []}
            for i in scored:
                r = rows[i]
                home = out[i][m] > 50
                take_upper = home == (r['line'] >= 0)
                res, up_profit, low_profit = settle(r['line'], r['homeWater'], r['awayWater'], *r['score'])
                item = {'result': res if take_upper else flip[res], 'profit': up_profit if take_upper else low_profit,
                        'expected': p_up[i] if take_upper else 1 - p_up[i]}
                items['all'].append(item)
                items[band(out[i][m])].append(item)
            market_error = np.array([arr['gd'][i] - arr['end'][i] for i in scored])
            block[m] = {'withDirection': len(scored),
                        'corrWithMarketError': round(float(np.corrcoef([out[i][m] for i in scored], market_error)[0, 1]), 4),
                        **{k: summarise(v) for k, v in items.items()}}
        cands = [out[i]['candidates'] for i in idx if out[i]]
        block['medianCandidates'] = float(np.median(cands)) if cands else 0
        report['windows'][name] = block
    (HERE / 'results-v1style.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    with open(CACHE / 'v1style-scores.pkl', 'wb') as stream:
        pickle.dump({'matchIds': [r['matchId'] for r in rows], 'scores': out}, stream)
    print(f"sigma = {arr['sigma']:.3f} goals")
    for name, w in report['windows'].items():
        print(f"== {name}: targets {w['targets']}, median candidates {w['medianCandidates']}")
        for m in MODES:
            print(f"  [{m}] with direction {w[m]['withDirection']}, corr with market error {w[m]['corrWithMarketError']:+.4f}")
            for k in ('all', 'green', 'orange', 'red'):
                s_ = w[m][k]
                if 'winRate' in s_:
                    print(f"    {k:7s} N={s_['N']:6d}  win {s_['winRate']:.3f}±{s_['winSE']:.3f}  baseline {s_['baseline']:.3f}  "
                          f"lift {s_['lift']:+.3f}  ROI {s_['roi']:+.3f}")

if __name__ == '__main__':
    main()
