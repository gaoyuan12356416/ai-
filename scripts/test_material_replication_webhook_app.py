"""Isolated HTTP, delivery, and legacy-compatibility checks. No live services."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from features.material_replication_broadcast import delivery, service


TOKEN = "replication-test-" + "r" * 48


def payload(kind="replication_started", **changes):
    item = {
        "resource_id": "RES-01", "resource_name": "测试短剧",
        "original_material_id": "MAT-01", "original_material_name": "原始素材",
    }
    if kind == "replication_failed":
        item["failed_languages"] = ["西班牙语", "法语"]
    result = {"event_type": kind, "editor_username": "editor_demo", "items": [item]}
    result.update(changes)
    return result


class DeliveryFailure(RuntimeError):
    def __init__(self, code="message_send_failed", retryable=False, uncertain=False):
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain
        super().__init__(code)


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 8, 27, 6, tzinfo=timezone.utc)
        self.store = service.ReplicationOutbox(
            Path(self.temp.name) / "test.sqlite3", clock=lambda: self.now,
        )
        self.editor = mock.Mock(return_value={"matched": True, "email": "test@example.invalid", "admin_user_id": "1"})
        self.person = mock.Mock(return_value={"matched": True, "open_id": "ou_test_editor"})
        self.sender = mock.Mock(return_value={"message_id": "om_test_message"})
        self.runtime = delivery.ReplicationRuntime(
            self.store.db_path, (TOKEN,), "oc_test_fallback", self.editor,
            self.person, self.sender, store=self.store,
        )

    def tearDown(self):
        self.runtime.stop()
        self.temp.cleanup()

    def enqueue(self, body=None, key="replication-test-001"):
        return self.store.enqueue(key, payload() if body is None else body)

    def process_next(self):
        row = self.store.claim_next()
        self.assertIsNotNone(row)
        return self.runtime.process(row)


class ReplicationDeliveryTests(RuntimeFixture):
    def test_private_success_uses_exact_recipient_and_frozen_message(self):
        original = self.enqueue()
        done = self.process_next()
        self.assertEqual((done["status"], done["delivery_kind"]), ("delivered", "private"))
        self.editor.assert_called_once_with("editor_demo")
        self.person.assert_called_once_with("test@example.invalid")
        args = self.sender.call_args.args
        self.assertEqual(args[:2], ("open_id", "ou_test_editor"))
        self.assertIn("以下素材已自动发起复刻任务", args[2])
        self.assertIn(service.format_batch_id(original["id"]), args[2])
        self.assertLessEqual(len(args[3]), 50)

    def test_failed_languages_are_not_reported_as_all_failed(self):
        self.enqueue(payload("replication_failed"))
        self.process_next()
        text = self.sender.call_args.args[2]
        self.assertIn("失败语种：西班牙语、法语", text)
        self.assertNotIn("日语", text)
        self.assertIn("重试基本也不会成功", text)

    def test_mapping_failure_uses_existing_fallback(self):
        self.editor.return_value = {"matched": False, "code": "optimizer_not_found"}
        self.enqueue()
        queued = self.process_next()
        self.assertEqual(queued["phase"], "fallback")
        self.assertEqual(queued["status"], "queued")
        self.sender.assert_not_called()
        done = self.process_next()
        self.assertEqual((done["status"], done["delivery_kind"]), ("delivered", "fallback"))
        self.assertEqual(self.sender.call_args.args[:2], ("chat_id", "oc_test_fallback"))

    def test_definitive_private_rejection_clears_only_current_marker(self):
        self.enqueue()
        self.sender.side_effect = DeliveryFailure()
        queued = self.process_next()
        self.assertEqual(queued["phase"], "fallback")
        self.assertFalse(queued["uncertain"])
        self.sender.side_effect = None
        self.assertEqual(self.process_next()["delivery_kind"], "fallback")

    def test_transient_definitive_failure_retries_same_target_uuid(self):
        self.enqueue()
        self.sender.side_effect = DeliveryFailure(retryable=True)
        self.assertEqual(self.process_next()["status"], "retry")
        first = self.sender.call_args.args
        self.person.return_value = {"matched": True, "open_id": "ou_changed_mapping"}
        self.now += timedelta(seconds=2)
        self.sender.side_effect = None
        self.assertEqual(self.process_next()["status"], "delivered")
        self.assertEqual(self.sender.call_args.args, first)
        self.person.assert_called_once()

    def test_timeout_then_explicit_rejection_keeps_uncertainty_sticky(self):
        self.enqueue()
        self.sender.side_effect = DeliveryFailure("feishu_send_unavailable", True, True)
        row = self.process_next()
        self.assertTrue(row["uncertain"])
        original_args = self.sender.call_args.args
        self.sender.side_effect = DeliveryFailure(retryable=False, uncertain=False)
        for _ in range(4):
            self.now += timedelta(seconds=200)
            row = self.process_next()
        self.assertEqual(row["status"], "delivery_unknown")
        self.assertEqual(row["phase"], "private")
        self.assertEqual(row["delivery_kind"], "")
        self.assertTrue(all(call.args == original_args for call in self.sender.call_args_list))
        self.assertEqual(self.sender.call_count, 5)

    def test_missing_message_id_is_uncertain(self):
        self.enqueue()
        self.sender.return_value = {}
        row = self.process_next()
        self.assertEqual(row["status"], "retry")
        self.assertTrue(row["uncertain"])

    def test_fallback_unknown_never_returns_to_private(self):
        self.editor.return_value = {"matched": False, "code": "optimizer_not_found"}
        self.enqueue()
        self.process_next()
        self.sender.side_effect = DeliveryFailure("feishu_send_unavailable", True, True)
        first = self.process_next()
        self.now += timedelta(seconds=2)
        self.sender.side_effect = None
        done = self.process_next()
        self.assertEqual(first["phase"], "fallback")
        self.assertEqual(done["delivery_kind"], "fallback")
        self.assertEqual(self.sender.call_args_list[0].args, self.sender.call_args_list[1].args)

    def test_crash_after_send_preserves_marker_and_reuses_original_target(self):
        original = self.enqueue()
        with mock.patch.object(self.store, "delivered", side_effect=sqlite3.OperationalError("simulated")):
            with self.assertRaises(sqlite3.OperationalError):
                self.process_next()
        pending = self.store.get(original["id"])
        self.assertTrue(pending["uncertain"])
        first = self.sender.call_args.args
        self.now += timedelta(seconds=301)
        self.assertEqual(self.process_next()["status"], "delivered")
        self.assertEqual(self.sender.call_args.args, first)

    def test_expired_uncertainty_window_never_sends_again(self):
        original = self.enqueue()
        self.sender.side_effect = DeliveryFailure("feishu_send_unavailable", True, True)
        self.process_next()
        self.now += timedelta(seconds=3301)
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.get(original["id"])["status"], "delivery_unknown")
        self.assertEqual(self.sender.call_count, 1)


class ReplicationHTTPTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.ready = mock.patch.object(self.runtime, "ready", return_value=True)
        self.ready.start()
        self.inject = mock.patch.object(app, "MATERIAL_REPLICATION_RUNTIME", self.runtime)
        self.inject.start()
        self.server = app.ThreadedHTTPServer(("127.0.0.1", 0), app.DramaMaterialHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.inject.stop()
        self.ready.stop()
        super().tearDown()

    def post(self, data=None, key="replication-http-001", token=TOKEN, headers=None, path=delivery.ENDPOINT):
        data = payload() if data is None else data
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        if key is not None:
            request_headers["Idempotency-Key"] = key
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            conn.request("POST", path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            conn.close()

    def test_actual_route_accepts_both_types_without_sending_or_waiting(self):
        for i, kind in enumerate(("replication_started", "replication_failed")):
            status, headers, result = self.post(payload(kind), key="replication-kind-%d" % i)
            self.assertEqual(status, 202)
            self.assertEqual(result["delivery_status"], "queued")
            self.assertEqual(result["delivery_kind"], "")
            self.assertTrue(result["batch_id"].startswith("MRB-"))
            self.assertIn("no-store", headers["Cache-Control"])
        self.sender.assert_not_called()

    def test_duplicate_and_conflict(self):
        first = self.post()[2]
        duplicate = self.post()[2]
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["batch_id"], duplicate["batch_id"])
        self.assertEqual(self.post(payload(editor_username="other"))[0], 409)

    def test_auth_separate_from_legacy_and_header_required(self):
        for token in (None, "old-status-token-" + "x" * 48, "short"):
            self.assertEqual(self.post(token=token)[0], 401)
        self.assertEqual(self.post(token=None, headers={"X-API-Token": TOKEN})[0], 401)
        self.assertEqual(self.post(key=None)[0], 400)

    def test_batch_atomic_validation(self):
        invalid = payload()
        invalid["items"].append(dict(invalid["items"][0], original_material_id=123))
        self.assertEqual(self.post(invalid)[0], 422)
        self.assertIsNone(self.store.get(1))

    def test_strict_json_and_unknown_fields(self):
        for raw in (b'{"items":[],"items":[]}', b'{"a":NaN}', b'\xff', b'{} trailing'):
            self.assertEqual(self.post(raw)[0], 400)
        self.assertEqual(self.post(dict(payload(), extra=True))[0], 422)

    def test_size_content_type_and_empty_items(self):
        self.assertEqual(self.post(b"x" * 32769)[0], 413)
        self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post(payload(items=[]))[0], 422)
        self.assertEqual(self.post(headers={"Content-Encoding": "gzip"})[0], 400)

    def test_fifty_items_and_item_limit(self):
        item = payload()["items"][0]
        body = payload(items=[dict(item, original_material_id=str(i)) for i in range(50)])
        self.assertEqual(self.post(body)[2]["item_count"], 50)
        body["items"].append(dict(item))
        self.assertEqual(self.post(body, key="too-many-items")[0], 422)

    def test_render_size_rejected_before_enqueue(self):
        with mock.patch.object(service, "validate_message_size", side_effect=service.ReplicationError("message_too_large", "too large", 413)):
            self.assertEqual(self.post()[0], 413)
        self.assertIsNone(self.store.get(1))

    def test_unavailable_worker_or_config_rejects_without_enqueue(self):
        with mock.patch.object(self.runtime, "ready", return_value=False):
            self.assertEqual(self.post()[0], 503)
        with mock.patch.object(self.runtime, "tokens", ()):
            self.assertEqual(self.post()[0], 503)
        self.assertIsNone(self.store.get(1))

    def test_terminal_duplicate_is_status_lookup_not_resend(self):
        first = self.post()[2]
        self.process_next()
        result = self.post()[2]
        self.assertEqual(result["batch_id"], first["batch_id"])
        self.assertEqual(result["delivery_status"], "delivered")
        self.assertTrue(result["duplicate"])
        self.assertEqual(self.sender.call_count, 1)


class SharedSenderCertaintyTests(unittest.TestCase):
    def test_invalid_json_200_is_unknown_without_changing_legacy_retry_flag(self):
        response = mock.Mock(status_code=200)
        response.json.side_effect = ValueError("bad json")
        with self.assertRaises(app.MaterialStatusDeliveryError) as raised:
            app.material_status_feishu_response(response, "message_send")
        self.assertTrue(raised.exception.uncertain)
        self.assertFalse(raised.exception.retryable)

    def test_explicit_rejection_is_known_but_http500_is_unknown(self):
        for http_status, expected in ((400, False), (429, False), (500, True)):
            response = mock.Mock(status_code=http_status)
            response.json.return_value = {"code": 230001, "msg": "rejected"}
            with self.assertRaises(app.MaterialStatusDeliveryError) as raised:
                app.material_status_feishu_response(response, "message_send")
            self.assertEqual(raised.exception.uncertain, expected)


if __name__ == "__main__":
    unittest.main()
