#!/usr/bin/env python3
"""Offline tests for the material-status broadcast pure module."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import contextlib
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.material_status_broadcast import service


def valid_payload(**changes):
    item = {
        "resource_id": "res-20260728-001",
        "resource_name": "暮色心约",
        "task_start_time": "2026-07-28T08:30:15+00:00",
        "drama_dubbing_type": "AI配音",
        "task_type": "素材制作",
        "original_material_name": "source-video.mp4",
        "material_name": "source-video-final.mp4",
        "language": "英语",
        "final_status": "制作完成",
        "optimizer_name": "张三",
    }
    item.update(changes)
    return item


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class ValidationTests(unittest.TestCase):
    def test_exact_payload_is_normalized_and_hashed(self):
        first = valid_payload(
            resource_id="  res-20260728-001  ",
            resource_name="  暮色心约  ",
            task_start_time="2026-07-28T16:30:15+08:00",
            drama_dubbing_type="  AI配音  ",
            optimizer_name="  张三  ",
        )
        second = valid_payload(task_start_time="2026-07-28T08:30:15Z")

        normalized = service.normalize_payload(first)

        self.assertEqual(normalized["resource_id"], "res-20260728-001")
        self.assertEqual(normalized["resource_name"], "暮色心约")
        self.assertEqual(normalized["drama_dubbing_type"], "AI配音")
        self.assertEqual(normalized["optimizer_name"], "张三")
        self.assertEqual(normalized["task_start_time"], "2026-07-28T08:30:15Z")
        self.assertEqual(service.payload_hash(first), service.payload_hash(second))
        self.assertEqual(tuple(normalized), service.PAYLOAD_FIELDS)

    def test_optimizer_name_may_be_empty_but_other_fields_may_not(self):
        normalized = service.normalize_payload(valid_payload(optimizer_name="  "))
        self.assertEqual(normalized["optimizer_name"], "")

        for field in service.PAYLOAD_FIELDS:
            if field in ("optimizer_name", "task_start_time"):
                continue
            with self.subTest(field=field):
                with self.assertRaises(service.MaterialStatusError) as caught:
                    service.normalize_payload(valid_payload(**{field: ""}))
                self.assertEqual(caught.exception.code, "invalid_payload")
                self.assertEqual(caught.exception.status, 422)

        limits = {
            "resource_id": 128,
            "resource_name": 255,
            "drama_dubbing_type": 64,
            "task_type": 64,
            "original_material_name": 255,
            "material_name": 255,
            "language": 100,
            "final_status": 64,
            "optimizer_name": 100,
        }
        for field, limit in limits.items():
            with self.subTest(field=field, boundary="accepted"):
                service.normalize_payload(
                    valid_payload(**{field: "x" * limit})
                )
            with self.subTest(field=field, boundary="rejected"):
                with self.assertRaises(service.MaterialStatusError):
                    service.normalize_payload(
                        valid_payload(**{field: "x" * (limit + 1)})
                    )

    def test_payload_rejects_missing_unknown_and_invalid_time(self):
        missing = valid_payload()
        missing.pop("language")
        with self.assertRaises(service.MaterialStatusError):
            service.normalize_payload(missing)

        unknown = valid_payload()
        unknown["email"] = "person@example.com"
        with self.assertRaises(service.MaterialStatusError):
            service.normalize_payload(unknown)

        for timestamp in (
            "2026-07-28T08:30:15",
            "2026-07-28 08:30:15Z",
            "2026-02-30T08:30:15Z",
            "2026-07-28T08:30:15+24:00",
            "2026-07-28T08:30:15.1234567Z",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(service.MaterialStatusError):
                    service.normalize_payload(
                        valid_payload(task_start_time=timestamp)
                    )

    def test_idempotency_key_and_bearer_validation(self):
        self.assertEqual(
            service.validate_idempotency_key("material:20260728:0001"),
            "material:20260728:0001",
        )
        for value in (None, "", "short", "has whitespace", "汉字不允许000"):
            with self.subTest(value=value):
                with self.assertRaises(service.MaterialStatusError):
                    service.validate_idempotency_key(value)

        self.assertTrue(
            service.validate_bearer_authorization(
                "Bearer independent-secret",
                "independent-secret",
            )
        )
        self.assertFalse(
            service.validate_bearer_authorization(
                "Bearer wrong",
                "independent-secret",
            )
        )
        self.assertFalse(
            service.validate_bearer_authorization(
                "Basic independent-secret",
                "independent-secret",
            )
        )
        self.assertFalse(
            service.validate_bearer_authorization(
                "Bearer " + ("\x80" * 40),
                "a" * 40,
            )
        )

    def test_audit_ip_only_trusts_local_reverse_proxy(self):
        self.assertEqual(
            service.extract_audit_source_ip(
                "127.0.0.1",
                "203.0.113.8",
            ),
            "203.0.113.8",
        )
        self.assertEqual(
            service.extract_audit_source_ip(
                "::1",
                "2001:db8::8",
            ),
            "2001:db8::8",
        )
        # A remote peer's spoofed X-Real-IP is ignored.  This helper does not
        # authorize or reject either address; it only produces an audit value.
        self.assertEqual(
            service.extract_audit_source_ip(
                "198.51.100.10",
                "203.0.113.99",
            ),
            "198.51.100.10",
        )
        self.assertEqual(
            service.extract_audit_source_ip("127.0.0.1", "bad, 203.0.113.4"),
            "127.0.0.1",
        )

    def test_message_formats_include_event_and_shanghai_time(self):
        private = service.format_private_message(valid_payload(), event_id=12)
        fallback = service.format_fallback_message(
            valid_payload(optimizer_name=""),
            reason_code="optimizer_name_missing",
            event_id=13,
        )

        self.assertIn("【素材任务最终状态播报】", private)
        self.assertIn("资源名：暮色心约", private)
        self.assertIn("剧集配音类型：AI配音", private)
        self.assertIn("任务类型：素材制作", private)
        self.assertIn("事件编号：MSE-0000000012", private)
        self.assertIn(
            "任务开始时间：2026-07-28T16:30:15+08:00"
            "（Asia/Shanghai，UTC+08:00）",
            private,
        )
        self.assertIn("【⚠️ 素材任务播报未能私聊】", fallback)
        self.assertIn("事件编号：MSE-0000000013", fallback)
        self.assertIn("失败原因：optimizer_name_missing", fallback)
        self.assertIn("说明：接口未提供优化师名称", fallback)
        self.assertIn("资源名：暮色心约", fallback)
        self.assertIn("剧集配音类型：AI配音", fallback)
        self.assertIn("任务类型：素材制作", fallback)
        self.assertIn("优化师名称：（未提供）", fallback)

        expected_labels = (
            "资源ID：",
            "资源名：",
            "任务开始时间：",
            "剧集配音类型：",
            "任务类型：",
            "素材原始名：",
            "素材名：",
            "语种：",
            "最终状态：",
            "优化师名称：",
        )
        for message in (private, fallback):
            positions = [message.index(label) for label in expected_labels]
            self.assertEqual(positions, sorted(positions))
            for label in expected_labels:
                self.assertEqual(message.count(label), 1)

    def test_result_details_are_whitelisted_and_require_masking(self):
        self.assertEqual(
            service.sanitize_result_details(
                {
                    "admin_user_id": 17,
                    "masked_email": "z***@example.com",
                    "feishu_message_id": "om_1234567890",
                    "failure_code": "none",
                }
            ),
            {
                "admin_user_id": "17",
                "masked_email": "z***@example.com",
                "feishu_message_id": "om_1234567890",
                "failure_code": "none",
            },
        )
        for details in (
            {"email": "person@example.com"},
            {"masked_email": "person@example.com"},
            {"open_id": "ou_1234567890"},
            {"token": "secret"},
        ):
            with self.subTest(details=details):
                with self.assertRaises(service.MaterialStatusError):
                    service.sanitize_result_details(details)


class OptimizerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.sqlite3"
        self.clock = MutableClock()
        self.cache = service.MaterialStatusOptimizerCache(
            self.path,
            clock=self.clock,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_replace_persists_exact_names_and_upsert_adds_misses(self):
        count = self.cache.replace_all(
            [
                {
                    "optimizer_name": "fengkai",
                    "admin_user_id": "17",
                    "email": "fengkai@example.com",
                },
                {
                    "optimizer_name": "CaseSensitive",
                    "admin_user_id": "18",
                    "email": "case@example.com",
                },
            ]
        )

        self.assertEqual(count, 2)
        self.assertEqual(self.cache.count(), 2)
        self.assertEqual(
            self.cache.get(" fengkai ")["admin_user_id"],
            "17",
        )
        self.assertIsNone(self.cache.get("casesensitive"))
        reopened = service.MaterialStatusOptimizerCache(self.path)
        self.assertEqual(reopened.get("fengkai")["email"], "fengkai@example.com")

        self.clock.advance(60)
        added = self.cache.upsert(
            "new_optimizer",
            "19",
            "new@example.com",
        )
        self.assertEqual(added["admin_user_id"], "19")
        self.assertEqual(self.cache.count(), 3)

    def test_conflicting_refresh_does_not_replace_last_good_cache(self):
        self.cache.upsert("fengkai", "17", "fengkai@example.com")

        with self.assertRaises(service.MaterialStatusError):
            self.cache.replace_all(
                [
                    {
                        "optimizer_name": "duplicate",
                        "admin_user_id": "20",
                        "email": "first@example.com",
                    },
                    {
                        "optimizer_name": "duplicate",
                        "admin_user_id": "21",
                        "email": "second@example.com",
                    },
                ]
            )

        self.assertEqual(self.cache.count(), 1)
        self.assertEqual(
            self.cache.get("fengkai")["email"],
            "fengkai@example.com",
        )


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "material-status.sqlite3"
        self.clock = MutableClock()
        self.store = service.MaterialStatusOutbox(
            self.db_path,
            clock=self.clock,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_storage_is_additive_and_idempotent(self):
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO sentinel(value) VALUES('keep')")
            conn.commit()

        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)

        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM sentinel").fetchone()[0],
                "keep",
            )
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(material_status_broadcast_outbox)"
                )
            }
        self.assertIn("source_ip", columns)
        self.assertIn("result_json", columns)
        self.assertNotIn("email", columns)
        self.assertNotIn("open_id", columns)
        self.assertNotIn("token", columns)

    def test_enqueue_deduplicates_same_payload_and_rejects_conflict(self):
        first = self.store.enqueue(
            "material:20260728:0001",
            valid_payload(),
            source_ip="203.0.113.8",
        )
        duplicate = self.store.enqueue(
            "material:20260728:0001",
            valid_payload(task_start_time="2026-07-28T16:30:15+08:00"),
            source_ip="198.51.100.4",
        )

        self.assertTrue(first["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(duplicate["source_ip"], "203.0.113.8")

        for changes in (
            {"resource_name": "另一资源名"},
            {"drama_dubbing_type": "真人配音"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(service.MaterialStatusError) as caught:
                    self.store.enqueue(
                        "material:20260728:0001",
                        valid_payload(**changes),
                    )
                self.assertEqual(caught.exception.code, "idempotency_conflict")
                self.assertEqual(caught.exception.status, 409)

    def test_concurrent_enqueue_creates_exactly_one_event(self):
        def enqueue_once(_):
            return self.store.enqueue(
                "material:20260728:concurrent",
                valid_payload(resource_id="res-concurrent"),
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(enqueue_once, range(8)))

        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual(sum(bool(item["created"]) for item in results), 1)

    def test_claim_retry_then_deliver(self):
        event = self.store.enqueue(
            "material:20260728:0002",
            valid_payload(),
            max_attempts=3,
        )
        claimed = self.store.claim_next(lease_seconds=30)

        self.assertEqual(claimed["id"], event["id"])
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertTrue(claimed["lease_id"])
        self.assertIsNone(self.store.claim_next())

        retried = self.store.schedule_retry(
            claimed["id"],
            claimed["lease_id"],
            "feishu_timeout",
            (
                "email=person@example.com open_id=ou_1234567890 "
                "Authorization: Bearer top-secret"
            ),
            delay_seconds=30,
            result={"failure_code": "feishu_timeout"},
        )
        self.assertEqual(retried["status"], "retry")
        self.assertEqual(retried["lease_id"], "")
        self.assertNotIn("person@example.com", retried["last_error_message"])
        self.assertNotIn("ou_1234567890", retried["last_error_message"])
        self.assertNotIn("top-secret", retried["last_error_message"])
        self.assertIsNone(self.store.claim_next())

        self.clock.advance(30)
        second_claim = self.store.claim_next(lease_seconds=30)
        self.assertEqual(second_claim["attempt_count"], 2)

        delivered = self.store.mark_delivered(
            second_claim["id"],
            second_claim["lease_id"],
            delivery_kind="private",
            metadata={
                "admin_user_id": "17",
                "masked_email": "z***@example.com",
                "feishu_message_id": "om_1234567890",
            },
        )
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["delivery_kind"], "private")
        self.assertEqual(delivered["result"]["admin_user_id"], "17")
        self.assertTrue(delivered["delivered_at"])
        self.assertIsNone(self.store.claim_next())

    def test_retry_exhaustion_and_explicit_dead_letter(self):
        event = self.store.enqueue(
            "material:20260728:0003",
            valid_payload(),
            max_attempts=1,
        )
        claimed = self.store.claim_next()
        exhausted = self.store.schedule_retry(
            event["id"],
            claimed["lease_id"],
            "mapping_failed",
            "mapping unavailable",
            delay_seconds=0,
            result={"failure_code": "mapping_failed"},
        )
        self.assertEqual(exhausted["status"], "dead_letter")
        self.assertTrue(exhausted["dead_lettered_at"])

        other = self.store.enqueue(
            "material:20260728:0004",
            valid_payload(resource_id="res-20260728-004"),
        )
        other_claim = self.store.claim_next()
        dead = self.store.mark_dead_letter(
            other["id"],
            other_claim["lease_id"],
            "invalid_mapping",
            "mapping is permanently invalid",
        )
        self.assertEqual(dead["status"], "dead_letter")
        self.assertEqual(dead["last_error_code"], "invalid_mapping")

    def test_stale_lease_cannot_complete_after_reclaim(self):
        event = self.store.enqueue(
            "material:20260728:0005",
            valid_payload(),
            max_attempts=3,
        )
        first = self.store.claim_next(lease_seconds=5)
        self.clock.advance(5)
        second = self.store.claim_next(lease_seconds=5)

        self.assertEqual(second["id"], event["id"])
        self.assertNotEqual(first["lease_id"], second["lease_id"])
        with self.assertRaises(service.MaterialStatusError) as caught:
            self.store.mark_delivered(
                event["id"],
                first["lease_id"],
            )
        self.assertEqual(caught.exception.code, "outbox_lease_conflict")
        delivered = self.store.mark_delivered(
            event["id"],
            second["lease_id"],
            delivery_kind="fallback",
        )
        self.assertEqual(delivered["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
