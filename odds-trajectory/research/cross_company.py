"""Cross-company (澳彩 vs Royal) signals at a decision stage, bet at the Royal price.

Targets are Royal stage inputs with a verified result (build_inputs.py). For each
target the 澳彩 quote state is read directly from the snapshot with the same
cleaning as V1 (pre-kickoff, no in-play mark, line a 0.25 multiple, waters in
(0,3]) and the target's own kickoff/cutoff. One 澳彩 quote is enough, unlike V1's
three-point minimum. A cutoff minute holding conflicting 澳彩 quotes is dropped.

Both books are put on one scale, the equivalent home handicap
    s = line + K * (p_home - 0.5),
with p_home the de-margined home-cover probability and K goals per unit of
probability, estimated from Royal's own consecutive line moves on the dev window.

Strategies (all settled at the Royal stage quote, 1 unit, actual half-win/loss):
  * fair-price: treat 澳彩 as the fair line, bet Royal when its EV beats a margin.
  * logistic: market-only vs market + cross-company features, fitted on dev.

    python odds-trajectory/research/cross_company.py [--stage 10]
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'v1' / 'code'))
from build_inputs import rows  # noqa: E402
from matching_core import _quote, clock  # noqa: E402
from reliability import DEV, HOLDOUT  # noqa: E402

CACHE = HERE / 'cache'
RESULT_Z = {'win': 1, 'half_win': 1, 'loss': -1, 'half_loss': -1, 'push': 0}


def p_home(home, away):
    ih, ia = 1 / (1 + home), 1 / (1 + away)
    return ih / (ih + ia)


def state_at(times, quotes, t, ordered=False):
    """Last quote at or before t; None when absent or the minute is ambiguous.

    ``ordered`` series (V1 points) already resolved same-minute order or were rejected.
    """
    i = np.searchsorted(times, t, side='right')
    if i == 0:
        return None
    if ordered:
        return quotes[i - 1]
    same = [q for ts, q in zip(times[:i], quotes[:i]) if ts == times[i - 1]]
    return same[-1] if len(set(same)) == 1 else None


def macau_quotes(targets):
    wanted = {r['matchId']: r for r in targets}
    found = defaultdict(list)
    for q in rows('odds_history'):
        r = wanted.get(q['match_id'])
        if r is None or q['company_id'] != '1' or q['market_type'] != 'asia' or q['score']:
            continue
        ts = clock(q['change_time_local'])
        quote = _quote(q)
        if ts is None or quote is None or ts >= r['kickoff']:
            continue
        found[q['match_id']].append((ts, quote))
    out = {}
    for mid, items in found.items():
        items.sort(key=lambda x: x[0])
        out[mid] = (np.array([t for t, _ in items]), [q for _, q in items])
    return out


def royal_series(r):
    times = np.array(r['tickTimes'])
    quotes = [(-x['home_handicap'], x['home_water'], x['away_water']) for x in r['records']]
    return times, quotes


def build(stage):
    targets = [r for r in pickle.load(open(CACHE / f'inputs-company3-T{stage}.pkl', 'rb')) if 'score' in r]
    macau = macau_quotes(targets)
    recs = []
    for r in targets:
        rt, rq = royal_series(r)
        row = {'matchId': r['matchId'], 'kickoff': r['kickoff'], 'firstTier': r['firstTier'],
               'line': r['line'], 'homeWater': r['homeWater'], 'awayWater': r['awayWater'],
               'z': RESULT_Z[r['upperResult']], 'upperProfit': r['upperProfit'], 'lowerProfit': r['lowerProfit']}
        for name, at in (('R', r['cutoff']), ('R60', r['cutoff'] - 3000), ('Ro', rt[0])):
            row[name] = state_at(rt, rq, at, ordered=True)
        row['ageR'] = (r['cutoff'] - rt[-1]) / 60
        m = macau.get(r['matchId'])
        if m is not None:
            mt, mq = m
            row['M'] = state_at(mt, mq, r['cutoff'])
            row['M60'] = state_at(mt, mq, r['cutoff'] - 3000)
            row['Mo'] = mq[0] if mt[0] <= r['cutoff'] else None
            before = mt[mt <= r['cutoff']]
            row['ageM'] = (r['cutoff'] - before[-1]) / 60 if len(before) else None
        recs.append(row)
    return pd.DataFrame(recs)


def equivalent(q, k):
    if q is None or (isinstance(q, float) and np.isnan(q)):
        return np.nan
    line, home, away = q
    return line + k * (p_home(home, away) - .5)


def logistic(X, y, l2=1e-3, steps=30):
    w = np.zeros(X.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-X @ w))
        H = X.T @ (X * (p * (1 - p))[:, None]) + l2 * np.eye(len(w))
        w += np.linalg.solve(H, X.T @ (y - p) - l2 * w)
    return w


def summary(profit, won=None, implied=None):
    profit = np.asarray(profit, float)
    n = len(profit)
    out = {'N': int(n), 'roi': round(float(profit.mean()), 4) if n else None,
           'roiSE': round(float(profit.std() / np.sqrt(n)), 4) if n > 1 else None}
    if won is not None and n:
        ok = ~np.isnan(won)
        out['winRate'] = round(float(np.nanmean(won)), 4) if ok.any() else None
        out['marketImplied'] = round(float(np.mean(implied)), 4)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=int, default=10)
    args = parser.parse_args()
    df = build(args.stage)
    df['dt'] = pd.to_datetime(df.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    period = np.where(df.dt < pd.Timestamp(DEV[0]), 'warmup',
                      np.where(df.dt < pd.Timestamp(DEV[1]), 'dev',
                               np.where(df.dt < pd.Timestamp(HOLDOUT[1]), 'holdout', 'after')))
    df['period'] = period
    report = {'stage': args.stage, 'royalTargets': int(len(df)),
              'withMacauAtCutoff': int(df.M.notna().sum()) if 'M' in df else 0}

    # K from Royal line moves inside each match path (dev window only).
    dev_ids = set(df.matchId[df.period == 'dev'])
    shifts = []
    for r in pickle.load(open(CACHE / f'inputs-company3-T{args.stage}.pkl', 'rb')):
        if r['matchId'] not in dev_ids:
            continue
        recs = r['records']
        for a, b in zip(recs, recs[1:]):
            if -b['home_handicap'] - (-a['home_handicap']) == .25:
                shifts.append(p_home(a['home_water'], a['away_water']) - p_home(b['home_water'], b['away_water']))
    K = .25 / float(np.median(shifts))
    report['K_goalsPerProb'] = round(K, 3)

    for col in ('R', 'R60', 'Ro', 'M', 'M60', 'Mo'):
        df['s' + col] = [equivalent(q, K) for q in df[col]] if col in df else np.nan
    df['pR'] = [p_home(q[1], q[2]) for q in df.R]
    df['marginM'] = [np.nan if q is None or q != q else 1 / (1 + q[1]) + 1 / (1 + q[2]) - 1 for q in df.M]
    df['marginR'] = [1 / (1 + q[1]) + 1 / (1 + q[2]) - 1 for q in df.R]
    report['medianMargin'] = {'royal': round(float(df.marginR.median()), 4), 'macau': round(float(df.marginM.median()), 4)}
    df['gap'] = df.sM - df.sR                     # + : 澳彩 rates home stronger than Royal
    df['dR60'], df['dM60'] = df.sR - df.sR60, df.sM - df.sM60
    df['dRo'], df['dMo'] = df.sR - df.sRo, df.sM - df.sMo
    df['sameLine'] = [q is not None and q == q and q[0] == r[0] for q, r in zip(df.M, df.R)]

    home_up = df.line >= 0
    df['homeProfit'] = np.where(home_up, df.upperProfit, df.lowerProfit)
    df['awayProfit'] = np.where(home_up, df.lowerProfit, df.upperProfit)
    hz = np.where(home_up, df.z, -df.z)
    df['homeWon'] = np.where(hz == 0, np.nan, (hz > 0).astype(float))
    df['homeWaterR'] = [q[1] for q in df.R]
    df['awayWaterR'] = [q[2] for q in df.R]

    both = df[df.gap.notna()].copy()
    report['bothBooks'] = {p: int((both.period == p).sum()) for p in ('dev', 'holdout')}
    report['gapDistribution'] = {k: round(float(v), 4) for k, v in both.gap.describe(percentiles=[.05, .25, .5, .75, .95]).items()}

    def bet(frame, side_home):
        profit = np.where(side_home, frame.homeProfit, frame.awayProfit)
        won = np.where(side_home, frame.homeWon, 1 - frame.homeWon)
        implied = np.where(side_home, frame.pR, 1 - frame.pR)
        return profit, won, implied

    # 1. Follow the side 澳彩 prices stronger than Royal, by gap size.
    follow = {}
    for lo in (0.0, .05, .10, .20):
        for p in ('dev', 'holdout'):
            x = both[(both.period == p) & (both.gap.abs() > lo)]
            follow[f'|gap|>{lo:.2f}:{p}'] = summary(*bet(x, x.gap > 0))
    report['followMacauGap'] = follow

    # 2. Parameter-free fair price: 澳彩 equivalent line converted to Royal's line.
    fair = {}
    p_fair_home = (.5 + (both.sM - both.line) / K).clip(.01, .99)
    ev_home = p_fair_home * (1 + both.homeWaterR) - 1
    ev_away = (1 - p_fair_home) * (1 + both.awayWaterR) - 1
    side_home = ev_home >= ev_away
    best = np.maximum(ev_home, ev_away)
    for m in (0.0, .02, .04, .06):
        for p in ('dev', 'holdout'):
            mask = (both.period == p) & (best > m)
            x = both[mask]
            fair[f'EV>{m:.2f}:{p}'] = summary(*bet(x, side_home[mask]))
    report['macauFairPrice'] = fair

    # 3. Movement divergence over the last 50 minutes before the cutoff.
    div = both[both.dR60.notna() & both.dM60.notna()].copy()
    div['divergence'] = div.dM60 - div.dR60
    moves = {}
    for lo in (.05, .10, .20):
        for p in ('dev', 'holdout'):
            x = div[(div.period == p) & (div.divergence.abs() > lo)]
            moves[f'|dM60-dR60|>{lo:.2f}:{p}'] = summary(*bet(x, x.divergence > 0))
    report['followMacauMoveVsRoyal'] = moves

    # 4. Logistic: market only vs market + cross-company features (dev fit).
    feats = ['gap', 'dR60', 'dM60', 'dRo', 'dMo']
    X_all = both.copy()
    for f in feats:
        X_all[f + '_na'] = X_all[f].isna().astype(float)
        X_all[f] = X_all[f].fillna(0.)
    X_all['ageM'] = np.log1p(X_all.ageM.fillna(0.).clip(lower=0))
    X_all['logitR'] = np.log(X_all.pR / (1 - X_all.pR))
    cols_m = ['logitR']
    cols_x = ['logitR', *feats, 'dR60_na', 'dM60_na', 'dMo_na', 'ageM']

    def design(frame, cols):
        return np.column_stack([np.ones(len(frame)), *[frame[c].to_numpy(float) for c in cols]])
    fit = X_all[(X_all.period == 'dev') & X_all.homeWon.notna()]
    hold = X_all[(X_all.period == 'holdout') & X_all.homeWon.notna()]
    models = {}
    for name, cols in (('marketOnly', cols_m), ('market+cross', cols_x)):
        w = logistic(design(fit, cols), fit.homeWon.to_numpy())
        def ll(frame):
            p = np.clip(1 / (1 + np.exp(-design(frame, cols) @ w)), 1e-6, 1 - 1e-6)
            y = frame.homeWon.to_numpy()
            return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())
        models[name] = {'coef': dict(zip(['const', *cols], np.round(w, 4).tolist())),
                        'logLossDev': round(ll(fit), 5), 'logLossHoldout': round(ll(hold), 5)}
        if name == 'market+cross':
            evs = {}
            for p in ('dev', 'holdout'):
                x = X_all[X_all.period == p]
                ph = 1 / (1 + np.exp(-design(x, cols) @ w))
                eh, ea = ph * (1 + x.homeWaterR) - 1, (1 - ph) * (1 + x.awayWaterR) - 1
                sh = eh >= ea
                bestev = np.maximum(eh, ea)
                for m in (0.0, .02, .04):
                    mask = bestev > m
                    evs[f'EV>{m:.2f}:{p}'] = summary(*bet(x[mask], sh[mask]))
            models[name]['evFilter'] = evs
    report['logistic'] = models
    report['holdoutLogLossGain'] = round(models['marketOnly']['logLossHoldout'] - models['market+cross']['logLossHoldout'], 5)

    (HERE / 'results').mkdir(exist_ok=True)
    path = HERE / 'results' / f'cross-company-T{args.stage}.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    both.drop(columns=['R', 'R60', 'Ro', 'M', 'M60', 'Mo']).to_pickle(CACHE / f'cross-T{args.stage}.pkl')
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
