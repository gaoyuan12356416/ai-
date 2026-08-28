#!/usr/bin/env python3
"""Repair and revalidate exact failed drama episodes without publishing."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.drama_selector import select_drama_pool_episodes  # noqa: E402
from features.x_posts.selector import (  # noqa: E402
    CandidateSelectionError,
    previous_source_date,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    XPostError,
    download_media,
    probe_media,
)
from scripts.x_post_daily_runner import (  # noqa: E402
    DailyRunError,
    MediaRepairClient,
    _connect_from_config,
    _preflight_candidate,
    process_lock,
)
from scripts.x_post_media_repair_backfill import (  # noqa: E402
    BackfillError,
    _atomic_write_report,
    _parse_environment_file,
    _safe_error,
    _validate_report_path,
    configured_environment,
    load_environment_files,
)
from scripts.x_post_schedule_runner import (  # noqa: E402
    ScheduleConfig,
    ScheduleRunError,
    ScheduleSidecarClient,
)


MAX_ITEMS = 20
_CONTENT_ID = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")
_ERROR_CODE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")
SCHEDULE_ENV_PATH = Path("/etc/x-post-schedule.env")
_SAFE_SCHEDULE_KEYS = frozenset(
    {
        "X_POST_SCHEDULE_DRAMA_APP_ID",
        "X_POST_SCHEDULE_CANDIDATE_POOL_LIMIT",
        "X_POST_SCHEDULE_DRAMA_CHECK_PATH",
        "X_POST_SCHEDULE_DRAMA_POOL_PATH",
        "X_POST_SCHEDULE_DUE_PATH",
        "X_POST_SCHEDULE_FAILURE_PATH",
        "X_POST_SCHEDULE_GRACE_SECONDS",
        "X_POST_SCHEDULE_INTERNAL_TIMEOUT",
        "X_POST_SCHEDULE_LOCK_PATH",
        "X_POST_SCHEDULE_MATERIAL_CHECK_PATH",
        "X_POST_SCHEDULE_MATERIAL_POOL_PATH",
        "X_POST_SCHEDULE_MAX_DUE_BATCHES",
        "X_POST_SCHEDULE_MAX_REPAIRS_PER_RUN",
        "X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS",
        "X_POST_SCHEDULE_PLAN_PATH",
        "X_POST_SCHEDULE_PLAN_QUERY_PATH",
        "X_POST_SCHEDULE_PUBLISH_PATH_TEMPLATE",
        "X_POST_SCHEDULE_START_DATE",
        "X_POST_SCHEDULE_STORAGE_PREFLIGHT_PATH",
        "X_POST_SCHEDULE_WORK_DIR",
    }
)
_BACKFILL_ACCOUNT = {
    "id": 1,
    "username": "x_drama_repair",
    "x_user_id": "1",
    "display_name": "X Drama Repair",
}


def load_drama_environment_files(
    daily_path=None,
    token_path=None,
    schedule_path=SCHEDULE_ENV_PATH,
):
    """Load the same bounded configuration layers as the schedule service."""
    kwargs = {}
    if daily_path is not None:
        kwargs["daily_path"] = daily_path
    if token_path is not None:
        kwargs["token_path"] = token_path
    values = load_environment_files(**kwargs)
    schedule = _parse_environment_file(
        schedule_path,
        _SAFE_SCHEDULE_KEYS,
    )
    overlap = set(values).intersection(schedule)
    if overlap:
        raise BackfillError(
            "schedule environment duplicates an existing assignment",
            code="x_post_backfill_config_invalid",
        )
    values.update(schedule)
    return values


def normalize_items(pool_ids, content_ids, episode_numbers, error_codes):
    values = (pool_ids, content_ids, episode_numbers, error_codes)
    if any(not isinstance(value, list) for value in values):
        raise BackfillError(
            "drama repair arguments must be repeated lists",
            code="x_post_drama_backfill_items_invalid",
        )
    lengths = {len(value) for value in values}
    if lengths != {len(pool_ids)} or not 1 <= len(pool_ids) <= MAX_ITEMS:
        raise BackfillError(
            "drama repair arguments must have matching counts",
            code="x_post_drama_backfill_items_invalid",
        )
    normalized = []
    identities = set()
    for raw_pool_id, raw_content_id, raw_episode, raw_error in zip(*values):
        try:
            pool_id = int(raw_pool_id)
            episode_number = int(raw_episode)
        except (TypeError, ValueError, OverflowError):
            raise BackfillError(
                "pool IDs and episode numbers must be positive integers",
                code="x_post_drama_backfill_items_invalid",
            ) from None
        content_id = str(raw_content_id or "").strip()
        error_code = str(raw_error or "").strip()
        identity = (pool_id, content_id)
        if (
            pool_id <= 0
            or episode_number <= 0
            or not _CONTENT_ID.fullmatch(content_id)
            or not _ERROR_CODE.fullmatch(error_code)
            or identity in identities
        ):
            raise BackfillError(
                "drama repair identity is invalid or duplicated",
                code="x_post_drama_backfill_items_invalid",
            )
        identities.add(identity)
        normalized.append(
            {
                "pool_item_id": pool_id,
                "content_id": content_id,
                "episode_number": episode_number,
                "expected_error_code": error_code,
            }
        )
    return normalized


def _success_checks(items):
    return [
        {
            "pool_item_id": item["pool_item_id"],
            "content_id": item["content_id"],
            "error_code": "",
            "error_message": "",
            "expected_error_code": item["expected_error_code"],
            "expected_episode_number": item["episode_number"],
        }
        for item in items
    ]


def execute_backfill(
    config,
    items,
    *,
    sidecar=None,
    repair_client=None,
    connection_factory=None,
    downloader=download_media,
    prober=probe_media,
    lock_factory=process_lock,
    now=None,
):
    items = normalize_items(
        [item.get("pool_item_id") for item in items],
        [item.get("content_id") for item in items],
        [item.get("episode_number") for item in items],
        [item.get("expected_error_code") for item in items],
    )
    config.validate()
    if not config.repair_url:
        raise BackfillError(
            "X media repair is disabled",
            code="x_post_media_repair_disabled",
        )
    sidecar = sidecar or ScheduleSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
    )
    repair_client = repair_client or MediaRepairClient(
        config.repair_url,
        config.repair_token,
        timeout=config.repair_timeout,
        max_output_bytes=config.max_media_bytes,
    )
    connection_factory = connection_factory or _connect_from_config
    checks = _success_checks(items)
    with lock_factory(config.lock_path) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "requested_count": len(items),
                "ready_count": 0,
                "restored_count": 0,
                "results": [],
            }
        sidecar.preflight_storage(config.storage_preflight_path)
        guarded = sidecar.record_drama_pool_checks(
            config.drama_check_path,
            checks,
            validate_only=True,
        )
        if int(guarded.get("validated_count") or 0) != len(items):
            raise BackfillError(
                "one or more drama rows failed the revalidation guard",
                code="x_post_drama_backfill_guard_failed",
            )
        connection = connection_factory(config)
        try:
            source_date = previous_source_date(shanghai_now(now))
            timestamp = max(1, int(shanghai_now(now).timestamp()))
            repair_state = {"attempted": 0}
            results = []
            work_root = Path(config.work_dir)
            if (
                not work_root.exists()
                or not work_root.is_dir()
                or work_root.is_symlink()
            ):
                raise BackfillError(
                    "daily media work directory is unavailable",
                    code="x_post_storage_unavailable",
                )
            with tempfile.TemporaryDirectory(
                prefix="x-post-drama-repair-backfill-",
                dir=str(work_root),
            ) as temporary:
                for rank, requested in enumerate(items, 1):
                    pool = {
                        "id": requested["pool_item_id"],
                        "content_id": requested["content_id"],
                        "created_at": "1970-01-01T00:00:00+00:00",
                        "next_sub_number": requested["episode_number"],
                        "assigned_account_id": 0,
                        "assigned_at": "",
                        "assigned_source_queue_id": None,
                        "candidate_account_id": _BACKFILL_ACCOUNT["id"],
                    }
                    candidates = select_drama_pool_episodes(
                        connection,
                        [pool],
                        account_ids=[_BACKFILL_ACCOUNT["id"]],
                        schema=config.mysql_database,
                        app_id=config.drama_app_id,
                    )
                    if (
                        len(candidates) != 1
                        or int(candidates[0]["episode_number"])
                        != requested["episode_number"]
                    ):
                        raise BackfillError(
                            "requested drama episode is not selectable",
                            code="x_post_drama_backfill_episode_unavailable",
                        )
                    candidate = dict(candidates[0])
                    candidate["pool_item_id"] = requested["pool_item_id"]
                    candidate["pool_created_at"] = pool["created_at"]
                    candidate["source_date"] = source_date
                    item = _preflight_candidate(
                        config,
                        candidate,
                        _BACKFILL_ACCOUNT,
                        rank,
                        timestamp,
                        Path(temporary)
                        / (
                            "%s-%s.mp4"
                            % (
                                requested["pool_item_id"],
                                requested["episode_number"],
                            )
                        ),
                        downloader,
                        prober,
                        repair_client=repair_client,
                        repair_state=repair_state,
                    )
                    results.append(
                        {
                            "pool_item_id": requested["pool_item_id"],
                            "content_id": requested["content_id"],
                            "episode_number": requested["episode_number"],
                            "status": (
                                "repaired_ready"
                                if item.get("media_repair_job_key")
                                else "validated_ready"
                            ),
                            "repair_profile": str(
                                item.get("media_repair_profile", "")
                            ),
                            "repair_job_key": str(
                                item.get("media_repair_job_key", "")
                            ),
                            "preflight_sha256": str(
                                item.get("preflight_sha256", "")
                            ),
                            "preflight_size": int(
                                item.get("preflight_size", 0) or 0
                            ),
                            "preflight_duration": float(
                                item.get("preflight_duration", 0) or 0
                            ),
                        }
                    )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        restored = sidecar.record_drama_pool_checks(
            config.drama_check_path,
            checks,
        )
        if int(restored.get("updated_count") or 0) != len(items):
            raise BackfillError(
                "one or more drama rows changed before restoration",
                code="x_post_drama_backfill_restore_conflict",
            )
        return {
            "status": "completed",
            "requested_count": len(items),
            "ready_count": len(results),
            "restored_count": len(items),
            "repair_attempted_count": int(repair_state["attempted"]),
            "results": results,
        }


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Repair exact validation-failed drama episodes and restore only "
            "successfully revalidated unbound rows; never create or publish."
        )
    )
    parser.add_argument("--pool-item-id", action="append", required=True)
    parser.add_argument("--content-id", action="append", required=True)
    parser.add_argument("--episode-number", action="append", required=True)
    parser.add_argument("--expected-error-code", action="append", required=True)
    parser.add_argument("--report-path")
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if args.report_path:
            report_target = _validate_report_path(args.report_path)
        items = normalize_items(
            args.pool_item_id,
            args.content_id,
            args.episode_number,
            args.expected_error_code,
        )
        values = load_drama_environment_files()
        with configured_environment(values):
            result = execute_backfill(ScheduleConfig.from_env(), items)
        if report_target is not None:
            _atomic_write_report(report_target, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "completed" else 2
    except (
        BackfillError,
        DailyRunError,
        CandidateSelectionError,
        ScheduleRunError,
        XPostError,
    ) as exc:
        code, message = _safe_error(exc)
        result = {
            "status": "failed",
            "error_code": code[:64],
            "error_message": message,
        }
        if report_target is not None:
            try:
                _atomic_write_report(report_target, result)
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception as exc:
        result = {
            "status": "failed",
            "error_code": "x_post_drama_backfill_unexpected_error",
            "error_message": type(exc).__name__,
        }
        if report_target is not None:
            try:
                _atomic_write_report(report_target, result)
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
