#!/usr/bin/env python3
"""Guardedly re-arm one exact same-day zero-write schedule failure."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.service import (  # noqa: E402
    FAILED_PREFLIGHT_RECOVERY_REASON,
    XPostError,
    XPostStore,
)
from scripts.x_post_daily_runner import process_lock  # noqa: E402
from scripts.x_post_media_repair_backfill import _safe_error  # noqa: E402
from scripts.x_post_schedule_pre_x_recover import (  # noqa: E402
    _atomic_write_recovery_report,
    _validate_recovery_report_path,
)


DB_PATH = Path("/var/lib/x-post-automation/accounts.sqlite3")
LOCK_PATH = "/run/x-post-daily/runner.lock"


def execute_recovery(
    run_id,
    expected_error_code,
    *,
    reason,
    actor,
    validate_only=False,
    db_path=DB_PATH,
    lock_factory=process_lock,
):
    with lock_factory(LOCK_PATH) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "run_id": run_id,
                "validate_only": validate_only,
                "validated_count": 0,
                "updated_count": 0,
            }
        result = XPostStore(Path(db_path)).recover_failed_preflight_schedule_run(
            run_id,
            expected_error_code,
            reason=reason,
            actor=actor,
            validate_only=validate_only,
        )
    result = dict(result)
    result["status"] = "validated" if validate_only else "recovered"
    return result


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Re-arm one exact same-day failed_preflight schedule run with "
            "zero queues/logs/X writes. The command never publishes."
        )
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-error-code", required=True)
    parser.add_argument(
        "--reason",
        required=True,
        help="Must equal %s" % FAILED_PREFLIGHT_RECOVERY_REASON,
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report-path")
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if not args.validate_only and not args.report_path:
            raise XPostError(
                "x_post_failed_preflight_recovery_report_required",
                "A dedicated report path is required for state recovery",
                400,
            )
        if args.report_path:
            report_target = _validate_recovery_report_path(args.report_path)
        result = execute_recovery(
            args.run_id,
            args.expected_error_code,
            reason=args.reason,
            actor=args.actor,
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
            "error_code": "x_post_failed_preflight_recovery_unexpected_error",
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
        if result.get("status") in {"validated", "recovered"}
        and result.get("report_status") != "failed"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
