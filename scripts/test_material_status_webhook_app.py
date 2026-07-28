#!/usr/bin/env python3
"""HTTP and orchestration tests for the material-status webhook integration."""

from pathlib import Path
import http.client
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from features.material_status_broadcast import service


TEST_TOKEN = "mst_test_" + ("a" * 48)


def valid_payload(**changes):
    payload = {
        "resource_id": "TEST-RESOURCE-001",
        "task_start_time": "2026-07-28T14:30:00+08:00",
        "task_type": "素材制作联调测试",
        "original_material_name": "source.mp4",
        "material_name": "final.mp4",
        "language": "zh-CN",
        "final_status": "测试完成",
        "optimizer_name": "测试优化师",
    }
    payload.update(changes)
    return payload


class MaterialStatusHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = service.MaterialStatusOutbox(
            Path(self.temp.name) / "events.sqlite3"
        )
        self.old_values = {
            name: getattr(app, name)
            for name in (
                "MATERIAL_STATUS_WEBHOOK_TOKENS",
                "MATERIAL_STATUS_WEBHOOK_FALLBACK_CHAT_ID",
                "MATERIAL_STATUS_OUTBOX",
                "MATERIAL_STATUS_WORKER_THREAD",
                "FEISHU_APP_ID",
                "FEISHU_APP_SECRET",
                "ADMIN_MAPPING_MYSQL_HOST",
                "ADMIN_MAPPING_MYSQL_USER",
                "ADMIN_MAPPING_MYSQL_DATABASE",
            )
        }
        app.MATERIAL_STATUS_WEBHOOK_TOKENS = (TEST_TOKEN,)
        app.MATERIAL_STATUS_WEBHOOK_FALLBACK_CHAT_ID = "oc_test_fallback"
        app.MATERIAL_STATUS_OUTBOX = self.store
        app.MATERIAL_STATUS_WORKER_THREAD = mock.Mock()
        app.MATERIAL_STATUS_WORKER_THREAD.is_alive.return_value = True
        app.FEISHU_APP_ID = "cli_test"
        app.FEISHU_APP_SECRET = "test-only-secret"
        app.ADMIN_MAPPING_MYSQL_HOST = "127.0.0.1"
        app.ADMIN_MAPPING_MYSQL_USER = "test"
        app.ADMIN_MAPPING_MYSQL_DATABASE = "kunlunads_dev"
        self.server = app.ThreadedHTTPServer(
            ("127.0.0.1", 0),
            app.DramaMaterialHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for name, value in self.old_values.items():
            setattr(app, name, value)
        self.temp.cleanup()

    def post(
        self,
        payload,
        token=TEST_TOKEN,
        idempotency_key="mst-test-0001",
        content_type="application/json; charset=utf-8",
        extra_headers=None,
    ):
        body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        headers.update(extra_headers or {})
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=5,
        )
        try:
            connection.request(
                "POST",
                "/api/integrations/v1/material-task-status-events",
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw)
        finally:
            connection.close()

    def test_token_is_mandatory_and_x_api_token_is_not_accepted(self):
        status, headers, payload = self.post(
            valid_payload(),
            token=None,
            extra_headers={"X-API-Token": TEST_TOKEN},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "invalid_token")
        self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
        self.assertNotIn(TEST_TOKEN, json.dumps(payload))

        status, _, payload = self.post(
            valid_payload(),
            token="\x80" * 40,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "invalid_token")

    def test_accept_duplicate_and_idempotency_conflict(self):
        first_status, _, first = self.post(valid_payload())
        duplicate_status, _, duplicate = self.post(valid_payload())
        conflict_status, _, conflict = self.post(
            valid_payload(final_status="另一个状态")
        )

        self.assertEqual(first_status, 202)
        self.assertEqual(first["code"], "accepted")
        self.assertFalse(first["duplicate"])
        self.assertRegex(first["event_id"], r"^MSE-\d{10}$")
        self.assertEqual(duplicate_status, 202)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["event_id"], first["event_id"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["code"], "idempotency_conflict")

        claimed = self.store.claim_next(lease_seconds=60)
        self.store.mark_delivered(
            claimed["id"],
            claimed["lease_id"],
            delivery_kind="fallback",
        )
        terminal_status, _, terminal = self.post(valid_payload())
        self.assertEqual(terminal_status, 202)
        self.assertTrue(terminal["duplicate"])
        self.assertEqual(terminal["delivery_status"], "delivered")

    def test_short_token_fails_closed_and_rotation_accepts_either_long_token(self):
        app.MATERIAL_STATUS_WEBHOOK_TOKENS = ("too-short",)
        status, _, payload = self.post(valid_payload())
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "service_unavailable")

        rotated_token = "mst_rotated_" + ("b" * 48)
        app.MATERIAL_STATUS_WEBHOOK_TOKENS = (TEST_TOKEN, rotated_token)
        status, _, payload = self.post(
            valid_payload(resource_id="TEST-RESOURCE-ROTATED"),
            token=rotated_token,
            idempotency_key="mst-test-rotated-0001",
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["code"], "accepted")

    def test_worker_unavailable_returns_503_before_enqueue(self):
        with mock.patch.object(
            app,
            "material_status_worker_ready",
            return_value=False,
        ):
            status, _, payload = self.post(
                valid_payload(),
                idempotency_key="mst-test-worker-unavailable",
            )

        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "service_unavailable")
        self.assertIsNone(self.store.claim_next())

    def test_validation_and_body_limit(self):
        invalid = valid_payload()
        invalid.pop("language")
        status, _, payload = self.post(
            invalid,
            idempotency_key="mst-test-0002",
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["code"], "invalid_payload")

        status, _, payload = self.post(
            b"{" + (b"x" * (app.MATERIAL_STATUS_WEBHOOK_MAX_BODY_BYTES + 1)),
            idempotency_key="mst-test-0003",
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload["code"], "payload_too_large")

    def test_http_contract_errors(self):
        status, _, payload = self.post(
            valid_payload(),
            idempotency_key=None,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "idempotency_key_required")

        status, _, payload = self.post(
            valid_payload(),
            idempotency_key="short",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_idempotency_key")

        status, _, payload = self.post(
            valid_payload(),
            idempotency_key="mst-test-content-type",
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["code"], "unsupported_media_type")

        status, _, payload = self.post(
            b"{not-json",
            idempotency_key="mst-test-invalid-json",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_json")


class MaterialStatusDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = service.MaterialStatusOutbox(
            Path(self.temp.name) / "events.sqlite3"
        )

    def tearDown(self):
        self.temp.cleanup()

    def claim(self, key, payload=None, max_attempts=5):
        created = self.store.enqueue(
            key,
            payload or valid_payload(),
            max_attempts=max_attempts,
        )
        return self.store.claim_next(lease_seconds=60), created

    def test_exact_username_to_sub_user_email_mapping(self):
        with mock.patch.object(
            app,
            "run_material_status_mapping_query",
            side_effect=[
                [["17", "测试优化师"]],
                [["optimizer@example.com"]],
            ],
        ) as query:
            result = app.resolve_material_status_optimizer(" 测试优化师 ")

        self.assertTrue(result["matched"])
        self.assertEqual(result["admin_user_id"], "17")
        self.assertEqual(result["email"], "optimizer@example.com")
        self.assertIn("admin_users", query.call_args_list[0].args[0])
        self.assertIn("username", query.call_args_list[0].args[0])
        self.assertIn("BINARY TRIM(username)", query.call_args_list[0].args[0])
        self.assertNotIn("LOWER(", query.call_args_list[0].args[0])
        self.assertNotIn("测试优化师", query.call_args_list[0].args[0])
        self.assertIn("admin_user_group", query.call_args_list[1].args[0])
        self.assertIn("sub_user_id=17", query.call_args_list[1].args[0])
        self.assertIn("status=0", query.call_args_list[1].args[0])

    def test_private_delivery(self):
        event, _ = self.claim("mst-delivery-0001")
        with (
            mock.patch.object(
                app,
                "resolve_material_status_optimizer",
                return_value={
                    "matched": True,
                    "admin_user_id": "17",
                    "email": "optimizer@example.com",
                },
            ),
            mock.patch.object(
                app,
                "lookup_material_status_feishu_open_id",
                return_value={"matched": True, "open_id": "ou_test_private"},
            ),
            mock.patch.object(
                app,
                "send_material_status_feishu_text",
                return_value={"message_id": "om_test_private"},
            ) as sender,
            mock.patch.object(app, "material_status_record_audit"),
        ):
            delivered = app.process_material_status_event(
                event,
                outbox=self.store,
            )

        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["delivery_kind"], "private")
        self.assertEqual(
            delivered["result"]["masked_email"],
            "o***@example.com",
        )
        self.assertNotIn("open_id", delivered["result"])
        self.assertEqual(sender.call_args.args[0], "open_id")
        self.assertEqual(sender.call_args.args[1], "ou_test_private")
        self.assertIn("事件编号：MSE-", sender.call_args.args[2])
        self.assertEqual(
            sender.call_args.args[3],
            "mst-MSE-0000000001-private",
        )

    def test_missing_optimizer_delivers_to_fallback(self):
        event, _ = self.claim(
            "mst-delivery-0002",
            valid_payload(optimizer_name=""),
        )
        with (
            mock.patch.object(
                app,
                "send_material_status_feishu_text",
                return_value={"message_id": "om_test_fallback"},
            ) as sender,
            mock.patch.object(app, "material_status_record_audit"),
        ):
            delivered = app.process_material_status_event(
                event,
                outbox=self.store,
            )

        self.assertEqual(delivered["delivery_kind"], "fallback")
        self.assertEqual(
            delivered["result"]["failure_code"],
            "optimizer_name_missing",
        )
        self.assertEqual(sender.call_args.args[0], "chat_id")
        self.assertEqual(
            sender.call_args.args[1],
            app.MATERIAL_STATUS_WEBHOOK_FALLBACK_CHAT_ID,
        )
        self.assertEqual(
            sender.call_args.args[3],
            "mst-MSE-0000000001-fallback",
        )

    def test_transient_private_failure_retries_without_fallback(self):
        event, _ = self.claim("mst-delivery-0003", max_attempts=3)
        with (
            mock.patch.object(
                app,
                "resolve_material_status_optimizer",
                return_value={
                    "matched": True,
                    "admin_user_id": "17",
                    "email": "optimizer@example.com",
                },
            ),
            mock.patch.object(
                app,
                "lookup_material_status_feishu_open_id",
                return_value={"matched": True, "open_id": "ou_test_private"},
            ),
            mock.patch.object(
                app,
                "send_material_status_feishu_text",
                side_effect=app.MaterialStatusDeliveryError(
                    "feishu_send_unavailable",
                    "timeout",
                    retryable=True,
                ),
            ) as sender,
        ):
            retried = app.process_material_status_event(
                event,
                outbox=self.store,
            )

        self.assertEqual(retried["status"], "retry")
        self.assertEqual(retried["last_error_code"], "feishu_send_unavailable")
        self.assertEqual(sender.call_count, 1)

    def test_permanent_private_failure_uses_fallback(self):
        event, _ = self.claim("mst-delivery-0004")

        def send(receive_id_type, receive_id, text, message_uuid):
            if receive_id_type == "open_id":
                raise app.MaterialStatusDeliveryError(
                    "message_send_failed",
                    "recipient unavailable",
                    retryable=False,
                )
            return {"message_id": "om_test_fallback"}

        with (
            mock.patch.object(
                app,
                "resolve_material_status_optimizer",
                return_value={
                    "matched": True,
                    "admin_user_id": "17",
                    "email": "optimizer@example.com",
                },
            ),
            mock.patch.object(
                app,
                "lookup_material_status_feishu_open_id",
                return_value={"matched": True, "open_id": "ou_test_private"},
            ),
            mock.patch.object(
                app,
                "send_material_status_feishu_text",
                side_effect=send,
            ),
            mock.patch.object(app, "material_status_record_audit"),
        ):
            delivered = app.process_material_status_event(
                event,
                outbox=self.store,
            )

        self.assertEqual(delivered["delivery_kind"], "fallback")
        self.assertEqual(
            delivered["result"]["failure_code"],
            "private_send_failed",
        )

    def test_fallback_failure_exhaustion_is_dead_letter(self):
        event, _ = self.claim("mst-delivery-0005", max_attempts=1)
        with (
            mock.patch.object(
                app,
                "resolve_material_status_optimizer",
                return_value={
                    "matched": False,
                    "code": "optimizer_not_found",
                    "message": "not found",
                },
            ),
            mock.patch.object(
                app,
                "send_material_status_feishu_text",
                side_effect=app.MaterialStatusDeliveryError(
                    "message_send_failed",
                    "fallback unavailable",
                    retryable=False,
                ),
            ),
            mock.patch.object(app, "material_status_record_audit"),
        ):
            dead = app.process_material_status_event(
                event,
                outbox=self.store,
            )

        self.assertEqual(dead["status"], "dead_letter")
        self.assertEqual(dead["last_error_code"], "fallback_send_failed")

    def test_fallback_unknown_result_retries_same_fallback_phase(self):
        event, _ = self.claim("mst-delivery-fallback-phase")
        event["last_error_code"] = "fallback_send_failed"
        event["result"] = {"failure_code": "optimizer_not_found"}
        with (
            mock.patch.object(
                app,
                "resolve_material_status_optimizer",
            ) as resolver,
            mock.patch.object(
                app,
                "send_material_status_feishu_text",
                return_value={"message_id": "om_retry_fallback"},
            ) as sender,
            mock.patch.object(app, "material_status_record_audit"),
        ):
            delivered = app.process_material_status_event(
                event,
                outbox=self.store,
            )

        resolver.assert_not_called()
        self.assertEqual(delivered["delivery_kind"], "fallback")
        self.assertEqual(
            sender.call_args.args[3],
            "mst-MSE-0000000001-fallback",
        )

    def test_feishu_send_requires_code_message_id_and_uses_uuid(self):
        success = mock.Mock(status_code=200)
        success.json.return_value = {
            "code": 0,
            "data": {"message_id": "om_success"},
        }
        with (
            mock.patch.object(
                app,
                "get_feishu_tenant_access_token",
                return_value="tenant-test-token",
            ),
            mock.patch.object(app.requests, "post", return_value=success) as post,
        ):
            result = app.send_material_status_feishu_text(
                "chat_id",
                "oc_test",
                "test",
                "mst-MSE-0000000001-fallback",
            )

        self.assertEqual(result["message_id"], "om_success")
        self.assertEqual(
            post.call_args.kwargs["json"]["uuid"],
            "mst-MSE-0000000001-fallback",
        )

        for response_body in (
            {"data": {"message_id": "om_missing_code"}},
            {"code": 0, "data": {}},
        ):
            with self.subTest(response_body=response_body):
                invalid = mock.Mock(status_code=200)
                invalid.json.return_value = response_body
                with (
                    mock.patch.object(
                        app,
                        "get_feishu_tenant_access_token",
                        return_value="tenant-test-token",
                    ),
                    mock.patch.object(app.requests, "post", return_value=invalid),
                ):
                    with self.assertRaises(app.MaterialStatusDeliveryError):
                        app.send_material_status_feishu_text(
                            "chat_id",
                            "oc_test",
                            "test",
                            "mst-MSE-0000000001-fallback",
                        )

    def test_feishu_auth_failure_clears_cache_and_refreshes_once(self):
        stale = mock.Mock(status_code=401)
        stale.json.return_value = {
            "code": 99991663,
            "msg": "invalid tenant token",
        }
        success = mock.Mock(status_code=200)
        success.json.return_value = {
            "code": 0,
            "data": {"message_id": "om_after_refresh"},
        }
        with app.AUTH_CACHE_LOCK:
            original_cache = dict(app.FEISHU_TENANT_ACCESS_TOKEN_CACHE)
            app.FEISHU_TENANT_ACCESS_TOKEN_CACHE.update(
                {"token": "stale-token", "expires_at": 9999999999}
            )
        try:
            with (
                mock.patch.object(
                    app,
                    "get_feishu_tenant_access_token",
                    side_effect=["stale-token", "fresh-token"],
                ) as token_getter,
                mock.patch.object(
                    app.requests,
                    "post",
                    side_effect=[stale, success],
                ) as post,
            ):
                result = app.send_material_status_feishu_text(
                    "chat_id",
                    "oc_test",
                    "test",
                    "mst-MSE-0000000001-fallback",
                )
            self.assertEqual(result["message_id"], "om_after_refresh")
            self.assertEqual(token_getter.call_count, 2)
            self.assertEqual(post.call_count, 2)
            with app.AUTH_CACHE_LOCK:
                self.assertEqual(
                    app.FEISHU_TENANT_ACCESS_TOKEN_CACHE["token"],
                    "",
                )
        finally:
            with app.AUTH_CACHE_LOCK:
                app.FEISHU_TENANT_ACCESS_TOKEN_CACHE.clear()
                app.FEISHU_TENANT_ACCESS_TOKEN_CACHE.update(original_cache)


if __name__ == "__main__":
    unittest.main()
