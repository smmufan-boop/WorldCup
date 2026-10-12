"""Check whether the public odds endpoint serves euro (胜平负) and size (大小球).

Requests the same detail URL the collector uses for asia, varying only ``type``,
and prints the HTTP status, top-level JSON keys and the first entries of any
list found, so the response format can be compared with the asia one before
changing the collector. Read-only; Python 3.10+ standard library only.

    python odds-trajectory/tools/probe_markets.py 54470884
    python odds-trajectory/tools/probe_markets.py 54470884 --companies 1 3 8 --types asia euro size
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

URL = 'https://www.daqiuxiaoqiu.com/sport-data/soccer/biz/dqd/v1/match/odds/detail/{match}?companyId={company}&type={market}'


def lists(value, path='$', depth=0):
    """Yield (path, list) for every list inside the JSON, shallow first."""
    if depth > 4:
        return
    if isinstance(value, list):
        yield path, value
        for i, item in enumerate(value[:1]):
            yield from lists(item, f'{path}[{i}]', depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from lists(item, f'{path}.{key}', depth + 1)


def probe(match, company, market, timeout):
    url = URL.format(match=match, company=company, market=market)
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        return {'url': url, 'status': error.code, 'error': str(error)}
    except (urllib.error.URLError, TimeoutError) as error:
        return {'url': url, 'status': None, 'error': str(error)}
    out = {'url': url, 'status': status, 'bytes': len(body)}
    try:
        data = json.loads(body)
    except ValueError:
        out['nonJsonPreview'] = body[:300].decode('utf-8', 'replace')
        return out
    out['topLevelKeys'] = list(data) if isinstance(data, dict) else type(data).__name__
    found = [(p, v) for p, v in lists(data) if v]
    out['nonEmptyLists'] = [{'path': p, 'length': len(v), 'first': v[0], 'last': v[-1]} for p, v in found[:3]]
    if not found:
        out['preview'] = json.dumps(data, ensure_ascii=False)[:400]
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('match', help='source match_id, e.g. one from odds_history')
    parser.add_argument('--companies', nargs='+', default=['1', '3', '8'])
    parser.add_argument('--types', nargs='+', default=['asia', 'euro', 'size'])
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--pause', type=float, default=1.0, help='seconds between requests')
    args = parser.parse_args()
    for market in args.types:
        for company in args.companies:
            print(json.dumps(probe(args.match, company, market, args.timeout), ensure_ascii=False, indent=1))
            time.sleep(args.pause)


if __name__ == '__main__':
    main()
