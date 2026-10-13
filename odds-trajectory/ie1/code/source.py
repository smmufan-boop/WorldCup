"""Polite live acquisition for IE1 (same endpoint and company ids as V1/euro-totals).

* At most one request per ``minIntervalSeconds`` across the process.
* HTTP 401/403/429 stops all new requests (SourceBlocked); review before resuming.
* Every response body is saved verbatim (gzip + SHA-256 of the body) before parsing,
  so a decision can be audited later against exactly what was fetched.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from quotes import ODDS_URL, SOURCE_CID


class SourceBlocked(RuntimeError):
    """The source refused access; stop dispatching until a human reviews it."""


class Fetcher:
    def __init__(self, evidence_dir, min_interval=0.5, timeout=12, attempts=3, retry_delays=(5, 20)):
        self.dir = Path(evidence_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.min_interval, self.timeout = min_interval, timeout
        self.attempts, self.retry_delays = attempts, retry_delays
        self._last = 0.0
        self.blocked = None

    def _wait(self):
        delay = self._last + self.min_interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def get(self, url, tag):
        if self.blocked:
            raise SourceBlocked(self.blocked)
        error = None
        for attempt in range(self.attempts):
            self._wait()
            requested = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body, status = resp.read(), resp.status
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    self.blocked = f"HTTP {exc.code} at {url}"
                    raise SourceBlocked(self.blocked) from exc
                error = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = str(exc)
            else:
                responded = datetime.now(timezone.utc).isoformat(timespec="seconds")
                digest = hashlib.sha256(body).hexdigest()
                path = self.dir / f"{tag}-{digest[:16]}.json.gz"
                envelope = {"url": url, "status": status, "requestedAt": requested, "respondedAt": responded,
                            "sha256": digest, "body": body.decode("utf-8", errors="replace")}
                with gzip.open(path, "wt", encoding="utf-8") as fh:
                    json.dump(envelope, fh, ensure_ascii=False)
                return json.loads(body), {"path": str(path), "sha256": digest, "respondedAt": responded}
            if attempt < self.attempts - 1:
                time.sleep(self.retry_delays[min(attempt, len(self.retry_delays) - 1)])
        raise RuntimeError(f"Request failed after {self.attempts} attempts: {error}")

    def market(self, match_id, company_id, market):
        cid = SOURCE_CID[market].get(str(company_id))
        if cid is None:
            return None, None
        url = ODDS_URL.format(match_id=match_id, company_id=cid, market=market)
        return self.get(url, f"{match_id}-{market}-{cid}")


def load_fixtures_csv(path):
    """CSV columns: match_id, competition, home, away, kickoff_local (ISO with +08:00)."""
    import csv

    from quotes import clock
    with open(path, encoding="utf-8-sig", newline="") as fh:
        out = []
        for row in csv.DictReader(fh):
            kickoff = clock(row["kickoff_local"])
            if kickoff is not None:
                out.append({"matchId": row["match_id"].strip(), "competition": row["competition"].strip(),
                            "home": row["home"].strip(), "away": row["away"].strip(), "kickoff": kickoff})
    return out


def load_fixtures_db(path, start, end):
    """Read fixtures from a V1-schema SQLite ``matches`` table (read-only)."""
    import sqlite3

    from quotes import clock
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT match_id, competition_name, team_A_name, team_B_name, kickoff_local, status "
                          "FROM matches WHERE cmp_type='soccer'").fetchall()
    finally:
        db.close()
    out = []
    for mid, comp, home, away, kick, status in rows:
        k = clock(kick)
        if k is not None and start <= k <= end and status not in ("Postponed", "Cancelled"):
            out.append({"matchId": mid, "competition": comp, "home": home, "away": away, "kickoff": k})
    return out
