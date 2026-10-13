"""Check local-model expected-goals predictions and compare them with the Royal Asian handicap.

Input: odds-trajectory/xg/predictions.csv (one row per match; see README.md).
  1. data checks   missing fields, duplicates, prediction lead time, internal consistency
  2. matching      to the Royal T-10 records: match_id, else home/away/date, else same
                   kickoff with one team name equal
  3. comparison    model expected goal difference (home_xg - away_xg, and the local
                   pre-market version when present) vs the Royal T-10 implied
                   supremacy mu: accuracy against the actual goal difference, and
                   backing the side the model rates higher than the market, by gap

    python odds-trajectory/xg/check_predictions.py [path.csv]
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'supremacy'))
from supremacy import supremacy  # noqa: E402

CACHE = HERE.parent / 'research' / 'cache'
FLIP = {'win': 'loss', 'half_win': 'half_loss', 'push': 'push', 'half_loss': 'half_win', 'loss': 'win'}
GAPS = (0., .1, .2, .3)


def check(d):
    print(f'== 1. 数据检查：{len(d)} 行，开球 {d.kickoff.min()} … {d.kickoff.max()}')
    for c in ('match_id', 'kickoff', 'home', 'away', 'home_xg', 'away_xg', 'predicted_at'):
        if c not in d:
            print(f'  缺少必需列 {c}')
        elif d[c].isna().any():
            print(f'  {c} 缺失 {int(d[c].isna().sum())} 行')
    lead = (pd.to_datetime(d.kickoff) - pd.to_datetime(d.predicted_at)).dt.total_seconds() / 60
    print(f'  预测提前量（分钟）：最小 {lead.min():.0f}，中位 {lead.median():.0f}；晚于开球前 10 分钟的 {int((lead < 10).sum())} 行')
    print(f"  重复：match_id {int(d.match_id.dropna().duplicated().sum())}，开球+主客队 {int(d.duplicated(['kickoff', 'home', 'away']).sum())}")
    for c in ('model_version', 'expectation_basis', 'timestamp_evidence', 'lineup_known'):
        if c in d:
            print(f'  {c}: ' + '; '.join(f'{k} ×{v}' for k, v in d[c].value_counts(dropna=False).items()))


def match(d):
    recs = [r for r in pickle.load(open(CACHE / 'inputs-company3-T10.pkl', 'rb')) if 'score' in r]
    by_id, by_name, by_time = {}, {}, {}
    for r in recs:
        by_id.setdefault(r['matchId'], r)
        by_id.setdefault(r['sourceId'], r)
        by_name.setdefault((r['home'], r['away'], r['fixtureKey'][2]), r)
        by_time.setdefault(r['kickoff'], []).append(r)
    pairs, how, missing = [], {'match_id': 0, '主客队+日期': 0, '开球时间+一队同名': 0}, []
    for _, x in d.iterrows():
        r = by_id.get(x.match_id) if isinstance(x.match_id, str) else None
        m = 'match_id'
        if r is None:
            r, m = by_name.get((x.home, x.away, str(x.kickoff)[:10])), '主客队+日期'
        if r is None:
            t = pd.Timestamp(x.kickoff).tz_localize('Asia/Shanghai').timestamp()
            c = [q for q in by_time.get(t, []) if q['home'] == x.home or q['away'] == x.away]
            r, m = (c[0] if len(c) == 1 else None), '开球时间+一队同名'
        if r is None:
            missing.append(x)
            continue
        how[m] += 1
        pairs.append((x, r))
    print(f'\n== 2. 匹配到 Royal T-10 且有已核验赛果：{len(pairs)} / {len(d)}（' + '，'.join(f'{k} {v}' for k, v in how.items()) + '）')
    if missing:
        print(f'  未匹配 {len(missing)} 场（队名为英文、或 Royal 没有该场），前 10 场：')
        for x in missing[:10]:
            print(f'    {x.kickoff}  {x.home} vs {x.away}  {x.get("competition", "")}')
    return pairs


def compare(pairs):
    report = {}
    versions = [('final', 'home_xg', 'away_xg', '最终预期（可能已含市场）')]
    if 'local_home_xg' in pairs[0][0]:
        versions.append(('local', 'local_home_xg', 'local_away_xg', '本地模型（市场融合前）'))
    print('\n== 3. 与 Royal T-10 隐含净胜球 μ 对比（主队视角）')
    for key, hc, ac, label in versions:
        rows = []
        for x, r in pairs:
            mu = supremacy(r['line'], r['homeWater'], r['awayWater'])
            egd, gd = x[hc] - x[ac], r['score'][0] - r['score'][1]
            home = egd > mu
            upper_home = r['line'] >= 0
            same = home == upper_home
            rows.append({'mu': mu, 'egd': egd, 'gd': gd, 'gap': egd - mu,
                         'result': r['upperResult'] if same else FLIP[r['upperResult']],
                         'profit': r['upperProfit'] if same else r['lowerProfit']})
        o = pd.DataFrame(rows)
        block = {'n': len(o), 'corrModel': round(float(np.corrcoef(o.egd, o.gd)[0, 1]), 3),
                 'corrMarket': round(float(np.corrcoef(o.mu, o.gd)[0, 1]), 3),
                 'maeModel': round(float((o.egd - o.gd).abs().mean()), 3),
                 'maeMarket': round(float((o.mu - o.gd).abs().mean()), 3),
                 'corrGapMarketError': round(float(np.corrcoef(o.gap, o.gd - o.mu)[0, 1]), 3), 'byGap': {}}
        se = 1 / np.sqrt(len(o))
        print(f"  [{label}] {len(o)} 场：与实际净胜球的相关 模型 {block['corrModel']:+.3f} / 市场 {block['corrMarket']:+.3f}；"
              f"平均绝对误差 模型 {block['maeModel']:.3f} / 市场 {block['maeMarket']:.3f}")
        print(f"    模型−市场 的差距 与 市场误差（实际−μ）的相关 {block['corrGapMarketError']:+.3f}（标准误约 {se:.3f}）")
        for th in GAPS:
            s = o[o.gap.abs() >= th]
            dec = s[s.result != 'push']
            if not len(dec):
                continue
            w = float(dec.result.isin(['win', 'half_win']).mean())
            block['byGap'][th] = {'n': len(s), 'winRate': round(w, 3), 'roi': round(float(s.profit.mean()), 3)}
            print(f'    |模型−市场| ≥ {th:.1f} 球：{len(s):4d} 场，按模型方向下注 赢盘率 {w:.3f} ±{np.sqrt(w * (1 - w) / len(dec)):.3f}，'
                  f'ROI {s.profit.mean():+.3f}')
        report[key] = block
    return report


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / 'predictions.csv'
    d = pd.read_csv(path, dtype={'match_id': str})
    check(d)
    pairs = match(d)
    report = compare(pairs) if pairs else {}
    (HERE / 'results-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')


if __name__ == '__main__':
    main()
