#!/usr/bin/env python3
"""Offline safety tests for exact pre-X schedule recovery."""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import XPostError  # noqa: E402
from scripts.x_post_schedule_pre_x_recover import (  # noqa: E402
    execute_recovery,
    main,
)


@contextlib.contextmanager
def acquired_lock(_path):
    yield object()


@contextlib.contextmanager
def unavailable_lock(_path):
    yield None


class FakeStore:
    def __init__(self):
        self.calls = []

    def recover_pre_x_schedule_failure(
        self,
        queue_id,
        expected_error_code,
        *,
        validate_only=False,
    ):
        self.calls.append(
            (queue_id, expected_error_code, validate_only)
        )
        return {
            "queue_id": queue_id,
            "log_id": 45,
            "schedule_run_id": 17,
            "drama_pool_item_id": 2,
            "expected_error_code": expected_error_code,
            "validate_only": validate_only,
            "validated_count": 1,
            "updated_count": 0 if validate_only else 1,
        }


class PreXScheduleRecoveryTests(unittest.TestCase):
    def test_execute_is_exact_lock_guarded_and_never_publishes(self):
        store = FakeStore()
        with mock.patch(
            "scripts.x_post_schedule_pre_x_recover.XPostStore",
            return_value=store,
        ):
            validated = execute_recovery(
                "45",
                "invalid_short_base_url",
                validate_only=True,
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
            recovered = execute_recovery(
                "45",
                "invalid_short_base_url",
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
        self.assertEqual(validated["status"], "validated")
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(
            store.calls,
            [
                (45, "invalid_short_base_url", True),
                (45, "invalid_short_base_url", False),
            ],
        )

    def test_lock_and_unexpected_failures_are_fail_closed(self):
        skipped = execute_recovery(
            45,
            "invalid_short_base_url",
            lock_factory=unavailable_lock,
        )
        self.assertEqual(skipped["status"], "skipped_locked")
        with self.assertRaises(XPostError):
            execute_recovery(
                "bad",
                "invalid_short_base_url",
                lock_factory=acquired_lock,
            )
        with mock.patch(
            "scripts.x_post_schedule_pre_x_recover.execute_recovery",
            side_effect=RuntimeError(
                "https://source.example/private?token=must-not-leak"
            ),
        ), mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--queue-id",
                    "45",
                    "--expected-error-code",
                    "invalid_short_base_url",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertNotIn("must-not-leak", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
