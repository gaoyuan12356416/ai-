#!/usr/bin/env python3
"""Run one audited previous-day X schedule tick at its frozen clock."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.selector import SHANGHAI_TZ  # noqa: E402
from features.x_posts.service import (  # noqa: E402
    PREVIOUS_DAY_STALE_CLAIM_RECOVERY_REASON,
    XPostError,
)
from scripts.x_post_daily_runner import process_lock  # noqa: E402
from scripts.x_post_media_repair_backfill import _safe_error  # noqa: E402
from scripts.x_post_schedule_pre_x_recover import (  # noqa: E402
    _atomic_write_recovery_report,
    _validate_recovery_report_path,
)
from scripts.x_post_schedule_runner import (  # noqa: E402
    ScheduleConfig,
    execute_schedule_tick,
)


def _argument_parser():
    parser = argparse.ArgumentParser(
        description="Execute re-armed previous-day runs using one explicit frozen clock."
    )
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--frozen-time", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--report-path", required=True)
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if args.reason != PREVIOUS_DAY_STALE_CLAIM_RECOVERY_REASON:
            raise XPostError(
                "x_post_previous_day_runner_not_allowed",
                "Previous-day runner reason is not allowed",
                409,
            )
        if not re.fullmatch(r"[a-f0-9]{40}", str(args.deployed_commit).lower()):
            raise XPostError(
                "x_post_previous_day_runner_not_allowed",
                "Previous-day runner requires an exact deployed commit",
                409,
            )
        frozen = datetime.strptime(
            "%s %s" % (args.run_date, args.frozen_time), "%Y-%m-%d %H:%M"
        ).replace(tzinfo=SHANGHAI_TZ)
        current = datetime.now(SHANGHAI_TZ)
        if frozen.date() != current.date() - timedelta(days=1):
            raise XPostError(
                "x_post_previous_day_runner_date_conflict",
                "Frozen clock must belong to exactly the previous Shanghai date",
                409,
            )
        report_target = _validate_recovery_report_path(args.report_path)
        config = ScheduleConfig.from_env()
        with process_lock(config.lock_path) as acquired:
            if acquired is None:
                result = {"status": "skipped_locked", "batches": []}
            else:
                result = execute_schedule_tick(config, now=frozen)
        result = dict(result)
        result["operator_reason"] = args.reason
        result["deployed_commit"] = str(args.deployed_commit).lower()
        result["frozen_now"] = frozen.isoformat(timespec="minutes")
    except XPostError as exc:
        code, message = _safe_error(exc)
        result = {"status": "failed", "error_code": code, "error_message": message}
    except Exception as exc:
        result = {
            "status": "failed",
            "error_code": str(getattr(exc, "code", "x_post_previous_day_runner_unexpected_error")),
            "error_message": type(exc).__name__,
        }
    if report_target is not None:
        try:
            _atomic_write_recovery_report(report_target, result)
        except Exception:
            result = dict(result)
            result["report_status"] = "failed"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main())
