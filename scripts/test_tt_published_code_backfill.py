#!/usr/bin/env python3
"""Offline safety tests for the historical TT code backfill."""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_posts.code_routes import (  # noqa: E402
    TTCodeRouteResolver,
    ensure_code_route_storage,
)
from features.tt_posts.links import build_w2a_url, build_w2a_url_from_fields  # noqa: E402
import scripts.backfill_tt_published_codes as backfill_module  # noqa: E402
from scripts.backfill_tt_published_codes import (  # noqa: E402
    TTCodeBackfillError,
    apply_plan,
    backup_database,
    inspect_database,
)


class TTPublishedCodeBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db_path = root / "tt-post.sqlite3"
        self.backup_path = root / "backups" / "before.sqlite3"
        self.backup_path.parent.mkdir()
        self._create_database()

    @staticmethod
    def _long_url(queue_id: int, content_id: str) -> str:
        return build_w2a_url(
            {
                "username": "creator%s" % queue_id,
                "timestamp": 1785988800 + queue_id,
                "material_language": "en",
                "drama_name": "Drama%s" % queue_id,
                "tag": "none",
                "link_id": queue_id,
                "page_name": "Creator %s" % queue_id,
                "page_id": "640%s" % queue_id,
                "material_name": "Clip %s" % queue_id,
                "material_id": "500%s" % queue_id,
                "queue_id": queue_id,
                "content_id": content_id,
                "channel": "AIpost",
            }
        )

    def _create_database(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE tt_post_queue(
                    id INTEGER PRIMARY KEY,
                    scheduled_at_utc TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    account_username TEXT NOT NULL,
                    creator_username_snapshot TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    long_url TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    publish_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE tt_post_event(
                    id INTEGER PRIMARY KEY,
                    queue_id INTEGER,
                    to_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE TABLE tt_post_direct_test("
                "id INTEGER PRIMARY KEY,code TEXT NOT NULL,status TEXT NOT NULL)"
            )
            ensure_code_route_storage(conn)
            for queue_id, status, publish_id, code in (
                (2, "published", "publish-2", ""),
                (3, "published", "publish-3", ""),
                (4, "published", "publish-4", "Z9Y8"),
                (5, "failed", "", ""),
            ):
                content_id = "CONTENT%05d" % queue_id
                conn.execute(
                    """
                    INSERT INTO tt_post_queue(
                        id,scheduled_at_utc,account_id,account_username,
                        creator_username_snapshot,content_id,material_id,
                        long_url,code,status,publish_id,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        queue_id,
                        "2026-08-0%sT08:00:00Z" % queue_id,
                        "640%s" % queue_id,
                        "creator%s" % queue_id,
                        "creator%s" % queue_id,
                        content_id,
                        "500%s" % queue_id,
                        self._long_url(queue_id, content_id),
                        code,
                        status,
                        publish_id,
                        "2026-08-0%sT07:00:00Z" % queue_id,
                        "2026-08-0%sT08:05:00Z" % queue_id,
                    ),
                )
                if status == "published":
                    conn.execute(
                        "INSERT INTO tt_post_event(id,queue_id,to_status,created_at) "
                        "VALUES(?,?,?,?)",
                        (
                            queue_id,
                            queue_id,
                            "published",
                            "2026-08-0%sT08:04:00Z" % queue_id,
                        ),
                    )
            conn.execute(
                "INSERT INTO tt_post_direct_test(id,code,status) VALUES(1,'','published')"
            )
            conn.commit()

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_dry_run_is_read_only_and_excludes_direct_test(self):
        before = self._sha(self.db_path)
        result = inspect_database(self.db_path)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(
            [item["queue_id"] for item in result["candidates"]],
            [2, 3],
        )
        self.assertEqual(before, self._sha(self.db_path))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT code FROM tt_post_direct_test WHERE id=1").fetchone()[0], "")

    def test_guarded_apply_backs_up_and_resolves_codes(self):
        exact = inspect_database(self.db_path, queue_ids=[2, 3])
        backup = backup_database(self.db_path, self.backup_path)
        written = apply_plan(
            self.db_path,
            queue_ids=[2, 3],
            expected_count=exact["candidate_count"],
            expected_hash=exact["plan_sha256"],
            choice_fn=lambda alphabet: alphabet[0],
        )
        self.assertEqual([item["code"] for item in written], ["AAAA", "AAAB"])
        self.assertEqual(backup["sha256"], self._sha(self.backup_path))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            queues = conn.execute(
                "SELECT id,code,long_url FROM tt_post_queue WHERE id IN (2,3) ORDER BY id"
            ).fetchall()
            self.assertEqual([row["code"] for row in queues], ["AAAA", "AAAB"])
            self.assertTrue(all("af_channel=AIpost" in row["long_url"] for row in queues))
            routes = conn.execute(
                "SELECT code,state,af_channel,published_at,long_url "
                "FROM tt_post_code_route ORDER BY queue_id"
            ).fetchall()
            self.assertEqual([row["state"] for row in routes], ["published", "published"])
            self.assertTrue(all(row["af_channel"] == "TT" for row in routes))
            self.assertTrue(all("af_channel=TT" in row["long_url"] for row in routes))
            self.assertEqual(conn.execute("SELECT code FROM tt_post_direct_test WHERE id=1").fetchone()[0], "")
        resolved = TTCodeRouteResolver(self.db_path).resolve("AAAA", "Search")
        self.assertEqual(resolved["item"]["content_id"], "CONTENT00002")
        self.assertEqual(resolved["item"]["route_mode"], "code_exact")
        self.assertEqual(inspect_database(self.db_path)["candidate_count"], 0)

    def test_changed_plan_and_incomplete_history_fail_closed(self):
        exact = inspect_database(self.db_path, queue_ids=[2, 3])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tt_post_queue SET publish_id='changed' WHERE id=3")
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            apply_plan(
                self.db_path,
                queue_ids=[2, 3],
                expected_count=exact["candidate_count"],
                expected_hash=exact["plan_sha256"],
            )
        self.assertEqual(raised.exception.code, "tt_code_backfill_plan_changed")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tt_post_queue SET long_url='' WHERE id=2")
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            inspect_database(self.db_path, queue_ids=[2])
        self.assertEqual(raised.exception.code, "tt_code_backfill_long_url_missing")

    def test_non_legacy_attribution_channel_is_rejected(self):
        original = self._long_url(2, "CONTENT00002")
        fields = dict(parse_qsl(urlsplit(original).query, keep_blank_values=True))
        rebuilt = build_w2a_url_from_fields(
            {
                key: fields[key]
                for key in (
                    "af_dp",
                    "c",
                    "af_adset",
                    "af_adset_id",
                    "af_ad",
                    "af_ad_id",
                    "af_c_id",
                )
            },
            channel="Search",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tt_post_queue SET long_url=? WHERE id=2", (rebuilt,))
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            inspect_database(self.db_path, queue_ids=[2])
        self.assertEqual(raised.exception.code, "tt_code_backfill_channel_invalid")

    def test_capacity_guard_never_recycles_an_existing_route(self):
        exact = inspect_database(self.db_path, queue_ids=[2, 3])
        with patch.object(backfill_module, "CODE_ALPHABET", "A"), patch.object(
            backfill_module, "CODE_LENGTH", 1
        ):
            with self.assertRaises(TTCodeBackfillError) as raised:
                apply_plan(
                    self.db_path,
                    queue_ids=[2, 3],
                    expected_count=exact["candidate_count"],
                    expected_hash=exact["plan_sha256"],
                )
        self.assertEqual(
            raised.exception.code,
            "tt_code_backfill_capacity_exhausted",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_queue WHERE code<>''"
                ).fetchone()[0],
                1,
            )

    def test_backup_path_is_exclusive_and_preserves_existing_file(self):
        self.backup_path.write_bytes(b"operator-owned")
        with self.assertRaises(TTCodeBackfillError) as raised:
            backup_database(self.db_path, self.backup_path)
        self.assertEqual(raised.exception.code, "tt_code_backfill_backup_exists")
        self.assertEqual(self.backup_path.read_bytes(), b"operator-owned")


if __name__ == "__main__":
    unittest.main()
