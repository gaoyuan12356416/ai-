#!/usr/bin/env python3
"""Start a new audited X short-drama replay generation without publishing."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.service import (  # noqa: E402
    DRAMA_REPLAY_REASON,
    XPostError,
    XPostStore,
)
from scripts.x_post_daily_runner import process_lock  # noqa: E402
from scripts.x_post_media_repair_backfill import (  # noqa: E402
    _atomic_write_report,
    _safe_error,
)


DB_PATH = Path("/var/lib/x-post-automation/accounts.sqlite3")
LOCK_PATH = "/run/x-post-daily/runner.lock"
REPLAY_REPORT_ROOT = Path("/mnt/data-disk/x-post-automation/replays")


def _load_expected_snapshots(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise XPostError(
            "x_post_drama_replay_snapshot_file_invalid",
            "Replay snapshot file is unavailable or invalid JSON",
            400,
        ) from exc
    if not isinstance(payload, list):
        raise XPostError(
            "x_post_drama_replay_snapshot_file_invalid",
            "Replay snapshot file must contain a JSON array",
            400,
        )
    return payload


def _validate_report_path(path, report_root=REPLAY_REPORT_ROOT):
    supplied = Path(path)
    root = Path(report_root)
    if (
        not supplied.is_absolute()
        or not root.is_absolute()
        or supplied.suffix.lower() != ".json"
    ):
        raise XPostError(
            "x_post_drama_replay_report_path_invalid",
            "Replay report path must be an absolute JSON path",
            400,
        )
    lexical_root = Path(os.path.abspath(root))
    lexical_target = Path(os.path.abspath(supplied))
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError:
        relative = None
    if relative is None or not relative.parts:
        raise XPostError(
            "x_post_drama_replay_report_path_invalid",
            "Replay report path is outside the dedicated audit root",
            400,
        )
    if (
        not lexical_root.exists()
        or not lexical_root.is_dir()
        or lexical_root.is_symlink()
    ):
        raise XPostError(
            "x_post_drama_replay_report_path_invalid",
            "Replay report root is unavailable or unsafe",
            400,
        )
    cursor = lexical_target.parent
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise XPostError(
                "x_post_drama_replay_report_path_invalid",
                "Replay report path contains a symlink",
                400,
            )
        if cursor == lexical_root:
            break
        if cursor == cursor.parent:
            raise XPostError(
                "x_post_drama_replay_report_path_invalid",
                "Replay report path is outside the dedicated audit root",
                400,
            )
        cursor = cursor.parent
    resolved_root = lexical_root.resolve(strict=True)
    resolved_target = lexical_target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        raise XPostError(
            "x_post_drama_replay_report_path_invalid",
            "Replay report path resolves outside the audit root",
            400,
        ) from None
    if (
        not lexical_target.parent.exists()
        or not lexical_target.parent.is_dir()
        or lexical_target.exists()
        or lexical_target.is_symlink()
    ):
        raise XPostError(
            "x_post_drama_replay_report_path_invalid",
            "Replay report target must be a new file in an existing directory",
            400,
        )
    return resolved_target


def execute_replay_reset(
    pool_item_ids,
    expected_snapshots,
    *,
    actor_user_id,
    actor_name,
    apply=False,
    confirmation="",
    db_path=DB_PATH,
    lock_factory=process_lock,
):
    if not isinstance(apply, bool):
        raise XPostError(
            "invalid_request",
            "apply must be a boolean",
            400,
        )
    if apply and confirmation != DRAMA_REPLAY_REASON:
        raise XPostError(
            "x_post_drama_replay_confirmation_required",
            "Apply requires the exact replay policy confirmation",
            400,
        )
    with lock_factory(LOCK_PATH) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "validate_only": not apply,
                "validated_count": 0,
                "reset_count": 0,
            }
        result = XPostStore(Path(db_path)).reset_drama_pool_for_replay(
            list(pool_item_ids),
            actor={
                "user_id": actor_user_id,
                "name": actor_name,
            },
            reason=DRAMA_REPLAY_REASON,
            expected_snapshots=expected_snapshots,
            validate_only=not apply,
        )
    result = dict(result)
    result["status"] = "reset" if apply else "validated"
    return result


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Validate or start a new audited replay generation. This command "
            "never creates a queue and never publishes to X."
        )
    )
    parser.add_argument("--pool-id", action="append", required=True, type=int)
    parser.add_argument("--expected-snapshots", required=True)
    parser.add_argument("--actor-user-id", required=True)
    parser.add_argument("--actor-name", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report-path")
    parser.add_argument("--db-path", default=str(DB_PATH))
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if args.apply and not args.report_path:
            raise XPostError(
                "x_post_drama_replay_report_required",
                "A dedicated report path is required when applying replay",
                400,
            )
        if args.report_path:
            report_target = _validate_report_path(args.report_path)
        expected_snapshots = _load_expected_snapshots(
            args.expected_snapshots
        )
        result = execute_replay_reset(
            args.pool_id,
            expected_snapshots,
            actor_user_id=args.actor_user_id,
            actor_name=args.actor_name,
            apply=bool(args.apply),
            confirmation=args.confirm,
            db_path=Path(args.db_path),
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
            "error_code": "x_post_drama_replay_unexpected_error",
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
        if result["status"] in {"validated", "reset"}
        and result.get("report_status") != "failed"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
