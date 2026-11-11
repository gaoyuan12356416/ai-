from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import requests

from features.fb_page_assignment.notify import DeliveryError, Notifier, alert_text


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeHttp:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "admin.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drama_admin_user(user_id TEXT,open_id TEXT,name TEXT,status TEXT)")
            db.execute("INSERT INTO drama_admin_user VALUES(?,?,?,?)", ("user_fixture", "ou_fixture", "郜远", "active"))
        self.env = {"DRAMA_JOB_DB_PATH": str(self.path), "FEISHU_APP_ID": "app_fixture",
                    "FEISHU_APP_SECRET": "secret_fixture"}
        self.task = {"task_id": "a" * 32, "external_task_id": "caller_fixture", "alert_reasons": ["no_available_page"],
                     "rows": [{"drama_id": "d", "page_id": None, "error_message": "没有可用 Page"}]}

    def token(self):
        return FakeResponse({"code": 0, "tenant_access_token": "token_fixture", "expire": 3600})

    def receipt(self):
        return FakeResponse({"code": 0, "data": {"message_id": "om_fixture"}})

    def test_private_send_uses_current_identity_stable_uuid_and_token_cache(self):
        http = FakeHttp([self.token(), self.receipt(), self.receipt()])
        notifier = Notifier(self.env, session=http)
        self.assertEqual(notifier.send(self.task), "om_fixture")
        self.assertEqual(notifier.send(self.task), "om_fixture")
        self.assertEqual(len(http.calls), 3)
        for url, options in http.calls[1:]:
            self.assertTrue(url.endswith("/im/v1/messages"))
            self.assertEqual(options["params"], {"receive_id_type": "user_id"})
            self.assertEqual(options["json"]["receive_id"], "user_fixture")
            self.assertEqual(options["json"]["uuid"], "fbpa-" + self.task["task_id"])
            self.assertEqual(options["timeout"], 15)
            self.assertNotIn("secret_fixture", json.dumps(options["json"]))

    def test_recipient_uses_open_id_when_user_id_is_absent(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE drama_admin_user SET user_id=''")
        self.assertEqual(Notifier(self.env, session=FakeHttp([])).recipient(), ("open_id", "ou_fixture"))

    def test_recipient_must_be_unique_active_person(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO drama_admin_user VALUES(?,?,?,?)", ("another", "ou_another", "郜远", "active"))
        with self.assertRaises(DeliveryError) as caught:
            Notifier(self.env, session=FakeHttp([])).send(self.task)
        self.assertEqual(caught.exception.code, "feishu_recipient_not_unique")
        self.assertFalse(caught.exception.uncertain)

    def test_missing_identity_database_is_not_created(self):
        missing = self.path.with_name("missing.sqlite3")
        env = dict(self.env, DRAMA_JOB_DB_PATH=str(missing))
        with self.assertRaises(DeliveryError) as caught:
            Notifier(env, session=FakeHttp([])).send(self.task)
        self.assertEqual(caught.exception.code, "feishu_recipient_source_unavailable")
        self.assertFalse(missing.exists())

    def test_send_timeout_is_unknown_and_exception_does_not_include_credentials(self):
        notifier = Notifier(self.env, session=FakeHttp([self.token(), requests.Timeout("Bearer token_fixture")]))
        with self.assertRaises(DeliveryError) as caught:
            notifier.send(self.task)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(str(caught.exception), "feishu_send_unknown")

    def test_token_timeout_is_known_failure_before_send(self):
        http = FakeHttp([requests.Timeout("secret_fixture")])
        with self.assertRaises(DeliveryError) as caught:
            Notifier(self.env, session=http).send(self.task)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, "feishu_token_unavailable")
        self.assertEqual(len(http.calls), 1)

    def test_explicit_rejection_is_known_but_5xx_is_uncertain(self):
        for status, uncertain in [(400, False), (503, True)]:
            notifier = Notifier(self.env, session=FakeHttp([self.token(), FakeResponse({"code": 230001}, status)]))
            with self.assertRaises(DeliveryError) as caught:
                notifier.send(self.task)
            self.assertEqual(caught.exception.uncertain, uncertain)
            self.assertEqual(caught.exception.code, "feishu_rejected_230001")

    def test_accepted_without_receipt_and_malformed_json_are_unknown(self):
        for response in [FakeResponse({"code": 0, "data": {}}), FakeResponse(ValueError("token_fixture"))]:
            with self.assertRaises(DeliveryError) as caught:
                Notifier(self.env, session=FakeHttp([self.token(), response])).send(self.task)
            self.assertTrue(caught.exception.uncertain)
            self.assertNotIn("token_fixture", str(caught.exception))

    def test_alert_text_has_trace_ids_and_is_byte_bounded(self):
        task = dict(self.task, rows=[{"drama_id": "剧" * 128, "page_id": None,
                                     "error_message": "错误" * 3000}] * 100)
        message = alert_text(task)
        self.assertIn("caller_fixture", message)
        self.assertIn("a" * 32, message)
        self.assertIn("ads_ai.fb_drama_page_assignment_log", message)
        self.assertLessEqual(len(message.encode("utf-8")), 18000)


if __name__ == "__main__":
    unittest.main()
