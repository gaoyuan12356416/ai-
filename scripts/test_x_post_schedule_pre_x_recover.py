#!/usr/bin/env python3
"""Offline safety tests for exact pre-X schedule recovery."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import XPostError  # noqa: E402
from scripts.x_post_schedule_pre_x_recover import (  # noqa: E402
    _atomic_write_recovery_report,
    _validate_recovery_report_path,
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
                    "--validate-only",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertNotIn("must-not-leak", output.call_args.args[0])

    def test_mutating_cli_requires_a_dedicated_report(self):
        with mock.patch(
            "scripts.x_post_schedule_pre_x_recover.execute_recovery"
        ) as execute, mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--queue-id",
                    "45",
                    "--expected-error-code",
                    "invalid_short_base_url",
                ]
            )
        self.assertEqual(exit_code, 1)
        execute.assert_not_called()
        result = json.loads(output.call_args.args[0])
        self.assertEqual(
            result["error_code"],
            "x_post_pre_x_recovery_report_required",
        )

    def test_report_is_new_json_confined_to_dedicated_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "recoveries"
            report_dir = root / "run-17"
            report_dir.mkdir(parents=True)
            report = report_dir / "apply.json"
            outside = base / "accounts.sqlite3"
            outside.write_text("database", encoding="utf-8")

            self.assertEqual(
                _validate_recovery_report_path(report, root),
                report.resolve(),
            )
            with self.assertRaises(XPostError):
                _validate_recovery_report_path(outside, root)
            with self.assertRaises(XPostError):
                _validate_recovery_report_path(
                    report_dir / "apply.txt",
                    root,
                )

            with mock.patch(
                "scripts.x_post_schedule_pre_x_recover."
                "RECOVERY_REPORT_ROOT",
                root,
            ):
                _atomic_write_recovery_report(
                    report,
                    {"status": "recovered", "updated_count": 1},
                )
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8")),
                {"status": "recovered", "updated_count": 1},
            )
            with self.assertRaises(XPostError):
                _validate_recovery_report_path(report, root)
            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "database",
            )

    def test_report_rejects_symlinked_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "recoveries"
            real = root / "real"
            root.mkdir()
            real.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest("directory symlinks unavailable: %s" % exc)
            with self.assertRaises(XPostError):
                _validate_recovery_report_path(
                    linked / "apply.json",
                    root,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
