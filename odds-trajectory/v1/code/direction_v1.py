"""Offline V1 company-isolated outcome-evidence aggregation.

Selected functions are copied exactly from the dashboard source using its AST.
Only the legacy weight path is exported. No services or data stores are imported.
This advisory index is not a calibrated win probability.
"""
from collections import Counter
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import math

VERSION = 'trajectory-direction-v1'
EXPECTED_CONFIGURATION_VERSION = 'trajectory-direction-v1:e485690037a4'
CONFIG_PATH = Path(__file__).with_name('parameters.json')
COMPANY_PARAMETERS = {
    '1': {'power': 2.0, 'shrinkage': 50.0},
    '3': {'power': 2.0, 'shrinkage': 20.0},
}


@lru_cache(maxsize=1)
def configuration():
    """Load and verify the published V1 parameter snapshot and hash."""
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    if config['companies'] != COMPANY_PARAMETERS:
        raise ValueError('This package requires the published V1 parameter snapshot')
    config['version'] = VERSION + ':' + hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    if config['version'] != EXPECTED_CONFIGURATION_VERSION:
        raise ValueError('Published V1 configuration metadata or parameters changed')
    return config


DEFAULT = {'power': 2., 'shrinkage': 20.}


RESULTS = {'win': 1, 'half_win': 1, 'loss': -1, 'half_loss': -1}


def observations(company, scores=None):
    """Strict-first deduplication, valid rankable scores only. No missing=zero."""
    seen = set()
    for group in ('strict','approx'):
        for s in company.get('groups',{}).get(group,{}).get('samples',[]):
            mid = str(s.get('matchId') or '')
            # Upstream already deduplicates verified fixture identities. Retain
            # an additional same-ID guard across strict/approx here.
            if not mid or mid in seen:
                continue
            seen.add(mid)
            point = (scores or {}).get(group,{}).get(mid) if scores is not None else s.get('pointScore')
            point = point or {}
            value = point.get('direction_score')
            reason = None
            if point.get('rankable') is not True:
                reason = 'diagnostic'
            elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=100:
                reason = 'invalid_score'
            result = (s.get('settlements',{}).get('upper') or {}).get('result')
            if reason is None and result not in (*RESULTS,'push'):
                reason = 'unknown_result'
            yield {'matchId': mid, 'group': group, 'score': value, 'result': result,
                   'reason': reason, 'sample': s, 'pointScore': point}


def _aggregate_legacy(rows, parameters):
    p, alpha = parameters['power'], parameters['shrinkage']
    weights = []
    counts = Counter()
    excluded = Counter()
    for r in rows:
        if r['reason']:
            excluded[r['reason']] += 1
            continue
        counts[r['result']] += 1
        if r['result'] == 'push':
            continue
        w = (r['score']/100)**p if p else 1.
        weights.append((r,w,RESULTS[r['result']]))
    total = math.fsum(w for _,w,_ in weights)
    signed = math.fsum(w*z for _,w,z in weights)
    denominator = total + alpha
    score = 50 + 50*signed/denominator if total else None
    square = math.fsum(w*w for _,w,_ in weights)
    return {'score': score, 'sampleCount': len(weights), 'positiveWeightCount': sum(w>0 for _,w,_ in weights),
        'effectiveN': total*total/square if square else 0., 'weightSum': total,
        'weightedWinRate': (total+signed)/(2*total) if total else None,
        'counts': {k:counts[k] for k in ('win','half_win','push','half_loss','loss')},
        'excluded': dict(excluded), 'contributions': {r['matchId']:{'weight':w,
            'points':50*w*z/denominator if total else None,'group':r['group'],'result':r['result']}
            for r,w,z in weights}}


def score_company(company, scores=None, *, parameters=None, annotate=False):
    params = parameters or DEFAULT
    rows = list(observations(company,scores))
    out = aggregate(rows,params)
    contributions = out.pop('contributions')
    out['groups'] = {g:{k:v for k,v in aggregate([r for r in rows if r['group']==g],params).items()
                         if k!='contributions'} for g in ('strict','approx')}
    a,b = (out['groups'][g]['score'] for g in ('strict','approx'))
    out['groupConflict'] = a is not None and b is not None and (a-50)*(b-50)<0
    out['parameters'] = dict(params)
    out['status'] = 'available' if out['score'] is not None else 'insufficient_evidence'
    if annotate:
        for r in rows:
            r['sample']['directionContribution'] = contributions.get(r['matchId']) or {
                'weight': None, 'points': None, 'reason': r['reason'] or ('push' if r['result']=='push' else 'zero_weight')}
    return out


def aggregate(rows, parameters):
    """Validate V1 parameters, then use the unchanged legacy aggregation."""
    if any(key in parameters for key in
           ('weightBlend', 'amplitudeScale', 'amplitudeStrength')):
        raise ValueError('V2 direction weighting parameters are not supported by this V1 package')
    if not all(not isinstance(parameters.get(key), bool)
               and isinstance(parameters.get(key), (int, float))
               and math.isfinite(parameters[key]) and parameters[key] >= 0
               for key in ('power', 'shrinkage')):
        raise ValueError('Invalid direction parameters')
    return _aggregate_legacy(rows, parameters)
