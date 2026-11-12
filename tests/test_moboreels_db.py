import datetime
import json
from pathlib import Path
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
        with patch("features.moboreels_library.db.subprocess.run", return_value=response):
            with self.assertRaises(DatabaseError) as failure:
                self.db._read("SELECT 1;")
        self.assertNotIn("private-password", str(failure.exception))

    def test_read_rejects_writable_endpoint_response(self):
        response = SimpleNamespace(returncode=0, stdout='0\n{"total":1}\n', stderr="")
        with patch("features.moboreels_library.db.subprocess.run", return_value=response):
            with self.assertRaises(DatabaseError):
                self.db._read("SELECT 1;")

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
