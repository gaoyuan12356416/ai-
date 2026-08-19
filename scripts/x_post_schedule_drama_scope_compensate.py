#!/usr/bin/env python3
"""Create one audited same-day drama scope compensation child."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.service import XPostError, XPostStore  # noqa: E402
from scripts.x_post_daily_runner import process_lock  # noqa: E402
from scripts.x_post_media_repair_backfill import _safe_error  # noqa: E402
from scripts.x_post_schedule_pre_x_recover import (  # noqa: E402
    _atomic_write_recovery_report,
    _validate_recovery_report_path,
)


DB_PATH = Path("/var/lib/x-post-automation/accounts.sqlite3")
LOCK_PATH = "/run/x-post-daily/runner.lock"


def execute_compensation(
    original_run_id,
    expected_error_code,
    *,
    actor,
    deployed_commit,
    compensation_publish_time,
    validate_only=False,
    db_path=DB_PATH,
    lock_factory=process_lock,
    now=None,
):
    with lock_factory(LOCK_PATH) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "original_run_id": original_run_id,
                "validate_only": validate_only,
                "validated_count": 0,
                "updated_count": 0,
            }
        result = XPostStore(Path(db_path)).create_same_day_drama_scope_compensation(
            original_run_id,
            expected_error_code,
            actor=actor,
            deployed_commit=deployed_commit,
            compensation_publish_time=compensation_publish_time,
            validate_only=validate_only,
            now=now,
        )
    result = dict(result)
    result["status"] = "validated" if validate_only else "created"
    return result


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Create one exact same-day drama compensation run and contract "
            "only unclaimed future slots to the saved replacement scope."
        )
    )
    parser.add_argument("--original-run-id", required=True)
    parser.add_argument(
        "--expected-error-code",
        default="x_post_schedule_drama_shortage",
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--compensation-publish-time", required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report-path")
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if not args.validate_only and not args.report_path:
            raise XPostError(
                "x_post_drama_scope_compensation_report_required",
                "A dedicated report path is required for state compensation",
                400,
            )
        if args.report_path:
            report_target = _validate_recovery_report_path(args.report_path)
        result = execute_compensation(
            args.original_run_id,
            args.expected_error_code,
            actor=args.actor,
            deployed_commit=args.deployed_commit,
            compensation_publish_time=args.compensation_publish_time,
            validate_only=bool(args.validate_only),
        )
    except XPostError as exc:
        code, message = _safe_error(exc)
        result = {
            "status": "failed",
            "error_code": code,
            "error_message": message,
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "error_code": "x_post_drama_scope_compensation_unexpected_error",
            "error_message": type(exc).__name__,
        }
    if report_target is not None:
        try:
            _atomic_write_recovery_report(report_target, result)
        except Exception:
            result = dict(result)
            result["report_status"] = "failed"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return (
        0
        if result.get("status") in {"validated", "created"}
        and result.get("report_status") != "failed"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
