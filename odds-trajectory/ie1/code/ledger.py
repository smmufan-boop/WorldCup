"""IE1 forward ledger (SQLite): every T-10 run, first eligible pick per fixture, settlement.

Rules mirrored from the V1 Royal T-10 registration:
* One real fixture -> at most one pick: the first run that produced tier A/B,
  generated before kickoff. Later runs never add, overwrite or flip it.
* The pick locks the exact company quote (line + odds + quote time) used for EV.
* Frozen inputs (book, parameters hash, evidence SHA-256s) are stored with each run.
* Settlement uses the locked terms and a verified regular-90-minute score only.
* Every on-time run also tracks its best stakeable candidate (registered pick or
  not, gates ignored) for closing-line value, filled in after kickoff.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

from quotes import BJT

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, fixture_key TEXT NOT NULL, match_id TEXT NOT NULL,
  kickoff REAL NOT NULL, cutoff REAL NOT NULL, generated_at REAL NOT NULL,
  params_version TEXT NOT NULL, params_hash TEXT NOT NULL, input_hash TEXT NOT NULL,
  status TEXT NOT NULL, tier TEXT, units REAL, decision_text TEXT,
  input_json TEXT NOT NULL, result_json TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS picks (
  fixture_key TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  match_id TEXT NOT NULL, competition TEXT, home TEXT, away TEXT, kickoff REAL NOT NULL,
  tier TEXT NOT NULL, units REAL NOT NULL, company TEXT NOT NULL, market TEXT NOT NULL,
  side TEXT NOT NULL, line REAL, odds REAL NOT NULL, quote_ts REAL NOT NULL, ev REAL NOT NULL,
  locked_at REAL NOT NULL, score_home INTEGER, score_away INTEGER, profit REAL, settled_at REAL);
CREATE TABLE IF NOT EXISTS clv (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id), kind TEXT NOT NULL, fixture_key TEXT NOT NULL,
  match_id TEXT NOT NULL, kickoff REAL NOT NULL, company TEXT NOT NULL, market TEXT NOT NULL,
  side TEXT NOT NULL, line REAL, odds REAL NOT NULL, ev REAL NOT NULL, age_minutes REAL,
  fallback_total REAL, close_ts REAL, close_line REAL, close_home REAL, close_away REAL,
  close_fair_p REAL, clv REAL, closed_at REAL, close_error TEXT);
"""


def fixture_key(meta):
    """Real-fixture identity: normalised teams + Beijing kickoff date (source ids differ)."""
    norm = lambda s: "".join(unicodedata.normalize("NFKC", str(s or "")).split()).casefold()
    day = datetime.fromtimestamp(meta["kickoff"], BJT).date().isoformat()
    return f"{norm(meta['home'])}|{norm(meta['away'])}|{day}"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class Ledger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def has_run(self, meta):
        return self.db.execute("SELECT 1 FROM runs WHERE match_id=? AND status IN ('ok','late') LIMIT 1",
                               (meta["matchId"],)).fetchone() is not None

    def record(self, meta, inputs, params, result, report, generated_at):
        """Store one run; register a pick if it is the first eligible one for the fixture."""
        key = fixture_key(meta)
        d = result.get("decision") or {}
        late = generated_at >= meta["kickoff"]
        status = "late" if late else "ok"
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (fixture_key,match_id,kickoff,cutoff,generated_at,params_version,params_hash,"
                "input_hash,status,tier,units,decision_text,input_json,result_json,report) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, meta["matchId"], meta["kickoff"], meta["kickoff"] - params["cutoffSeconds"], generated_at,
                 params["version"], digest(params), digest(inputs), status, d.get("tier"), d.get("units", 0.0),
                 d.get("text"), json.dumps(inputs, ensure_ascii=False, default=str),
                 json.dumps(result, ensure_ascii=False, default=str), report))
            run_id = cur.lastrowid
            pick = result.get("pick")
            registered = False
            if (not late and pick and d.get("tier") in ("A", "B") and d.get("units", 0) > 0
                    and self.db.execute("SELECT 1 FROM picks WHERE fixture_key=?", (key,)).fetchone() is None):
                self.db.execute(
                    "INSERT INTO picks (fixture_key,run_id,match_id,competition,home,away,kickoff,tier,units,company,"
                    "market,side,line,odds,quote_ts,ev,locked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, run_id, meta["matchId"], meta.get("competition"), meta["home"], meta["away"],
                     meta["kickoff"], d["tier"], d["units"], pick["company"], pick["market"], pick["side"],
                     pick["line"], pick["odds"], pick["quoteTs"], pick["ev"], generated_at))
                registered = True
            cand = next((c for c in result.get("candidates") or []
                         if c["market"] in result.get("stakeMarkets", ()) and c["company"] in
                         result.get("stakeCompanies", ())), None)
            if registered:
                cand = pick
            if cand and not late:
                self.db.execute(
                    "INSERT INTO clv (run_id,kind,fixture_key,match_id,kickoff,company,market,side,line,odds,ev,"
                    "age_minutes,fallback_total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, "pick" if registered else "candidate", key, meta["matchId"], meta["kickoff"],
                     cand["company"], cand["market"], cand["side"], cand["line"], cand["odds"], cand["ev"],
                     cand.get("ageMinutes"), (result.get("fairMarket") or {}).get("total")))
        return run_id, status, registered

    def open_clv(self, t):
        cur = self.db.execute("SELECT * FROM clv WHERE closed_at IS NULL AND kickoff <= ? ORDER BY kickoff", (t,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]

    def set_clv(self, run_id, data, when, error=None):
        with self.db:
            if data is None:
                self.db.execute("UPDATE clv SET closed_at=?, close_error=? WHERE run_id=?", (when, error, run_id))
                return
            self.db.execute(
                "UPDATE clv SET close_ts=?,close_line=?,close_home=?,close_away=?,close_fair_p=?,clv=?,closed_at=? "
                "WHERE run_id=?", (data["closeTs"], data["closeLine"], data["closeHome"], data["closeAway"],
                                   data["closeFairP"], data["clv"], when, run_id))

    def clv_rows(self):
        cur = self.db.execute("SELECT c.*, p.profit FROM clv c LEFT JOIN picks p ON p.run_id=c.run_id "
                              "WHERE c.clv IS NOT NULL ORDER BY c.kickoff")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]

    def pending(self):
        cols = ("fixture_key", "match_id", "kickoff", "market", "side", "line", "odds")
        return [dict(zip(cols, r)) for r in self.db.execute(
            "SELECT " + ",".join(cols) + " FROM picks WHERE settled_at IS NULL ORDER BY kickoff")]

    def settle(self, key, score, profit, when):
        with self.db:
            self.db.execute("UPDATE picks SET score_home=?,score_away=?,profit=?,settled_at=? "
                            "WHERE fixture_key=? AND settled_at IS NULL", (score[0], score[1], profit, when, key))

    def picks(self):
        cur = self.db.execute("SELECT * FROM picks ORDER BY kickoff")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]
