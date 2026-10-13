"""Asian handicap vs euro 1X2 (Royal): does their disagreement predict the result?

Both markets are put on one scale, implied goal supremacy mu (home minus away):
  euro   de-margined home/draw/away probabilities -> the independent-Poisson
         (mu_E, total_E) that reproduces them best (grid search)
  asian  T-10 line and waters -> mu_A at the same expected total (supremacy.py)
  gap    d = mu_E - mu_A at T-10 (+ = euro rates the home side stronger)
  move   (mu_E change) - (mu_A change) from T-60 to T-10

Tested against the market error of the Asian price e = goal difference - mu_A:
correlation, mean error by gap size, and follow-euro / follow-asian rules
chosen on dev and reported on holdout, settled at the Royal T-10 Asian quote.
Also: which T-10 price predicts the goal difference better (squared error).

Data: research/cache/inputs-company3-T10.pkl (Asian, verified results) and the
euro-totals partial snapshot (Royal euro, source id 545). Coverage of the
euro snapshot is partial and not random (see euro-totals/README.md).

    python odds-trajectory/supremacy/euro_asia.py
"""
from __future__ import annotations

import glob
import gzip
import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'research'))
from build_inputs import settle  # noqa: E402
from supremacy import supremacy  # noqa: E402
from v1style import WINDOWS, baseline_fit, summarise, ts  # noqa: E402

EURO = HERE.parent / 'euro-totals' / 'snapshots' / '2026-10-13-partial' / 'data'
CACHE = HERE.parent / 'research' / 'cache'
MU = np.round(np.arange(-5, 5.0001, .01), 3)
TOT = np.round(np.arange(1.2, 5.0001, .02), 3)
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}


def jsonl(table):
    for part in sorted(glob.glob(str(EURO / f'{table}-part-*.jsonl.gz'))):
        with gzip.open(part, 'rt', encoding='utf-8') as stream:
            for line in stream:
                yield json.loads(line)


def euro_quotes(match_ids):
    """Valid pre-match Royal euro quotes per match: [(changed_at, home, draw, away)]."""
    tasks = {}
    for t in jsonl('tasks'):  # id, match_id, market, source_cid, company, ...
        if t[2] == 'euro' and t[3] == '545' and t[1] in match_ids:
            tasks[t[0]] = t[1]
    out = defaultdict(list)
    for q in jsonl('quotes'):  # task_id, source_order, changed_at, ..., home/draw/away, ..., quote_valid, prematch
        if q[0] in tasks and q[11] == 1 and q[12] == 1 and q[2] is not None:
            out[tasks[q[0]]].append((q[2], q[5], q[6], q[7]))
    return out


def state_at(quotes, at):
    """Last quote at or before ``at``; None if absent or that minute holds different prices."""
    before = [q for q in quotes if q[0] <= at]
    if not before:
        return None
    last = max(q[0] for q in before)
    prices = {q[1:] for q in before if q[0] == last}
    return next(iter(prices)) if len(prices) == 1 else None


def poisson_table():
    """P(home), P(draw) for every (mu, total) on the grid."""
    k = np.arange(16)
    fact = np.array([float(np.prod(np.arange(1, i + 1))) for i in k])
    ph_tab, pd_tab = np.empty((len(MU), len(TOT))), np.empty((len(MU), len(TOT)))
    for j, total in enumerate(TOT):
        lh = np.clip((total + MU) / 2, 1e-3, None)[:, None]
        la = np.clip((total - MU) / 2, 1e-3, None)[:, None]
        ph = np.exp(-lh) * lh ** k / fact
        pa = np.exp(-la) * la ** k / fact
        cdf_a = np.cumsum(pa, axis=1)
        pd_tab[:, j] = (ph * pa).sum(1)
        ph_tab[:, j] = (ph[:, 1:] * cdf_a[:, :-1]).sum(1)
    return ph_tab, pd_tab


PH, PD = poisson_table()
PA = 1 - PH - PD


def euro_fit(home, draw, away):
    inv = np.array([1 / home, 1 / draw, 1 / away])
    p = inv / inv.sum()
    err = (PH - p[0]) ** 2 + (PD - p[1]) ** 2 + (PA - p[2]) ** 2
    i, j = np.unravel_index(np.argmin(err), err.shape)
    return float(MU[i]), float(TOT[j]), float(np.sqrt(err[i, j]))


def asian_mu(r, at, total):
    times = np.array(r['tickTimes'])
    i = np.searchsorted(times, at, side='right') - 1
    x = r['records'][max(i, 0)]
    return supremacy(-x['home_handicap'], x['home_water'], x['away_water'], total)


def main():
    records = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    kept = {}
    for r in sorted(records, key=lambda r: r['matchId']):
        kept.setdefault(tuple(r['fixtureKey']), r)
    asian = {r['matchId']: r for r in kept.values()}
    quotes = euro_quotes(set(asian))
    rows = []
    for mid, qs in quotes.items():
        r = asian[mid]
        k = r['kickoff']
        e10, e60 = state_at(qs, k - 600), state_at(qs, k - 3600)
        if e10 is None:
            continue
        mu_e, tot_e, fit_err = euro_fit(*e10)
        mu_a = asian_mu(r, k - 600, tot_e)
        row = {'matchId': mid, 'kickoff': k, 'line': r['line'], 'homeWater': r['homeWater'],
               'awayWater': r['awayWater'], 'score': r['score'], 'gd': r['score'][0] - r['score'][1],
               'muE': mu_e, 'totalE': tot_e, 'fitErr': fit_err, 'muA': mu_a, 'gap': mu_e - mu_a,
               'firstTier': r['firstTier'], 'euro': e10}
        if e60 is not None and r['tickTimes'][0] <= k - 3600:
            mu_e60, tot_e60, _ = euro_fit(*e60)
            row['moveGap'] = (mu_e - mu_e60) - (mu_a - asian_mu(r, k - 3600, tot_e60))
        rows.append(row)
    rows.sort(key=lambda x: x['kickoff'])
    kick = np.array([x['kickoff'] for x in rows])
    win = {n: (kick >= ts(a)) & (kick < ts(b)) for n, (a, b) in WINDOWS.items()}
    p_up = baseline_fit(rows, win['dev'])
    gd = np.array([x['gd'] for x in rows], float)
    mu_a, mu_e = np.array([x['muA'] for x in rows]), np.array([x['muE'] for x in rows])
    gap = mu_e - mu_a
    move = np.array([x.get('moveGap', np.nan) for x in rows])
    err = gd - mu_a
    report = {'matches': len(rows), 'perWindow': {n: int(m.sum()) for n, m in win.items()},
              'medianFitError': float(np.median([x['fitErr'] for x in rows])),
              'gapQuantiles': {q: round(float(np.quantile(gap, q)), 3) for q in (.05, .25, .5, .75, .95)},
              'windows': {}, 'rules': []}
    print(f"matched matches with Royal euro at T-10: {len(rows)} ({report['perWindow']})")
    print(f"euro fit residual (median) {report['medianFitError']:.4f}; gap mu_E - mu_A quantiles {report['gapQuantiles']}")
    for n, m in win.items():
        mv = m & ~np.isnan(move)
        blend = (mu_a + mu_e) / 2
        bins = [-9, -.15, -.05, .05, .15, 9]
        by_gap = [(round(float(err[m & (gap > lo) & (gap <= hi)].mean()), 3), int((m & (gap > lo) & (gap <= hi)).sum()))
                  for lo, hi in zip(bins, bins[1:])]
        block = {'n': int(m.sum()), 'corrGapError': round(float(np.corrcoef(gap[m], err[m])[0, 1]), 4),
                 'corrMoveGapError': round(float(np.corrcoef(move[mv], err[mv])[0, 1]), 4) if mv.sum() > 30 else None,
                 'nMove': int(mv.sum()),
                 'mseAsian': round(float(((gd - mu_a)[m] ** 2).mean()), 4),
                 'mseEuro': round(float(((gd - mu_e)[m] ** 2).mean()), 4),
                 'mseBlend': round(float(((gd - blend)[m] ** 2).mean()), 4),
                 'meanErrorByGap': by_gap}
        report['windows'][n] = block
        print(f"== {n}: n={block['n']}  corr(gap, market error) {block['corrGapError']:+.4f} (SE {1/np.sqrt(block['n']):.4f})"
              f"  corr(move gap, error) {block['corrMoveGapError']} (n={block['nMove']})")
        print(f"   MSE of goal difference: asian {block['mseAsian']}  euro {block['mseEuro']}  blend {block['mseBlend']}")
        print('   mean market error by gap (<-0.15, -0.15~-0.05, ±0.05, 0.05~0.15, >0.15): '
              + ', '.join(f'{v:+.3f}(n={c})' for v, c in by_gap))

    def bets(mask, home_side):
        items = []
        for i in np.nonzero(mask)[0]:
            r = rows[i]
            take_upper = home_side[i] == (r['line'] >= 0)
            res, up, low = settle(r['line'], r['homeWater'], r['awayWater'], *r['score'])
            items.append({'result': res if take_upper else FLIP[res], 'profit': up if take_upper else low,
                          'expected': p_up[i] if take_upper else 1 - p_up[i]})
        return summarise(items) if items else {'N': 0}

    for signal, values in (('gap', gap), ('moveGap', move)):
        for t in (.03, .05, .10, .15):
            for mode in ('follow euro', 'follow asian'):
                home = values > 0 if mode == 'follow euro' else values < 0
                rule = {'signal': signal, 'threshold': t, 'mode': mode}
                for n, m in win.items():
                    rule[n] = bets(m & (np.abs(np.nan_to_num(values)) > t), home)
                report['rules'].append(rule)
    ok = [r for r in report['rules'] if r['dev'].get('N', 0) >= 200 and 'lift' in r['dev']]
    print('\n== rules (best on dev first; holdout unchanged)')
    for r in sorted(ok, key=lambda r: -r['dev']['lift'])[:8]:
        h = r['holdout']
        print(f"  {r['signal']:8s} |x|>{r['threshold']:.2f} {r['mode']:12s} dev {r['dev']['winRate']:.3f}({r['dev']['lift']:+.3f}, N={r['dev']['N']})"
              f" | holdout {h.get('winRate', float('nan')):.3f}({h.get('lift', float('nan')):+.3f}, N={h['N']}, ROI {h.get('roi', float('nan')):+.3f})")
    report['selected'] = max(ok, key=lambda r: r['dev']['lift']) if ok else None

    # Bet in the euro market itself: back the side euro underrates relative to Asian, at euro T-10 odds.
    margin = np.array([sum(1 / o for o in x['euro']) - 1 for x in rows])
    report['euroMarginMedian'] = round(float(np.median(margin)), 4)
    report['euroBets'] = []
    print(f"\n== betting the euro market at its T-10 odds (median euro margin {report['euroMarginMedian']:.3f})")
    for t in (.05, .10, .15, .20):
        rule = {'threshold': t}
        for n, m in win.items():
            profits, wins = [], []
            for i in np.nonzero(m & (np.abs(gap) > t))[0]:
                home_odds, _, away_odds = rows[i]['euro']
                back_home = gap[i] < 0  # Asian rates home stronger than euro does
                won = gd[i] > 0 if back_home else gd[i] < 0
                odds = home_odds if back_home else away_odds
                profits.append(odds - 1 if won else -1.)
                wins.append(won)
            rule[n] = {'N': len(profits), 'hitRate': round(float(np.mean(wins)), 4) if wins else None,
                       'roi': round(float(np.mean(profits)), 4) if profits else None,
                       'roiSE': round(float(np.std(profits) / np.sqrt(len(profits))), 4) if len(profits) > 1 else None}
        report['euroBets'].append(rule)
        print(f"  |gap|>{t:.2f} back the side euro underrates: " + ' | '.join(
            f"{n} N={rule[n]['N']} hit {rule[n]['hitRate']} ROI {rule[n]['roi']:+.3f}±{rule[n]['roiSE']:.3f}" for n in win))
    (HERE / 'results-euro-asia.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
