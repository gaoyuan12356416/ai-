"""Independent fault injection: temporary SQLite, mocks and loopback only."""

import ast
from datetime import datetime, timedelta, timezone
from email.message import Message
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from features.material_replication_broadcast import delivery, service
from scripts import deploy_material_replication as deploy

TOKEN = "independent-safety-token-" + "q" * 40


def example():
    return {"event_type": "replication_started", "editor_username": "editor_qa", "items": [{
        "resource_id": "QA-01", "resource_name": "测试资源",
        "original_material_id": "QA-MAT-01", "original_material_name": "原始素材",
    }]}


def response(status, body):
    result = mock.Mock(status_code=status)
    result.json.return_value = body
    return result


class DeliveryError(RuntimeError):
    def __init__(self, uncertain=False, retryable=False):
        self.code, self.uncertain, self.retryable = "qa_send_failure", uncertain, retryable
        super().__init__("simulated delivery failure")


class SafetyFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="replication-independent-qa-")
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 8, 27, 8, tzinfo=timezone.utc)
        self.store = service.ReplicationOutbox(Path(self.temp.name) / "outbox.sqlite3", clock=lambda: self.now)
        self.editor = mock.Mock(return_value={"matched": True, "email": "qa@example.invalid"})
        self.person = mock.Mock(return_value={"matched": True, "open_id": "ou_qa_editor"})
        self.sender = mock.Mock(return_value={"message_id": "om_qa_confirmed"})
        self.runtime = delivery.ReplicationRuntime(
            self.store.db_path, (TOKEN,), "oc_qa_fallback", self.editor,
            self.person, self.sender, store=self.store,
        )
        self.addCleanup(self.runtime.stop)
        self.sequence = 0
        original_connect = socket.socket.connect

        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
                raise AssertionError("external network forbidden in independent QA")
            return original_connect(sock, address)

        guard = mock.patch.object(socket.socket, "connect", loopback_only)
        guard.start()
        self.addCleanup(guard.stop)

    def enqueue(self, max_attempts=5):
        self.sequence += 1
        return self.store.enqueue("qa-safety-%08d" % self.sequence, example(), max_attempts=max_attempts)

    def step(self):
        row = self.store.claim_next()
        self.assertIsNotNone(row)
        self.runtime._last_send = 0.0
        return self.runtime.process(row)

    def handler(self, body=None, content_length=None, key="qa-http-safety-0001"):
        raw = json.dumps(example() if body is None else body, ensure_ascii=False).encode("utf-8")
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + TOKEN
        headers["Idempotency-Key"] = key
        headers["Content-Length"] = str(len(raw)) if content_length is None else content_length
        return SimpleNamespace(headers=headers, rfile=io.BytesIO(raw), close_connection=False,
                               client_address=("127.0.0.1", 12345))


class SenderSafetyTests(SafetyFixture):
    def test_auth_retry_preserves_earlier_uncertainty_metadata(self):
        replies = [response(503, {"code": 99991663}), response(400, {"code": 230001})]
        with (
            mock.patch.object(app, "get_feishu_tenant_access_token", side_effect=["qa-old", "qa-new"]),
            mock.patch.object(app.requests, "post", side_effect=replies) as post,
            mock.patch.object(app, "invalidate_material_status_feishu_token"),
        ):
            with self.assertRaises(app.MaterialStatusDeliveryError) as caught:
                app.send_material_status_feishu_text("open_id", "ou_qa_editor", "冻结正文", "mrb-qa-private")
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable, "legacy retryability must not change")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"], post.call_args_list[1].kwargs["json"])

    def test_auth_retry_unknown_must_not_switch_runtime_to_fallback(self):
        self.enqueue(max_attempts=1)
        self.runtime.send_text = app.send_material_status_feishu_text
        replies = [response(503, {"code": 99991663}), response(400, {"code": 230001})]
        with (
            mock.patch.object(app, "get_feishu_tenant_access_token", side_effect=["qa-old", "qa-new"]),
            mock.patch.object(app.requests, "post", side_effect=replies) as post,
            mock.patch.object(app, "invalidate_material_status_feishu_token"),
        ):
            ended = self.step()
        self.assertEqual((ended["status"], ended["phase"]), ("delivery_unknown", "private"))
        self.assertEqual(ended["fallback_text"], "")
        self.assertEqual(post.call_count, 2)

    def test_http500_with_success_code_is_not_confirmed(self):
        self.enqueue(max_attempts=1)
        self.runtime.send_text = app.send_material_status_feishu_text
        with (
            mock.patch.object(app, "get_feishu_tenant_access_token", return_value="qa-tenant"),
            mock.patch.object(app.requests, "post", return_value=response(
                500, {"code": 0, "data": {"message_id": "om_ambiguous"}},
            )) as post,
        ):
            ended = self.step()
        self.assertEqual((ended["status"], ended["phase"], ended["delivery_kind"]), ("delivery_unknown", "private", ""))
        self.assertEqual(post.call_count, 1)

    def test_malformed_success_shapes_never_trigger_fallback(self):
        for body in ({}, {"code": 0}, {"code": 0, "data": ["invalid"]}):
            with self.subTest(body=body):
                self.enqueue(max_attempts=1)
                self.runtime.send_text = app.send_material_status_feishu_text
                with (
                    mock.patch.object(app, "get_feishu_tenant_access_token", return_value="qa-tenant"),
                    mock.patch.object(app.requests, "post", return_value=response(200, body)),
                ):
                    ended = self.step()
                self.assertEqual((ended["status"], ended["phase"]), ("delivery_unknown", "private"))

    def test_invalid_json200_is_unknown_end_to_end(self):
        self.enqueue(max_attempts=1)
        malformed = response(200, None)
        malformed.json.side_effect = ValueError("invalid JSON")
        self.runtime.send_text = app.send_material_status_feishu_text
        with (
            mock.patch.object(app, "get_feishu_tenant_access_token", return_value="qa-tenant"),
            mock.patch.object(app.requests, "post", return_value=malformed),
        ):
            ended = self.step()
        self.assertEqual((ended["status"], ended["phase"]), ("delivery_unknown", "private"))


class RuntimeSafetyTests(SafetyFixture):
    def test_worker_recovers_post_ack_storage_failure_without_new_destination(self):
        self.store.clock = lambda: datetime.now(timezone.utc)
        original = self.enqueue()
        completed = threading.Event()
        acknowledged = self.store.delivered
        attempts = []

        def fail_first_ack(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("simulated post-ack storage failure")
            return acknowledged(*args, **kwargs)

        self.runtime.audit = lambda row: completed.set() if row["status"] == "delivered" else None
        with mock.patch.object(self.store, "delivered", side_effect=fail_first_ack):
            worker = self.runtime.start()
            self.assertIs(self.runtime.start(), worker)
            try:
                self.assertTrue(completed.wait(5), "mock worker did not recover in time")
            finally:
                self.runtime.stop()
        ended = self.store.get(original["id"])
        self.assertEqual((ended["status"], ended["delivery_kind"]), ("delivered", "private"))
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.sender.call_args_list[0].args, self.sender.call_args_list[1].args)

    def test_window_at_3299_3300_3301_seconds(self):
        for elapsed in (3299, 3300, 3301):
            with self.subTest(elapsed=elapsed):
                original = self.enqueue(max_attempts=2)
                self.sender.reset_mock()
                self.sender.side_effect = DeliveryError(uncertain=True, retryable=True)
                first = self.step()
                self.now += timedelta(seconds=elapsed)
                claimed = self.store.claim_next()
                if elapsed < 3300:
                    self.assertIsNotNone(claimed)
                    self.runtime.process(claimed)
                else:
                    self.assertIsNone(claimed)
                ended = self.store.get(original["id"])
                self.assertEqual(ended["status"], "delivery_unknown")
                self.assertEqual(ended["first_uncertain_at"], first["first_uncertain_at"])
                self.assertEqual(self.sender.call_count, 2 if elapsed < 3300 else 1)

    def test_window_rechecked_after_claim_before_send(self):
        original = self.enqueue()
        self.sender.side_effect = DeliveryError(uncertain=True, retryable=True)
        self.step()
        self.now += timedelta(seconds=3299)
        claimed = self.store.claim_next()
        self.now += timedelta(seconds=1)
        with self.assertRaises(service.ReplicationError) as caught:
            self.runtime.process(claimed)
        self.assertEqual(caught.exception.code, "delivery_window_expired")
        self.assertEqual(self.store.get(original["id"])["status"], "delivery_unknown")
        self.assertEqual(self.sender.call_count, 1)

    def test_crash_between_persisted_marker_and_post(self):
        original = self.enqueue()
        row = self.store.claim_next()
        row = self.store.freeze_target(row["id"], row["lease_id"], "ou_qa_frozen")
        marked = self.store.begin_send(row["id"], row["lease_id"])
        self.sender.assert_not_called()
        self.now += timedelta(seconds=301)
        self.store = service.ReplicationOutbox(self.store.db_path, clock=lambda: self.now)
        self.runtime._store = self.store
        self.editor.side_effect = AssertionError("frozen editor must not be remapped")
        self.assertEqual(self.step()["status"], "delivered")
        self.assertEqual(self.sender.call_args.args, (
            "open_id", "ou_qa_frozen", marked["message_text"], marked["message_uuid"],
        ))
        self.assertEqual(self.store.get(original["id"])["first_uncertain_at"], marked["first_uncertain_at"])

    def test_expired_and_replaced_lease_never_reaches_sender(self):
        self.enqueue()
        stale = self.store.claim_next(lease_seconds=5)
        stale = self.store.freeze_target(stale["id"], stale["lease_id"], "ou_qa_frozen")
        self.now += timedelta(seconds=5)
        with self.assertRaises(service.ReplicationError):
            self.runtime.process(stale)
        fresh = self.store.claim_next()
        with self.assertRaises(service.ReplicationError):
            self.runtime.process(stale)
        self.sender.assert_not_called()
        self.assertEqual(self.runtime.process(fresh)["status"], "delivered")

    def test_private_exhaustion_grants_full_fallback_budget(self):
        self.enqueue()
        self.sender.side_effect = DeliveryError(retryable=True)
        for _ in range(5):
            ended = self.step()
            self.now += timedelta(seconds=200)
        self.assertEqual((ended["phase"], ended["status"], ended["attempt_count"]), ("fallback", "queued", 0))
        for _ in range(4):
            self.assertEqual(self.step()["status"], "retry")
            self.now += timedelta(seconds=200)
        self.sender.side_effect = None
        done = self.step()
        self.assertEqual((done["status"], done["delivery_kind"], done["attempt_count"]), ("delivered", "fallback", 5))
        self.assertEqual(self.sender.call_count, 10)

    def test_actual_requests_wire_encoding_matches_estimate(self):
        text, target, key = "中文 🚀 \\\" quoted\n换行", "ou_qa_editor", "mrb-qa-private"
        prepared = app.requests.Request("POST", "https://example.invalid/never-send", json={
            "receive_id": target, "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False), "uuid": key,
        }).prepare()
        self.assertEqual(len(prepared.body), service._serialized_request_size(text, target, key))


class HTTPSafetyTests(SafetyFixture):
    def test_enormous_numeric_content_length_is_structured_400(self):
        reply = mock.Mock()
        delivery.handle_request(self.handler(content_length="9" * 5000), self.runtime, reply)
        self.assertEqual(reply.call_args.args[1], 400)
        self.assertEqual(reply.call_args.args[2]["code"], "invalid_request")
        self.assertIsNone(self.store.get(1))

    def test_duplicate_http_does_not_rerender(self):
        reply = mock.Mock()
        with mock.patch.object(self.runtime, "ready", return_value=True):
            delivery.handle_request(self.handler(), self.runtime, reply)
            first = reply.call_args.args[2]
            with mock.patch.object(service, "format_private_message", side_effect=AssertionError("no rerender")):
                delivery.handle_request(self.handler(), self.runtime, reply)
        self.assertEqual(reply.call_args.args[1], 202)
        self.assertTrue(reply.call_args.args[2]["duplicate"])
        self.assertEqual(reply.call_args.args[2]["batch_id"], first["batch_id"])
        self.sender.assert_not_called()

    def test_empty_editor_accepted_then_existing_fallback(self):
        body = example()
        body["editor_username"] = ""
        reply = mock.Mock()
        self.editor.return_value = {"matched": False, "code": "optimizer_name_missing"}
        with mock.patch.object(self.runtime, "ready", return_value=True):
            delivery.handle_request(self.handler(body), self.runtime, reply)
        self.assertEqual(reply.call_args.args[1], 202)
        self.assertEqual(self.step()["phase"], "fallback")
        self.assertEqual(self.step()["delivery_kind"], "fallback")
        self.assertEqual(self.sender.call_args.args[:2], ("chat_id", "oc_qa_fallback"))

    def test_inbound_32768_accepted_32769_rejected(self):
        raw = json.dumps(example(), ensure_ascii=False).encode("utf-8")
        reply = mock.Mock()
        with mock.patch.object(self.runtime, "ready", return_value=True):
            handler = self.handler(content_length="32768", key="qa-body-limit-0001")
            handler.rfile = io.BytesIO(raw + b" " * (32768 - len(raw)))
            delivery.handle_request(handler, self.runtime, reply)
            self.assertEqual(reply.call_args.args[1], 202)
            delivery.handle_request(self.handler(content_length="32769", key="qa-body-limit-0002"), self.runtime, reply)
            self.assertEqual(reply.call_args.args[1], 413)
        self.assertIsNone(self.store.get(2))


class DeploymentSafetyTests(SafetyFixture):
    def rollback_fixture(self, partial=False):
        root = Path(self.temp.name) / "deploy-fixture"
        live, data = root / "live", root / "data"
        backup = data / "backups" / "qa-before"
        live.mkdir(parents=True)
        backup.mkdir(parents=True)
        original, deployed = b"original-app", b"deployed-app"
        (live / "app.py").write_bytes(original if partial else deployed)
        (backup / "app.py").write_bytes(original)
        paths = {"LIVE": live, "DATA": data, "ENV": root / "test.env",
                 "DROPIN": root / "dropin.conf", "NGINX": root / "nginx.conf"}
        for name in ("ENV", "DROPIN", "NGINX"):
            paths[name].write_text("fake-" + name, encoding="utf-8")
        manifest = {
            "deployed_hashes": {"app.py": hashlib.sha256(deployed).hexdigest()},
            "original_app_sha256": hashlib.sha256(original).hexdigest(),
            "backup_hashes": {"app.py": hashlib.sha256(original).hexdigest()},
            "configuration_hashes": {str(paths[name]): deploy.digest(paths[name])
                                     for name in ("ENV", "DROPIN", "NGINX")},
        }
        (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return backup, paths

    def test_partial_deploy_with_original_app_can_rollback(self):
        backup, paths = self.rollback_fixture(partial=True)
        with (
            mock.patch.multiple(deploy, **paths), mock.patch.object(deploy, "storage_guard"),
            mock.patch.object(deploy, "stats", return_value={}),
            mock.patch.object(deploy, "command", return_value="") as command,
            mock.patch("builtins.print"),
        ):
            deploy.rollback(backup)
        self.assertEqual((paths["LIVE"] / "app.py").read_bytes(), b"original-app")
        self.assertTrue(paths["ENV"].exists())
        self.assertFalse(paths["DROPIN"].exists())
        self.assertTrue((backup / "withdrawn-dropin.conf").exists())
        self.assertIn(mock.call(["systemctl", "stop", deploy.UNIT]), command.call_args_list)

    def test_rollback_refuses_active_work_before_service_stop(self):
        backup, paths = self.rollback_fixture()
        with (
            mock.patch.multiple(deploy, **paths), mock.patch.object(deploy, "storage_guard"),
            mock.patch.object(deploy, "stats", return_value={"material_status_broadcast_outbox": {"processing": 1}}),
            mock.patch.object(deploy, "command", return_value="") as command,
        ):
            with self.assertRaisesRegex(RuntimeError, "pending broadcast"):
                deploy.rollback(backup)
        command.assert_not_called()
        self.assertEqual((paths["LIVE"] / "app.py").read_bytes(), b"deployed-app")

    def test_rollback_refuses_later_configuration_drift(self):
        backup, paths = self.rollback_fixture()
        paths["NGINX"].write_text("later-config", encoding="utf-8")
        with (
            mock.patch.multiple(deploy, **paths), mock.patch.object(deploy, "storage_guard"),
            mock.patch.object(deploy, "command", return_value="") as command,
        ):
            with self.assertRaisesRegex(RuntimeError, "newer feature configuration"):
                deploy.rollback(backup)
        command.assert_not_called()
        self.assertEqual(paths["NGINX"].read_text(), "later-config")

    def verification_fixture(self):
        backup = Path(self.temp.name) / "verify-backup"
        backup.mkdir()
        live = Path(self.temp.name) / "live"
        legacy = live / "features/material_status_broadcast/service.py"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy-placeholder")
        (backup / "manifest.json").write_text(json.dumps({
            "deployed_hashes": {}, "legacy_sha256": deploy.digest(legacy),
        }), encoding="utf-8")
        env = Path(self.temp.name) / "verify.env"
        env.write_text("MATERIAL_REPLICATION_WEBHOOK_TOKENS=" + TOKEN + "\n", encoding="utf-8")
        return backup, live, env

    def test_verification_submits_only_invalid_batches(self):
        backup, live, env = self.verification_fixture()
        calls = []

        def rejected(base, path, expected, token="", body=b"{}"):
            calls.append((path, expected, token, body))
            return {"status": expected, "code": "mock_rejection"}

        with (
            mock.patch.object(deploy, "LIVE", live), mock.patch.object(deploy, "ENV", env),
            mock.patch.object(deploy, "probe", side_effect=rejected),
            mock.patch.object(deploy, "stats", return_value={"material_replication_broadcast_outbox": {}}),
            mock.patch.object(deploy, "command", side_effect=lambda args: "active" if "is-active" in args else "123"),
            mock.patch("builtins.print"),
        ):
            deploy.verify(backup)
        self.assertEqual(len(calls), 8)
        for path, expected, token, body in calls:
            self.assertIn(expected, (401, 413, 422))
            if expected == 401:
                self.assertFalse(token)
            elif expected == 413:
                self.assertGreater(len(body), 32768)
            else:
                self.assertEqual(path, delivery.ENDPOINT)
                with self.assertRaises(service.ReplicationError):
                    service.normalize_payload(json.loads(body))

    def test_verification_missing_outbox_fails_without_writing_proof(self):
        backup, live, env = self.verification_fixture()
        for snapshot in ({}, {"material_status_broadcast_outbox": {}}):
            with (
                self.subTest(snapshot=snapshot),
                mock.patch.object(deploy, "LIVE", live), mock.patch.object(deploy, "ENV", env),
                mock.patch.object(deploy, "probe", return_value={"status": 401}),
                mock.patch.object(deploy, "stats", return_value=snapshot),
                mock.patch.object(deploy, "command", return_value="active"),
                mock.patch.object(deploy, "atomic_bytes") as write_proof,
                mock.patch("builtins.print") as print_result,
            ):
                with self.assertRaisesRegex(RuntimeError, "new outbox was not initialized"):
                    deploy.verify(backup)
                write_proof.assert_not_called()
                print_result.assert_not_called()
                self.assertFalse((backup / "verification.json").exists())

    def test_token_bearing_probe_never_follows_redirect(self):
        captured = []

        class RedirectStub(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", "/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                captured.append(self.headers.get("Authorization"))
                body = b'{"code":"invalid_payload"}'
                self.send_response(422)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectStub)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with self.assertRaises(RuntimeError):
                deploy.probe("http://127.0.0.1:%d" % server.server_address[1], "/probe", 422, TOKEN)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(captured, [])

    def test_sources_parse_with_python39_grammar(self):
        relatives = ["app.py", "features/material_replication_broadcast/service.py",
                     "features/material_replication_broadcast/delivery.py", "scripts/deploy_material_replication.py"]
        relatives.extend(name.replace(".", "/") + ".py" for name in FULL_REGRESSION_MODULES)
        for relative in relatives:
            with self.subTest(path=relative):
                ast.parse((ROOT / relative).read_text(encoding="utf-8"), feature_version=(3, 9))


class DocumentationSafetyTests(SafetyFixture):
    def test_documented_examples_render_exactly_as_runtime(self):
        docs = ROOT / "doc/050.material-replication-broadcast"
        specification = json.loads((docs / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(specification["openapi"], "3.0.3")
        self.assertIn(delivery.ENDPOINT, specification["paths"])
        markdown = (docs / "api-doc.md").read_text(encoding="utf-8")
        messages = [block for block in re.findall(r"```text\n(.*?)\n```", markdown, re.S)
                    if block.startswith("【")]
        self.assertEqual(len(messages), 2)
        for index, name in enumerate(("started", "failed"), 1):
            payload = json.loads((docs / "examples" / (name + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(service.normalize_payload(payload), payload)
            self.assertEqual(service.format_private_message(payload, index), messages[index - 1])


FULL_REGRESSION_MODULES = (
    "scripts.test_material_replication_broadcast",
    "scripts.test_material_replication_webhook_app",
    "scripts.test_material_replication_safety",
    "scripts.test_material_status_broadcast",
    "scripts.test_material_status_webhook_app",
    "scripts.test_x_accounts_app_contract",
    "scripts.test_tt_posts_app_contract",
    "scripts.test_tt_auto_publish_app_contract",
    "scripts.test_x_auto_publish_app_contract",
    "scripts.test_fb_auto_app_contract",
    "scripts.test_drama_synthesis_upgrade",
)


def run_full_regression():
    """Run the reviewed impact set with a process-wide external-network fence."""
    blocked = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check_host(host):
        if host not in (None, "127.0.0.1", "::1", "localhost", b"127.0.0.1", b"::1", b"localhost"):
            blocked.append("external network attempt")
            raise AssertionError("external network forbidden in full regression")

    def guarded_connect(sock, address):
        check_host(address[0] if isinstance(address, tuple) else address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        check_host(address[0] if isinstance(address, tuple) else address)
        return original_connect_ex(sock, address)

    def guarded_dns(host, *args, **kwargs):
        check_host(host)
        return original_getaddrinfo(host, *args, **kwargs)

    with (
        mock.patch.object(socket.socket, "connect", guarded_connect),
        mock.patch.object(socket.socket, "connect_ex", guarded_connect_ex),
        mock.patch.object(socket, "getaddrinfo", guarded_dns),
    ):
        suite = unittest.defaultTestLoader.loadTestsFromNames(FULL_REGRESSION_MODULES)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    summary = {"tests": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped),
               "external_network_attempts": len(blocked), "modules": list(FULL_REGRESSION_MODULES)}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.wasSuccessful() and not blocked else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--full-regression"]:
        sys.exit(run_full_regression())
    unittest.main()
