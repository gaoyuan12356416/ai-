#!/usr/bin/env python3
"""Offline safety tests for operator-stopped schedule recovery."""

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

from features.x_posts.service import (  # noqa: E402
    MATERIAL_OPERATOR_STOP_ERROR_CODE,
    MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
    XPostError,
)
from scripts.x_post_schedule_operator_stop_recover import (  # noqa: E402
    _atomic_write_operator_stop_report,
    _validate_operator_stop_report_path,
    execute_recovery,
    main,
    normalize_queue_ids,
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

    def recover_operator_stopped_material_schedule_queues(
        self,
        run_id,
        queue_ids,
        expected_error_code,
        *,
        reason,
        actor,
        validate_only,
        now,
    ):
        self.calls.append(
            {
                "run_id": run_id,
                "queue_ids": queue_ids,
                "expected_error_code": expected_error_code,
                "reason": reason,
                "actor": actor,
                "validate_only": validate_only,
                "now": now,
            }
        )
        return {
            "run_id": run_id,
            "queue_ids": queue_ids,
            "expected_error_code": expected_error_code,
            "reason": reason,
            "actor": actor,
            "validate_only": validate_only,
            "validated_count": len(queue_ids),
            "updated_count": 0 if validate_only else len(queue_ids),
        }


class OperatorStoppedScheduleRecoveryTests(unittest.TestCase):
    def test_validate_and_apply_forward_exact_store_parameters(self):
        store = FakeStore()
        validation_now = object()
        apply_now = object()
        with mock.patch(
            "scripts.x_post_schedule_operator_stop_recover.XPostStore",
            return_value=store,
        ):
            validated = execute_recovery(
                "271",
                "901, 902",
                MATERIAL_OPERATOR_STOP_ERROR_CODE,
                reason=MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                actor="codex_operator",
                validate_only=True,
                now=validation_now,
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
            recovered = execute_recovery(
                271,
                [901, 902],
                MATERIAL_OPERATOR_STOP_ERROR_CODE,
                reason=MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                actor="codex_operator",
                now=apply_now,
                db_path=Path("unused.sqlite3"),
                lock_factory=acquired_lock,
            )
        self.assertEqual(validated["status"], "validated")
        self.assertEqual(validated["updated_count"], 0)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["updated_count"], 2)
        self.assertEqual(
            store.calls,
            [
                {
                    "run_id": 271,
                    "queue_ids": [901, 902],
                    "expected_error_code": MATERIAL_OPERATOR_STOP_ERROR_CODE,
                    "reason": MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                    "actor": "codex_operator",
                    "validate_only": True,
                    "now": validation_now,
                },
                {
                    "run_id": 271,
                    "queue_ids": [901, 902],
                    "expected_error_code": MATERIAL_OPERATOR_STOP_ERROR_CODE,
                    "reason": MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                    "actor": "codex_operator",
                    "validate_only": False,
                    "now": apply_now,
                },
            ],
        )

    def test_queue_ids_are_strict_unique_positive_and_bounded(self):
        self.assertEqual(normalize_queue_ids("1, 2,50"), [1, 2, 50])
        for invalid in (
            "",
            "0",
            "1,1",
            "01",
            "1,",
            "1,-2",
            ",".join(str(value) for value in range(1, 52)),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(XPostError):
                normalize_queue_ids(invalid)

    def test_lock_conflict_is_a_zero_update_skip(self):
        with mock.patch(
            "scripts.x_post_schedule_operator_stop_recover.XPostStore"
        ) as store:
            result = execute_recovery(
                271,
                "901,902",
                MATERIAL_OPERATOR_STOP_ERROR_CODE,
                reason=MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                actor="codex_operator",
                lock_factory=unavailable_lock,
            )
        store.assert_not_called()
        self.assertEqual(result["status"], "skipped_locked")
        self.assertEqual(result["updated_count"], 0)

    def test_mutating_cli_requires_a_new_audit_report(self):
        with mock.patch(
            "scripts.x_post_schedule_operator_stop_recover.execute_recovery"
        ) as execute, mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--run-id",
                    "271",
                    "--queue-ids",
                    "901,902",
                    "--expected-error-code",
                    MATERIAL_OPERATOR_STOP_ERROR_CODE,
                    "--reason",
                    MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                    "--actor",
                    "codex_operator",
                ]
            )
        self.assertEqual(exit_code, 1)
        execute.assert_not_called()
        result = json.loads(output.call_args.args[0])
        self.assertEqual(
            result["error_code"],
            "x_post_operator_stop_recovery_report_required",
        )

    def test_validate_only_cli_forwards_without_a_report(self):
        with mock.patch(
            "scripts.x_post_schedule_operator_stop_recover.execute_recovery",
            return_value={
                "status": "validated",
                "validated_count": 2,
                "updated_count": 0,
            },
        ) as execute, mock.patch("builtins.print"):
            exit_code = main(
                [
                    "--run-id",
                    "271",
                    "--queue-ids",
                    "901,902",
                    "--expected-error-code",
                    MATERIAL_OPERATOR_STOP_ERROR_CODE,
                    "--reason",
                    MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                    "--actor",
                    "codex_operator",
                    "--validate-only",
                ]
            )
        self.assertEqual(exit_code, 0)
        execute.assert_called_once_with(
            "271",
            "901,902",
            MATERIAL_OPERATOR_STOP_ERROR_CODE,
            reason=MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
            actor="codex_operator",
            validate_only=True,
        )

    def test_apply_cli_forwards_exact_parameters_and_writes_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "recoveries"
            report_dir = root / "run-271"
            report_dir.mkdir(parents=True)
            report = report_dir / "apply.json"
            with mock.patch(
                "scripts.x_post_schedule_operator_stop_recover."
                "RECOVERY_REPORT_ROOT",
                root,
            ), mock.patch(
                "scripts.x_post_schedule_operator_stop_recover.execute_recovery",
                return_value={
                    "status": "recovered",
                    "validated_count": 2,
                    "updated_count": 2,
                },
            ) as execute, mock.patch("builtins.print"):
                exit_code = main(
                    [
                        "--run-id",
                        "271",
                        "--queue-ids",
                        "901,902",
                        "--expected-error-code",
                        MATERIAL_OPERATOR_STOP_ERROR_CODE,
                        "--reason",
                        MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                        "--actor",
                        "codex_operator",
                        "--report-path",
                        str(report),
                    ]
                )
            self.assertEqual(exit_code, 0)
            execute.assert_called_once_with(
                "271",
                "901,902",
                MATERIAL_OPERATOR_STOP_ERROR_CODE,
                reason=MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                actor="codex_operator",
                validate_only=False,
            )
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8"))["status"],
                "recovered",
            )

    def test_reason_and_expected_error_are_exact(self):
        for error_code, reason in (
            (
                "x_post_schedule_operator_stopped",
                MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
            ),
            (MATERIAL_OPERATOR_STOP_ERROR_CODE, "near_match_recovery_reason"),
        ):
            with self.subTest(
                error_code=error_code,
                reason=reason,
            ), self.assertRaises(XPostError):
                execute_recovery(
                    271,
                    "901,902",
                    error_code,
                    reason=reason,
                    actor="codex_operator",
                    lock_factory=acquired_lock,
                )

    def test_report_is_atomic_new_json_and_confined_to_audit_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "recoveries"
            report_dir = root / "run-271"
            report_dir.mkdir(parents=True)
            report = report_dir / "apply.json"
            outside = base / "accounts.sqlite3"
            outside.write_text("database", encoding="utf-8")

            self.assertEqual(
                _validate_operator_stop_report_path(report, root),
                report.resolve(),
            )
            with self.assertRaises(XPostError):
                _validate_operator_stop_report_path(outside, root)
            with self.assertRaises(XPostError):
                _validate_operator_stop_report_path(
                    report_dir / "apply.txt",
                    root,
                )

            with mock.patch(
                "scripts.x_post_schedule_operator_stop_recover."
                "RECOVERY_REPORT_ROOT",
                root,
            ):
                _atomic_write_operator_stop_report(
                    report,
                    {"status": "recovered", "updated_count": 2},
                )
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8")),
                {"status": "recovered", "updated_count": 2},
            )
            with mock.patch(
                "scripts.x_post_schedule_operator_stop_recover."
                "RECOVERY_REPORT_ROOT",
                root,
            ), self.assertRaises(XPostError):
                _atomic_write_operator_stop_report(
                    report,
                    {"status": "recovered", "updated_count": 99},
                )
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8")),
                {"status": "recovered", "updated_count": 2},
            )
            with self.assertRaises(XPostError):
                _validate_operator_stop_report_path(report, root)
            self.assertEqual(outside.read_text(encoding="utf-8"), "database")

    def test_report_publish_race_never_clobbers_competing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "recoveries"
            report_dir = root / "run-271"
            report_dir.mkdir(parents=True)
            report = report_dir / "apply.json"
            competing_payload = "competing operator evidence\n"

            from scripts import x_post_schedule_operator_stop_recover as module

            real_link = module.os.link

            def create_competing_target(source, target):
                Path(target).write_text(
                    competing_payload,
                    encoding="utf-8",
                )
                return real_link(source, target)

            with mock.patch.object(
                module,
                "RECOVERY_REPORT_ROOT",
                root,
            ), mock.patch.object(
                module.os,
                "link",
                side_effect=create_competing_target,
            ), self.assertRaises(XPostError) as caught:
                _atomic_write_operator_stop_report(
                    report,
                    {"status": "recovered", "updated_count": 2},
                )
            self.assertEqual(
                caught.exception.code,
                "x_post_operator_stop_recovery_report_exists",
            )
            self.assertEqual(
                report.read_text(encoding="utf-8"),
                competing_payload,
            )
            self.assertEqual(
                list(report_dir.glob(".apply.json.*.tmp")),
                [],
            )

    def test_unexpected_errors_do_not_leak_details(self):
        with mock.patch(
            "scripts.x_post_schedule_operator_stop_recover.execute_recovery",
            side_effect=RuntimeError(
                "https://source.example/private?token=must-not-leak"
            ),
        ), mock.patch("builtins.print") as output:
            exit_code = main(
                [
                    "--run-id",
                    "271",
                    "--queue-ids",
                    "901,902",
                    "--expected-error-code",
                    MATERIAL_OPERATOR_STOP_ERROR_CODE,
                    "--reason",
                    MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
                    "--actor",
                    "codex_operator",
                    "--validate-only",
                ]
            )
        self.assertEqual(exit_code, 1)
        payload = output.call_args.args[0]
        self.assertNotIn("must-not-leak", payload)
        self.assertNotIn("source.example", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
