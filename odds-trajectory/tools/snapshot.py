#!/usr/bin/env python3
"""Verify and restore the published odds-trajectory snapshot (standard library only)."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from typing import Any, Iterator


TABLES = ("matches", "odds_history", "odds_minute_order", "match_score_aliases")
NULL_POLICY = "no SQL NULL allowed; empty CSV cell is empty text"
BATCH_SIZE = 5000


class SnapshotError(Exception):
    """A snapshot or destination does not meet the published contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotError(message)


def nonnegative_int(value: Any, label: str) -> int:
    require(type(value) is int and value >= 0, f"{label} must be a nonnegative integer")
    return value


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def snapshot_path(root: Path, relative: Any) -> Path:
    require(isinstance(relative, str) and bool(relative), "file path must be a nonempty string")
    require("\\" not in relative and ":" not in relative, f"use a relative POSIX file path: {relative}")
    pure = PurePosixPath(relative)
    require(not pure.is_absolute() and ".." not in pure.parts, f"unsafe file path: {relative}")
    require(str(pure) == relative, f"file path must be normalized: {relative}")
    path = (root / Path(*pure.parts)).resolve(strict=True)
    require(path.is_relative_to(root), f"file path escapes snapshot directory: {relative}")
    require(path.is_file(), f"not a regular file: {relative}")
    return path


def read_manifest(root: Path) -> dict[str, Any]:
    manifest_path = snapshot_path(root, "manifest.json")
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    require(isinstance(manifest, dict), "manifest.json must be a JSON object")
    require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1,
            "unsupported manifest schema_version (expected 1)")
    require(manifest.get("null_policy", NULL_POLICY) == NULL_POLICY,
            "unsupported null_policy; this tool preserves all CSV cells as text")
    tables = manifest.get("tables")
    require(isinstance(tables, dict) and set(tables) == set(TABLES),
            f"manifest tables must be exactly {', '.join(TABLES)}")
    for table in TABLES:
        info = tables[table]
        require(isinstance(info, dict), f"tables.{table} must be an object")
        columns = info.get("columns")
        require(isinstance(columns, list) and bool(columns), f"tables.{table}.columns must be a nonempty list")
        require(all(isinstance(column, str) and column and "\x00" not in column for column in columns),
                f"tables.{table}.columns must contain nonempty column names")
        require(len(columns) == len(set(columns)), f"tables.{table}.columns contains duplicates")
        nonnegative_int(info.get("rows"), f"tables.{table}.rows")
        row_digest = info.get("row_sha256")
        require(isinstance(row_digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", row_digest) is not None,
                f"tables.{table}.row_sha256 must be a 64-character SHA256 hex digest")
    files = manifest.get("files")
    support = manifest.get("support_files")
    require(isinstance(files, list) and bool(files), "manifest files must be a nonempty list")
    require(isinstance(support, list), "manifest support_files must be a list")
    seen_paths: set[str] = set()
    seen_tables: set[str] = set()
    for group, entries in (("files", files), ("support_files", support)):
        for number, entry in enumerate(entries, 1):
            label = f"{group}[{number}]"
            require(isinstance(entry, dict), f"{label} must be an object")
            relative = entry.get("path")
            snapshot_path(root, relative)
            require(relative not in seen_paths, f"duplicate manifest path: {relative}")
            require(relative != "manifest.json", "manifest.json cannot hash itself")
            seen_paths.add(relative)
            nonnegative_int(entry.get("bytes"), f"{label}.bytes")
            digest = entry.get("sha256")
            require(isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) is not None,
                    f"{label}.sha256 must be a 64-character SHA256 hex digest")
            if group == "files":
                table = entry.get("table")
                require(table in TABLES, f"unknown table in {label}: {table}")
                require(relative.endswith(".csv.gz"), f"CSV part must end in .csv.gz: {relative}")
                nonnegative_int(entry.get("rows"), f"{label}.rows")
                seen_tables.add(table)
    require(seen_tables == set(TABLES), "every table, including empty tables, must have a CSV part")
    require(any(entry["path"] == "schema.sql" for entry in support),
            "support_files must include schema.sql")
    return manifest


def verify_file(root: Path, entry: dict[str, Any]) -> None:
    path = snapshot_path(root, entry["path"])
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    require(size == entry["bytes"], f"byte count mismatch: {entry['path']} (expected {entry['bytes']}, got {size})")
    require(digest.hexdigest() == entry["sha256"].lower(), f"SHA256 mismatch: {entry['path']}")


def csv_rows(root: Path, entry: dict[str, Any], columns: list[str]) -> Iterator[list[str]]:
    """Yield UTF-8 CSV cells unchanged; an empty cell is an empty string."""
    path = snapshot_path(root, entry["path"])
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        header = next(reader, None)
        require(header == columns, f"CSV header mismatch: {entry['path']}")
        for row_number, row in enumerate(reader, 1):
            require(len(row) == len(columns),
                    f"CSV column count mismatch: {entry['path']} data row {row_number}")
            yield row


def update_row_digest(digest: Any, row: list[str] | tuple[str, ...]) -> None:
    digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def schema_authorizer(action: int, arg1: str | None, arg2: str | None,
                      database: str | None, source: str | None) -> int:
    """Permit only the four table definitions and ordinary indexes in schema.sql."""
    if source is not None or database not in (None, "main"):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_CREATE_TABLE:
        allowed = arg1 in TABLES
    elif action == sqlite3.SQLITE_CREATE_INDEX:
        allowed = arg2 in TABLES
    elif action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE):
        allowed = arg1 in ("sqlite_master", "sqlite_schema")
    elif action == sqlite3.SQLITE_READ:
        allowed = arg1 in (*TABLES, "sqlite_master", "sqlite_schema")
    elif action == sqlite3.SQLITE_REINDEX:
        allowed = True
    else:
        allowed = False
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY


def create_schema(connection: sqlite3.Connection, script: str,
                  manifest: dict[str, Any]) -> None:
    connection.set_authorizer(schema_authorizer)
    try:
        connection.executescript(script)
    finally:
        connection.set_authorizer(None)
    objects = connection.execute("SELECT type, name FROM sqlite_master WHERE type != 'index'").fetchall()
    require(set(objects) == {("table", table) for table in TABLES},
            "schema.sql must create exactly the four tables, without views or triggers")
    for table in TABLES:
        info = connection.execute(f"PRAGMA table_xinfo({quote_identifier(table)})").fetchall()
        require([column[1] for column in info] == manifest["tables"][table]["columns"],
                f"schema.sql column mismatch for {table}")
        require(all(column[2].strip().upper() == "TEXT" and column[6] == 0 for column in info),
                f"schema.sql must use ordinary TEXT columns for {table}")


def verify(snapshot_dir: str) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    root = Path(snapshot_dir).resolve(strict=True)
    require(root.is_dir(), f"snapshot directory does not exist: {snapshot_dir}")
    manifest = read_manifest(root)
    total_bytes = 0
    for entry in [*manifest["support_files"], *manifest["files"]]:
        verify_file(root, entry)
        total_bytes += entry["bytes"]
    script = snapshot_path(root, "schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(":memory:") as connection:
        create_schema(connection, script, manifest)
    counts = dict.fromkeys(TABLES, 0)
    row_digests = {table: hashlib.sha256() for table in TABLES}
    for entry in manifest["files"]:
        table = entry["table"]
        count = 0
        for row in csv_rows(root, entry, manifest["tables"][table]["columns"]):
            update_row_digest(row_digests[table], row)
            count += 1
        require(count == entry["rows"],
                f"CSV row count mismatch: {entry['path']} (expected {entry['rows']}, got {count})")
        counts[table] += count
    for table in TABLES:
        require(counts[table] == manifest["tables"][table]["rows"],
                f"table row count mismatch: {table} (expected {manifest['tables'][table]['rows']}, got {counts[table]})")
        require(row_digests[table].hexdigest() == manifest["tables"][table]["row_sha256"].lower(),
                f"table row SHA256 mismatch: {table}")
    result = {"ok": True, "command": "verify", "schema_version": 1,
              "files": len(manifest["files"]) + len(manifest["support_files"]),
              "bytes": total_bytes, "tables": counts}
    return root, manifest, script, result


def restore(snapshot_dir: str, destination: str) -> dict[str, Any]:
    root, manifest, script, result = verify(snapshot_dir)
    target = Path(destination).absolute()
    require(not target.exists() and not target.is_symlink(), f"destination must not exist: {target}")
    require(target.parent.is_dir(), f"destination parent directory does not exist: {target.parent}")
    # O_EXCL also protects the destination if it is created after the check above.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(target)
        create_schema(connection, script, manifest)
        connection.execute("BEGIN")
        for entry in manifest["files"]:
            table = entry["table"]
            columns = manifest["tables"][table]["columns"]
            names = ", ".join(quote_identifier(column) for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            insert = f"INSERT INTO {quote_identifier(table)} ({names}) VALUES ({placeholders})"
            batch: list[list[str]] = []
            for row in csv_rows(root, entry, columns):
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    connection.executemany(insert, batch)
                    batch.clear()
            if batch:
                connection.executemany(insert, batch)
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
                  for table in TABLES}
        require(counts == result["tables"], "restored table counts do not match manifest")
        for table in TABLES:
            columns = manifest["tables"][table]["columns"]
            names = ", ".join(quote_identifier(column) for column in columns)
            digest = hashlib.sha256()
            for row in connection.execute(f"SELECT {names} FROM {quote_identifier(table)} ORDER BY rowid"):
                require(all(isinstance(cell, str) for cell in row),
                        f"restored table contains a non-text or NULL value: {table}")
                update_row_digest(digest, row)
            require(digest.hexdigest() == manifest["tables"][table]["row_sha256"].lower(),
                    f"restored table row SHA256 mismatch: {table}")
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        require(integrity == ["ok"], "restored SQLite integrity_check failed: " + "; ".join(integrity))
        connection.commit()
        connection.close()
        connection = None
    except BaseException:
        if connection is not None:
            connection.close()
        target.unlink(missing_ok=True)
        raise
    return {"ok": True, "command": "restore", "destination": str(target),
            "tables": counts, "row_sha256": "ok", "integrity_check": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="check hashes, file sizes, schema and CSV row counts")
    check.add_argument("snapshot_dir", help="directory containing manifest.json, schema.sql and data/")
    recover = commands.add_parser("restore", help="verify, then create a new SQLite database")
    recover.add_argument("snapshot_dir", help="directory containing the snapshot")
    recover.add_argument("destination", help="new .sqlite3 file; its parent directory must already exist")
    args = parser.parse_args()
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            break
        except OverflowError:
            limit //= 10
    try:
        if args.command == "verify":
            result = verify(args.snapshot_dir)[3]
        else:
            result = restore(args.snapshot_dir, args.destination)
    except (SnapshotError, OSError, ValueError, EOFError, csv.Error, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "command": args.command, "error": str(error)}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "command": args.command, "error": "interrupted"}))
        return 130
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
