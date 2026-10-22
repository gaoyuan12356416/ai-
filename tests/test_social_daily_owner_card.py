import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from features.social_analytics import routes
from features.social_daily_report.collector import present
from features.social_daily_report.report import build_cards, require_complete
from test_social_daily_report import fixture, DAY


class OwnerCardTests(unittest.TestCase):
    def test_people_precede_channel_summary_and_no_placeholders(self):
        report = present(fixture(), DAY)
        text = json.dumps(build_cards(report, "chat"), ensure_ascii=False)
        self.assertLess(text.index("小明"), text.index("渠道汇总"))
        self.assertLess(text.index("郜远"), text.index("渠道汇总"))
        self.assertNotIn("待核实", text)
        require_complete(report)

    def test_null_metric_blocks_official_delivery_even_if_quality_claims_complete(self):
        report = present(fixture(), DAY)
        report["rows"][0]["platforms"]["youtube"]["revenue_cents"] = None
        with self.assertRaisesRegex(ValueError, "metrics_incomplete"):
            require_complete(report)

    def test_incomplete_report_is_never_frozen_or_sent(self):
        from scripts.social_publisher_daily_report import main
        from datetime import datetime, timedelta
        from features.social_daily_report.collector import BEIJING
        day = (datetime.now(BEIJING).date() - timedelta(days=1)).isoformat()
        report = present(fixture(), day)
        report["platforms"]["youtube"]["quality"]["state"] = "unavailable"
        with tempfile.TemporaryDirectory() as folder, \
             patch("scripts.social_publisher_daily_report.collect", return_value=report), \
             patch("scripts.social_publisher_daily_report.send_card") as send, \
             patch("scripts.social_publisher_daily_report.SocialDeliveryStore.freeze") as freeze:
            with self.assertRaisesRegex(ValueError, "sources_incomplete"):
                main(["--date", day, "--send", "--state-dir", folder])
            send.assert_not_called()
            freeze.assert_not_called()
            failures = list((Path(folder) / "failures" / day).glob("*.json"))
            self.assertEqual(len(failures), 1)
            evidence = json.loads(failures[0].read_text(encoding="utf-8"))
            self.assertEqual(evidence["reason"], "daily_report_sources_incomplete")
            self.assertEqual(evidence["quality"]["platforms"]["youtube"]["state"], "unavailable")

    def test_daily_query_has_more_time_without_bypassing_gate_or_overall_deadline(self):
        app = dict(ADMIN_MAPPING_MYSQL_HOST="101.32.56.53", ADMIN_MAPPING_MYSQL_PORT=63350,
                   ADMIN_MAPPING_MYSQL_USER="reader", ADMIN_MAPPING_MYSQL_PASSWORD="test-only-secret")
        success = SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        for requested, sql_ms, gate_seconds, process_seconds in [(None, 8000, "12", 25),
                                                                (20000, 20000, "24", 37),
                                                                (999999, 20000, "24", 37)]:
            settings = dict(app)
            if requested is not None:
                settings["SOCIAL_ANALYTICS_STATEMENT_TIMEOUT_MS"] = requested
            with patch.object(routes.subprocess, "run", return_value=success) as run, \
                 patch.dict(routes.os.environ, {"SQL_GATE_BYPASS": "1"}):
                routes.query_rows(settings, "SELECT 1")
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/mysql")
            kwargs = run.call_args.kwargs
            self.assertIn("MAX_EXECUTION_TIME=%d" % sql_ms, kwargs["input"])
            self.assertEqual(kwargs["env"]["SQL_GATE_EXEC_TIMEOUT_SECONDS"], gate_seconds)
            self.assertEqual(kwargs["timeout"], process_seconds)
            self.assertNotIn("SQL_GATE_BYPASS", kwargs["env"])
        settings.update(SOCIAL_ANALYTICS_QUERY_DEADLINE=110)
        with patch.object(routes.time, "monotonic", return_value=100), \
             patch.object(routes.subprocess, "run", return_value=success) as run:
            routes.query_rows(settings, "SELECT 1")
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
        with patch.object(routes.time, "monotonic", return_value=110), \
             patch.object(routes.subprocess, "run") as run:
            with self.assertRaises(routes.ReportError):
                routes.query_rows(settings, "SELECT 1")
            run.assert_not_called()

    def test_mysql_timeout_has_safe_persistent_reason(self):
        app = dict(ADMIN_MAPPING_MYSQL_HOST="101.32.56.53", ADMIN_MAPPING_MYSQL_PORT=63350,
                   ADMIN_MAPPING_MYSQL_USER="reader", ADMIN_MAPPING_MYSQL_PASSWORD="test-only-secret")
        failure = SimpleNamespace(returncode=1, stdout="1\n", stderr="ERROR 3024 (HY000): test-only-secret")
        with patch.object(routes.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(routes.ReportError, "MySQL 3024") as raised:
                routes.query_rows(app, "SELECT 1")
        self.assertNotIn("test-only-secret", str(raised.exception))

    def test_read_retry_is_bounded_and_keeps_completed_source_cached(self):
        routes._cache.clear()
        app = {"SOCIAL_ANALYTICS_QUERY_ATTEMPTS": 2,
               "SOCIAL_ANALYTICS_QUERY_DEADLINE": time.monotonic() + 30}
        rows = [dict(date=DAY, site_id="2284", campaign_id="id", clicks=1, views=1,
                     installs=0, conversions=0, revenue_cents=1, refund_cents=0)]
        with patch.object(routes, "query_rows", side_effect=[routes.ReportError("busy", 503), rows]) as query, \
             patch.object(routes.time, "sleep") as sleep:
            routes.source(app, DAY, DAY, "youtube")
            routes.source(app, DAY, DAY, "youtube")
            self.assertEqual(query.call_count, 2)
            sleep.assert_called_once_with(1)
        routes._cache.clear()

    def test_web_request_does_not_gain_retry_or_bypass_query_gate(self):
        routes._cache.clear()
        with patch.object(routes, "query_rows", side_effect=routes.ReportError("busy", 503)) as query:
            with self.assertRaises(routes.ReportError):
                routes.source({}, DAY, DAY, "youtube")
            self.assertEqual(query.call_count, 1)

    def test_private_send_resolves_recipient_and_preserves_formal_archive(self):
        from scripts.social_daily_private_preview import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = sqlite3.connect(root / "users.sqlite")
            db.execute("CREATE TABLE drama_admin_user(user_id TEXT,name TEXT,open_id TEXT)")
            db.execute("INSERT INTO drama_admin_user VALUES('one','郜远','ou_verified')")
            db.commit()
            db.close()
            source = root / "source.json"
            source.write_text(json.dumps(present(fixture(), DAY)), encoding="utf-8")
            archive = root / "reports" / DAY
            archive.mkdir(parents=True)
            (archive / "report.json").write_bytes(b"original")
            argv = ["--report-json", str(source), "--recipient-user-id", "one", "--preview-id", "review-v1",
                    "--state-dir", folder, "--db", str(root / "users.sqlite"), "--send"]
            with patch("scripts.social_daily_private_preview.send_card", return_value={"message_id": "m1"}) as send:
                main(argv)
                main(argv)
                send.assert_called_once()
                self.assertEqual(send.call_args.args[1], "ou_verified")
                self.assertEqual(send.call_args.kwargs["receive_id_type"], "open_id")
            self.assertEqual((archive / "report.json").read_bytes(), b"original")
            self.assertFalse((root / "delivery.sqlite3").exists())

    def test_ambiguous_private_recipient_is_rejected(self):
        from scripts.social_daily_private_preview import recipient
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "users.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE drama_admin_user(user_id TEXT,name TEXT,open_id TEXT)")
            db.executemany("INSERT INTO drama_admin_user VALUES(?,?,?)", [("u", "A", "ou_a"), ("u", "B", "ou_b")])
            db.commit(); db.close()
            with self.assertRaisesRegex(ValueError, "recipient_not_unique"):
                recipient(path, "u")


if __name__ == "__main__":
    unittest.main()
