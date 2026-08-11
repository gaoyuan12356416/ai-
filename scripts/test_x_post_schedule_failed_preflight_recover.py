#!/usr/bin/env python3
"""Offline safety tests for failed-preflight schedule recovery."""

from __future__ import annotations

import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import (  # noqa: E402
    FAILED_PREFLIGHT_RECOVERY_REASON,
)
from scripts.x_post_schedule_failed_preflight_recover import (  # noqa: E402
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

    def recover_failed_preflight_schedule_run(
        self,
        run_id,
        expected_error_code,
        *,
        reason,
        actor,
        verified_repair_job_key="",
        validate_only=False,
    ):
        self.calls.append(
            (
                run_id,
                expected_error_code,
                reason,
                actor,
                verified_repair_job_key,
                validate_only,
            )
        )
        return {
            "run_id": int(run_id),
            "expected_error_code": expected_error_code,
            "reason": reason,
            "actor": actor,
            "validate_only": validate_only,
            "validated_count": 1,
            "updated_count": 0 if validate_only else 1,
        }


class FailedPreflightScheduleRecoveryTests(unittest.TestCase):
    def test_execute_is_lock_guarded_and_never_publishes(self):
        store = FakeStore()
        with mock.patch(
            "scripts.x_post_schedule_failed_preflight_recover.XPostStore",
            return_value=store,
        ):
            validated = execute_recovery(
                95,
                "x_token_missing",
                reason=FAILED_PREFLIGHT_RECOVERY_REASON,
                actor="codex_operator",
                validate_only=True,
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
            recovered = execute_recovery(
                95,
                "x_token_missing",
                reason=FAILED_PREFLIGHT_RECOVERY_REASON,
                actor="codex_operator",
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
        self.assertEqual(validated["status"], "validated")
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(len(store.calls), 2)
        self.assertFalse(store.calls[-1][-1])

        skipped = execute_recovery(
            95,
            "x_token_missing",
            reason=FAILED_PREFLIGHT_RECOVERY_REASON,
            actor="codex_operator",
            lock_factory=unavailable_lock,
        )
        self.assertEqual(skipped["status"], "skipped_locked")

    def test_mutating_cli_requires_a_new_audit_report(self):
        with mock.patch(
            "scripts.x_post_schedule_failed_preflight_recover.execute_recovery"
        ) as execute, mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--run-id",
                    "95",
                    "--expected-error-code",
                    "x_token_missing",
                    "--reason",
                    FAILED_PREFLIGHT_RECOVERY_REASON,
                    "--actor",
                    "codex_operator",
                ]
            )
        self.assertEqual(exit_code, 1)
        execute.assert_not_called()
        result = json.loads(output.call_args.args[0])
        self.assertEqual(
            result["error_code"],
            "x_post_failed_preflight_recovery_report_required",
        )

    def test_unexpected_errors_do_not_leak_details(self):
        with mock.patch(
            "scripts.x_post_schedule_failed_preflight_recover.execute_recovery",
            side_effect=RuntimeError(
                "https://source.example/private?token=must-not-leak"
            ),
        ), mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--run-id",
                    "95",
                    "--expected-error-code",
                    "x_token_missing",
                    "--reason",
                    FAILED_PREFLIGHT_RECOVERY_REASON,
                    "--actor",
                    "codex_operator",
                    "--validate-only",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertNotIn("must-not-leak", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
