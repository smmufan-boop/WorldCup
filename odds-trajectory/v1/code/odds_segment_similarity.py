"""Handicap-node extension of the unmodified user-supplied point scorer.

Segments never share a quote across a handicap change. Segment i is paired
only with segment i at exactly the same handicap, including on return paths.
Weights are the mean of the two cleaned-quote-count proportions, not duration.
Unscorable mass is retained: a partial comparison never becomes a full score.
"""
from itertools import zip_longest
import json
import math

from odds_point_similarity import ALGORITHM_VERSION as REFERENCE_VERSION
from odds_point_similarity import compare_odds, _prepare

ALGORITHM_VERSION = REFERENCE_VERSION + ':handicap-nodes-v1'
WEIGHT_RULE = 'mean-of-cleaned-point-shares-v1'


def _segments(match):
    segments = []
    for record in match['records']:
        line = round(record['home_handicap'], 6)
        if not segments or segments[-1]['line'] != line:
            segments.append({'line': line, 'records': [], 'cleaned': 0, 'last': None})
        segment = segments[-1]
        state = tuple(round(record[key], 6) for key in ('home_handicap', 'home_water', 'away_water'))
        segment['records'].append(record)
        if state != segment['last']:
            segment['cleaned'] += 1
            segment['last'] = state
    return segments


def _summary(segment, total):
    if segment is None:
        return None
    return {'home_handicap': segment['line'], 'original': len(segment['records']),
            'cleaned': segment['cleaned'], 'point_share': segment['cleaned'] / total}


def compare_segmented_odds(a, b, config=None):
    """Return the reference result unchanged for fixed lines; extend changes.

All segments with direction scores contribute, including diagnostic 2/3/4
point segments. Only complete comparisons whose every segment is rankable can
rank. Both-flat/singleton/mismatched segments have no score, not a zero score.
The covered-only diagnostic average is separate from the (then null) total.
"""
    base = compare_odds(a, b, config)
    if base.get('reason') != 'A handicap changes within an observation window.':
        return base
    # compare_odds already validated every field, record and config before
    # returning the change-specific error. Do not bypass its other guardrails.
    pa, pb = _prepare(a, 'match_a'), _prepare(b, 'match_b')
    sa, sb = _segments(a), _segments(b)
    na, nb = len(pa['states']), len(pb['states'])
    result = {key: value for key, value in base.items() if key != 'reason'}
    result.update(algorithm_version=ALGORITHM_VERSION, aggregation='handicap_segments',
                  weight_rule=WEIGHT_RULE, segment_alignment='same-ordinal-and-exact-handicap',
                  segments=[], cleaned_point_count_ratio=max(na, nb) / min(na, nb))
    for index, (xa, xb) in enumerate(zip_longest(sa, sb), 1):
        ma, mb = _summary(xa, na), _summary(xb, nb)
        weight = ((ma['point_share'] if ma else 0) + (mb['point_share'] if mb else 0)) / 2
        item = {'index': index, 'match_a': ma, 'match_b': mb, 'weight': weight,
                'contribution': None}
        if xa is None or xb is None:
            score = {'status': 'not_comparable', 'rankable': False, 'direction_score': None,
                     'reasonCode': 'missing_segment', 'reason': '一场缺少对应换盘阶段'}
        elif xa['line'] != xb['line']:
            score = {'status': 'not_comparable', 'rankable': False, 'direction_score': None,
                     'reasonCode': 'different_segment_handicap', 'reason': '同序号阶段盘口不同，不跨段拼接'}
        else:
            score = compare_odds({**a, 'records': xa['records']}, {**b, 'records': xb['records']}, config)
        item['score'] = score
        if score['direction_score'] is not None:
            item['contribution'] = weight * score['direction_score']
        result['segments'].append(item)

    segments = result['segments']
    covered = [s for s in segments if s['contribution'] is not None]
    coverage = math.fsum(s['weight'] for s in covered)
    contribution = math.fsum(s['contribution'] for s in covered)
    complete = len(covered) == len(segments)
    result.update(score_coverage=coverage, unscored_weight=max(0., 1. - coverage),
                  rankable_coverage=math.fsum(s['weight'] for s in segments if s['score']['rankable']),
                  weighted_direction_contribution=contribution if covered else None,
                  partial_direction_score=contribution / coverage if covered and not complete else None,
                  direction_score=contribution if complete else None,
                  rankable=complete and all(s['score']['rankable'] for s in segments))
    # No RMSE, net change or Pearson across handicap boundaries. These are
    # within-segment diagnostics; RMSE combines as L2, never arithmetic RMSE.
    if complete:
        for key in ('relative_rmse', 'level_rmse'):
            result[key] = math.hypot(*(math.sqrt(s['weight']) * s['score'][key] for s in segments))
        for key in ('net_change_gap', 'range_gap', 'start_gap', 'end_gap', 'active_coverage'):
            result[key] = math.fsum(s['weight'] * s['score'][key] for s in segments)
        result['status'] = ('ok' if result['rankable'] else
                            'insufficient_points' if any(s['score']['status'] == 'insufficient_points' for s in segments)
                            else 'low_information')
    else:
        result['status'] = 'partial_segments' if covered else 'no_scorable_segments'
        result['reason'] = '存在缺段、异盘、记录不足或无方向证据的阶段；仅列分段诊断，不补零、不重分配权重生成总分。'
    result['notes'].extend([
        'Split before each new-handicap quote; do not interpolate across handicap changes.',
        'Pair by stage ordinal and exact handicap; repeated visits remain separate.',
        'Segment weight = 0.5 * (cleaned_count_a / total_a + cleaned_count_b / total_b).',
        'A missing segment retains half of the present side point share as unscored weight.',
        'Total requires every segment to have direction evidence; ranking also requires every segment to be rankable.',
    ])
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError) as error:
        return {'algorithm_version': ALGORITHM_VERSION, 'status': 'numerical_error',
                'rankable': False, 'direction_score': None, 'relative_rmse': None,
                'net_change_gap': None, 'reason': str(error)}
    return result
