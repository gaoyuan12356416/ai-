"""Offline worker crash, credential and local-execution regressions. No platform writes."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.client import CoordinatorClient, CoordinatorError
from features.pipeline_dispatch.credentials import seal_credentials, open_credentials
from features.pipeline_dispatch.worker import ReceiptOutbox, ExecutionContext, execute_claim, configuration, enroll_node
from features.pipeline_dispatch.executors import NodeExecutors
from features.tt_gpu.credentials import CredentialEnvelopeError

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
BINDING = {"job_id": 1, "generation": 2, "node_id": "third-node", "account_resource": "x:123", "operation_id": "operation-123", "request_sha256": "a" * 64}
JOB = {"id": 1, "generation": 2, "claim_token": "claim", "node_id": "third-node", "worker_id": "worker-1", "stage": "publish", "channel": "x", "payload": {"account_resource": "x:123"}}


class Client:
    def __init__(self):
        self.calls = []
        self.fail = False
    def post(self, route, payload):
        self.calls.append((route, payload))
        if self.fail:
            raise CoordinatorError()
        if route == "jobs/begin-operation":
            binding = {key: payload[key] for key in BINDING}
            return {"accepted": True, "credential_envelope": seal_credentials(KEY, {"access_token": "fake-token"}, binding)}
        return {"accepted": True}


class WorkerTests(unittest.TestCase):
    def test_envelope_roundtrip_and_clear(self):
        encrypted = seal_credentials(KEY, {"access_token": "private"}, BINDING, now=100)
        self.assertNotIn("private", encrypted)
        with open_credentials(encrypted, KEY, BINDING, now=101) as opened:
            reference = opened
            self.assertEqual(opened["access_token"], "private")
        self.assertEqual(reference, {})

    def test_binding_prevents_other_host_and_generation(self):
        encrypted = seal_credentials(KEY, {"access_token": "private"}, BINDING)
        for binding in ({**BINDING, "node_id": "wrong"}, {**BINDING, "generation": 3}):
            with self.assertRaises(CredentialEnvelopeError):
                with open_credentials(encrypted, KEY, binding):
                    self.fail("decrypted wrong binding")

    def test_expiry_only_checked_on_open(self):
        encrypted = seal_credentials(KEY, {"access_token": "private"}, BINDING, now=100)
        with self.assertRaises(CredentialEnvelopeError):
            with open_credentials(encrypted, KEY, BINDING, now=220):
                pass
        with open_credentials(encrypted, KEY, BINDING, now=219) as opened:
            self.assertEqual(opened["access_token"], "private")

    def test_refresh_credentials_are_never_enveloped(self):
        with self.assertRaises(CredentialEnvelopeError):
            seal_credentials(KEY, {"access_token": "x", "refresh_token": "y"}, BINDING)

    def test_receipt_survives_transport_failure_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "outbox.sqlite3"
            client = Client(); client.fail = True
            now = [100.0]
            outbox = ReceiptOutbox(path, clock=lambda: now[0])
            outbox.put("jobs/complete", {"receipt_id": "r1", "result": {"post_id": "123"}})
            self.assertFalse(outbox.flush(client))
            self.assertFalse(outbox.flush(client, only="r1"))
            self.assertEqual(len(client.calls), 1)
            now[0] += 15
            with self.assertRaises(CoordinatorError):
                outbox.flush(client, only="r1")
            client.fail = False
            now[0] += 30
            self.assertTrue(ReceiptOutbox(path, clock=lambda: now[0]).flush(client))
            before = len(client.calls)
            self.assertTrue(outbox.flush(client))
            self.assertEqual(len(client.calls), before)

    def test_receipt_identity_cannot_change(self):
        with tempfile.TemporaryDirectory() as temp:
            outbox = ReceiptOutbox(Path(temp) / "outbox")
            outbox.put("jobs/complete", {"result": 1}, "r1")
            with self.assertRaises(ValueError):
                outbox.put("jobs/complete", {"result": 2}, "r1")

    def test_pending_checkpoint_blocks_own_completion_but_not_other_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            outbox = ReceiptOutbox(Path(temp) / "outbox")
            outbox.put("jobs/checkpoint", {"job_id": 1, "generation": 1}, "a")
            outbox.put("jobs/complete", {"job_id": 1, "generation": 1}, "b")
            outbox.put("jobs/complete", {"job_id": 2, "generation": 1}, "c")
            calls = []
            class PartialClient:
                def post(self, route, payload):
                    calls.append((route, payload["job_id"]))
                    if payload["job_id"] == 1: raise CoordinatorError("canonical_reconciliation_pending", 409)
                    return {"accepted": True}
            self.assertFalse(outbox.flush(PartialClient()))
            self.assertEqual(calls, [("jobs/checkpoint", 1), ("jobs/complete", 2)])

    def test_durable_result_retries_only_callback(self):
        class Executor:
            calls = 0
            def execute(self, job, ctx):
                self.calls += 1
                return {"status": "published", "post_id": "123"}
        with tempfile.TemporaryDirectory() as temp:
            executor, client = Executor(), Client(); client.fail = True
            now = [100.0]
            outbox = ReceiptOutbox(Path(temp) / "outbox", clock=lambda: now[0])
            result = execute_claim(JOB, client, outbox, KEY, executor)
            self.assertEqual(result["result"]["post_id"], "123")
            client.fail = False
            now[0] += 15
            self.assertTrue(outbox.flush(client))
            self.assertEqual(executor.calls, 1)

    def test_write_failure_becomes_unknown_without_retry(self):
        class Executor:
            calls = 0
            def execute(self, job, ctx):
                self.calls += 1
                with ctx.operation("publish", {"body": "hello"}):
                    raise TimeoutError()
        with tempfile.TemporaryDirectory() as temp:
            executor, client = Executor(), Client()
            result = execute_claim(JOB, client, ReceiptOutbox(Path(temp) / "outbox"), KEY, executor)
            self.assertTrue(result["unknown_outcome"])
            self.assertEqual(executor.calls, 1)
            self.assertEqual([route for route, _ in client.calls].count("jobs/begin-operation"), 1)

    def test_heartbeat_is_independent_of_blocking_work(self):
        with tempfile.TemporaryDirectory() as temp:
            client = Client()
            with ExecutionContext(JOB, client, ReceiptOutbox(Path(temp) / "outbox"), KEY, heartbeat_seconds=.01):
                time.sleep(.05)
            self.assertGreaterEqual([route for route, _ in client.calls].count("jobs/heartbeat"), 2)

    def test_failed_heartbeat_prevents_next_api_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            client = Client(); client.fail = True
            with ExecutionContext(JOB, client, ReceiptOutbox(Path(temp) / "outbox"), KEY, heartbeat_seconds=.01) as ctx:
                time.sleep(.03)
                with self.assertRaises(Exception):
                    with ctx.operation("publish", {}):
                        self.fail("must not call external API")
            self.assertNotIn("jobs/begin-operation", [route for route, _ in client.calls])

    def test_third_node_config_has_no_server_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            config = configuration({"PIPELINE_NODE_ID": "new-worker-3", "PIPELINE_NODE_STATE_ROOT": temp, "PIPELINE_RENDER_BACKEND": "cpu"})
            self.assertEqual(config["node_id"], "new-worker-3")

    def test_enrollment_keeps_credentials_in_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "credentials.json"
            with patch("features.pipeline_dispatch.worker.hardware_inventory", return_value={"cpu_cores": 16}), patch.object(CoordinatorClient, "post", return_value={"node_token": "secret-node-token", "seal_key": KEY}) as post:
                result = enroll_node(destination, {"PIPELINE_NODE_ID": "server-3", "PIPELINE_ENROLLMENT_TOKEN": "x" * 32})
            self.assertNotIn("secret-node-token", json.dumps(result))
            self.assertEqual(json.loads(destination.read_text())["node_token"], "secret-node-token")
            self.assertEqual(post.call_args.args[0], "nodes/enroll")
            with self.assertRaises(ValueError):
                enroll_node(destination, {"PIPELINE_NODE_ID": "server-3", "PIPELINE_ENROLLMENT_TOKEN": "x" * 32})

    def test_plain_http_remote_is_rejected(self):
        with self.assertRaises(ValueError):
            CoordinatorClient("http://untrusted.example:18846", "x" * 32)

    def test_cpu_encoder_is_explicit_and_has_no_nvenc(self):
        from features.fb_gpu.prepare_worker import software_command
        command = software_command(["ffmpeg", "-i", "source", "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "output"])
        self.assertIn("libx264", command)
        self.assertNotIn("h264_nvenc", command)
        self.assertNotIn("-cq", command)
        self.assertEqual(command[command.index("-threads") + 1], "4")

    def test_only_configured_executors_advertised(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PIPELINE_EXECUTORS": "x_publish,fb_prepare"}):
            executor = NodeExecutors({"state_root": temp, "backend": "cpu"})
            self.assertEqual(executor.capabilities("prepare"), ["backend:cpu", "fb_prepare", "prepare:fb"])
            self.assertEqual(executor.capabilities("publish"), ["backend:cpu", "publish:x", "x_publish"])

    def test_upstream_valueerror_is_not_reclassified_as_bad_credential(self):
        encrypted = seal_credentials(KEY, {"access_token": "private"}, BINDING)
        with self.assertRaisesRegex(ValueError, "platform failure"):
            with open_credentials(encrypted, KEY, BINDING):
                raise ValueError("platform failure")


if __name__ == "__main__":
    unittest.main()
