#!/usr/bin/env python3
"""Idempotently backfill SQLite ad-control actions into ``ads_ai``."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def action_ids(limit=0):
    app.ensure_ad_control_tables()
    with app.JOB_DB_LOCK:
        conn = app.get_job_db_connection()
        try:
            sql = "SELECT action_id FROM ad_control_action ORDER BY created_at ASC"
            params = ()
            if limit:
                sql += " LIMIT ?"
                params = (int(limit),)
            return [str(row[0] or "") for row in conn.execute(sql, params).fetchall() if row[0]]
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite rows that already exist in ads_ai (normally unsafe for runner status)",
    )
    args = parser.parse_args()
    ids = action_ids(max(0, args.limit))
    if args.check:
        print(json.dumps({"source_count": len(ids), "check_only": True}, ensure_ascii=False))
        return 0
    if not hasattr(app, "ad_control_persist_action_log"):
        raise RuntimeError("app.py has not been patched with ads_ai action logging")
    success = 0
    skipped = 0
    errors = []
    for action_id in ids:
        try:
            if not args.force and app.ad_control_mysql_action(action_id):
                skipped += 1
                continue
            app.ad_control_persist_action_log(action_id)
            success += 1
        except Exception as exc:
            errors.append({"action_id": action_id, "error": str(exc)})
    print(json.dumps({
        "source_count": len(ids),
        "success_count": success,
        "existing_skipped_count": skipped,
        "error_count": len(errors),
        "errors": errors[:20],
    }, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
