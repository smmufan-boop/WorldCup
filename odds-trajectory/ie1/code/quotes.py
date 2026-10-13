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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "v1" / "code"))
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


def totals_path(rows, kickoff, cutoff):
    """IE1 size contract: handicap_value=goal line, home_water=over, away_water=under."""
    path = []
    for ts, row in sorted(_prematch(rows, kickoff, cutoff, "size"), key=lambda x: x[0]):
        line, over, under = (_float(row.get(k)) for k in ("handicap_value", "home_water", "away_water"))
        if None in (line, over, under) or line * 4 != int(line * 4) or not (0 < line <= 12):
            continue
        if not (0 < over <= 3 and 0 < under <= 3):
            continue
        q = {"ts": ts, "line": abs(line), "over": over, "under": under}
        if not path or (path[-1]["line"], path[-1]["over"], path[-1]["under"]) != (q["line"], over, under):
            path.append(q)
    return path


def euro_path(rows, kickoff, cutoff):
    """IE1 euro contract: home_water=home, handicap_value=draw, away_water=away (decimal)."""
    path = []
    for ts, row in sorted(_prematch(rows, kickoff, cutoff, "euro"), key=lambda x: x[0]):
        odds = [_float(row.get(k)) for k in ("home_water", "handicap_value", "away_water")]
        if None in odds or not all(1.01 <= o <= 100 for o in odds):
            continue
        q = {"ts": ts, "home": odds[0], "draw": odds[1], "away": odds[2]}
        if not path or [path[-1][k] for k in ("home", "draw", "away")] != odds:
            path.append(q)
    return path


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
        book[company] = {"asian": asian, "asianError": error,
                         "totals": totals_path(rows, kickoff, cutoff),
                         "euro": euro_path(rows, kickoff, cutoff)}
    return book
