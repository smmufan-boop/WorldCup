"""Join the published Asian snapshot with the euro/totals partial snapshot.

Builds, per real fixture, the IE1 market book at the T-10 cutoff:
  * Asian: V1 points() on odds_history rows (companies 1/3), minute-order evidence.
  * Totals/1X2: euro-totals ``quotes`` (Royal euro 545/size 3, Macau euro 80/size 1),
    keeping only valid prematch rows whose source minute ends at or before the
    cutoff (t10 strict rule). A minute flagged same_minute_conflict at the latest
    usable minute makes that market unavailable (fail closed, no price picking).
Settlement labels use the regular-90-minute rule in ../../DATA_DICTIONARY.md.
"""
from __future__ import annotations

import csv
import gzip
import json
import pickle
import re
from collections import defaultdict
from pathlib import Path

from quotes import asian_path, clock

ROOT = Path(__file__).resolve().parents[2]
ASIA = ROOT / "snapshots" / "2026-10-13" / "data"
ET = ROOT / "euro-totals" / "snapshots" / "2026-10-13-partial"
# euro-totals source ids -> IE1 company ids (Asian company ids of the same bookmaker)
ET_COMPANY = {("euro", "545"): "3", ("size", "3"): "3", ("euro", "80"): "1", ("size", "1"): "1"}
CUTOFF = 600


def _jsonl(prefix):
    for path in sorted((ET / "data").glob(prefix + "-part-*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def _csv(prefix):
    for path in sorted(ASIA.glob(prefix + "-part-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            yield from csv.DictReader(stream)


def regular_time_score(row):
    """Verified regular-90-minute score or None (DATA_DICTIONARY rule)."""
    note = row.get("score_note") or ""
    m = re.search(r"90分钟\s*\[?\s*(\d+)\s*[-:]\s*(\d+)", note)
    if m:
        return int(m.group(1)), int(m.group(2))
    if re.search(r"加时|点球|120", note + (row.get("period") or "")):
        return None
    if row.get("status") == "Played" and row.get("period") == "FT" and str(row.get("minute")) == "90":
        try:
            h, a = int(row["score_home"]), int(row["score_away"])
        except (TypeError, ValueError, KeyError):
            return None
        return (h, a) if h >= 0 and a >= 0 else None
    return None


def _et_path(rows, market):
    """Sorted, deduplicated path; None if the latest usable minute is conflicting."""
    if not rows:
        return []
    rows.sort(key=lambda r: (r["ts"], -r["order"]))  # source arrays are newest first
    if rows[-1]["conflict"]:
        return None
    path = []
    for r in rows:
        if r["conflict"]:
            continue  # unknown within-minute order; never pick one price
        key = (r["line"], r["a"], r["b"]) if market == "size" else (r["a"], r["d"], r["b"])
        if path and path[-1]["key"] == key:
            continue
        q = {"ts": r["ts"], "key": key}
        if market == "size":
            q.update(line=r["line"], over=r["a"], under=r["b"])
        else:
            q.update(home=r["a"], draw=r["d"], away=r["b"])
        path.append(q)
    for q in path:
        q.pop("key")
    return path


def build(cache=None):
    """Return a list of fixture dicts with book, meta and verified score."""
    if cache and Path(cache).exists():
        return pickle.loads(Path(cache).read_bytes())
    tasks, task_kick = {}, {}
    for r in _jsonl("tasks"):
        key = (r[2], r[3])
        if r[7] == "ok" and key in ET_COMPANY:
            tasks[r[0]] = (r[1], r[2], ET_COMPANY[key])
            task_kick[r[0]] = r[5]
    metas = {}
    for r in _jsonl("matches"):
        metas[r[0]] = json.loads(r[3])
    wanted = {mid for mid, market, company in tasks.values() if company == "3"}

    et_rows = defaultdict(list)  # (match, company, market) -> rows
    for q in _jsonl("quotes"):
        task = tasks.get(q[0])
        if task is None or not q[11] or not q[12] or q[2] is None:
            continue
        mid, market, company = task
        kickoff = task_kick[q[0]]
        cutoff = kickoff - CUTOFF
        if q[2] + 60 > cutoff:  # strict: whole source minute before the cutoff
            continue
        row = {"ts": q[2], "order": q[1], "conflict": bool(q[16])}
        if market == "size":
            row.update(line=q[9], a=q[8], b=q[10])
        else:
            row.update(a=q[5], d=q[6], b=q[7])
        et_rows[(mid, company, market)].append(row)

    asia_rows = defaultdict(list)
    for r in _csv("odds_history"):
        if r["company_id"] in ("1", "3") and r["match_id"] in wanted and r["market_type"] == "asia":
            asia_rows[(r["match_id"], r["company_id"])].append(r)
    order = defaultdict(lambda: defaultdict(list))
    for r in _csv("odds_minute_order"):
        if r["match_id"] in wanted and r["market_type"] == "asia":
            order[(r["match_id"], r["company_id"])][clock(r["change_time_local"])].append(r["sequence_json"])
    asia_meta = {r["match_id"]: r for r in _csv("matches") if r["match_id"] in wanted}

    fixtures, seen = [], set()
    for mid in sorted(wanted):
        meta, row = metas.get(mid) or {}, asia_meta.get(mid)
        group = meta.get("fixture_group") or mid
        if row is None or group in seen:
            continue
        kickoff = clock(row["kickoff_local"])
        if kickoff is None:
            continue
        cutoff = kickoff - CUTOFF
        book = {}
        for company in ("3", "1"):
            path, error = asian_path(asia_rows.get((mid, company), []), kickoff, cutoff,
                                     order.get((mid, company)))
            totals = _et_path(et_rows.get((mid, company, "size"), []), "size")
            euro = _et_path(et_rows.get((mid, company, "euro"), []), "euro")
            if path or totals or euro:
                book[company] = {"asian": path, "asianError": error,
                                 "totals": totals or [], "euro": euro or [],
                                 "totalsConflict": totals is None, "euroConflict": euro is None}
        if "3" not in book:
            continue
        seen.add(group)
        fixtures.append({"matchId": mid, "fixtureGroup": group, "kickoff": kickoff,
                         "competition": row["competition_name"], "home": row["team_A_name"],
                         "away": row["team_B_name"], "score": regular_time_score(row), "book": book})
    if cache:
        Path(cache).write_bytes(pickle.dumps(fixtures))
    return fixtures
