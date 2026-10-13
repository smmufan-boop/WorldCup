"""Raw aggregation-API payload -> odds_history-style rows for IE1.

The detail list in the payload is newest-first. Each element carries three price
fields (default keys homeWin / draw / awayWin, as recorded by the euro-totals
collector) plus a minute-precision ``MM-DD HH:MM`` source time and an in-play
score marker. Meaning per market:

  asia : homeWin = home HK water, draw = handicap text (受 = home receives), awayWin = away HK water
  size : homeWin = over HK water,  draw = goal line text ("2/2.5" = 2.25),  awayWin = under HK water
  euro : homeWin / draw / awayWin = decimal 1X2 odds

Field names are configurable (parameters.json -> payload). Run ``ie1.py probe``
on one live match first: it prints raw elements next to parsed rows, and parsing
fails closed (row dropped with a reason) rather than guessing.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime

import json

from quotes import BJT, clock

DEFAULT_FIELDS = {"list": "detail", "home": "homeWin", "mid": "draw", "away": "awayWin",
                  "time": ["time", "changeTime", "change_time", "updateTime", "date"],
                  "score": ["score", "matchScore"], "handicapValue": ["handicap", "handicapValue", "pk"]}

# Chinese Asian-handicap words -> goals. Compound "A/B" texts average the halves.
_WORDS = {"平手": 0.0, "平": 0.0, "半球": 0.5, "半": 0.5, "一球": 1.0, "一": 1.0, "球半": 1.5,
          "两球": 2.0, "两": 2.0, "两球半": 2.5, "三球": 3.0, "三": 3.0, "三球半": 3.5,
          "四球": 4.0, "四": 4.0, "四球半": 4.5, "五球": 5.0, "五": 5.0}
_TIME = re.compile(r"^\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s*$")


def find_list(payload, key):
    """Depth-first search for the first list stored under ``key``."""
    if isinstance(payload, dict):
        if isinstance(payload.get(key), list):
            return payload[key]
        for value in payload.values():
            found = find_list(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_list(value, key)
            if found is not None:
                return found
    return None


def handicap_goals(text):
    """'受一/球半' -> -1.25 (home receives), '半球' -> 0.5, '平手' -> 0.0."""
    t = unicodedata.normalize("NFKC", str(text or "")).strip().replace("受让", "受")
    if not t:
        return None
    sign = -1.0 if t.startswith("受") else 1.0
    t = t.lstrip("受").strip()
    parts = t.split("/")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        values = [_WORDS.get(p.strip()) for p in parts]
        if any(v is None for v in values):
            return None
    goals = sum(values) / len(values)
    return sign * goals if goals * 4 == int(goals * 4) else None


def goal_line(text):
    """'2/2.5' -> 2.25, '2.75' -> 2.75; non-quarter or negative -> None."""
    try:
        parts = [float(p) for p in str(text).strip().split("/")]
    except ValueError:
        return None
    line = sum(parts) / len(parts)
    return line if line >= 0 and line * 4 == int(line * 4) else None


def source_clock(text, kickoff):
    """Minute text without year -> BJT ISO time, year nearest to kickoff (None if tied)."""
    m = _TIME.match(str(text or ""))
    if not m:
        return None
    month, day, hour, minute = map(int, m.groups())
    k = datetime.fromtimestamp(kickoff, BJT)
    options = []
    for year in (k.year - 1, k.year, k.year + 1):
        try:
            options.append(datetime(year, month, day, hour, minute, tzinfo=BJT))
        except ValueError:
            continue
    if not options:
        return None
    options.sort(key=lambda d: abs((d - k).total_seconds()))
    if len(options) > 1 and abs(abs((options[0] - k).total_seconds()) - abs((options[1] - k).total_seconds())) < 60:
        return None
    return options[0]


def _first(element, keys):
    for key in keys:
        if key in element and element[key] not in (None, ""):
            return element[key]
    return None


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def rows_from_payload(payload, market, match_id, company_id, kickoff, fields=None, url=""):
    """Return (rows, problems). Rows follow the odds_history column names."""
    f = {**DEFAULT_FIELDS, **(fields or {})}
    detail = find_list(payload, f["list"])
    if detail is None:
        return [], [f"payload has no '{f['list']}' list"]
    rows, problems = [], []
    n = len(detail)
    for index, element in enumerate(detail):
        if not isinstance(element, dict):
            problems.append(f"#{index}: element is not an object")
            continue
        when = source_clock(_first(element, f["time"]), kickoff)
        if when is None:
            problems.append(f"#{index}: unparseable source time")
            continue
        score = _first(element, f["score"]) or ""
        a, mid, b = element.get(f["home"]), element.get(f["mid"]), element.get(f["away"])
        row = {"match_id": str(match_id), "company_id": str(company_id), "market_type": market,
               "source_time": when.strftime("%m-%d %H:%M"), "change_time_local": when.isoformat(timespec="minutes"),
               "score": str(score).strip(), "source_url": url,
               "source_order": n - 1 - index,  # oldest = 0 (payload is newest-first)
               "raw": [a, mid, b]}
        if market == "asia":
            goals = handicap_goals(mid)
            numeric = _num(_first(element, f["handicapValue"]))
            if goals is None or (numeric is not None and abs(abs(numeric) - abs(goals)) > 1e-9):
                problems.append(f"#{index}: handicap '{mid}' unparseable or disagrees with numeric {numeric}")
                continue
            row.update(handicap_text=str(mid), handicap_value=f"{abs(goals):.2f}",
                       home_water=str(a), away_water=str(b))
        elif market == "size":
            line = goal_line(mid)
            if line is None:
                problems.append(f"#{index}: goal line '{mid}' unparseable")
                continue
            row.update(handicap_text=str(mid), handicap_value=f"{line:.2f}", home_water=str(a), away_water=str(b))
        else:
            if None in (_num(a), _num(mid), _num(b)):
                problems.append(f"#{index}: 1X2 prices not numeric")
                continue
            row.update(home_water=str(a), handicap_value=str(mid), away_water=str(b), handicap_text="")
        rows.append(row)
    return rows, problems


def minute_order_evidence(rows):
    """Asian same-minute order evidence from one payload, in the V1 format.

    Returns {ts: [sequence_json]} where each sequence lists that minute's quotes
    oldest first as [handicap_text, handicap_value, home_water, away_water].
    """
    by_minute = {}
    for r in sorted(rows, key=lambda r: r["source_order"]):
        if r["market_type"] != "asia" or r["score"]:
            continue
        by_minute.setdefault(clock(r["change_time_local"]), []).append(
            [r["handicap_text"], r["handicap_value"], r["home_water"], r["away_water"]])
    return {ts: [json.dumps(seq, ensure_ascii=False)] for ts, seq in by_minute.items() if len(seq) > 1}

