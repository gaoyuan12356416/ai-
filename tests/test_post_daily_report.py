import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.post_daily_report.common import Window, read_db, in_window
from features.post_daily_report.delivery import DeliveryStore, DeliveryUnknown, DefiniteFailure, send_card
from features.post_daily_report.report import total, build_card, collect_report, validate_channel


class ReportTests(unittest.TestCase):
    def test_beijing_day_and_cutoff_boundaries(self):
        window = Window.for_date("2026-09-07")
        self.assertEqual(window.start.isoformat(), "2026-09-06T16:00:00+00:00")
        self.assertEqual(window.cutoff.isoformat(), "2026-09-08T02:00:00+00:00")
        self.assertTrue(in_window("2026-09-07T15:59:59Z", window.start, window.end))
        self.assertFalse(in_window("2026-09-07T16:00:00Z", window.start, window.end))

    def test_readonly_connection_never_creates_or_writes_publisher(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.db"
            with self.assertRaises(sqlite3.OperationalError):
                with read_db(path): pass
            self.assertFalse(path.exists())
            sqlite3.connect(str(path)).close()
            with read_db(path) as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("CREATE TABLE mutation(x)")

    def test_unknown_expected_not_zero_or_false_completion_rate(self):
        sums = total([{"expected": None, "published": 2, "late": 1}])
        self.assertIsNone(sums["expected"])
        self.assertIsNone(sums["missing"])
        self.assertEqual(sums["published"], 2)

    def test_channel_failure_isolated_and_visible(self):
        with patch("features.post_daily_report.report.importlib.import_module", side_effect=RuntimeError("secret-must-not-leak")):
            report = collect_report({}, Window.for_date("2026-09-07"))
        self.assertEqual(len(report["channels"]), 3)
        encoded = json.dumps(build_card(report), ensure_ascii=False)
        self.assertIn("数据不完整", encoded)
        self.assertNotIn("secret-must-not-leak", encoded)

    def test_overcompletion_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_channel({"rows": [{"expected": 1, "published": 2}]})

    def test_many_warnings_do_not_prevent_partial_report(self):
        report = {"date": "2026-09-07", "channels": [{"channel": "FB", "warnings": ["缺少历史计划快照：" + str(i) for i in range(300)], "rows": [{"source": "auto", "expected": None, "published": 0}]}]}
        card = build_card(report)
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(json.dumps({"content": content}, ensure_ascii=False).encode()), 29000)
        self.assertIn("296", content)
        self.assertIn("未知", content)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeliveryStore(Path(self.temp.name) / "delivery.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sent_is_deduplicated(self):
        self.assertTrue(self.store.claim("2026-09-07", "chat"))
        self.store.finish("2026-09-07", "chat", "sent", "message-1")
        self.assertIsNone(self.store.claim("2026-09-07", "chat"))

    def test_crash_or_ambiguous_send_not_retried(self):
        self.store.claim("2026-09-07", "chat")
        with self.assertRaises(DeliveryUnknown): self.store.claim("2026-09-07", "chat")
        self.store.finish("2026-09-07", "chat", "unknown")
        with self.assertRaises(DeliveryUnknown): self.store.claim("2026-09-07", "chat")

    def test_clear_rejection_bounded_and_keeps_same_uuid(self):
        ids = []
        for _ in range(3):
            ids.append(self.store.claim("2026-09-07", "chat"))
            self.store.finish("2026-09-07", "chat", "failed")
        self.assertEqual(len(set(ids)), 1)
        with self.assertRaises(DefiniteFailure): self.store.claim("2026-09-07", "chat")

    def test_sent_card_content_matches_checked_serialization(self):
        config = Path(self.temp.name) / "bot.json"
        config.write_text('{"appId":"fixture","appSecret":"fixture"}', encoding="utf-8")
        card = {"header": {"title": "日报"}, "elements": []}
        with patch("features.post_daily_report.delivery._post", side_effect=[{"code": 0, "tenant_access_token": "fixture"}, {"code": 0, "data": {"message_id": "m"}}]) as post:
            send_card(config, "chat", card, "uuid")
        self.assertEqual(post.call_args_list[1].args[1]["content"], json.dumps(card, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__": unittest.main()
