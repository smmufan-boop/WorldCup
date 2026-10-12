"""Pure trajectory matching layer used by the published V1 pipeline.

RULES_VERSION retains its historical source-order version label: its v2
suffix is a matching-rule version, not a direction-score V2 weight model.
The input rows must already be scoped to a single company and verified source.
History queries, collection, score verification, and settlement are caller work.
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

BJT = timezone(timedelta(hours=8), name="Asia/Shanghai")


def clock(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=BJT).timestamp() if dt.tzinfo is None else dt.timestamp()
    except (ValueError, TypeError):
        return None


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def daqiu_signed_line(handicap_text: Any, handicap_value: Any) -> float | None:
    """Convert the site's home-perspective Chinese handicap to local sign rules.

    The source API's numeric sign is not trusted on its own.  A Chinese line
    beginning with 受 means the home team receives the handicap, hence the away
    team is favourite and the local signed line is negative.
    """

    try:
        value = abs(float(handicap_value))
    except (TypeError, ValueError):
        return None
    text = unicodedata.normalize("NFKC", str(handicap_text or "")).strip()
    if not text:
        return None
    if "平手" in text and value < 1e-9:
        return 0.0
    return round(-value if text.startswith("受") or "受让" in text else value, 4)


RULES_VERSION = 'same-company-path-v2-source-order'


COMPANIES = {'1': '澳彩', '3': 'Royal'}


STAGES = (120, 60, 30, 10)


def stamp(ts):
    return datetime.fromtimestamp(ts, BJT).isoformat(timespec='seconds')


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def movement(value):
    return 1 if round(value, 2) >= .03 else -1 if round(value, 2) <= -.03 else 0


def _quote(row):
    line = daqiu_signed_line(row.get('handicap_text'), row.get('handicap_value'))
    try:
        home, away = float(row['home_water']), float(row['away_water'])
        if line is None or not all(math.isfinite(v) for v in (line, home, away)):
            return None
        if abs(line) > 20 or line * 4 != int(line * 4) or not (0 < home <= 3 and 0 < away <= 3):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return line, home, away


def _ordered_minute(values, evidence):
    """Use one complete provider sequence consistent with every saved snapshot.

    Do not combine unordered subsets, sort by price, infer from fetch time or
    choose the last-written row. Non-consecutive A/B/A repeats are real events.
    """
    sequences = set()
    for encoded in set(evidence):
        try:
            raw = json.loads(encoded)
            if not isinstance(raw, list) or any(not isinstance(q, list) or len(q) != 4 for q in raw):
                return None
            sequence = []
            for q in raw:
                quote = _quote(dict(zip(('handicap_text','handicap_value','home_water','away_water'),q)))
                if quote is None or quote not in values:
                    return None
                if not sequence or sequence[-1] != quote:
                    sequence.append(quote)
            if sequence:
                sequences.add(tuple(sequence))
        except (ValueError, TypeError):
            return None
    def contains(full, part):
        iterator = iter(full)
        return all(any(p == q for p in iterator) for q in part)
    candidates = [s for s in sequences if set(s) == values and all(contains(s, other) for other in sequences)]
    return candidates[0] if len(candidates) == 1 else None


def points(rows, kickoff, cutoff, order_evidence=None):
    """Deduplicate ticks, resolving minute ties only with verified source order.

    A timestamp is a source change time, not collection time. History fetched
    later is allowed but always marked retrospective, never prospective proof.
    """
    by_time = defaultdict(set)
    evidence = defaultdict(list, {k:list(v) for k,v in (order_evidence or {}).items()})
    for row in rows:
        ts = clock(row.get('change_time_local'))
        if row.get('score') or ts is None or ts >= kickoff or ts > cutoff:
            continue
        if row.get('market_type') != 'asia':
            continue
        quote = _quote(row)
        if quote is None:
            continue
        by_time[ts].add(quote)
        if row.get('_source_minute_sequence'):
            evidence[ts].append(row['_source_minute_sequence'])
    ordered = {ts:_ordered_minute(values,evidence[ts]) for ts,values in by_time.items() if len(values)>1 and evidence[ts]}
    # Multiple same-line internal observations retain useful extrema without
    # pretending their minute-level order is known. Ambiguous line switches or
    # first/latest quote remain unusable.
    ambiguous = {t:v for t,v in by_time.items() if len(v)>1 and not ordered.get(t)}
    if by_time and (any(len({q[0] for q in values}) > 1 for values in ambiguous.values())
                    or min(by_time) in ambiguous or max(by_time) in ambiguous):
        return [], '同一分钟存在不同报价，无法核验先后顺序'
    result = []
    for t, values in sorted(by_time.items()):
        for rank, q in enumerate(ordered.get(t) or sorted(values)):
            point = dict(ts=t,line=q[0],home=q[1],away=q[2],tied=t in ambiguous)
            if ordered.get(t):
                point.update(sourceOrder=rank,orderBasis='provider-array-minute-v1')
            result.append(point)
    return result, '' if len(result) >= 3 else '不足3条有效赛前记录'


def feature(ticks, cutoff):
    if len(ticks) < 3:
        return None
    segments = []
    for p in ticks:
        if not segments or segments[-1]['line'] != p['line']:
            segments.append({'line': p['line'], 'first': p, 'last': p, 'n': 1,
                             'minHome': p['home'], 'minAway': p['away']})
        else:
            s = segments[-1]
            s.update(last=p, n=s['n']+1, minHome=min(s['minHome'], p['home']), minAway=min(s['minAway'], p['away']))
    signs = {1 if p['line'] > 0 else -1 for p in ticks if p['line']}
    if len(signs) > 1:
        return None
    for s in segments:
        if s['first'].get('tied') or s['last'].get('tied'):
            return None
        s['direction'] = [movement(s['last'][side]-s['first'][side]) for side in ('home', 'away')]
    # Recent trend uses a genuine <=T-15m anchor and only the current line.
    recent = [p for p in ticks if p['ts'] >= segments[-1]['first']['ts']]
    anchor = next((p for p in reversed(recent) if p['ts'] <= cutoff-900), None)
    if anchor and anchor.get('tied'):
        return None
    delta = [round(ticks[-1][s]-anchor[s], 4) for s in ('home', 'away')] if anchor else None
    return {'n': len(ticks), 'path': [s['line'] for s in segments], 'segments': segments,
            'first': ticks[0], 'last': ticks[-1], 'giver': next(iter(signs), 0),
            'recentDelta': delta, 'recentAnchor': anchor,
            'lowSides': [side for side in ('home', 'away') if min(p[side] for p in ticks) <= .70],
            'dataAgeMinutes': round((cutoff-ticks[-1]['ts'])/60, 1), 'hash': digest(ticks),
            'orderedMinutes': len({p['ts'] for p in ticks if p.get('orderBasis')})}


def classify(target, candidate):
    if target is None or candidate is None:
        return None
    if (target['path'][0], target['path'][-1], target['giver']) != (candidate['path'][0], candidate['path'][-1], candidate['giver']):
        return None
    exact_path = target['path'] == candidate['path']
    pairs = list(zip(target['segments'], candidate['segments'])) if exact_path else [
        (target['segments'][0], candidate['segments'][0]), (target['segments'][-1], candidate['segments'][-1])]
    gaps = [abs(a[k][side]-b[k][side]) for a, b in pairs
            for k in ('first', 'last') for side in ('home', 'away')]
    # Compare within-line extrema too, so hidden low-water excursions do not
    # masquerade as the same water path just because endpoints match.
    gaps += [abs(a[k]-b[k]) for a, b in pairs for k in ('minHome', 'minAway')]
    gap = max(gaps)
    if any(a['direction'] != b['direction'] for a, b in pairs):
        return None
    ta, ca = target['recentDelta'], candidate['recentDelta']
    if (ta is None) != (ca is None):
        return None
    if ta is not None:
        if [movement(x) for x in ta] != [movement(x) for x in ca]:
            return None
        gap = max(gap, *(abs(a-b) for a, b in zip(ta, ca)))
    if gap <= .05 + 1e-8 and exact_path:
        return {'group': 'strict', 'distance': round(gap, 4), 'reason': '同盘路，各关键水位差≤0.05'}
    if gap <= .10 + 1e-8:
        return {'group': 'approx', 'distance': round(gap, 4),
                'reason': '同起终盘，关键水位差≤0.10' + ('；中间盘路不同' if not exact_path else '；超出严格水位阈值')}
    return None


def aggregate(samples, side):
    counts = Counter(s['settlements'][side]['result'] for s in samples)
    n = len(samples)
    denominator = n-counts['push']
    profit = round(sum(s['settlements'][side]['profit'] for s in samples), 6)
    return {'n': n, 'counts': {k: counts[k] for k in ('win', 'half_win', 'push', 'half_loss', 'loss')},
            'winRate': (counts['win']+counts['half_win'])/denominator if denominator else None,
            'profit': profit, 'roi': profit/n if n else None, 'smallSample': n < 20}


def fixture_key(row):
    # Team IDs are source scoped; never join unrelated source-number namespaces.
    ids = tuple(str(row.get(k, '')).strip() for k in ('team_A_id', 'team_B_id'))
    names = ids if all(ids) else tuple(''.join(str(row.get(k, '')).split()).casefold() for k in ('team_A_name', 'team_B_name'))
    kickoff = clock(row.get('kickoff_local'))
    return names + (datetime.fromtimestamp(kickoff,BJT).date().isoformat() if kickoff is not None else '',)
