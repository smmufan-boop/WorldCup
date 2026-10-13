"""Model score in the score + green / orange / red form: Royal Asian trajectory only, betting return target.

Same matches, target and grouping as similar_score.py; only the score
changes. Instead of averaging the returns of similar past matches, the score
is the output of the turning-point model (multi_source.fit_average: ridge
alpha 100 + gradient-boosted trees depth 2, leaf 400, averaged after
standardising each on its training predictions) on the trajectory-only
features (no T-10 level, depth, line type or water), target
z = (upper profit - lower profit) / 2.

Rolling by month: for each month Feb..Oct the model is trained on every match
whose kickoff + 90 min is before the month starts, then scores that month, so
dev scores are out of sample too. D = 50 + 10 * score (score in standard
deviations of the training predictions); D > 50 backs the upper side at the
T-10 quote. Decile and band cut points come from dev (02-01..07-31); holdout
(08-01..10-12) is evaluated once.

    python odds-trajectory/supremacy/model_score.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from multi_source import fit_average  # noqa: E402
from similar_score import BUFFER, build, grouped  # noqa: E402
from turning_points import month_start  # noqa: E402
from v1style import WINDOWS, baseline_fit, ts  # noqa: E402


def main():
    rows, F, _ = build()
    kick = np.array([r['kickoff'] for r in rows])
    z = np.array([r['z'] for r in rows])
    dev = (kick >= ts(WINDOWS['dev'][0])) & (kick < ts(WINDOWS['dev'][1]))
    hold = (kick >= ts(WINDOWS['holdout'][0])) & (kick < ts(WINDOWS['holdout'][1]))
    p_up = baseline_fit(rows, dev)
    D = np.full(len(rows), np.nan)
    report = {'method': __doc__.split('\n\n')[1:3], 'monthly': {}}
    print('逐月滚动：训练场次 → 当月场次，当月评分与回报的相关')
    for m in range(2, 11):
        lo, hi = month_start(2026, m), min(month_start(2026, m + 1), ts(WINDOWS['holdout'][1]))
        tr, te = kick + BUFFER < lo, (kick >= lo) & (kick < hi)
        D[te] = 50 + 10 * fit_average(F[tr], z[tr], F[te])
        c = float(np.corrcoef(D[te], z[te])[0, 1])
        report['monthly'][m] = {'train': int(tr.sum()), 'n': int(te.sum()), 'corr': round(c, 4)}
        print(f'  {m:2d} 月：{tr.sum():6d} → {te.sum():5d}，相关 {c:+.4f}')
    for name, mask in (('dev', dev), ('holdout', hold)):
        report[f'{name}Corr'] = round(float(np.corrcoef(D[mask], z[mask])[0, 1]), 4)
    print(f"开发段相关 {report['devCorr']:+.4f}，留出段相关 {report['holdoutCorr']:+.4f}")
    grouped(rows, D, kick, dev, hold, p_up, report)
    (HERE / 'results-model-score.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
