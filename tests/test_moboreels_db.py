import datetime
import errno
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.moboreels_library.db import Database, DatabaseError
from features.moboreels_library.settings import Settings


class DatabaseBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Path("unused"), "institution", "token-not-used", "internal", "reader", "private-password")
        self.db = Database(self.settings)

    def test_replica_read_uses_gate_stdin_and_never_puts_password_in_arguments(self):
        response = SimpleNamespace(returncode=0, stdout='1\n{"total":1}\n', stderr="")
        with patch("features.moboreels_library.db.subprocess.run", return_value=response) as execute:
            result = self.db._read("SELECT JSON_OBJECT('total',1);")
        args, kwargs = execute.call_args
        self.assertEqual(args[0][0], "/usr/bin/mysql")
        self.assertIn("--port=63350", args[0])
        self.assertNotIn(self.settings.db_password, " ".join(args[0]))
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], self.settings.db_password)
        self.assertNotIn("SQL_GATE_BYPASS", kwargs["env"])
        self.assertTrue(kwargs["input"].startswith("SELECT @@read_only;"))
        self.assertEqual(result, [{"total": 1}])

    def test_read_errors_do_not_expose_driver_stderr_or_credentials(self):
        response = SimpleNamespace(returncode=1, stdout="", stderr="secret driver data private-password")
        with patch("features.moboreels_library.db.subprocess.run", return_value=response) as execute, self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(DatabaseError) as failure:
                self.db._read("SELECT 1;")
        self.assertNotIn("private-password", str(failure.exception))
        self.assertEqual(execute.call_count, 1)
        self.assertNotIn("private-password", " ".join(logs.output))
        self.assertNotIn("secret driver data", " ".join(logs.output))

    def test_read_rejects_writable_endpoint_response(self):
        response = SimpleNamespace(returncode=0, stdout='0\n{"total":1}\n', stderr="")
        with patch("features.moboreels_library.db.subprocess.run", return_value=response) as execute, self.assertLogs(level="WARNING"):
            with self.assertRaises(DatabaseError):
                self.db._read("SELECT 1;")
        self.assertEqual(execute.call_count, 1)

    def test_mysql_json_rows_preserve_literal_unicode_line_separators(self):
        # This is the CLI's actual one-JSON-per-LF format, not escaped ASCII fixture data.
        rows = [{"story_id": "42", "introduce": "Before\u0085middle\u2028line\u2029after"},
                {"story_id": "43", "raw_json": {"story_name": "Other\u2028name"}}]
        stdout = "1\n" + "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        self.assertGreater(len(stdout.splitlines()), 3)
        response = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        with patch("features.moboreels_library.db.subprocess.run", return_value=response) as execute:
            self.assertEqual(self.db._read("SELECT JSON_OBJECT('story_id',42);"), rows)
        self.assertEqual(execute.call_count, 1)

    def test_transient_mysql_failure_retries_same_fifo_query_and_logs_only_errno(self):
        failed = SimpleNamespace(returncode=1, stdout="1\npartial-private-row", stderr="ERROR 2013 (HY000): private-password secret SQL result")
        success = SimpleNamespace(returncode=0, stdout='1\n{"story_id":"42"}\n', stderr="")
        with patch("features.moboreels_library.db.subprocess.run", side_effect=[failed, success]) as execute, \
                patch("features.moboreels_library.db.time.sleep") as sleep, self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.db._read("SELECT JSON_OBJECT('story_id',42);"), [{"story_id": "42"}])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args_list[0], execute.call_args_list[1])
        self.assertEqual(execute.call_args.args[0][0], "/usr/bin/mysql")
        self.assertIn("--port=63350", execute.call_args.args[0])
        sleep.assert_called_once_with(1)
        log = " ".join(logs.output)
        self.assertIn("mysql_errno=2013", log)
        for private in ("private-password", "secret SQL", "partial-private-row", "JSON_OBJECT"):
            self.assertNotIn(private, log)

    def test_transient_failures_stop_after_three_fifo_attempts(self):
        response = SimpleNamespace(returncode=124, stdout="", stderr="SQL_GATE_EXECUTION_TIMEOUT private-password")
        with patch("features.moboreels_library.db.subprocess.run", return_value=response) as execute, \
                patch("features.moboreels_library.db.time.sleep") as sleep, self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(DatabaseError):
                self.db._read("SELECT 1;")
        self.assertEqual(execute.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(len(logs.output), 3)
        self.assertNotIn("private-password", " ".join(logs.output))

    def test_python_timeout_retries_without_logging_captured_output(self):
        failure = subprocess.TimeoutExpired("private-command", 100, output="private-row", stderr="private-password")
        response = SimpleNamespace(returncode=0, stdout='1\n{"total":1}\n', stderr="")
        with patch("features.moboreels_library.db.subprocess.run", side_effect=[failure, response]) as execute, \
                patch("features.moboreels_library.db.time.sleep"), self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.db._read("SELECT 1;"), [{"total": 1}])
        self.assertEqual(execute.call_count, 2)
        self.assertIn("TimeoutExpired", " ".join(logs.output))
        self.assertNotIn("private-", " ".join(logs.output))

    def test_permanent_mysql_failure_never_retries(self):
        for mysql_errno in (1045, 1064, 1146):
            with self.subTest(mysql_errno=mysql_errno):
                response = SimpleNamespace(returncode=1, stdout="", stderr="ERROR %d (HY000): private-password" % mysql_errno)
                with patch("features.moboreels_library.db.subprocess.run", return_value=response) as execute, \
                        patch("features.moboreels_library.db.time.sleep") as sleep, self.assertLogs(level="WARNING"):
                    with self.assertRaises(DatabaseError):
                        self.db._read("SELECT 1;")
                self.assertEqual(execute.call_count, 1)
                sleep.assert_not_called()

    def test_malformed_json_and_invalid_encoding_never_retry(self):
        cases = [SimpleNamespace(returncode=0, stdout='1\n{"private":', stderr=""),
                 UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private-password")]
        for result in cases:
            with self.subTest(result_type=type(result).__name__):
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("features.moboreels_library.db.subprocess.run", **options) as execute, \
                        patch("features.moboreels_library.db.time.sleep") as sleep, self.assertLogs(level="WARNING") as logs:
                    with self.assertRaises(DatabaseError):
                        self.db._read("SELECT 1;")
                self.assertEqual(execute.call_count, 1)
                sleep.assert_not_called()
                self.assertNotIn("private", " ".join(logs.output))

    def test_os_failure_retries_only_for_transient_errno(self):
        response = SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        for code, expected_calls in ((errno.EAGAIN, 2), (errno.ENOENT, 1)):
            with self.subTest(errno=code):
                with patch("features.moboreels_library.db.subprocess.run", side_effect=[OSError(code, "private-password"), response]) as execute, \
                        patch("features.moboreels_library.db.time.sleep"), self.assertLogs(level="WARNING") as logs:
                    if expected_calls == 2:
                        self.assertEqual(self.db._read("SELECT 1;"), [])
                    else:
                        with self.assertRaises(DatabaseError):
                            self.db._read("SELECT 1;")
                self.assertEqual(execute.call_count, expected_calls)
                self.assertNotIn("private-password", " ".join(logs.output))

    def test_catalog_search_is_hex_encoded_and_page_size_is_bounded(self):
        query = "' OR 1=1; -- 中文"
        with patch.object(self.db, "_read", return_value=[]) as read:
            self.db.list_dramas({"q": query, "page_size": 50})
        sql = read.call_args.args[0]
        self.assertNotIn(query, sql)
        self.assertIn(query.encode().hex(), sql)
        self.assertIn("ORDER BY source_publish_time DESC,story_id DESC", sql)
        self.assertIn("is_show=1", sql)
        with self.assertRaises(ValueError):
            self.db.list_dramas({"page_size": 101})

    def test_promotion_mirror_keeps_success_and_uses_only_fixed_table(self):
        row = {"institution_user_id": "institution", "star_user_id": "star", "project_type": 2,
               "story_id": "42", "tenant_key": "tenant", "user_id": "user", "star_name": "Name",
               "status": "succeeded", "request_id": "req", "koc_code": "CODE", "deep_link": "https://example.test/a",
               "home_link": "https://example.test/b", "request_json": {"story_id": "42"}, "response_json": None,
               "created_at": "2026-11-12T09:00:00.001Z", "updated_at": "2026-11-12T09:00:01.002Z"}
        with patch.object(self.db, "_write") as write:
            self.db.mirror_promotion(row)
        sql, values = write.call_args.args
        self.assertTrue(sql.startswith("INSERT INTO ads_ai.cdreader_koc_promotion"))
        self.assertIn("VALUES(updated_at)>=updated_at", sql)
        self.assertIn("status<>'succeeded' OR VALUES(status)='succeeded'", sql)
        self.assertIsInstance(values[-1], datetime.datetime)
        self.assertIsNone(values[-3])
        self.assertEqual(json.loads(values[-4]), {"story_id": "42"})


if __name__ == "__main__":
    unittest.main()
