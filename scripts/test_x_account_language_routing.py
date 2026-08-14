#!/usr/bin/env python3
"""Offline regression tests for X account drama-language routing."""

from __future__ import annotations

import sqlite3
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from features.x_accounts.language import (
    canonical_drama_language,
    same_drama_language,
)
from features.x_auto_posts.service import (
    AutoPostServiceError,
    XAutoPostService,
)
from features.x_posts.service import XPostError, XPostStore
from scripts.migrate_x_account_drama_languages import main as migrate_main
from scripts.x_post_daily_runner import (
    _plan_candidate,
    _preflight_candidates,
)


def material_candidate(material_id, language):
    return {
        "source_type": "material",
        "source_date": "2026-08-13",
        "material_id": str(material_id),
        "content_id": "content-%s" % material_id,
        "material_url": "https://media.example.com/%s.mp4" % material_id,
        "material_name": "material-%s" % material_id,
        "material_language": language,
        "drama_name": "drama-%s" % material_id,
        "tag": "tag",
        "description": "description",
        "page_name": "",
        "page_id": "",
    }


class XAccountLanguageRoutingTests(unittest.TestCase):
    def test_language_alias_is_canonical_and_comparable(self):
        self.assertEqual(canonical_drama_language("JP"), "ja")
        self.assertEqual(canonical_drama_language("pt_BR"), "pt-br")
        self.assertTrue(same_drama_language("jp", "ja"))
        self.assertFalse(same_drama_language("en", "ja"))

    def test_queue_freezes_canonical_account_language_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = XPostStore(Path(temporary) / "posts.sqlite3")
            candidate = material_candidate("101", "jp")
            candidate.update(
                {
                    "account_id": 19,
                    "account_username": "japanese19",
                    "account_drama_language": "ja",
                    "page_name": "Japanese 19",
                    "page_id": "x19",
                }
            )
            values = store._queue_payload(candidate)
            self.assertEqual(values["account_drama_language"], "ja")
            candidate["account_drama_language"] = "en"
            with self.assertRaisesRegex(
                XPostError, "does not match"
            ) as raised:
                store._queue_payload(candidate)
            self.assertEqual(
                raised.exception.code, "x_post_account_language_mismatch"
            )

    def test_material_candidates_route_to_matching_accounts_not_input_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = [
                {
                    "id": 1,
                    "username": "english1",
                    "x_user_id": "x1",
                    "display_name": "English",
                    "drama_language": "en",
                    "long_video_eligible": False,
                },
                {
                    "id": 2,
                    "username": "japanese2",
                    "x_user_id": "x2",
                    "display_name": "Japanese",
                    "drama_language": "ja",
                    "long_video_eligible": False,
                },
            ]
            candidates = [
                material_candidate("201", "jp"),
                material_candidate("202", "en"),
            ]

            def preflight(
                _config, candidate, account, rank, timestamp,
                _destination, _downloader, _prober, **_kwargs
            ):
                return _plan_candidate(
                    account, candidate, rank, timestamp
                )

            with mock.patch(
                "scripts.x_post_daily_runner._preflight_candidate",
                side_effect=preflight,
            ):
                planned, failures = _preflight_candidates(
                    SimpleNamespace(work_dir=temporary),
                    candidates,
                    accounts,
                    1,
                    object(),
                    object(),
                )
            self.assertEqual(failures, [])
            self.assertEqual(
                [
                    (
                        item["account_id"],
                        item["material_id"],
                        item["account_drama_language"],
                    )
                    for item in planned
                ],
                [(1, "202", "en"), (2, "201", "ja")],
            )

    def test_drama_assignment_matches_languages_and_bound_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "posts.sqlite3"
            store = XPostStore(db_path)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                for index, language in enumerate(("jp", "en"), start=1):
                    timestamp = "2026-08-14T00:00:0%s+00:00" % index
                    conn.execute(
                        "INSERT INTO x_post_drama_pool("
                        "content_id,drama_name,description,language,labels,"
                        "name_tag,status,free_episode_count,next_sub_number,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,"
                        "'pending',3,1,?,?)",
                        (
                            "content-%s" % index,
                            "drama-%s" % index,
                            "description",
                            language,
                            "tag",
                            "name-tag",
                            timestamp,
                            timestamp,
                        ),
                    )
                conn.commit()
            items = store.available_drama_pool_items(
                50,
                account_ids=[1, 2],
                account_languages={1: "en", 2: "ja"},
            )
            self.assertEqual(
                [
                    (item["candidate_account_id"], item["language"])
                    for item in items
                ],
                [(1, "en"), (2, "jp")],
            )
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    "DROP TRIGGER trg_x_post_drama_pool_assignment_evidence"
                )
                conn.execute(
                    "UPDATE x_post_drama_pool SET assigned_account_id=1 "
                    "WHERE language='jp'"
                )
                conn.commit()
            with self.assertRaises(XPostError) as raised:
                store.available_drama_pool_items(
                    50,
                    account_ids=[1, 2],
                    account_languages={1: "en", 2: "ja"},
                )
            self.assertEqual(
                raised.exception.code,
                "x_post_drama_account_language_mismatch",
            )
            with self.assertRaises(XPostError) as conflict:
                store.assert_account_drama_language_change(1, "en")
            self.assertEqual(
                conflict.exception.code,
                "x_account_drama_language_conflict",
            )

    def test_x_auto_accepts_jp_alias_and_rejects_other_language(self):
        XAutoPostService._assert_account_language(
            {"drama_language": "ja"}, "jp"
        )
        with self.assertRaises(AutoPostServiceError) as raised:
            XAutoPostService._assert_account_language(
                {"drama_language": "en"}, "ja"
            )
        self.assertEqual(
            raised.exception.code, "x_auto_account_language_mismatch"
        )

    def test_migration_requires_backup_and_updates_only_explicit_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "accounts.sqlite3"
            backup_path = root / "accounts.before.sqlite3"
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    "CREATE TABLE x_authorized_account("
                    "id INTEGER PRIMARY KEY,username TEXT NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO x_authorized_account(id,username) VALUES(?,?)",
                    [(19, "japanese19"), (20, "japanese20"), (21, "english21")],
                )
                conn.commit()
            with self.assertRaisesRegex(RuntimeError, "requires --backup"):
                migrate_main(
                    [
                        "--db", str(db_path), "--set", "19=ja", "--apply"
                    ]
                )
            migrate_main(
                [
                    "--db", str(db_path),
                    "--set", "19=jp",
                    "--set", "20=ja",
                    "--apply",
                    "--backup", str(backup_path),
                ]
            )
            self.assertTrue(backup_path.is_file())
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                rows = conn.execute(
                    "SELECT id,drama_language FROM x_authorized_account "
                    "ORDER BY id"
                ).fetchall()
            self.assertEqual(rows, [(19, "ja"), (20, "ja"), (21, "en")])

    def test_migration_cli_runs_outside_repository_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "accounts.sqlite3"
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    "CREATE TABLE x_authorized_account("
                    "id INTEGER PRIMARY KEY,username TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO x_authorized_account(id,username) VALUES(19,'japanese19')"
                )
                conn.commit()
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            script_path = Path(__file__).with_name(
                "migrate_x_account_drama_languages.py"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--db",
                    str(db_path),
                    "--set",
                    "19=jp",
                ],
                cwd=str(root),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["mode"], "dry_run")

    def test_oauth_sidecar_cli_imports_from_systemd_script_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            script_path = (
                Path(__file__).resolve().parents[1]
                / "features"
                / "x_accounts"
                / "oauth_service.py"
            )
            completed = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                cwd=temporary,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("X OAuth account sidecar", completed.stdout)


if __name__ == "__main__":
    unittest.main()
