#!/usr/bin/env python3
"""Offline orchestration tests for the independent TT auto-post runner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tt_auto_post_runner import (  # noqa: E402
    RunnerConfig,
    RunnerError,
    run_once,
)


class FakeSidecarClient:
    calls = []
    tick_fails = False

    def __init__(self, config):
        config.validate()

    def post(self, path, payload, timeout):
        self.__class__.calls.append((path, dict(payload), timeout))
        if path.endswith("/tick"):
            if self.__class__.tick_fails:
                raise RunnerError("blacklist_unavailable", "tick failed", 503)
            return {"ok": True, "created_runs": []}
        return {"ok": True, "claimed": False}


class TTAutoPostRunnerTests(unittest.TestCase):
    def setUp(self):
        FakeSidecarClient.calls = []
        FakeSidecarClient.tick_fails = False
        self.config = RunnerConfig(
            internal_url="http://127.0.0.1:18831",
            internal_token="i" * 48,
            worker_id="runner-test",
            timeout=30,
            execute_timeout=120,
            lock_path=str(Path.cwd() / "runner-test.lock"),
            scheduler_lock_path=str(Path.cwd() / "scheduler-test.lock"),
            worker_count=3,
            max_tasks_per_worker=1,
            publish_poll_seconds=2,
        )

    @mock.patch(
        "scripts.tt_auto_post_runner.SidecarClient", FakeSidecarClient
    )
    def test_tick_failure_still_attempts_all_execute_workers(self):
        FakeSidecarClient.tick_fails = True
        with self.assertRaises(RunnerError) as caught:
            run_once(self.config)
        self.assertEqual(caught.exception.code, "tt_auto_post_runner_partial_failure")
        execute_calls = [
            call for call in FakeSidecarClient.calls if call[0].endswith("execute-next")
        ]
        self.assertEqual(
            len(
                [
                    call
                    for call in execute_calls
                    if "prepare" in call[1]["worker_id"]
                ]
            ),
            2,
        )
        self.assertGreaterEqual(
            len(
                [
                    call
                    for call in execute_calls
                    if call[1]["worker_id"].endswith("-publish")
                ]
            ),
            1,
        )
        self.assertEqual(
            {call[1]["worker_id"] for call in execute_calls},
            {
                "runner-test-prepare-1",
                "runner-test-prepare-2",
                "runner-test-publish",
            },
        )
        self.assertEqual(
            {
                tuple(call[1].get("phases") or []) for call in execute_calls
            },
            {
                ("selection", "prepare"),
                ("publish", "reconcile"),
            },
        )

    @mock.patch(
        "scripts.tt_auto_post_runner.SidecarClient", FakeSidecarClient
    )
    def test_workers_stop_when_no_task_is_claimed(self):
        result = run_once(self.config)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["execute"]), 3)
        self.assertEqual(
            sorted(len(items) for items in result["execute"]),
            [0, 1, 1],
        )

    def test_worker_batch_is_fixed_to_one_task_per_oneshot(self):
        invalid = RunnerConfig(
            **{
                **self.config.__dict__,
                "max_tasks_per_worker": 2,
            }
        )
        with self.assertRaises(RunnerError):
            invalid.validate()

    def test_documented_placeholder_bearer_is_rejected(self):
        invalid = RunnerConfig(
            **{
                **self.config.__dict__,
                "internal_token": "replace-with-unique-random-token-at-least-32-characters",
            }
        )
        with self.assertRaises(RunnerError):
            invalid.validate()


if __name__ == "__main__":
    unittest.main()
