#!/usr/bin/env python3

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service


def plan_candidate(account_id, pool_item, source_date="2026-07-22"):
    material_id = str(pool_item["material_id"])
    return {
        "account_id": account_id,
        "account_username": "account%s" % account_id,
        "source_date": source_date,
        "pool_item_id": int(pool_item["id"]),
        "pool_created_at": str(pool_item["created_at"]),
        "material_key": material_id,
        "material_id": material_id,
        "content_id": "content-%s" % material_id,
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "material-%s.mp4" % material_id,
        "material_language": "en",
        "drama_name": "Drama %s" % material_id,
        "tag": "Fantasy",
        "description": "A safe drama description.",
        "page_name": "Account %s" % account_id,
        "page_id": "200%s" % account_id,
        "spend": 0,
        "preflight_sha256": "a" * 64,
        "preflight_size": 1024,
        "facebook_violation_count": 0,
        "tiktok_violation_count": 0,
        "twitter_violation_count": 0,
        "resource_audit_count": 0,
        "dangerous_tag_count": 0,
    }


class XPostMaterialPoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "accounts.sqlite3"
        self.store = service.XPostStore(self.db_path)
        self.actor = {"user_id": "admin-1", "name": "Admin"}

    def tearDown(self):
        self.temporary.cleanup()

    def add(self, *material_ids):
        return self.store.add_pool_materials(list(material_ids), self.actor)["items"]

    def test_add_fifo_validation_and_delete_available_item(self):
        items = self.add("00101", "102", "103", "104")
        self.assertEqual([item["material_id"] for item in items], ["101", "102", "103", "104"])
        available = self.store.available_pool_items(10)
        self.assertEqual([item["material_id"] for item in available], ["101", "102", "103", "104"])
        self.assertEqual(
            [(item["created_at"], item["id"]) for item in available],
            sorted((item["created_at"], item["id"]) for item in available),
        )

        with self.assertRaises(service.XPostError) as duplicate:
            self.add("101")
        self.assertEqual(duplicate.exception.code, "x_post_pool_material_already_exists")

        check = self.store.record_pool_checks(
            [
                {
                    "pool_item_id": items[0]["id"],
                    "error_code": "material_safety_check_failed",
                    "error_message": "drama mapping is incomplete",
                }
            ]
        )
        self.assertEqual(check["updated_count"], 1)
        queried = self.store.query_pool({"material_id": "101"})
        self.assertEqual(queried["items"][0]["status"], "unpublished")
        self.assertEqual(queried["items"][0]["availability"], "validation_failed")
        self.assertIn(
            "101",
            [item["material_id"] for item in self.store.available_pool_items(10)],
        )
        summary = self.store.query_pool({})["summary"]
        self.assertEqual(summary["unpublished"], 4)
        self.assertEqual(summary["available"], 3)

        deleted = self.store.delete_pool_material(items[3]["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(
            [item["material_id"] for item in self.store.available_pool_items(10)],
            ["101", "102", "103"],
        )

    def test_pool_plan_is_atomic_fifo_and_success_only_marks_published(self):
        items = self.add("201", "202", "203", "204")
        reversed_candidates = [
            plan_candidate(account_id, pool_item)
            for account_id, pool_item in zip((2, 3, 4), reversed(items[:3]))
        ]
        with self.assertRaises(service.XPostError) as out_of_order:
            self.store.create_daily_plan(
                "2026-07-23",
                "2026-07-22",
                reversed_candidates,
                require_pool=True,
            )
        self.assertEqual(out_of_order.exception.code, "invalid_request")
        self.assertIsNone(self.store.get_run_by_date("2026-07-23"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0], 0)

        candidates = [
            plan_candidate(account_id, pool_item)
            for account_id, pool_item in zip((2, 3, 4), items[:3])
        ]
        plan = self.store.create_daily_plan(
            "2026-07-23",
            "2026-07-22",
            candidates,
            require_pool=True,
        )
        self.assertEqual(
            [queue["pool_item_id"] for queue in plan["queues"]],
            [item["id"] for item in items[:3]],
        )
        self.assertEqual(
            [item["material_id"] for item in self.store.available_pool_items(10)],
            ["204"],
        )

        first_queue = plan["queues"][0]
        log = self.store.reserve_log(first_queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view?c=test",
            "https://ai.yingliangads.com/s2l/%s.html" % log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\nDescription" % log["id"],
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media-201")
        self.store.mark_published(
            log["id"],
            "media-201",
            "9000201",
            "https://x.com/account2/status/9000201",
        )
        published = self.store.query_pool({"material_id": "201"})["items"][0]
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["availability"], "published")
        self.assertTrue(published["published_at"])

        second_log = self.store.reserve_log(plan["queues"][1]["id"])
        self.store.prepare_log(
            second_log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view?c=test",
            "https://ai.yingliangads.com/s2l/%s.html" % second_log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\nDescription" % second_log["id"],
        )
        self.store.mark_publishing(second_log["id"])
        self.store.mark_failed(second_log["id"], "x_upstream_error", "known failure")
        failed = self.store.query_pool({"material_id": "202"})["items"][0]
        self.assertEqual(failed["status"], "unpublished")
        self.assertEqual(failed["availability"], "failed")

        third_log = self.store.reserve_log(plan["queues"][2]["id"])
        self.store.prepare_log(
            third_log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view?c=test",
            "https://ai.yingliangads.com/s2l/%s.html" % third_log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\nDescription" % third_log["id"],
        )
        self.store.mark_publishing(third_log["id"])
        self.store.mark_failed(
            third_log["id"],
            "x_post_outcome_unknown",
            "transport interrupted",
            unknown_outcome=True,
        )
        unknown = self.store.query_pool({"material_id": "203"})["items"][0]
        self.assertEqual(unknown["status"], "unpublished")
        self.assertEqual(unknown["availability"], "needs_review")

        for pool_item in items[:3]:
            with self.assertRaises(service.XPostError) as occupied:
                self.store.delete_pool_material(pool_item["id"])
            self.assertIn(
                occupied.exception.code,
                {"x_post_pool_item_published", "x_post_pool_item_occupied"},
            )

    def test_historical_queue_material_cannot_reenter_pool(self):
        payload = plan_candidate(
            2,
            {
                "id": 999,
                "material_id": "999",
                "created_at": "2026-07-22T00:00:00Z",
            },
        )
        payload.pop("pool_item_id")
        payload.pop("pool_created_at")
        payload["run_date"] = "2026-07-23"
        self.store.enqueue(payload)
        with self.assertRaises(service.XPostError) as used:
            self.add("999")
        self.assertEqual(used.exception.code, "x_post_pool_material_already_used")

    def test_pool_material_cannot_enter_non_pool_queue_or_legacy_plan(self):
        pool_item = self.add("701")[0]
        payload = plan_candidate(2, pool_item)
        payload.pop("pool_item_id")
        payload.pop("pool_created_at")
        payload["run_date"] = "2026-07-23"

        with self.assertRaises(service.XPostError) as canary_conflict:
            self.store.enqueue(payload)
        self.assertEqual(canary_conflict.exception.code, "x_post_pool_item_occupied")

        plan_payloads = []
        for account_id, material_id in zip((2, 3, 4), (701, 702, 703)):
            candidate = plan_candidate(
                account_id,
                {
                    "id": material_id,
                    "material_id": str(material_id),
                    "created_at": "2026-07-22T00:00:%02dZ" % (material_id - 700),
                },
            )
            candidate.pop("pool_item_id")
            candidate.pop("pool_created_at")
            plan_payloads.append(candidate)
        with self.assertRaises(service.XPostError) as plan_conflict:
            self.store.create_daily_plan(
                "2026-07-23",
                "2026-07-22",
                plan_payloads,
            )
        self.assertEqual(plan_conflict.exception.code, "x_post_pool_item_occupied")
        self.assertIsNone(self.store.get_run_by_date("2026-07-23"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0],
                0,
            )

    def test_defensive_query_and_delete_detect_legacy_material_key_occupancy(self):
        payload = plan_candidate(
            2,
            {
                "id": 801,
                "material_id": "801",
                "created_at": "2026-07-22T00:00:00Z",
            },
        )
        payload.pop("pool_item_id")
        payload.pop("pool_created_at")
        payload["run_date"] = "2026-07-23"
        self.store.enqueue(payload)

        timestamp = service.utc_now()
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            # Simulate a legacy/corrupt database created before the current
            # cross-table insert guard existed.
            conn.execute("DROP TRIGGER trg_x_post_pool_queue_guard")
            cursor = conn.execute(
                "INSERT INTO x_post_material_pool("
                "material_key,material_id,status,created_at,updated_at"
                ") VALUES('801','801','unpublished',?,?)",
                (timestamp, timestamp),
            )
            pool_item_id = int(cursor.lastrowid)
            conn.commit()

        queried = self.store.query_pool({"material_id": "801"})["items"][0]
        self.assertEqual(queried["queue_id"], 1)
        self.assertEqual(queried["availability"], "occupied")
        self.assertEqual(self.store.available_pool_items(10), [])
        with self.assertRaises(service.XPostError) as occupied:
            self.store.delete_pool_material(pool_item_id)
        self.assertEqual(occupied.exception.code, "x_post_pool_item_occupied")

    def test_daily_plan_requires_pool_when_enforced(self):
        payloads = []
        for account_id, material_id in zip((2, 3, 4), (301, 302, 303)):
            payload = plan_candidate(
                account_id,
                {
                    "id": material_id,
                    "material_id": str(material_id),
                    "created_at": "2026-07-22T00:00:00Z",
                },
            )
            payload.pop("pool_item_id")
            payload.pop("pool_created_at")
            payloads.append(payload)
        with self.assertRaises(service.XPostError) as required:
            self.store.create_daily_plan(
                "2026-07-23",
                "2026-07-22",
                payloads,
                require_pool=True,
            )
        self.assertEqual(required.exception.code, "x_post_pool_required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
