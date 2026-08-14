#!/usr/bin/env python3
"""Back up and migrate explicit X account drama-language assignments."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
from pathlib import Path

from features.x_accounts.language import canonical_drama_language


def parse_assignment(value):
    raw = str(value or "")
    account_id, separator, language = raw.partition("=")
    if not separator or not account_id.isdigit() or int(account_id) <= 0:
        raise argparse.ArgumentTypeError("assignment must use ACCOUNT_ID=LANGUAGE")
    try:
        normalized = canonical_drama_language(language)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return int(account_id), normalized


def table_columns(conn, table):
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def backup_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("backup destination already exists")
    with contextlib.closing(sqlite3.connect(str(source))) as source_conn:
        with contextlib.closing(sqlite3.connect(str(destination))) as backup_conn:
            source_conn.backup(backup_conn)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--set", dest="assignments", action="append", required=True,
        type=parse_assignment,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)

    db_path = args.db.resolve()
    if not db_path.is_file():
        raise RuntimeError("X account database does not exist")
    assignments = dict(args.assignments)
    if len(assignments) != len(args.assignments):
        raise RuntimeError("account assignments must be unique")
    if args.apply and args.backup is None:
        raise RuntimeError("--apply requires --backup")

    with contextlib.closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        if "x_authorized_account" not in {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }:
            raise RuntimeError("x_authorized_account table is missing")
        columns = table_columns(conn, "x_authorized_account")
        placeholders = ",".join("?" for _item in assignments)
        rows = conn.execute(
            "SELECT id,username%s FROM x_authorized_account WHERE id IN (%s) "
            "ORDER BY id"
            % (
                ",drama_language" if "drama_language" in columns else "",
                placeholders,
            ),
            tuple(assignments),
        ).fetchall()
    found = {int(row["id"]): row for row in rows}
    missing = sorted(set(assignments).difference(found))
    if missing:
        raise RuntimeError("X account IDs do not exist: %s" % missing)

    preview = [
        {
            "id": account_id,
            "username": str(found[account_id]["username"] or ""),
            "from": (
                str(found[account_id]["drama_language"] or "en")
                if "drama_language" in found[account_id].keys()
                else "en"
            ),
            "to": language,
        }
        for account_id, language in sorted(assignments.items())
    ]
    if not args.apply:
        print(json.dumps({"mode": "dry_run", "items": preview}, ensure_ascii=False))
        return 0

    backup_path = args.backup.resolve()
    if backup_path == db_path:
        raise RuntimeError("backup path must differ from the source database")
    backup_database(db_path, backup_path)
    with contextlib.closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        if "drama_language" not in table_columns(conn, "x_authorized_account"):
            conn.execute(
                "ALTER TABLE x_authorized_account ADD COLUMN "
                "drama_language TEXT NOT NULL DEFAULT 'en'"
            )
        for account_id, language in assignments.items():
            cursor = conn.execute(
                "UPDATE x_authorized_account SET drama_language=? WHERE id=?",
                (language, account_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise RuntimeError("account update changed unexpectedly")
        conn.commit()
    print(
        json.dumps(
            {
                "mode": "applied",
                "backup": str(backup_path),
                "items": preview,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
