#!/usr/bin/env python3
"""Safely backfill tenant-aware owners for legacy X authorization rows.

The schema migration intentionally leaves ``owner_tenant_key`` empty because the
old X database did not store it. This operator tool joins the legacy owner user
ID to the AI backend's ``drama_admin_user`` table and writes only unique,
non-empty tenant matches. It is a dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


DEFAULT_X_DB = "/var/lib/x-post-automation/accounts.sqlite3"
DEFAULT_ADMIN_DB = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
REQUIRED_X_COLUMNS = {
    "id",
    "owner_tenant_key",
    "owner_user_id",
    "owner_name",
    "owner_email",
}


def table_columns(conn, table):
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def require_schema(x_conn, admin_conn):
    x_columns = table_columns(x_conn, "x_authorized_account")
    missing = sorted(REQUIRED_X_COLUMNS - x_columns)
    if missing:
        raise RuntimeError(
            "x_authorized_account is missing migrated columns: %s; run ensure_storage first"
            % ",".join(missing)
        )
    admin_columns = table_columns(admin_conn, "drama_admin_user")
    required_admin = {"user_id", "tenant_key", "name"}
    missing_admin = sorted(required_admin - admin_columns)
    if missing_admin:
        raise RuntimeError("drama_admin_user is missing columns: %s" % ",".join(missing_admin))
    return admin_columns


def backfill_legacy_owners(x_db, admin_db, apply=False):
    x_db = Path(x_db)
    admin_db = Path(admin_db)
    if not x_db.is_file():
        raise RuntimeError("X accounts database does not exist: %s" % x_db)
    if not admin_db.is_file():
        raise RuntimeError("AI backend database does not exist: %s" % admin_db)

    x_conn = sqlite3.connect(str(x_db), timeout=30)
    admin_conn = sqlite3.connect(str(admin_db), timeout=30)
    x_conn.row_factory = sqlite3.Row
    admin_conn.row_factory = sqlite3.Row
    try:
        x_conn.execute("PRAGMA busy_timeout=30000")
        admin_conn.execute("PRAGMA busy_timeout=30000")
        admin_columns = require_schema(x_conn, admin_conn)
        legacy_rows = x_conn.execute(
            """
            SELECT id,owner_user_id,owner_name,owner_email
            FROM x_authorized_account
            WHERE TRIM(COALESCE(owner_tenant_key,''))=''
            ORDER BY id
            """
        ).fetchall()

        select_fields = ["user_id", "tenant_key", "name"]
        if "email" in admin_columns:
            select_fields.append("email")
        resolutions = []
        unresolved = []
        for row in legacy_rows:
            owner_user_id = str(row["owner_user_id"] or "").strip()
            if not owner_user_id:
                unresolved.append({"account_id": int(row["id"]), "reason": "empty_owner_user_id"})
                continue
            matches = admin_conn.execute(
                "SELECT %s FROM drama_admin_user WHERE user_id=?" % ",".join(select_fields),
                (owner_user_id,),
            ).fetchall()
            if len(matches) != 1:
                unresolved.append({"account_id": int(row["id"]), "reason": "match_count_%d" % len(matches)})
                continue
            match = matches[0]
            tenant_key = str(match["tenant_key"] or "").strip()
            if not tenant_key:
                unresolved.append({"account_id": int(row["id"]), "reason": "empty_tenant_key"})
                continue
            resolutions.append(
                {
                    "account_id": int(row["id"]),
                    "owner_user_id": owner_user_id,
                    "owner_tenant_key": tenant_key,
                    "owner_name": str(match["name"] or row["owner_name"] or ""),
                    "owner_email": str(
                        (match["email"] if "email" in select_fields else "") or row["owner_email"] or ""
                    ),
                }
            )

        updated = 0
        if apply and resolutions:
            x_conn.execute("BEGIN IMMEDIATE")
            try:
                for item in resolutions:
                    cursor = x_conn.execute(
                        """
                        UPDATE x_authorized_account
                        SET owner_tenant_key=?,owner_name=?,owner_email=?
                        WHERE id=? AND owner_user_id=?
                          AND TRIM(COALESCE(owner_tenant_key,''))=''
                        """,
                        (
                            item["owner_tenant_key"],
                            item["owner_name"],
                            item["owner_email"],
                            item["account_id"],
                            item["owner_user_id"],
                        ),
                    )
                    updated += int(cursor.rowcount or 0)
                if updated != len(resolutions):
                    raise RuntimeError("owner rows changed concurrently; rolling back")
                x_conn.commit()
            except Exception:
                x_conn.rollback()
                raise

        return {
            "mode": "apply" if apply else "dry-run",
            "legacy_rows": len(legacy_rows),
            "resolvable_rows": len(resolutions),
            "updated_rows": updated,
            "unresolved_rows": len(unresolved),
            "unresolved": unresolved,
        }
    finally:
        admin_conn.close()
        x_conn.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--x-db", default=DEFAULT_X_DB)
    parser.add_argument("--admin-db", default=DEFAULT_ADMIN_DB)
    parser.add_argument("--apply", action="store_true", help="write unique owner matches")
    parser.add_argument(
        "--require-all-resolved",
        action="store_true",
        help="exit 2 when any legacy row remains unresolved",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        result = backfill_legacy_owners(args.x_db, args.admin_db, apply=args.apply)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    if args.require_all_resolved and result["unresolved_rows"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
