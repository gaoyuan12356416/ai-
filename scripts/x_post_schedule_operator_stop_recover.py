#!/usr/bin/env python3
"""Guardedly recover exact operator-stopped material schedule queues."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.service import (  # noqa: E402
    MATERIAL_OPERATOR_STOP_ERROR_CODE,
    MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
    XPostError,
    XPostStore,
)
from scripts.x_post_daily_runner import process_lock  # noqa: E402
from scripts.x_post_media_repair_backfill import (  # noqa: E402
    _safe_error,
)
from scripts.x_post_schedule_pre_x_recover import (  # noqa: E402
    _validate_recovery_report_path as _validate_shared_report_path,
)


DB_PATH = Path("/var/lib/x-post-automation/accounts.sqlite3")
LOCK_PATH = "/run/x-post-daily/runner.lock"
RECOVERY_REPORT_ROOT = Path(
    "/mnt/data-disk/x-post-automation/recoveries"
)
MAX_QUEUE_IDS = 50
MAX_SQLITE_INTEGER = 9223372036854775807
_POSITIVE_INTEGER = re.compile(r"\A[1-9][0-9]*\Z")
_ERROR_CODE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")


def _invalid_request(message):
    raise XPostError("invalid_request", message, 400)


def _normalize_positive_id(raw, field_name):
    if isinstance(raw, bool):
        _invalid_request("%s must be a positive integer" % field_name)
    value = str(raw or "").strip()
    if not _POSITIVE_INTEGER.fullmatch(value):
        _invalid_request("%s must be a positive integer" % field_name)
    parsed = int(value)
    if parsed > MAX_SQLITE_INTEGER:
        _invalid_request("%s is outside the supported range" % field_name)
    return parsed


def normalize_queue_ids(raw):
    """Return one ordered, unique, bounded list of positive queue IDs."""
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        _invalid_request("queue_ids must be a comma-separated list")
    if not values or len(values) > MAX_QUEUE_IDS:
        _invalid_request("queue_ids must contain between 1 and 50 IDs")
    normalized = []
    seen = set()
    for raw_value in values:
        queue_id = _normalize_positive_id(raw_value, "queue_id")
        if queue_id in seen:
            _invalid_request("queue_ids must be unique positive integers")
        seen.add(queue_id)
        normalized.append(queue_id)
    return normalized


def _normalize_arguments(
    run_id,
    queue_ids,
    expected_error_code,
    *,
    reason,
    actor,
    validate_only,
):
    normalized_run_id = _normalize_positive_id(run_id, "run_id")
    normalized_queue_ids = normalize_queue_ids(queue_ids)
    expected_error_code = str(expected_error_code or "")
    actor = str(actor or "")
    if not _ERROR_CODE.fullmatch(expected_error_code):
        _invalid_request("expected_error_code is invalid")
    if expected_error_code != MATERIAL_OPERATOR_STOP_ERROR_CODE:
        _invalid_request("expected_error_code does not match the exact stop code")
    if reason != MATERIAL_OPERATOR_STOP_RECOVERY_REASON:
        _invalid_request("reason does not match the fixed recovery reason")
    if (
        not actor
        or actor != actor.strip()
        or len(actor) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in actor)
    ):
        _invalid_request("actor is invalid")
    if not isinstance(validate_only, bool):
        _invalid_request("validate_only must be a boolean")
    return (
        normalized_run_id,
        normalized_queue_ids,
        expected_error_code,
        actor,
    )


def execute_recovery(
    run_id,
    queue_ids,
    expected_error_code,
    *,
    reason,
    actor,
    validate_only=False,
    now=None,
    db_path=DB_PATH,
    lock_factory=process_lock,
):
    (
        run_id,
        queue_ids,
        expected_error_code,
        actor,
    ) = _normalize_arguments(
        run_id,
        queue_ids,
        expected_error_code,
        reason=reason,
        actor=actor,
        validate_only=validate_only,
    )
    with lock_factory(LOCK_PATH) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "run_id": run_id,
                "queue_ids": queue_ids,
                "expected_error_code": expected_error_code,
                "validate_only": validate_only,
                "validated_count": 0,
                "updated_count": 0,
            }
        store = XPostStore(Path(db_path))
        result = store.recover_operator_stopped_material_schedule_queues(
            run_id,
            queue_ids,
            expected_error_code,
            reason=reason,
            actor=actor,
            validate_only=validate_only,
            now=now,
        )
    result = dict(result)
    result["status"] = "validated" if validate_only else "recovered"
    return result


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Recover exact zero-attempt material schedule queues stopped by "
            "an operator. The command only repairs ledger state and never "
            "publishes."
        )
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--queue-ids",
        required=True,
        help="Comma-separated unique positive queue IDs (maximum 50)",
    )
    parser.add_argument(
        "--expected-error-code",
        required=True,
        help="Must equal %s" % MATERIAL_OPERATOR_STOP_ERROR_CODE,
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Must equal %s" % MATERIAL_OPERATOR_STOP_RECOVERY_REASON,
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report-path")
    return parser


def _validate_operator_stop_report_path(path, report_root=None):
    """Restrict evidence to a new JSON file in the recovery audit tree."""
    return _validate_shared_report_path(
        path,
        Path(report_root or RECOVERY_REPORT_ROOT),
    )


def _atomic_write_operator_stop_report(path, result):
    target = _validate_operator_stop_report_path(path)
    parent = target.parent
    payload = (
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name,
        suffix=".tmp",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise XPostError(
                "x_post_operator_stop_recovery_report_exists",
                "Recovery report target already exists",
                409,
            ) from None
        temporary.unlink()
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path):
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if not args.validate_only and not args.report_path:
            raise XPostError(
                "x_post_operator_stop_recovery_report_required",
                "A dedicated report path is required for state recovery",
                400,
            )
        if args.report_path:
            report_target = _validate_operator_stop_report_path(
                args.report_path
            )
        result = execute_recovery(
            args.run_id,
            args.queue_ids,
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
            "error_code": "x_post_operator_stop_recovery_unexpected_error",
            "error_message": type(exc).__name__,
        }
    if report_target is not None:
        try:
            _atomic_write_operator_stop_report(report_target, result)
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
