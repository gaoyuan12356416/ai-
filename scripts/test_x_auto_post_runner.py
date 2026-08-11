#!/usr/bin/env python3
"""Offline orchestration contracts for the X auto-template runner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.x_auto_post_runner import RunnerConfig, RunnerError, run_once  # noqa: E402


class FakeSidecarClient:
    calls = []
    tick_fails = False

    def __init__(self, config):
        config.validate()

    def post(self, path, payload, timeout):
        self.__class__.calls.append((path, dict(payload), timeout))
        if path.endswith("/tick") and self.__class__.tick_fails:
            raise RunnerError("source_unavailable", "tick failed", 503)
        if path.endswith("/tick"):
            return {"ok": True, "created_runs": []}
        return {"ok": True, "claimed": False}


class XAutoPostRunnerTests(unittest.TestCase):
    def setUp(self):
        FakeSidecarClient.calls = []
        FakeSidecarClient.tick_fails = False
        self.config = RunnerConfig(
            internal_url="http://127.0.0.1:18833",
            internal_token="i" * 48,
            worker_id="x-auto-runner-test",
            timeout=30,
            execute_timeout=10200,
            publish_timeout=9000,
            lease_seconds=10800,
            lock_path="/run/x-post-daily/runner.lock",
            scheduler_lock_path="/run/x-auto-post/scheduler.lock",
            worker_count=2,
            max_tasks_per_worker=1,
        )

    @mock.patch("scripts.x_auto_post_runner.SidecarClient", FakeSidecarClient)
    def test_tick_failure_does_not_skip_reconciliation_workers(self):
        FakeSidecarClient.tick_fails = True
        with self.assertRaises(RunnerError) as caught:
            run_once(self.config)
        self.assertEqual(caught.exception.code, "x_auto_post_runner_partial_failure")
        execute = [call for call in FakeSidecarClient.calls if call[0].endswith("execute-next")]
        self.assertEqual(len(execute), 2)

    @mock.patch("scripts.x_auto_post_runner.SidecarClient", FakeSidecarClient)
    def test_each_worker_processes_at_most_one_task(self):
        result = run_once(self.config)
        self.assertTrue(result["ok"])
        self.assertEqual([len(items) for items in result["execute"]], [1, 1])

    def test_defaults_use_new_sidecar_and_existing_x_publish_lock(self):
        config = RunnerConfig.from_env(
            {"X_AUTO_POST_INTERNAL_TOKEN": "a" * 48}
        )
        self.assertEqual(config.internal_url, "http://127.0.0.1:18833")
        self.assertEqual(config.lock_path, "/run/x-post-daily/runner.lock")
        self.assertEqual(config.scheduler_lock_path, "/run/x-auto-post/scheduler.lock")
        self.assertEqual(config.worker_count, 1)
        config.validate()

    def test_old_tt_port_and_placeholder_bearer_are_rejected(self):
        for changes in (
            {"internal_url": "http://127.0.0.1:18831"},
            {
                "internal_token": (
                    "replace-with-unique-random-token-at-least-32-characters"
                )
            },
            {"max_tasks_per_worker": 2},
            {"execute_timeout": 9200},
            {"lease_seconds": 10400},
        ):
            with self.subTest(changes=changes):
                invalid = RunnerConfig(**{**self.config.__dict__, **changes})
                with self.assertRaises(RunnerError):
                    invalid.validate()


if __name__ == "__main__":
    unittest.main()
