#!/usr/bin/env python3
"""Verify or restore the Euro / totals JSONL snapshot using Python's standard library."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from typing import Any, Iterator
import zlib


TABLES = ("matches", "tasks", "attempts", "quotes", "meta")
BATCH_SIZE = 5000
SQLITE_INTEGER_MIN = -(2 ** 63)
SQLITE_INTEGER_MAX = 2 ** 63 - 1


class SnapshotError(Exception):
    """The snapshot or destination does not satisfy its published contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotError(message)


def progress(phase: str, **fields: Any) -> None:
    print(json.dumps({"phase": phase, **fields}, ensure_ascii=False,
                     separators=(",", ":")), file=sys.stderr, flush=True)


def nonnegative_int(value: Any, label: str) -> int:
    require(type(value) is int and value >= 0, f"{label} must be a nonnegative integer")
    return value


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def snapshot_path(root: Path, relative: Any) -> Path:
    require(isinstance(relative, str) and bool(relative), "file path must be a nonempty string")
    require("\\" not in relative and ":" not in relative,
            f"use a relative POSIX file path: {relative}")
    pure = PurePosixPath(relative)
    require(not pure.is_absolute() and ".." not in pure.parts, f"unsafe file path: {relative}")
    require(str(pure) == relative, f"file path must be normalized: {relative}")
    path = (root / Path(*pure.parts)).resolve(strict=True)
    require(path.is_relative_to(root), f"file path escapes snapshot directory: {relative}")
    require(path.is_file(), f"not a regular file: {relative}")
    return path


def reject_json_constant(value: str) -> None:
    raise SnapshotError(f"non-finite JSON number is forbidden: {value}")


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def column_names(value: Any, label: str, *, empty: bool = False) -> list[str]:
    require(isinstance(value, list) and (empty or bool(value)), f"{label} must be a list of column names")
    require(all(isinstance(column, str) and column and "\x00" not in column for column in value),
            f"{label} must contain nonempty column names")
    require(len(value) == len(set(value)), f"{label} contains duplicate column names")
    return value


def check_sha256(value: Any, label: str) -> None:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None,
            f"{label} must be a 64-character SHA256 hex digest")


def read_manifest(root: Path) -> dict[str, Any]:
    with snapshot_path(root, "manifest.json").open("r", encoding="utf-8") as stream:
        manifest = json.load(stream, parse_constant=reject_json_constant,
                             object_pairs_hook=unique_json_object)
    require(isinstance(manifest, dict), "manifest.json must be a JSON object")
    require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1,
            "unsupported manifest schema_version (expected 1)")
    tables = manifest.get("tables")
    require(isinstance(tables, dict) and set(tables) == set(TABLES),
            f"manifest tables must be exactly {', '.join(TABLES)}")
    for table in TABLES:
        info = tables[table]
        require(isinstance(info, dict), f"tables.{table} must be an object")
        columns = column_names(info.get("columns"), f"tables.{table}.columns")
        primary_key = column_names(info.get("primary_key"), f"tables.{table}.primary_key")
        order_by = column_names(info.get("order_by"), f"tables.{table}.order_by")
        require(set(primary_key).issubset(columns), f"unknown primary key column for {table}")
        require(order_by == primary_key, f"{table}.order_by must equal primary_key in key order")
        nonnegative_int(info.get("rows"), f"tables.{table}.rows")
        check_sha256(info.get("row_sha256"), f"tables.{table}.row_sha256")
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
            check_sha256(entry.get("sha256"), f"{label}.sha256")
            if group == "files":
                table = entry.get("table")
                require(isinstance(table, str) and table in TABLES, f"unknown table in {label}: {table}")
                require(relative.endswith(".jsonl.gz"), f"data part must end in .jsonl.gz: {relative}")
                nonnegative_int(entry.get("rows"), f"{label}.rows")
                seen_tables.add(table)
    require(seen_tables == set(TABLES), "every table, including empty tables, must have a JSONL part")
    require(any(entry["path"] == "schema.sql" for entry in support),
            "support_files must include schema.sql")
    return manifest


def verify_file(root: Path, entry: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    size = 0
    with snapshot_path(root, entry["path"]).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    require(size == entry["bytes"],
            f"byte count mismatch: {entry['path']} (expected {entry['bytes']}, got {size})")
    require(digest.hexdigest() == entry["sha256"].lower(), f"SHA256 mismatch: {entry['path']}")


def validate_row(row: Any, columns: list[str], label: str) -> list[Any]:
    require(isinstance(row, list), f"JSONL row must be an array: {label}")
    require(len(row) == len(columns), f"JSONL column count mismatch: {label}")
    for column, value in zip(columns, row):
        require(value is None or type(value) in (str, int, float),
                f"cell must be SQL NULL, TEXT, INTEGER or REAL: {label}, column {column}")
        if type(value) is int:
            require(SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX,
                    f"INTEGER is outside SQLite's signed 64-bit range: {label}, column {column}")
        elif type(value) is float:
            require(math.isfinite(value), f"non-finite REAL: {label}, column {column}")
    return row


def jsonl_rows(root: Path, entry: dict[str, Any], columns: list[str]) -> Iterator[list[Any]]:
    with gzip.open(snapshot_path(root, entry["path"]), "rt", encoding="utf-8", newline="") as stream:
        for row_number, line in enumerate(stream, 1):
            label = f"{entry['path']} row {row_number}"
            require(bool(line.strip()), f"blank JSONL line: {label}")
            try:
                row = json.loads(line, parse_constant=reject_json_constant)
            except json.JSONDecodeError as error:
                raise SnapshotError(f"invalid JSONL: {label}: {error}") from error
            yield validate_row(row, columns, label)


def update_row_digest(digest: Any, row: list[Any] | tuple[Any, ...]) -> None:
    digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8"))
    digest.update(b"\n")


def schema_authorizer(action: int, arg1: str | None, arg2: str | None,
                      database: str | None, source: str | None) -> int:
    """Permit only the five ordinary table definitions and their ordinary indexes."""
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
    objects = connection.execute("SELECT type, name, tbl_name FROM sqlite_master").fetchall()
    require({(kind, name) for kind, name, _ in objects if kind != "index"}
            == {("table", table) for table in TABLES},
            "schema.sql must create exactly the five tables, without views or triggers")
    require(all(table in TABLES for kind, _, table in objects if kind == "index"),
            "schema.sql contains an index outside the five published tables")
    for table in TABLES:
        info = connection.execute(f"PRAGMA table_xinfo({quote_identifier(table)})").fetchall()
        require([column[1] for column in info] == manifest["tables"][table]["columns"],
                f"schema.sql column mismatch for {table}")
        require(all(column[2].strip().upper() in ("INTEGER", "REAL", "TEXT") and column[6] == 0
                    for column in info),
                f"schema.sql must use ordinary INTEGER, REAL or TEXT columns for {table}")
        primary_key = [column[1] for column in sorted(info, key=lambda column: column[5]) if column[5]]
        require(primary_key == manifest["tables"][table]["primary_key"],
                f"schema.sql primary key mismatch for {table}")
        references = connection.execute(f"PRAGMA foreign_key_list({quote_identifier(table)})").fetchall()
        require(all(reference[2] in TABLES for reference in references),
                f"schema.sql foreign key refers to an unpublished table: {table}")


def verify(snapshot_dir: str) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    root = Path(snapshot_dir).resolve(strict=True)
    require(root.is_dir(), f"snapshot directory does not exist: {snapshot_dir}")
    progress("verify_manifest")
    manifest = read_manifest(root)
    total_bytes = 0
    progress("verify_file_hashes", files=len(manifest["files"]) + len(manifest["support_files"]))
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
        for row in jsonl_rows(root, entry, manifest["tables"][table]["columns"]):
            update_row_digest(row_digests[table], row)
            count += 1
        require(count == entry["rows"],
                f"JSONL row count mismatch: {entry['path']} (expected {entry['rows']}, got {count})")
        counts[table] += count
        progress("verify_rows", path=entry["path"], rows=count)
    for table in TABLES:
        require(counts[table] == manifest["tables"][table]["rows"],
                f"table row count mismatch: {table} (expected {manifest['tables'][table]['rows']}, got {counts[table]})")
        require(row_digests[table].hexdigest() == manifest["tables"][table]["row_sha256"].lower(),
                f"table row SHA256 mismatch: {table}")
    result = {"ok": True, "command": "verify", "schema_version": 1,
              "files": len(manifest["files"]) + len(manifest["support_files"]),
              "bytes": total_bytes, "tables": counts, "row_sha256": "ok"}
    return root, manifest, script, result


def restore(snapshot_dir: str, destination: str) -> dict[str, Any]:
    target = Path(destination).absolute()
    require(not target.exists() and not target.is_symlink(), f"destination must not exist: {target}")
    require(target.parent.is_dir(), f"destination parent directory does not exist: {target.parent}")
    root, manifest, script, result = verify(snapshot_dir)
    # O_EXCL also protects against a destination created after the initial check.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(target)
        create_schema(connection, script, manifest)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        # Keep original foreign keys, allowing table order in the manifest to vary.
        connection.execute("PRAGMA defer_foreign_keys=ON")
        progress("restore_insert")
        for entry in manifest["files"]:
            table = entry["table"]
            columns = manifest["tables"][table]["columns"]
            names = ", ".join(quote_identifier(column) for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            insert = f"INSERT INTO {quote_identifier(table)} ({names}) VALUES ({placeholders})"
            batch: list[list[Any]] = []
            for row in jsonl_rows(root, entry, columns):
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    connection.executemany(insert, batch)
                    batch.clear()
            if batch:
                connection.executemany(insert, batch)
            progress("restore_part", path=entry["path"], rows=entry["rows"])
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
                  for table in TABLES}
        require(counts == result["tables"], "restored table counts do not match manifest")
        for table in TABLES:
            columns = manifest["tables"][table]["columns"]
            names = ", ".join(quote_identifier(column) for column in columns)
            order_by = ", ".join(quote_identifier(column) for column in manifest["tables"][table]["order_by"])
            digest = hashlib.sha256()
            for row in connection.execute(f"SELECT {names} FROM {quote_identifier(table)} ORDER BY {order_by}"):
                validate_row(list(row), columns, f"restored table {table}")
                update_row_digest(digest, row)
            require(digest.hexdigest() == manifest["tables"][table]["row_sha256"].lower(),
                    f"restored table row SHA256 mismatch: {table} (including native value types and NULL)")
            progress("restore_row_hash", table=table, rows=counts[table])
        progress("restore_integrity_check")
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        require(integrity == ["ok"], "restored SQLite integrity_check failed: " + "; ".join(integrity))
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        require(not foreign_keys, f"restored SQLite foreign_key_check failed: {foreign_keys[:5]}")
        connection.commit()
        connection.close()
        connection = None
    except BaseException:
        if connection is not None:
            connection.close()
        target.unlink(missing_ok=True)
        raise
    return {"ok": True, "command": "restore", "destination": str(target),
            "tables": counts, "row_sha256": "ok", "integrity_check": "ok", "foreign_key_check": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="check file hashes, schema, native JSON values and row hashes")
    check.add_argument("snapshot_dir", help="directory containing manifest.json, schema.sql and data/")
    recover = commands.add_parser("restore", help="verify, then create and check a new SQLite database")
    recover.add_argument("snapshot_dir", help="directory containing the snapshot")
    recover.add_argument("destination", help="new .sqlite3 file; its parent directory must already exist")
    args = parser.parse_args()
    try:
        result = verify(args.snapshot_dir)[3] if args.command == "verify" else restore(args.snapshot_dir, args.destination)
    except (SnapshotError, OSError, ValueError, EOFError, sqlite3.Error, zlib.error) as error:
        print(json.dumps({"ok": False, "command": args.command, "error": str(error)}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "command": args.command, "error": "interrupted"}))
        return 130
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
