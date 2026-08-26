#!/usr/bin/env python3
"""Back up and normalize legacy drama output selections without reinterpreting history."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

OUTPUT_KEYS = ("concat_video", "no_bgm_video", "cover_16x9", "random_template")
REQUIRED_COLUMNS = {"id", "outputs_json", "output_video_url", "output_video_no_bgm_url", "cover_16x9_url"}


def normalize(row):
    try:
        raw = json.loads(row["outputs_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("row %s has invalid outputs_json" % row["id"]) from exc
    if not isinstance(raw, dict):
        raise ValueError("row %s outputs_json is not an object" % row["id"])
    legacy_random = bool(raw.get("random_template", raw.get("random_template_video", False)))
    values = {
        "concat_video": bool(raw["concat_video"]) if "concat_video" in raw else bool(row["output_video_url"]),
        "no_bgm_video": bool(raw["no_bgm_video"]) if "no_bgm_video" in raw else bool(row["output_video_no_bgm_url"]),
        "cover_16x9": bool(raw["cover_16x9"]) if "cover_16x9" in raw else bool(row["cover_16x9_url"]),
        "random_template": legacy_random,
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migrate(db_path, *, apply=False, backup_path=None):
    db = Path(db_path).resolve(strict=True)
    if not db.is_file():
        raise ValueError("database path must be a file")
    if apply:
        if not backup_path:
            raise ValueError("--backup is required with --apply")
        backup = Path(backup_path).resolve()
        if not backup.is_absolute() or backup == db or backup.exists():
            raise ValueError("backup must be a new absolute path")
        backup.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=30)
        destination = sqlite3.connect(str(backup), timeout=30)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        with backup.open("rb+") as handle:
            os.fsync(handle.fileno())
    uri = "file:%s?mode=%s" % (db.as_posix(), "rw" if apply else "ro")
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(drama_material_job)")}
        if not REQUIRED_COLUMNS.issubset(columns):
            raise ValueError("drama_material_job schema mismatch")
        rows = conn.execute(
            "SELECT id,outputs_json,output_video_url,output_video_no_bgm_url,cover_16x9_url FROM drama_material_job ORDER BY id"
        ).fetchall()
        changes = []
        for row in rows:
            encoded = normalize(row)
            if encoded != row["outputs_json"]:
                changes.append((row["id"], row["outputs_json"], encoded))
        if apply and changes:
            conn.execute("BEGIN IMMEDIATE")
            for row_id, original, encoded in changes:
                cursor = conn.execute(
                    "UPDATE drama_material_job SET outputs_json=? WHERE id=? AND outputs_json=?",
                    (encoded, row_id, original),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("concurrent output migration conflict")
            conn.commit()
        return {"rows": len(rows), "changes": len(changes), "applied": bool(apply)}
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate and report changes without writing (default)")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    args = parser.parse_args(argv)
    result = migrate(args.db_path, apply=args.apply, backup_path=args.backup)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
