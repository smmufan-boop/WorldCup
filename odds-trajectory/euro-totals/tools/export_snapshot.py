#!/usr/bin/env python3
"""Freeze an in-progress euro/totals SQLite run without writing to its source."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

TABLES = ("matches", "tasks", "attempts", "quotes", "meta")
FLAGS = ("quote_valid", "prematch", "t10_eligible", "t10_strict_eligible",
         "t10_cutoff_minute_ambiguous", "same_minute_conflict", "consecutive_duplicate")
BJT = timezone(timedelta(hours=8))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def file_info(root, path, **extra):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(), **extra}


def export(source, destination, rows_per_part):
    source, destination = source.resolve(), destination.resolve()
    if destination.exists() or rows_per_part < 1:
        raise ValueError("Use a new destination directory and positive rows_per_part")
    destination.mkdir(parents=True)
    (destination / "data").mkdir()
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        captured_at = datetime.now(BJT).isoformat(timespec="seconds")
        if set(definitions) != set(TABLES):
            raise ValueError("Unexpected source tables; review the export contract")
        indices = [sql for table, sql in db.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")
            if table in TABLES]
        schema_path = destination / "schema.sql"
        schema_path.write_text("-- Frozen euro/totals data; no worker, triggers, or live dispatch.\n" +
            "\n\n".join([definitions[name] + ";" for name in TABLES] + [sql + ";" for sql in indices]) + "\n",
            encoding="utf-8", newline="\n")
        manifest = {"schema_version": 1, "snapshot_at_bjt": captured_at,
            "source_run": "Pankou/ENN/market_backfill/run_20261013_045725_1f74d9",
            "source_database": "markets.sqlite3", "snapshot_kind": "in-progress partial backfill",
            "consistency": "single read-only SQLite transaction, including committed WAL",
            "data_format": "UTF-8 gzip JSON Lines; one native row array per line, no header",
            "null_policy": "SQL NULL maps to JSON null; empty TEXT stays empty string",
            "row_hash_format": "compact UTF-8 JSON array with ensure_ascii=False, allow_nan=False, followed by LF, in primary-key order",
            "rows_per_part": rows_per_part, "tables": {}, "files": [],
            "support_files": [file_info(destination, schema_path)]}
        groups = defaultdict(Counter)
        states = Counter()
        expected_quote_rows = 0
        fixture_groups = set()
        metadata_statuses = Counter()
        meta_values = {}
        for table in TABLES:
            infos = list(db.execute("PRAGMA table_info(" + quoted(table) + ")"))
            columns = [row[1] for row in infos]
            keys = [row[1] for row in sorted(infos, key=lambda row: row[5]) if row[5]]
            if not keys:
                raise ValueError(f"{table}: no stable primary key")
            fields = ",".join(map(quoted, columns))
            order = ",".join(map(quoted, keys))
            table_digest = hashlib.sha256()
            null_counts = Counter()
            ranges = {}
            flags = Counter()
            count = 0
            part = 0
            part_rows = 0
            compressed = None
            binary = None
            path = None

            def close_part():
                if compressed is not None:
                    compressed.close()
                    binary.close()
                    manifest["files"].append(file_info(destination, path, table=table, rows=part_rows))

            try:
                for row in db.execute("SELECT " + fields + " FROM " + quoted(table) + " ORDER BY " + order):
                    if compressed is None or part_rows == rows_per_part:
                        close_part()
                        part += 1
                        path = destination / "data" / f"{table}-part-{part:04d}.jsonl.gz"
                        binary = path.open("wb")
                        compressed = gzip.GzipFile(filename="", mode="wb", fileobj=binary, mtime=0, compresslevel=6)
                        part_rows = 0
                    content = encoded(row)
                    compressed.write(content)
                    table_digest.update(content)
                    part_rows += 1
                    count += 1
                    values = dict(zip(columns, row))
                    null_counts.update(name for name, value in values.items() if value is None)
                    for name in ("kickoff", "changed_at", "requested_at", "response_at", "updated_at"):
                        value = values.get(name)
                        if value is not None and value != "":
                            previous = ranges.get(name)
                            ranges[name] = {"min": min(previous["min"], value), "max": max(previous["max"], value)} if previous else {"min": value, "max": value}
                    if table == "matches":
                        metadata = json.loads(values["metadata_json"])
                        fixture_groups.add(str(metadata.get("fixture_group")))
                        metadata_statuses[str(metadata.get("status") or "")] += 1
                    elif table == "tasks":
                        key = (values["company"], values["market"])
                        group = groups[key]
                        group["tasks"] += 1
                        group["state:" + values["state"]] += 1
                        states[values["state"]] += 1
                        for name in ("raw_rows", "valid_rows", "t10_rows", "t10_strict_rows"):
                            group[name] += values[name]
                        group["tasks_with_t10"] += values["t10_rows"] > 0
                        group["tasks_with_t10_strict"] += values["t10_strict_rows"] > 0
                        expected_quote_rows += values["raw_rows"]
                    elif table == "quotes":
                        flags.update({name: values[name] for name in FLAGS})
                    elif table == "meta":
                        meta_values[values["key"]] = json.loads(values["value_json"])
                    if count % 100000 == 0:
                        print(json.dumps({"table": table, "exported_rows": count}), flush=True)
                if compressed is None:
                    part = 1
                    path = destination / "data" / f"{table}-part-{part:04d}.jsonl.gz"
                    with path.open("wb") as output:
                        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as stream:
                            pass
                    manifest["files"].append(file_info(destination, path, table=table, rows=0))
                else:
                    close_part()
                    compressed = None
                    binary = None
            finally:
                if compressed is not None:
                    compressed.close()
                if binary is not None:
                    binary.close()
            actual = db.execute("SELECT COUNT(*) FROM " + quoted(table)).fetchone()[0]
            if actual != count:
                raise ValueError(f"{table}: source count differs from exported row count")
            info = {"columns": columns, "column_types": [row[2] for row in infos],
                    "primary_key": keys, "order_by": keys, "rows": count,
                    "row_sha256": table_digest.hexdigest(), "null_fields": dict(null_counts), "ranges": ranges}
            if table == "quotes":
                info["flag_counts"] = dict(flags)
                if count != expected_quote_rows:
                    raise ValueError("Quote count differs from frozen tasks.raw_rows sum")
            manifest["tables"][table] = info
            print(json.dumps({"table": table, "rows": count, "parts": part}), flush=True)
        expected_total = manifest["tables"]["matches"]["rows"] * 4
        if manifest["tables"]["tasks"]["rows"] != expected_total:
            raise ValueError("Task inventory is not exactly four markets per source match ID")
        terminal = sum(states[name] for name in ("ok", "missing_company", "empty_detail", "failed"))
        remaining = sum(states[name] for name in ("pending", "running", "retry"))
        if terminal + remaining != expected_total:
            raise ValueError("Unrecognized task states")
        manifest["collection"] = {"state": meta_values.get("state"), "complete": False,
            "match_ids": manifest["tables"]["matches"]["rows"], "fixture_groups": len(fixture_groups),
            "metadata_status_counts": dict(metadata_statuses), "total_tasks": expected_total,
            "task_state_counts": dict(states), "terminal_processed": terminal, "remaining": remaining,
            "processed_percent": 100 * terminal / expected_total,
            "groups": [{"company": company, "market": market, **dict(group)} for (company, market), group in sorted(groups.items())],
            "completion_basis": "Partial export only; does not substitute for source final audit or completion.json"}
        # Progress is an auxiliary observation; the frozen SQL counts above are authoritative.
        for name in ("protocol.json", "progress.json", "progress.md", "audit/inventory.json"):
            original = source.parent / name
            if original.exists():
                target = destination / "source-evidence" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original.read_bytes())
                manifest["support_files"].append(file_info(destination, target))
        db.rollback()
    finally:
        db.close()
    manifest["export_finished_at_bjt"] = datetime.now(BJT).isoformat(timespec="seconds")
    manifest["total_compressed_bytes"] = sum(item["bytes"] for item in manifest["files"])
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"snapshot": str(destination), "rows": {table: info["rows"] for table, info in manifest["tables"].items()},
                      "compressed_bytes": manifest["total_compressed_bytes"], "collection": manifest["collection"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--rows-per-part", type=int, default=100000)
    args = parser.parse_args()
    export(args.source, args.destination, args.rows_per_part)
