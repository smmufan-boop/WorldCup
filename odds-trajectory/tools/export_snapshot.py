#!/usr/bin/env python3
"""Export a consistent, read-only SQLite trajectory snapshot using stdlib only."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TABLES = ("matches", "odds_history", "odds_minute_order", "match_score_aliases")
BJT = timezone(timedelta(hours=8))


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_info(root, path, **extra):
    return {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
            "sha256": digest_file(path), **extra}


def export(source, destination, rows_per_part):
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("Snapshot destination already exists; choose a new directory")
    if rows_per_part < 1:
        raise ValueError("rows_per_part must be positive")
    destination.mkdir(parents=True)
    (destination / "data").mkdir()
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        # The first read pins a single WAL view for every exported table.
        definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        snapshot_time = datetime.now(BJT).isoformat(timespec="seconds")
        if any(table not in definitions for table in TABLES):
            raise ValueError("Source database is missing a required trajectory table")
        indices = [sql for table, sql in db.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")
            if table in TABLES]
        schema = "-- Trajectory snapshot: tables and source indices only.\n" + "\n\n".join(
            [definitions[table] + ";" for table in TABLES] + [sql + ";" for sql in indices]) + "\n"
        schema_path = destination / "schema.sql"
        schema_path.write_text(schema, encoding="utf-8", newline="\n")
        manifest = {"schema_version": 1, "snapshot_at_bjt": snapshot_time,
                    "source": "Pankou/live_dashboard/data/daqiu_odds/daqiu_odds.sqlite3",
                    "consistency": "one SQLite read transaction; includes committed WAL records",
                    "encoding": "UTF-8 without BOM", "csv_dialect": "comma; quoted fields; LF",
                    "null_policy": "no SQL NULL allowed; empty CSV cell is empty text",
                    "row_hash_format": "SHA256 of each row as compact UTF-8 JSON array followed by LF, in file order",
                    "rows_per_part": rows_per_part, "tables": {}, "files": [],
                    "support_files": [file_info(destination, schema_path)]}
        for table in TABLES:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            primary_key = [row[1] for row in sorted(db.execute(f'PRAGMA table_info("{table}")'), key=lambda r: r[5]) if row[5]]
            fields = ",".join('"' + name + '"' for name in columns)
            cursor = db.execute(f'SELECT {fields} FROM "{table}" ORDER BY rowid')
            empty_counts = Counter()
            groups = Counter()
            ranges = {}
            ids = set()
            name_replacement_rows = 0
            row_digest = hashlib.sha256()
            count = 0
            part = 0
            stream = None
            writer = None
            part_rows = 0
            path = None

            def close_part():
                if stream is not None:
                    stream.close()
                    manifest["files"].append(file_info(destination, path, table=table, rows=part_rows))

            try:
                for row in cursor:
                    if any(value is None or not isinstance(value, str) for value in row):
                        raise ValueError(f"{table}: NULL or non-text source value; export halted")
                    if writer is None or part_rows == rows_per_part:
                        close_part()
                        part += 1
                        path = destination / "data" / f"{table}-part-{part:04d}.csv.gz"
                        # Empty gzip filename and fixed mtime keep the format reproducible.
                        binary = path.open("wb")
                        compressed = gzip.GzipFile(filename="", mode="wb", fileobj=binary, mtime=0, compresslevel=6)
                        # GzipFile does not close an explicitly supplied file object.
                        class OwnedText(io.TextIOWrapper):
                            def close(self):
                                try:
                                    super().close()
                                finally:
                                    self._owned_binary.close()
                        stream = OwnedText(compressed, encoding="utf-8", newline="")
                        stream._owned_binary = binary
                        writer = csv.writer(stream, lineterminator="\n")
                        writer.writerow(columns)
                        part_rows = 0
                    writer.writerow(row)
                    row_digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                    count += 1
                    part_rows += 1
                    values = dict(zip(columns, row))
                    empty_counts.update(name for name, value in values.items() if value == "")
                    for name in ("kickoff_local", "change_time_local", "fetched_at_utc"):
                        value = values.get(name)
                        if value:
                            previous = ranges.get(name)
                            ranges[name] = {"min": min(previous["min"], value), "max": max(previous["max"], value)} if previous else {"min": value, "max": value}
                    if table == "odds_history":
                        ids.add(values["match_id"])
                        groups[(values["company_id"], values["market_type"])] += 1
                    names = ("competition_name", "team_A_name", "team_B_name", "home_team", "away_team", "handicap_text")
                    name_replacement_rows += any("\ufffd" in values.get(name, "") for name in names)
                    if count % 100000 == 0:
                        print(json.dumps({"table": table, "exported_rows": count}), flush=True)
                # Even an empty table has a gzip CSV containing its column header.
                if writer is None:
                    part = 1
                    path = destination / "data" / f"{table}-part-{part:04d}.csv.gz"
                    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                        csv.writer(handle, lineterminator="\n").writerow(columns)
                    manifest["files"].append(file_info(destination, path, table=table, rows=0))
                else:
                    close_part()
                    stream = None
            finally:
                if stream is not None and not stream.closed:
                    stream.close()
            expected = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count != expected:
                raise ValueError(f"{table}: exported count differs from pinned source count")
            table_info = {"columns": columns, "primary_key": primary_key, "rows": count,
                          "row_sha256": row_digest.hexdigest(), "empty_fields": dict(empty_counts),
                          "ranges": ranges, "rows_with_replacement_character_in_name_or_line": name_replacement_rows}
            if table == "odds_history":
                table_info.update(unique_match_ids=len(ids), company_market_counts=[
                    {"company_id": cid, "market_type": market, "rows": rows}
                    for (cid, market), rows in sorted(groups.items())])
                table_info["orphan_match_ids"] = db.execute(
                    "SELECT COUNT(DISTINCT o.match_id) FROM odds_history o LEFT JOIN matches m USING(match_id) WHERE m.match_id IS NULL").fetchone()[0]
            manifest["tables"][table] = table_info
            print(json.dumps({"table": table, "rows": count, "parts": part}), flush=True)
        db.rollback()
    finally:
        db.close()
    manifest["export_finished_at_bjt"] = datetime.now(BJT).isoformat(timespec="seconds")
    manifest["total_compressed_bytes"] = sum(item["bytes"] for item in manifest["files"])
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"snapshot": str(destination), "rows": {name: info["rows"] for name, info in manifest["tables"].items()},
                      "compressed_bytes": manifest["total_compressed_bytes"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="authoritative source SQLite database")
    parser.add_argument("destination", type=Path, help="new snapshot directory; must not exist")
    parser.add_argument("--rows-per-part", type=int, default=100000)
    args = parser.parse_args()
    export(args.source, args.destination, args.rows_per_part)
