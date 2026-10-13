"""Quote acquisition and normalisation for IE1, following the repository V1 path.

Acquisition reuses the V1 source (daqiuxiaoqiu/Dongqiudi aggregation) and its
odds_history row contract. Asian rows are parsed with the V1 sign rules and the
V1 point builder (same-minute order evidence, T-cutoff, no in-play rows).
Totals ("size") and 1X2 ("euro") use the same endpoint with another ``type``;
their row mapping below is the IE1 contract that a collector must write.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2] / "v1" / "code"))
from matching_core import BJT, clock, daqiu_signed_line, points  # noqa: E402,F401

ODDS_URL = ("https://www.daqiuxiaoqiu.com/sport-data/soccer/biz/dqd/v1/match/odds/"
            "detail/{match_id}?companyId={company_id}&type={market}")
SCHEDULE_URL = "https://www.daqiuxiaoqiu.com/api/data/tab/new/important"
COMPANIES = {"1": "澳彩", "3": "Royal", "8": "Bet365"}
# The aggregation API uses market-specific company ids: euro is 545 for Royal
# and 80 for Macau, while asia/size use 3 and 1 (see euro-totals/DATA_DICTIONARY.md).
SOURCE_CID = {"asia": {"1": "1", "3": "3", "8": "8"}, "size": {"1": "1", "3": "3"},
              "euro": {"1": "80", "3": "545"}}
MARKETS = ("asia", "size", "euro")


def fetch_raw(match_id, company_id, market, timeout=20):
    """Download one raw payload; ``company_id`` is the IE1/Asian id (1 or 3)."""
    return _fetch(match_id, SOURCE_CID[market][company_id], market, timeout)


def _fetch(match_id, company_id, market, timeout):
    """Store the payload verbatim before parsing; the euro-totals collector
    (euro-totals/tools) defines the verified field mapping into ``quotes``."""
    if market not in MARKETS:
        raise ValueError("market must be asia, size or euro")
    url = ODDS_URL.format(match_id=match_id, company_id=company_id, market=market)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return {"url": url, "status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}


def _float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _prematch(rows, kickoff, cutoff, market):
    for row in rows:
        ts = clock(row.get("change_time_local"))
        if row.get("market_type") != market or row.get("score") or ts is None:
            continue
        if ts >= kickoff or ts > cutoff:
            continue
        yield ts, row


def asian_path(rows, kickoff, cutoff, order_evidence=None):
    """V1 Asian point path -> [{'ts','home_handicap','home','away'}] (HK waters).

    Returns (path, error). The V1 builder rejects ambiguous same-minute line
    switches; IE1 inherits that instead of inventing an order.
    """
    ticks, error = points(rows, kickoff, cutoff, order_evidence)
    path = [{"ts": t["ts"], "home_handicap": -t["line"], "home": t["home"], "away": t["away"]}
            for t in ticks]
    return path, error


def _minute_paths(rows, kickoff, cutoff, market, parse):
    """Totals/1X2 path with the euro-totals strict-minute and same-minute rules.

    A source minute is used only when it ends at or before the cutoff. A minute
    holding different quotes has unverified internal order, so it is skipped; if
    the latest usable minute is such a conflict the market is unavailable (None).
    """
    minutes = {}
    for ts, row in _prematch(rows, kickoff, cutoff - 60, market):
        q = parse(row)
        if q is not None:
            minutes.setdefault(ts, set()).add(q)
    if not minutes:
        return []
    if len(minutes[max(minutes)]) > 1:
        return None
    path = []
    for ts in sorted(minutes):
        if len(minutes[ts]) > 1:
            continue
        (q,) = minutes[ts]
        if not path or path[-1][1] != q:
            path.append((ts, q))
    return path


def _size_quote(row):
    line, over, under = (_float(row.get(k)) for k in ("handicap_value", "home_water", "away_water"))
    if None in (line, over, under) or line < 0 or line > 12 or line * 4 != int(line * 4):
        return None
    return (line, over, under) if 0 < over <= 3 and 0 < under <= 3 else None


def _euro_quote(row):
    odds = tuple(_float(row.get(k)) for k in ("home_water", "handicap_value", "away_water"))
    return odds if None not in odds and all(1.01 <= o <= 100 for o in odds) else None


def totals_path(rows, kickoff, cutoff):
    """IE1 size contract: handicap_value=goal line, home_water=over, away_water=under (HK)."""
    path = _minute_paths(rows, kickoff, cutoff, "size", _size_quote)
    return None if path is None else [{"ts": t, "line": q[0], "over": q[1], "under": q[2]} for t, q in path]


def euro_path(rows, kickoff, cutoff):
    """IE1 euro contract: home_water=home, handicap_value=draw, away_water=away (decimal)."""
    path = _minute_paths(rows, kickoff, cutoff, "euro", _euro_quote)
    return None if path is None else [{"ts": t, "home": q[0], "draw": q[1], "away": q[2]} for t, q in path]


def at_or_before(path, ts):
    """Last real quote at or before ``ts`` (never interpolated)."""
    found = None
    for q in path:
        if q["ts"] <= ts:
            found = q
        else:
            break
    return found


def asian_decimal(q):
    return (q["home_handicap"], 1 + q["home"], 1 + q["away"])


def totals_decimal(q):
    return (q["line"], 1 + q["over"], 1 + q["under"])


def euro_decimal(q):
    return (q["home"], q["draw"], q["away"])


def market_book(rows_by_company, kickoff, cutoff, order_evidence=None):
    """Build per-company Asian/totals/1X2 paths at one decision cutoff.

    ``rows_by_company`` maps company_id -> odds_history-like rows (any market).
    ``order_evidence`` maps company_id -> {ts: [sequence_json, ...]} for Asian rows.
    """
    book = {}
    for company, rows in rows_by_company.items():
        asian, error = asian_path(rows, kickoff, cutoff, (order_evidence or {}).get(company))
        totals, euro = totals_path(rows, kickoff, cutoff), euro_path(rows, kickoff, cutoff)
        book[company] = {"asian": asian, "asianError": error, "totals": totals or [], "euro": euro or [],
                         "totalsConflict": totals is None, "euroConflict": euro is None}
    return book
