#!/usr/bin/env python3
"""Offline tests for the daily X Post ledger and dedupe contract."""

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service


def candidate(account_id, username, material_id, rank=1):
    return {
        "account_id": account_id,
        "account_username": username,
        "source_date": "2026-07-22",
        "material_id": str(material_id),
        "content_id": "content-%s" % material_id,
        "material_url": "https://media.example.com/%s.mp4" % material_id,
        "material_name": "material-%s" % material_id,
        "material_language": "English",
        "drama_name": "Drama %s" % material_id,
        "tag": "romance",
        "description": "Safe drama description %s" % material_id,
        "page_name": "Page %s" % account_id,
        "page_id": "207695100000000000%s" % account_id,
        "candidate_rank": rank,
        "spend": 100.0 - rank,
        "preflight_sha256": ("%064x" % int(material_id))[-64:],
        "preflight_size": 5,
        "compliance_counts": {
            "facebook_violation_count": 0,
            "tiktok_violation_count": 0,
            "twitter_violation_count": 0,
            "resource_audit_count": 0,
            "dangerous_tag_count": 0,
        },
    }


def plan_candidates(materials=(1001, 1002, 1003)):
    return [
        candidate(2, "ShortsDramhx", materials[0], 1),
        candidate(3, "NextShortsy1", materials[1], 2),
        candidate(4, "GrapeShortlzod", materials[2], 3),
    ]


def create_legacy_queue(conn):
    conn.execute(
        """
        CREATE TABLE x_post_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            account_id INTEGER NOT NULL,
            account_username TEXT NOT NULL,
            source_date TEXT NOT NULL,
            material_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            material_url TEXT NOT NULL,
            material_name TEXT NOT NULL,
            material_language TEXT NOT NULL,
            drama_name TEXT NOT NULL,
            tag TEXT NOT NULL,
            description TEXT NOT NULL,
            page_name TEXT NOT NULL,
            page_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def insert_legacy(conn, key, account_id, material_id, created_at):
    conn.execute(
        """
        INSERT INTO x_post_queue(
            idempotency_key,account_id,account_username,source_date,material_id,
            content_id,material_url,material_name,material_language,drama_name,tag,
            description,page_name,page_id,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?)
        """,
        (
            key,
            account_id,
            "legacy%s" % account_id,
            "2026-07-22",
            str(material_id),
            "content",
            "https://media.example.com/legacy.mp4",
            "legacy",
            "English",
            "Legacy Drama",
            "safe",
            "description",
            "Legacy Page",
            str(account_id),
            "published",
            created_at,
            created_at,
        ),
    )


class XPostLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "ledger.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_canary_is_backfilled_and_migration_is_idempotent(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            create_legacy_queue(conn)
            insert_legacy(conn, "canary", 2, "005221348", "2026-07-23T02:10:00Z")
            conn.commit()

        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT material_key,run_date,original_material_url,"
                "media_repair_trigger_code,media_repair_job_key,"
                "media_repair_profile,media_repair_source_sha256 "
                "FROM x_post_queue WHERE material_id='005221348'"
            ).fetchone()
            tables = {
                item[0]
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            indexes = {
                item[0]
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            triggers = {
                item[0]
                for item in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            queue_columns = {
                item[1] for item in conn.execute("PRAGMA table_info(x_post_queue)")
            }
        self.assertEqual(
            row,
            ("5221348", "2026-07-23", "", "", "", "", ""),
        )
        self.assertIn("x_post_daily_run", tables)
        self.assertIn("x_post_material_pool", tables)
        self.assertIn("pool_item_id", queue_columns)
        self.assertIn("pool_created_at", queue_columns)
        self.assertIn("original_material_url", queue_columns)
        self.assertIn("media_repair_source_sha256", queue_columns)
        self.assertIn("ux_x_post_queue_material_key", indexes)
        self.assertIn("ux_x_post_queue_account_run_date", indexes)
        self.assertIn("ux_x_post_queue_pool_item_id", indexes)
        self.assertIn("idx_x_post_pool_fifo", indexes)
        self.assertIn("trg_x_post_queue_pool_required_insert", triggers)
        self.assertIn("trg_x_post_pool_queue_guard", triggers)
        self.assertIn("trg_x_post_pool_delete_guard", triggers)

    def test_repaired_candidate_freezes_final_url_and_safe_audit_fields(self):
        store = service.XPostStore(self.db_path)
        values = plan_candidates((6101, 6102, 6103))
        repaired = values[0]
        repaired["original_material_url"] = repaired["material_url"]
        repaired["material_url"] = (
            "https://media.example.com/x-post-repair/6101-output.mp4"
        )
        repaired["media_repair_trigger_code"] = "invalid_media_codec"
        repaired["media_repair_job_key"] = "xpost-repair:x-video-v1:6101:" + (
            "a" * 64
        )
        repaired["media_repair_profile"] = "x-video-v1"
        repaired["media_repair_source_sha256"] = "b" * 64

        plan = store.create_daily_plan("2026-07-23", "2026-07-22", values)
        queue = plan["queues"][0]
        self.assertEqual(queue["material_url"], repaired["material_url"])
        self.assertEqual(
            queue["original_material_url"], repaired["original_material_url"]
        )
        self.assertEqual(
            queue["media_repair_trigger_code"], "invalid_media_codec"
        )
        self.assertEqual(queue["media_repair_profile"], "x-video-v1")

        queried = store.query_logs(
            {"run_date": "2026-07-23", "page": 1, "page_size": 10}
        )
        item = next(
            row for row in queried["items"] if row["queue_id"] == queue["id"]
        )
        self.assertEqual(item["media_repair_trigger_code"], "invalid_media_codec")
        self.assertEqual(item["media_repair_profile"], "x-video-v1")
        self.assertNotIn("original_material_url", item)
        self.assertNotIn("media_repair_source_sha256", item)

    def test_partial_or_nonrepairable_media_audit_is_rejected(self):
        store = service.XPostStore(self.db_path)
        cases = []
        partial = plan_candidates((6201, 6202, 6203))
        partial[0]["original_material_url"] = partial[0]["material_url"]
        cases.append(("partial", partial))

        wrong_trigger = plan_candidates((6301, 6302, 6303))
        repaired = wrong_trigger[0]
        repaired["original_material_url"] = repaired["material_url"]
        repaired["material_url"] = (
            "https://media.example.com/x-post-repair/6301-output.mp4"
        )
        repaired["media_repair_trigger_code"] = (
            "x_long_video_requires_premium"
        )
        repaired["media_repair_job_key"] = "xpost-repair:x-video-v1:6301:" + (
            "c" * 64
        )
        repaired["media_repair_profile"] = "x-video-v1"
        repaired["media_repair_source_sha256"] = "d" * 64
        cases.append(("wrong_trigger", wrong_trigger))

        for label, values in cases:
            with self.subTest(label=label):
                with self.assertRaises(service.XPostError) as caught:
                    store.create_daily_plan(
                        "2026-07-23", "2026-07-22", values
                    )
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_migrated_published_canary_replays_across_days_without_x_write(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            create_legacy_queue(conn)
            insert_legacy(
                conn,
                "xpost:2026-07-22:2:5221348",
                2,
                "5221348",
                "2026-07-23T02:10:00Z",
            )
            conn.commit()
        service.ensure_storage(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO x_post_publish_log(
                    queue_id,account_id,status,attempt_count,long_url,short_url,
                    post_text,x_media_id,x_post_id,x_post_url,published_at,created_at,updated_at
                ) VALUES(1,2,'published',1,'https://example.invalid/frozen',
                    'https://ai.yingliangads.com/s2l/1.html',
                    'https://ai.yingliangads.com/s2l/1.html\ndescription',
                    'media1','2080128600917905497',
                    'https://x.com/legacy2/status/2080128600917905497',
                    '2026-07-23T02:11:00Z','2026-07-23T02:10:00Z',
                    '2026-07-23T02:11:00Z')
                """
            )
            conn.commit()

        payload = {
            "account_id": 2,
            "account_username": "legacy2",
            "source_date": "2026-07-22",
            "material_id": "5221348",
            "content_id": "content",
            "material_url": "https://media.example.com/legacy.mp4",
            "material_name": "legacy",
            "material_language": "English",
            "drama_name": "Legacy Drama",
            "tag": "safe",
            "description": "description",
            "page_name": "Legacy Page",
            "page_id": "2",
        }
        store = service.XPostStore(self.db_path)
        with mock.patch.object(service, "_beijing_today", return_value="2026-07-30"):
            replayed = store.enqueue(payload)
        self.assertFalse(replayed["created"])
        result = service.publish_canary(
            db_path=self.db_path,
            queue_id=replayed["id"],
            account={"id": 2, "username": "legacy2"},
            access_token="unused-but-required",
            public_root=Path(self.temp.name) / "s2l",
            short_base_url="https://ai.yingliangads.com/s2l",
            allowed_media_hosts=["media.example.com"],
        )
        self.assertEqual(result["post_id"], "2080128600917905497")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_publish_log").fetchone()[0], 1)

    def test_legacy_duplicate_material_or_account_day_fails_closed(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            create_legacy_queue(conn)
            insert_legacy(conn, "one", 2, "5221348", "2026-07-23T02:10:00Z")
            insert_legacy(conn, "two", 3, "05221348", "2026-07-24T02:10:00Z")
            conn.commit()
        with self.assertRaises(service.XPostError) as caught:
            service.ensure_storage(self.db_path)
        self.assertEqual(caught.exception.code, "x_post_storage_conflict")

        other = Path(self.temp.name) / "account-day.sqlite3"
        with contextlib.closing(sqlite3.connect(other)) as conn:
            create_legacy_queue(conn)
            insert_legacy(conn, "one", 2, "10", "2026-07-23T02:10:00Z")
            insert_legacy(conn, "two", 2, "11", "2026-07-23T03:10:00Z")
            conn.commit()
        with self.assertRaises(service.XPostError) as caught:
            service.ensure_storage(other)
        self.assertEqual(caught.exception.code, "x_post_storage_conflict")

    def test_daily_plan_is_atomic_idempotent_and_globally_deduplicated(self):
        store = service.XPostStore(self.db_path)
        first = store.create_daily_plan(
            "2026-07-23", "2026-07-22", plan_candidates()
        )
        repeated = store.create_daily_plan(
            "2026-07-23", "2026-07-22", plan_candidates()
        )
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(
            [row["id"] for row in first["queues"]],
            [row["id"] for row in repeated["queues"]],
        )
        with self.assertRaises(service.XPostError) as changed:
            store.create_daily_plan(
                "2026-07-23",
                "2026-07-22",
                plan_candidates((2001, 2002, 2003)),
            )
        self.assertEqual(changed.exception.code, "x_post_daily_run_exists")

        next_day = plan_candidates((1001, 3002, 3003))
        for item in next_day:
            item["source_date"] = "2026-07-23"
        with self.assertRaises(service.XPostError) as duplicate:
            store.create_daily_plan("2026-07-24", "2026-07-23", next_day)
        self.assertEqual(duplicate.exception.code, "x_post_material_already_used")
        self.assertIsNone(store.get_run_by_date("2026-07-24"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0], 3)

    def test_daily_plan_query_returns_only_atomic_run_and_queue_identity(self):
        store = service.XPostStore(self.db_path)
        missing = store.query_daily_plan("2026-07-23")
        self.assertEqual(
            missing,
            {"found": False, "run": None, "queues": []},
        )

        plan = store.create_daily_plan(
            "2026-07-23",
            "2026-07-22",
            plan_candidates(),
        )
        snapshot = store.query_daily_plan("2026-07-23")
        self.assertTrue(snapshot["found"])
        self.assertEqual(snapshot["run"]["id"], plan["id"])
        self.assertEqual(
            [queue["id"] for queue in snapshot["queues"]],
            [queue["id"] for queue in plan["queues"]],
        )
        self.assertEqual(
            [queue["account_id"] for queue in snapshot["queues"]],
            [2, 3, 4],
        )
        self.assertEqual(
            set(snapshot["queues"][0]),
            {
                "id",
                "run_id",
                "run_date",
                "source_date",
                "account_id",
                "candidate_rank",
                "status",
                "created_at",
                "updated_at",
            },
        )
        serialized = str(snapshot).lower()
        for forbidden in (
            "material_url",
            "description",
            "post_text",
            "short_url",
            "long_url",
            "token",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_account_day_and_compliance_guards_hold_before_insert(self):
        store = service.XPostStore(self.db_path)
        payload = candidate(2, "ShortsDramhx", 9001)
        payload["run_date"] = "2026-07-23"
        store.enqueue(payload)
        other = candidate(2, "ShortsDramhx", 9002)
        other["run_date"] = "2026-07-23"
        with self.assertRaises(service.XPostError) as caught:
            store.enqueue(other)
        self.assertEqual(caught.exception.code, "x_post_account_day_already_reserved")

        unsafe = plan_candidates((9101, 9102, 9103))
        unsafe[2]["dangerous_tag_count"] = 1
        with self.assertRaises(service.XPostError) as caught:
            store.create_daily_plan("2026-07-24", "2026-07-23", unsafe)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIsNone(store.get_run_by_date("2026-07-24"))

    def test_daily_plan_requires_complete_nonconflicting_compliance_evidence(self):
        store = service.XPostStore(self.db_path)
        cases = []

        missing = plan_candidates((9201, 9202, 9203))
        missing[0].pop("compliance_counts")
        cases.append(("missing", missing))

        null_value = plan_candidates((9301, 9302, 9303))
        null_value[0]["compliance_counts"]["twitter_violation_count"] = None
        cases.append(("null", null_value))

        conflicting = plan_candidates((9401, 9402, 9403))
        conflicting[0]["facebook_violation_count"] = 1
        cases.append(("conflicting_alias", conflicting))

        for label, payload in cases:
            with self.subTest(label=label):
                with self.assertRaises(service.XPostError) as caught:
                    store.create_daily_plan("2026-07-24", "2026-07-23", payload)
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertIsNone(store.get_run_by_date("2026-07-24"))

    def test_preflight_failure_is_idempotent_recoverable_and_cannot_overwrite_plan(self):
        store = service.XPostStore(self.db_path)
        first = store.record_run_failure(
            "2026-07-23",
            "2026-07-22",
            "x_post_daily_candidate_shortage",
            "Authorization=do-not-store",
            9,
        )
        repeated = store.record_run_failure(
            "2026-07-23",
            "2026-07-22",
            "x_post_daily_candidate_shortage",
            "Authorization=do-not-store",
            9,
        )
        self.assertTrue(first["recorded"])
        self.assertFalse(repeated["recorded"])
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["expected_count"], 9)
        self.assertEqual(repeated["expected_count"], 9)
        self.assertNotIn("do-not-store", first["error_message"])
        self.assertEqual(
            store.query_runs(
                {"status": "failed_preflight", "page": 1, "page_size": 10}
            )["pagination"]["total"],
            1,
        )

        candidates = [
            candidate(
                account_id,
                "daily_account_%s" % account_id,
                1000 + account_id,
                rank,
            )
            for rank, account_id in enumerate(range(2, 11), 1)
        ]
        plan = store.create_daily_plan(
            "2026-07-23", "2026-07-22", candidates
        )
        self.assertEqual(plan["id"], first["id"])
        self.assertEqual(plan["status"], "queued")
        self.assertEqual(plan["expected_count"], 9)
        self.assertEqual(len(plan["queues"]), 9)
        self.assertEqual(plan["error_code"], "")
        refused = store.record_run_failure(
            "2026-07-23",
            "2026-07-22",
            "late_failure",
            "must not overwrite a formal plan",
            9,
        )
        self.assertFalse(refused["recorded"])
        self.assertEqual(refused["status"], "queued")
        self.assertEqual(refused["error_code"], "")

    def test_post_creating_is_unknown_and_never_retried(self):
        store = service.XPostStore(self.db_path)
        plan = store.create_daily_plan(
            "2026-07-23", "2026-07-22", plan_candidates()
        )
        queue = plan["queues"][0]
        log = store.reserve_log(queue["id"])
        store.prepare_log(
            log["id"],
            service.build_w2a_url(
                {
                    "username": queue["account_username"],
                    "timestamp": 1784736000,
                    "material_language": queue["material_language"],
                    "drama_name": queue["drama_name"],
                    "tag": queue["tag"],
                    "log_id": log["id"],
                    "page_name": queue["page_name"],
                    "page_id": queue["page_id"],
                    "material_name": queue["material_name"],
                    "material_id": queue["material_id"],
                    "queue_id": queue["id"],
                    "content_id": queue["content_id"],
                }
            ),
            "https://ai.yingliangads.com/s2l/%s.html" % log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\ndescription" % log["id"],
        )
        store.mark_publishing(log["id"])
        store.mark_media_uploaded(log["id"], "media1")

        with self.assertRaises(service.XPostError) as caught:
            service.publish_canary(
                db_path=self.db_path,
                queue_id=queue["id"],
                account={"id": queue["account_id"], "username": queue["account_username"]},
                access_token="secret",
                public_root=Path(self.temp.name) / "s2l",
                short_base_url="https://ai.yingliangads.com/s2l",
                allowed_media_hosts=["media.example.com"],
            )
        self.assertEqual(caught.exception.code, "x_post_unknown_outcome")
        self.assertTrue(caught.exception.unknown_outcome)
        self.assertEqual(store.get_run(plan["id"])["status"], "needs_review")

        queried = store.query_logs({"run_date": "2026-07-23", "page": 1, "page_size": 10})
        self.assertEqual(queried["pagination"]["total"], 3)
        item = next(row for row in queried["items"] if row["queue_id"] == queue["id"])
        self.assertTrue(item["unknown_outcome"])
        self.assertNotIn("secret", item["error_message"])
        self.assertNotIn("material_url", item)
        self.assertNotIn("long_url", item)
        self.assertNotIn("post_text", item)
        unknown_only = store.query_logs(
            {
                "source_date": "2026-07-22",
                "unknown_outcome": 1,
                "page": 1,
                "page_size": 10,
            }
        )
        self.assertEqual(unknown_only["pagination"]["total"], 1)
        self.assertEqual(unknown_only["items"][0]["queue_id"], queue["id"])

    def test_query_pagination_is_bounded(self):
        store = service.XPostStore(self.db_path)
        plan = store.create_daily_plan("2026-07-23", "2026-07-22", plan_candidates())
        self.assertEqual(
            store.query_material_keys(["01001", "9999", "1003", "1001"]),
            ["1001", "1003"],
        )
        self.assertEqual(
            store.query_material_keys([str(value) for value in range(1001, 2001)]),
            ["1001", "1002", "1003"],
        )
        log = store.reserve_log(plan["queues"][0]["id"])
        store.mark_failed(log["id"], "x_post_rate_limited", "Too many requests", False)
        self.assertEqual(store.get_run(plan["id"])["status"], "stopped")
        result = store.query_runs({"page": 1, "page_size": 20})
        self.assertEqual(result["pagination"]["total"], 1)
        with self.assertRaises(service.XPostError) as caught:
            store.query_logs({"page": 1, "page_size": 101})
        self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(service.XPostError):
            store.query_material_keys([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
