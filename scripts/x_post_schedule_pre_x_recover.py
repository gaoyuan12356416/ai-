#!/usr/bin/env python3
"""Guardedly requeue one proven pre-X schedule failure without publishing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.service import XPostError, XPostStore  # noqa: E402
from scripts.x_post_daily_runner import process_lock  # noqa: E402
from scripts.x_post_media_repair_backfill import (  # noqa: E402
    _atomic_write_report,
    _safe_error,
    _validate_report_path,
)


DB_PATH = Path("/var/lib/x-post-automation/accounts.sqlite3")
LOCK_PATH = "/run/x-post-daily/runner.lock"
_ERROR_CODE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")


def execute_recovery(
    queue_id,
    expected_error_code,
    *,
    validate_only=False,
    db_path=DB_PATH,
    lock_factory=process_lock,
):
    try:
        queue_id = int(queue_id)
    except (TypeError, ValueError, OverflowError):
        raise XPostError(
            "invalid_request",
            "queue_id must be a positive integer",
            400,
        ) from None
    expected_error_code = str(expected_error_code or "").strip()
    if (
        queue_id <= 0
        or not _ERROR_CODE.fullmatch(expected_error_code)
        or not isinstance(validate_only, bool)
    ):
        raise XPostError(
            "invalid_request",
            "pre-X recovery arguments are invalid",
            400,
        )
    with lock_factory(LOCK_PATH) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "queue_id": queue_id,
                "validate_only": validate_only,
                "validated_count": 0,
                "updated_count": 0,
            }
        result = XPostStore(Path(db_path)).recover_pre_x_schedule_failure(
            queue_id,
            expected_error_code,
            validate_only=validate_only,
        )
    result = dict(result)
    result["status"] = "validated" if validate_only else "recovered"
    return result


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Requeue one exact zero-attempt schedule failure; never create "
            "a plan or publish."
        )
    )
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--expected-error-code", required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report-path")
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if args.report_path:
            report_target = _validate_report_path(args.report_path)
        result = execute_recovery(
            args.queue_id,
            args.expected_error_code,
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
            "error_code": "x_post_pre_x_recovery_unexpected_error",
            "error_message": type(exc).__name__,
        }
    if report_target is not None:
        try:
            _atomic_write_report(report_target, result)
        except Exception:
            result = dict(result)
            result["report_status"] = "failed"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return (
        0
        if result["status"] in {"validated", "recovered"}
        and result.get("report_status") != "failed"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
