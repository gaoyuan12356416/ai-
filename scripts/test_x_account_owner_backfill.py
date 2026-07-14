#!/usr/bin/env python3
"""Tests for the legacy X owner tenant backfill tool."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backfill_x_account_owners import backfill_legacy_owners, main  # noqa: E402


class XOwnerBackfillTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.x_db = root / "accounts.sqlite3"
        self.admin_db = root / "admin.sqlite3"
        with contextlib.closing(sqlite3.connect(self.x_db)) as conn:
            conn.executescript(
                """
                CREATE TABLE x_authorized_account (
                  id INTEGER PRIMARY KEY,
                  owner_tenant_key TEXT NOT NULL DEFAULT '',
                  owner_user_id TEXT NOT NULL DEFAULT '',
                  owner_name TEXT NOT NULL DEFAULT '',
                  owner_email TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO x_authorized_account VALUES(1,'','u1','Old One','old1@example.com');
                INSERT INTO x_authorized_account VALUES(2,'','u2','Old Two','old2@example.com');
                INSERT INTO x_authorized_account VALUES(3,'','u3','Old Three','old3@example.com');
                INSERT INTO x_authorized_account VALUES(4,'','u4','Old Four','old4@example.com');
                INSERT INTO x_authorized_account VALUES(5,'tenant-existing','u5','Existing','existing@example.com');
                INSERT INTO x_authorized_account VALUES(6,'','','Ownerless','ownerless@example.com');
                """
            )
            conn.commit()
        with contextlib.closing(sqlite3.connect(self.admin_db)) as conn:
            conn.executescript(
                """
                CREATE TABLE drama_admin_user (
                  user_id TEXT NOT NULL,
                  tenant_key TEXT NOT NULL DEFAULT '',
                  name TEXT NOT NULL DEFAULT '',
                  email TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO drama_admin_user VALUES('u1','tenant-a','New One','new1@example.com');
                INSERT INTO drama_admin_user VALUES('u3','tenant-c','Duplicate A','a@example.com');
                INSERT INTO drama_admin_user VALUES('u3','tenant-d','Duplicate B','b@example.com');
                INSERT INTO drama_admin_user VALUES('u4','','No Tenant','u4@example.com');
                """
            )
            conn.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.x_db)) as conn:
            return conn.execute(
                "SELECT id,owner_tenant_key,owner_name,owner_email FROM x_authorized_account ORDER BY id"
            ).fetchall()

    def test_dry_run_reports_without_writing(self):
        before = self.rows()
        result = backfill_legacy_owners(self.x_db, self.admin_db)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["legacy_rows"], 5)
        self.assertEqual(result["resolvable_rows"], 1)
        self.assertEqual(result["updated_rows"], 0)
        self.assertEqual(result["unresolved_rows"], 4)
        self.assertEqual(self.rows(), before)

    def test_apply_updates_only_unique_nonempty_tenant_match(self):
        result = backfill_legacy_owners(self.x_db, self.admin_db, apply=True)
        self.assertEqual(result["updated_rows"], 1)
        rows = self.rows()
        self.assertEqual(rows[0], (1, "tenant-a", "New One", "new1@example.com"))
        self.assertEqual(rows[1][1], "")
        self.assertEqual(rows[2][1], "")
        self.assertEqual(rows[3][1], "")
        self.assertEqual(rows[4][1], "tenant-existing")
        second = backfill_legacy_owners(self.x_db, self.admin_db, apply=True)
        self.assertEqual(second["updated_rows"], 0)
        self.assertEqual(second["legacy_rows"], 4)

    def test_require_all_resolved_cli_blocks_ownerless_rows(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--x-db", str(self.x_db),
                    "--admin-db", str(self.admin_db),
                    "--require-all-resolved",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn('"empty_owner_user_id"', output.getvalue())

    def test_admin_schema_without_email_preserves_legacy_email(self):
        with contextlib.closing(sqlite3.connect(self.admin_db)) as conn:
            conn.executescript(
                """
                DROP TABLE drama_admin_user;
                CREATE TABLE drama_admin_user (
                  user_id TEXT NOT NULL,
                  tenant_key TEXT NOT NULL DEFAULT '',
                  name TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO drama_admin_user VALUES('u1','tenant-a','New One');
                """
            )
            conn.commit()
        result = backfill_legacy_owners(self.x_db, self.admin_db, apply=True)
        self.assertEqual(result["updated_rows"], 1)
        self.assertEqual(self.rows()[0], (1, "tenant-a", "New One", "old1@example.com"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
