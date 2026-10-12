"""Compare V1 at decision stages T-120/T-60/T-30/T-10, each bet at its own Royal price.

Per stage: V1 ROI (dev/holdout, all and first-tier), whether V1 strength adds
information beyond that stage's de-margined price (logistic on dev picks), and
closing-line value: how far the Royal T-10 price moved toward the V1 pick,
in probability points on the equivalent-handicap scale. Also reports how much
better the T-10 price predicts results than each earlier price on the same matches.

    python odds-trajectory/research/stages.py   (after replay_v1.py --stage S for each S)
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cross_company import logistic, p_home  # noqa: E402
from reliability import DEV, HOLDOUT, evaluate, frame_for, load  # noqa: E402

STAGES = (120, 60, 30, 10)
K = 4.631  # goals per unit probability, estimated in cross_company.py on dev


def equivalent(line, home, away):
    return line + K * (p_home(home, away) - .5)


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def main():
    close = {r['matchId']: equivalent(r['line'], r['homeWater'], r['awayWater'])
             for r in pickle.load(open(HERE / 'cache' / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r}
    report = {'K': K, 'stages': {}}
    home_prob = {}
    for stage in STAGES:
        inputs, targets, pairs, z, buckets = load(f'company3-T{stage}')
        targets['dt'] = pd.to_datetime(targets.kickoff, unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
        f = frame_for(targets, targets.set_index('idx').score, z)
        out = {}
        for label, (a, b) in (('dev', DEV), ('holdout', HOLDOUT)):
            out[label] = evaluate(f, pd.Timestamp(a), pd.Timestamp(b))
            out[label + 'FirstTier'] = evaluate(f[f.firstTier], pd.Timestamp(a), pd.Timestamp(b))

        picks = f[f.bet].copy()
        picks['sX'] = equivalent(picks.line, picks.homeWater, picks.awayWater)
        picks['s10'] = picks.matchId.map(close)
        up_is_home = picks.line >= 0
        pick_home = np.where(picks.D > 50, up_is_home, ~up_is_home)
        picks['clv'] = np.where(pick_home, 1, -1) * (picks.s10 - picks.sX) / K  # + : price moved toward pick
        for label, (a, b) in (('dev', DEV), ('holdout', HOLDOUT)):
            x = picks[(picks.dt >= pd.Timestamp(a)) & (picks.dt < pd.Timestamp(b)) & picks.clv.notna()]
            out[label]['meanCLV_probPoints'] = round(float(x.clv.mean() * 100), 3)
            out[label]['positiveCLVShare'] = round(float((x.clv > 1e-9).mean()), 4)
            out[label]['zeroCLVShare'] = round(float((x.clv.abs() <= 1e-9).mean()), 4)

        fit = picks[(picks.dt >= pd.Timestamp(DEV[0])) & (picks.dt < pd.Timestamp(DEV[1])) & picks.won.notna()]
        X = np.column_stack([np.ones(len(fit)), np.log(fit.pPick / (1 - fit.pPick)), (fit.D - 50).abs() / 50])
        w = logistic(X, fit.won.to_numpy())
        out['pickWinLogitOnDev'] = dict(zip(['const', 'logitMarketPick', 'v1Strength'], np.round(w, 4).tolist()))
        report['stages'][f'T-{stage}'] = out

        for r in inputs:
            if 'score' in r and r['upperResult'] != 'push':
                p = p_home(r['homeWater'], r['awayWater'])
                won_upper = r['upperResult'] in ('win', 'half_win')
                home_won = won_upper if r['line'] >= 0 else not won_upper
                home_prob.setdefault(r['matchId'], {})[stage] = (p, float(home_won), r['line'])

    # Price informativeness on matches present at every stage with an unchanged line.
    common = [v for v in home_prob.values() if len(v) == len(STAGES) and len({v[s][2] for s in STAGES}) == 1]
    y = np.array([v[10][1] for v in common])
    report['priceLogLossSameLine'] = {'N': len(common), **{f'T-{s}': round(logloss(np.array([v[s][0] for v in common]), y), 5)
                                                          for s in STAGES}}
    (HERE / 'results').mkdir(exist_ok=True)
    path = HERE / 'results' / 'stages-royal.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
