#!/usr/bin/env python3
"""Contract and reliability tests for the independent TT prepare runner."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import tt_post_prepare_runner as runner


TOKEN = "t" * 48
CLAIM_TOKEN = "c" * 48


def config(**overrides):
    values = {
        "internal_url": runner.DEFAULT_INTERNAL_URL,
        "internal_token": TOKEN,
        "worker_id": runner.DEFAULT_WORKER_ID,
        "lease_seconds": 180,
        "renew_interval_seconds": 30,
        "internal_timeout": 60,
        "gpu_prepare_timeout": 9000,
        "process_timeout": 9300,
        "lock_path": runner.DEFAULT_LOCK_PATH,
    }
    values.update(overrides)
    return runner.PrepareRunnerConfig(**values)


class FakeClient:
    def __init__(self, claim=None, process_item=None, process_error=None):
        self.claim_result = claim
        self.process_item = process_item
        self.process_error = process_error
        self.claim_calls = []
        self.renew_calls = []
        self.process_calls = []

    def claim(self, *, worker_id, lease_seconds):
        self.claim_calls.append((worker_id, lease_seconds))
        return self.claim_result

    def renew(self, preparation_id, claim_token, *, lease_seconds):
        self.renew_calls.append(
            (preparation_id, claim_token, lease_seconds)
        )
        return {"item": {"id": preparation_id, "status": "preparing"}}

    def process(self, preparation_id, claim_token):
        self.process_calls.append((preparation_id, claim_token))
        if self.process_error is not None:
            raise self.process_error
        return {"item": dict(self.process_item)}


class ImmediateHeartbeat:
    instances = []

    def __init__(self, renew_fn, interval_seconds):
        self.renew_fn = renew_fn
        self.interval_seconds = interval_seconds
        self.closed = False
        self.renew_count = 0
        self.__class__.instances.append(self)

    def __enter__(self):
        self.renew_fn()
        self.renew_count += 1
        return self

    def close(self):
        self.closed = True

    def __exit__(self, *_args):
        self.close()

    def snapshot(self):
        return {
            "renew_count": self.renew_count,
            "renew_error_code": "",
        }


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit):
        return self._payload


class RecordingConnection:
    def __init__(self, response, timeout):
        self.response = response
        self.timeout = timeout
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class PrepareRunnerConfigTests(unittest.TestCase):
    def test_valid_config_is_loopback_and_process_timeout_covers_gpu(self):
        item = config()
        item.validate()
        rendered = repr(item)
        self.assertIn("internal_token=<redacted>", rendered)
        self.assertNotIn(TOKEN, rendered)

    def test_non_loopback_url_is_rejected(self):
        with self.assertRaises(runner.PrepareRunnerError):
            config(internal_url="http://43.166.178.132:18829").validate()

    def test_lease_must_cover_three_renew_intervals(self):
        with self.assertRaises(runner.PrepareRunnerError):
            config(
                lease_seconds=60,
                renew_interval_seconds=30,
            ).validate()

    def test_env_rejects_lease_above_sidecar_contract(self):
        with self.assertRaises(runner.PrepareRunnerError):
            runner.PrepareRunnerConfig.from_env(
                {
                    "TT_POST_INTERNAL_TOKEN": TOKEN,
                    "TT_POST_PREPARE_LEASE_SECONDS": "601",
                }
            )

    def test_process_timeout_must_exceed_gpu_timeout(self):
        with self.assertRaises(runner.PrepareRunnerError):
            config(process_timeout=9059).validate()


class PrepareRunnerTickTests(unittest.TestCase):
    def setUp(self):
        ImmediateHeartbeat.instances = []

    def test_idle_tick_does_not_process_or_renew(self):
        client = FakeClient(claim=None)
        result = runner.execute_prepare_tick(
            config(),
            client=client,
            heartbeat_factory=ImmediateHeartbeat,
        )
        self.assertEqual("idle", result["status"])
        self.assertEqual(1, len(client.claim_calls))
        self.assertEqual([], client.process_calls)
        self.assertEqual([], client.renew_calls)

    def test_one_tick_claims_renews_and_processes_exactly_one(self):
        client = FakeClient(
            claim={
                "item": {
                    "id": 17,
                    "material_id": "5391678",
                    "status": "preparing",
                },
                "claim_token": CLAIM_TOKEN,
            },
            process_item={
                "id": 17,
                "material_id": "5391678",
                "account_id": "2001",
                "preparation_status": "ready",
                "attempt_count": 1,
            },
        )
        result = runner.execute_prepare_tick(
            config(),
            client=client,
            heartbeat_factory=ImmediateHeartbeat,
        )
        self.assertEqual("processed", result["status"])
        self.assertEqual(1, result["claimed_count"])
        self.assertEqual(1, result["processed_count"])
        self.assertEqual([(17, CLAIM_TOKEN)], client.process_calls)
        self.assertEqual([(17, CLAIM_TOKEN, 180)], client.renew_calls)
        self.assertEqual("ready", result["item"]["preparation_status"])
        self.assertNotIn(CLAIM_TOKEN, json.dumps(result))
        self.assertTrue(ImmediateHeartbeat.instances[0].closed)

    def test_heartbeat_is_stopped_when_process_fails(self):
        client = FakeClient(
            claim={
                "item": {"id": 17, "status": "preparing"},
                "claim_token": CLAIM_TOKEN,
            },
            process_error=runner.PrepareRunnerError(
                "temporary_gpu_failure",
                "retry later",
                503,
            ),
        )
        with self.assertRaises(runner.PrepareRunnerError):
            runner.execute_prepare_tick(
                config(),
                client=client,
                heartbeat_factory=ImmediateHeartbeat,
            )
        self.assertTrue(ImmediateHeartbeat.instances[0].closed)


class LeaseHeartbeatTests(unittest.TestCase):
    def test_real_heartbeat_renews_until_closed(self):
        renewed = threading.Event()
        calls = []

        def renew():
            calls.append(time.monotonic())
            renewed.set()

        heartbeat = runner.LeaseHeartbeat(renew, 0.01)
        with heartbeat:
            self.assertTrue(renewed.wait(0.5))
        count_after_close = len(calls)
        time.sleep(0.03)
        self.assertGreaterEqual(count_after_close, 1)
        self.assertEqual(count_after_close, len(calls))
        self.assertEqual(count_after_close, heartbeat.snapshot()["renew_count"])

    def test_transient_renew_error_is_redacted_and_later_success_clears_it(self):
        calls = []
        success = threading.Event()

        def renew():
            calls.append(1)
            if len(calls) == 1:
                raise runner.PrepareRunnerError(
                    "renew_failed",
                    "Authorization: secret-value",
                    502,
                )
            success.set()

        heartbeat = runner.LeaseHeartbeat(renew, 0.01)
        with heartbeat:
            self.assertTrue(success.wait(0.5))
        self.assertEqual("", heartbeat.snapshot()["renew_error_code"])


class PrepareSidecarClientTests(unittest.TestCase):
    def test_claim_contract_uses_expected_path_and_safe_payload(self):
        connections = []

        def factory(_host, _port, timeout):
            connection = RecordingConnection(
                FakeResponse(
                    200,
                    {
                        "item": {"id": 8, "status": "preparing"},
                        "claim_token": CLAIM_TOKEN,
                    },
                ),
                timeout,
            )
            connections.append(connection)
            return connection

        client = runner.PrepareSidecarClient(
            runner.DEFAULT_INTERNAL_URL,
            TOKEN,
            timeout=60,
            process_timeout=9300,
            connection_factory=factory,
        )
        result = client.claim(worker_id="worker-1", lease_seconds=180)
        self.assertEqual(8, result["item"]["id"])
        method, path, body, headers = connections[0].requests[0]
        self.assertEqual("POST", method)
        self.assertEqual(
            "/internal/tt-posts/preparations/claim",
            path,
        )
        self.assertEqual(
            {"lease_seconds": 180, "worker_id": "worker-1"},
            json.loads(body),
        )
        self.assertEqual("Bearer " + TOKEN, headers["Authorization"])
        self.assertTrue(connections[0].closed)

    def test_process_uses_long_timeout_and_validates_identity(self):
        connections = []

        def factory(_host, _port, timeout):
            connection = RecordingConnection(
                FakeResponse(
                    200,
                    {"item": {"id": 9, "status": "ready"}},
                ),
                timeout,
            )
            connections.append(connection)
            return connection

        client = runner.PrepareSidecarClient(
            runner.DEFAULT_INTERNAL_URL,
            TOKEN,
            timeout=60,
            process_timeout=9300,
            connection_factory=factory,
        )
        client.process(9, CLAIM_TOKEN)
        self.assertEqual(9300, connections[0].timeout)
        self.assertEqual(
            "/internal/tt-posts/preparations/9/process",
            connections[0].requests[0][1],
        )

    def test_idle_claim_must_not_return_a_token(self):
        def factory(_host, _port, timeout):
            return RecordingConnection(
                FakeResponse(
                    200,
                    {"item": None, "claim_token": CLAIM_TOKEN},
                ),
                timeout,
            )

        client = runner.PrepareSidecarClient(
            runner.DEFAULT_INTERNAL_URL,
            TOKEN,
            timeout=60,
            process_timeout=9300,
            connection_factory=factory,
        )
        with self.assertRaises(runner.PrepareRunnerError):
            client.claim(worker_id="worker-1", lease_seconds=180)


class PrepareDeployContractTests(unittest.TestCase):
    def test_service_is_isolated_and_timeout_exceeds_gpu_prepare(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "deploy" / "tt-post-prepare.service").read_text(
            encoding="utf-8"
        )
        timer = (root / "deploy" / "tt-post-prepare.timer").read_text(
            encoding="utf-8"
        )
        env = (root / "deploy" / "tt-post.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("tt_post_prepare_runner.py", service)
        self.assertNotIn("tt_post_runner.py\n", service)
        self.assertIn("TimeoutStartSec=9600s", service)
        self.assertIn("Unit=tt-post-prepare.service", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(
            "TT_POST_PREPARE_RUNNER_LOCK_PATH=/run/tt-post/prepare-runner.lock",
            env,
        )
        self.assertIn("TT_POST_PREPARE_PROCESS_TIMEOUT=9300", env)


if __name__ == "__main__":
    unittest.main()
