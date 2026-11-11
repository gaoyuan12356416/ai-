import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from features.x_material_inventory import summarize, metadata_candidates, mysql_rows
from features.x_material_alert import AlertStore, build_card
from features.post_daily_report.delivery import DeliveryUnknown

NOW = datetime(2026, 11, 11, 2, 30, tzinfo=timezone.utc)
SCHEDULE = {"enabled": 1, "schedule_mode": "random", "random_daily_count": 2, "publish_times_json": "[]"}
CONFIG = {"chat_id": "test_chat", "mention_id": "ou_test", "feishu_config": "unused"}


def account(language="en", member=False):
    return {"drama_language": language, "subscription_type": "premium" if member else "none"}


def candidate(language="en", duration=60):
    return {"material_language": language, "source_duration": duration}


class InventoryTests(unittest.TestCase):
    def test_threshold_inclusive_and_language_specific(self):
        report = summarize(SCHEDULE, [account(), account("jp")],
                           [candidate()] * 4 + [candidate("ja")] * 5, now=NOW)
        self.assertEqual([(r["language"], r["threshold"], r["alert"]) for r in report["languages"]],
                         [("en", 4, True), ("ja", 4, False)])

    def test_fixed_schedule_disabled_and_zero_stock(self):
        schedule = dict(SCHEDULE, schedule_mode="fixed", publish_times_json='["10:00","18:00","20:00"]')
        report = summarize(schedule, [account(), account()], [], now=NOW)
        self.assertEqual(report["languages"][0]["threshold"], 12)
        self.assertTrue(report["languages"][0]["alert"])
        self.assertEqual(summarize(dict(schedule, enabled=0), [account()], [], now=NOW)["languages"], [])

    def test_long_stock_requires_same_language_capacity(self):
        report = summarize(SCHEDULE, [account(), account("ja", True)], [candidate(duration=1800)] * 300, now=NOW)
        row = report["languages"][0]
        self.assertEqual(row["remaining"], 0)
        self.assertTrue(row["alert"])
        self.assertIn("未计入可用余量", json.dumps(build_card(report, "ou_test"), ensure_ascii=False))

    def test_long_stock_does_not_mask_standard_shortage(self):
        row = summarize(SCHEDULE, [account(), account(member=True)],
                        [candidate(duration=1800)] * 300 + [candidate()] * 4, now=NOW)["languages"][0]
        self.assertEqual(row["remaining"], 304)
        self.assertTrue(row["short_low"])
        self.assertTrue(row["alert"])
        row = summarize(SCHEDULE, [account(member=True)], [candidate(duration=1800)] * 300, now=NOW)["languages"][0]
        self.assertFalse(row["alert"])

    def test_daily_dedupe_old_backlog_and_unknown_preserved(self):
        report = summarize(SCHEDULE, [account()], [], now=NOW)
        with tempfile.TemporaryDirectory() as folder:
            store = AlertStore(Path(folder) / "delivery.sqlite3")
            try:
                store.announce(CONFIG)  # Historic pending alert must not leak into daily send.
                key = store.enqueue_daily(report, CONFIG)
                sender = mock.Mock(return_value={"message_id": "test_message"})
                store.deliver(CONFIG, sender, only_key=key)
                store.enqueue_daily(report, CONFIG)
                self.assertEqual(store.deliver(CONFIG, sender, only_key=key), [])
                self.assertEqual(sender.call_count, 1)
                tomorrow = dict(report, report_date="2026-11-12")
                key2 = store.enqueue_daily(tomorrow, CONFIG)
                sender.side_effect = DeliveryUnknown("timeout")
                store.deliver(CONFIG, sender, only_key=key2)
                self.assertEqual(store.deliver(CONFIG, sender, only_key=key2), [])
                self.assertEqual(store.get(key2, CONFIG["chat_id"])["status"], "unknown")
            finally:
                store.close()

    def test_sufficient_inventory_enqueues_nothing(self):
        report = summarize(SCHEDULE, [account()], [candidate()] * 5, now=NOW)
        with tempfile.TemporaryDirectory() as folder:
            store = AlertStore(Path(folder) / "delivery.sqlite3")
            try:
                key = store.enqueue_daily(report, CONFIG)
                sender = mock.Mock()
                self.assertEqual(store.deliver(CONFIG, sender, only_key=key), [])
                sender.assert_not_called()
            finally:
                store.close()

    def test_cached_live_selector_uses_drama_language_and_never_probes(self):
        pool = [{"id": 1, "material_id": "101", "created_at": "2026-11-10T00:00:00Z"}]
        material = {"101": {"material_id": "101", "product": "", "material_type": 2,
                           "material_is_deleted": 1, "material_url": "https://media.test/a.mp4",
                           "material_name": "test", "material_language": "wrong", "content_id": "1", "video_duration": 1200}}
        drama = {"1": [{"content_id": "1", "series_code": "1", "language": "jp", "drama_name": "test",
                         "drama_labels": "test", "drama_description": "test"}]}
        deploy = {"1": [{"content_id": "1", "app_id": 1479, "app": "ios", "language": "ja", "deploy_time": 0}]}
        with mock.patch("features.x_posts.selector.probe_source_duration") as probe:
            rows, rejected = metadata_candidates(pool, (material, drama, deploy), NOW, {"media.test"})
            self.assertEqual(rows[0]["material_language"], "ja")
            self.assertEqual(rejected, {})
            # Inventory scans exceed the selector's per-page capacity of 100.
            many_pool, many_material = [], {}
            for i in range(250):
                key = str(1000 + i)
                many_pool.append(dict(pool[0], id=1000 + i, material_id=key))
                many_material[key] = dict(material["101"], material_id=key)
            rows, rejected = metadata_candidates(many_pool, (many_material, drama, deploy), NOW, {"media.test"})
            self.assertEqual(len(rows), 250)
            material["101"]["video_duration"] = 0
            rows, rejected = metadata_candidates(pool, (material, drama, deploy), NOW, {"media.test"})
            self.assertEqual(rows, [])
            self.assertEqual(rejected["material_duration_missing"], 1)
            probe.assert_not_called()

    def test_source_failure_not_a_zero_inventory_report(self):
        env = {"X_POST_DAILY_MYSQL_HOST": "101.32.56.53", "X_POST_DAILY_MYSQL_PORT": "63350",
               "X_POST_DAILY_MYSQL_USER": "test", "X_POST_DAILY_MYSQL_PASSWORD": "not-real"}
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "source_query_failed"):
                mysql_rows(env, "SELECT 1")


if __name__ == "__main__":
    unittest.main()
