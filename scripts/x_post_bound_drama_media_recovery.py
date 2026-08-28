#!/usr/bin/env python3
"""Repair and re-arm one exact bound-drama pre-X failure set; never call X."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.drama_selector import (  # noqa: E402
    DramaPoolRejection,
    select_drama_pool_episodes,
)
from features.x_posts.selector import (  # noqa: E402
    CandidateSelectionError,
    previous_source_date,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    BOUND_DRAMA_FAILED_MEDIA_RECOVERY_REASON,
    XPostError,
    XPostStore,
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
from scripts.x_post_drama_media_repair_backfill import (  # noqa: E402
    load_drama_environment_files,
)
from scripts.x_post_media_repair_backfill import (  # noqa: E402
    BackfillError,
    _atomic_write_report,
    _safe_error,
    _validate_report_path,
    configured_environment,
)
from scripts.x_post_schedule_runner import (  # noqa: E402
    ScheduleConfig,
    ScheduleRunError,
)


MAX_ITEMS = 20
_CONTENT_ID = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")
_ERROR_CODE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")


def normalize_manifest(raw):
    if not isinstance(raw, dict):
        raise BackfillError(
            "短剧恢复清单必须是对象",
            code="x_post_bound_drama_manifest_invalid",
        )
    try:
        run_id = int(raw.get("run_id"))
    except (TypeError, ValueError, OverflowError):
        run_id = 0
    queues = raw.get("queues")
    if run_id <= 0 or not isinstance(queues, list) or not 1 <= len(queues) <= MAX_ITEMS:
        raise BackfillError(
            "短剧恢复清单的批次或队列范围无效",
            code="x_post_bound_drama_manifest_invalid",
        )
    normalized = []
    seen_queues = set()
    seen_pools = set()
    for raw_item in queues:
        if not isinstance(raw_item, dict):
            raise BackfillError(
                "短剧恢复清单中的队列必须是对象",
                code="x_post_bound_drama_manifest_invalid",
            )
        try:
            queue_id = int(raw_item.get("queue_id"))
            pool_item_id = int(raw_item.get("pool_item_id"))
            episode_number = int(raw_item.get("episode_number"))
        except (TypeError, ValueError, OverflowError):
            queue_id = pool_item_id = episode_number = 0
        content_id = str(raw_item.get("content_id", "") or "").strip()
        expected_error_code = str(
            raw_item.get("expected_error_code", "") or ""
        ).strip()
        if (
            queue_id <= 0
            or pool_item_id <= 0
            or episode_number <= 0
            or queue_id in seen_queues
            or pool_item_id in seen_pools
            or not _CONTENT_ID.fullmatch(content_id)
            or not _ERROR_CODE.fullmatch(expected_error_code)
        ):
            raise BackfillError(
                "短剧恢复清单身份无效或重复",
                code="x_post_bound_drama_manifest_invalid",
            )
        seen_queues.add(queue_id)
        seen_pools.add(pool_item_id)
        normalized.append(
            {
                "queue_id": queue_id,
                "pool_item_id": pool_item_id,
                "content_id": content_id,
                "episode_number": episode_number,
                "expected_error_code": expected_error_code,
            }
        )
    normalized.sort(key=lambda item: item["queue_id"])
    return {"run_id": run_id, "queues": normalized}


def load_manifest(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackfillError(
            "短剧恢复清单无法读取或不是有效 JSON",
            code="x_post_bound_drama_manifest_invalid",
        ) from exc
    return normalize_manifest(raw)


def _repair_account(candidate):
    account_id = int(candidate.get("candidate_account_id") or 0)
    language = str(
        candidate.get("material_language")
        or candidate.get("language")
        or "en"
    )
    return {
        "id": account_id,
        "username": "x_drama_repair_%s" % account_id,
        "x_user_id": str(account_id),
        "display_name": "X Drama Repair %s" % account_id,
        "drama_language": language,
        # This command only validates/repairs bytes. The frozen queue keeps
        # its original target and delivery route, and no X endpoint is called.
        "long_video_eligible": True,
        "long_video_publish_eligible": True,
    }


def execute_recovery(
    config,
    db_path,
    manifest,
    *,
    deployed_commit,
    actor="codex_operator",
    apply=False,
    store=None,
    repair_client=None,
    connection_factory=None,
    candidate_loader=select_drama_pool_episodes,
    preflight_candidate=_preflight_candidate,
    downloader=download_media,
    prober=probe_media,
    lock_factory=process_lock,
    now=None,
):
    manifest = normalize_manifest(manifest)
    if not isinstance(apply, bool):
        raise BackfillError(
            "apply 参数必须是布尔值",
            code="x_post_bound_drama_manifest_invalid",
        )
    config.validate()
    if not config.repair_url:
        raise BackfillError(
            "X 媒体修复服务未启用",
            code="x_post_media_repair_disabled",
        )
    store = store or XPostStore(Path(db_path))
    repair_client = repair_client or MediaRepairClient(
        config.repair_url,
        config.repair_token,
        timeout=config.repair_timeout,
        max_output_bytes=config.max_media_bytes,
    )
    connection_factory = connection_factory or _connect_from_config
    current = shanghai_now(now)
    source_date = previous_source_date(current)
    timestamp = max(1, int(current.timestamp()))
    requested = manifest["queues"]
    with lock_factory(config.lock_path) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "run_id": manifest["run_id"],
                "requested_count": len(requested),
                "x_write_attempted": False,
                "results": [],
            }
        work_root = Path(config.work_dir)
        if (
            not work_root.exists()
            or not work_root.is_dir()
            or work_root.is_symlink()
        ):
            raise BackfillError(
                "定时发布媒体工作目录不可用",
                code="x_post_storage_unavailable",
            )
        connection = connection_factory(config)
        repair_state = {"attempted": 0}
        prepared = []
        try:
            with tempfile.TemporaryDirectory(
                prefix="x-post-bound-drama-recovery-",
                dir=str(work_root),
            ) as temporary:
                for rank, expected in enumerate(requested, 1):
                    frozen_queue = store.get_queue(expected["queue_id"])
                    if (
                        int(frozen_queue.get("schedule_run_id") or 0) != manifest["run_id"]
                        or frozen_queue.get("source_type") != "drama"
                        or int(frozen_queue.get("drama_pool_item_id") or 0) != expected["pool_item_id"]
                        or str(frozen_queue.get("content_id") or "") != expected["content_id"]
                        or int(frozen_queue.get("episode_number") or 0) != expected["episode_number"]
                    ):
                        raise BackfillError(
                            "清单与已冻结短剧队列身份不一致，未执行媒体修复",
                            code="x_post_bound_drama_source_changed",
                        )
                    pool = {
                        "id": expected["pool_item_id"],
                        "content_id": expected["content_id"],
                        "created_at": "1970-01-01T00:00:00Z",
                        "next_sub_number": expected["episode_number"],
                        "assigned_account_id": 1,
                        "assigned_at": "1970-01-01T00:00:00Z",
                        "assigned_source_queue_id": expected["queue_id"],
                        "candidate_account_id": 1,
                    }
                    candidates = candidate_loader(
                        connection,
                        [pool],
                        account_ids=[1],
                        schema=config.mysql_database,
                        app_id=config.drama_app_id,
                    )
                    if (
                        len(candidates) != 1
                        or str(candidates[0].get("content_id") or "")
                        != expected["content_id"]
                        or int(candidates[0].get("episode_number") or 0)
                        != expected["episode_number"]
                    ):
                        raise BackfillError(
                            "清单指定的已绑定短剧集数当前无法精确读取",
                            code="x_post_bound_drama_episode_unavailable",
                        )
                    candidate = dict(candidates[0])
                    if (
                        not str(candidate.get("material_id") or "")
                        or str(candidate.get("material_id")) != str(frozen_queue.get("material_id") or "")
                        or not str(candidate.get("material_url") or "")
                        or str(candidate.get("material_url")) != str(frozen_queue.get("material_url") or "")
                    ):
                        raise BackfillError(
                            "当前源剧集资源或URL与冻结队列不一致，禁止换源恢复",
                            code="x_post_bound_drama_source_changed",
                        )
                    candidate["pool_item_id"] = expected["pool_item_id"]
                    candidate["pool_created_at"] = pool["created_at"]
                    candidate["source_date"] = source_date
                    account = _repair_account(candidate)
                    candidate["candidate_account_id"] = account["id"]
                    item = preflight_candidate(
                        config,
                        candidate,
                        account,
                        rank,
                        timestamp,
                        Path(temporary) / ("%s.mp4" % expected["queue_id"]),
                        downloader,
                        prober,
                        repair_client=repair_client,
                        repair_state=repair_state,
                    )
                    if (
                        str(item.get("media_repair_trigger_code") or "")
                        != expected["expected_error_code"]
                        or not str(item.get("media_repair_job_key") or "")
                        or not str(item.get("media_repair_source_sha256") or "")
                    ):
                        raise BackfillError(
                            "已绑定短剧未生成完整且匹配的媒体修复证据",
                            code="x_post_bound_drama_repair_proof_invalid",
                        )
                    prepared.append(
                        {
                            **expected,
                            "material_url": str(item["material_url"]),
                            "preflight_sha256": str(item["preflight_sha256"]),
                            "preflight_size": int(item["preflight_size"]),
                            "preflight_duration": float(
                                item["preflight_duration"]
                            ),
                            "media_repair_trigger_code": str(
                                item["media_repair_trigger_code"]
                            ),
                            "media_repair_job_key": str(
                                item["media_repair_job_key"]
                            ),
                            "media_repair_profile": str(
                                item["media_repair_profile"]
                            ),
                            "media_repair_source_sha256": str(
                                item["media_repair_source_sha256"]
                            ),
                        }
                    )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

        validated = store.recover_failed_drama_schedule_queues(
            manifest["run_id"],
            prepared,
            reason=BOUND_DRAMA_FAILED_MEDIA_RECOVERY_REASON,
            actor=actor,
            deployed_commit=deployed_commit,
            validate_only=True,
            now=current,
        )
        applied = None
        if apply:
            applied = store.recover_failed_drama_schedule_queues(
                manifest["run_id"],
                prepared,
                reason=BOUND_DRAMA_FAILED_MEDIA_RECOVERY_REASON,
                actor=actor,
                deployed_commit=deployed_commit,
                validate_only=False,
                now=current,
            )
        final = applied or validated
        return {
            "status": "applied" if apply else "validated",
            "run_id": manifest["run_id"],
            "requested_count": len(requested),
            "repair_attempted_count": int(repair_state["attempted"]),
            "validated_queue_count": int(final["validated_queue_count"]),
            "updated_count": int(final["updated_count"]),
            "next_status": str(final["next_status"]),
            "x_write_attempted": False,
            "results": [
                {
                    "queue_id": item["queue_id"],
                    "pool_item_id": item["pool_item_id"],
                    "content_id": item["content_id"],
                    "episode_number": item["episode_number"],
                    "repair_job_key": item["media_repair_job_key"],
                    "preflight_sha256": item["preflight_sha256"],
                    "preflight_size": item["preflight_size"],
                    "preflight_duration": item["preflight_duration"],
                    "material_url": item["material_url"],
                }
                for item in prepared
            ],
        }


def _argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--actor", default="codex_operator")
    parser.add_argument("--report-path")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only after every repaired item passes the atomic ledger guard.",
    )
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if args.report_path:
            report_target = _validate_report_path(args.report_path)
        manifest = load_manifest(args.manifest)
        values = load_drama_environment_files()
        with configured_environment(values):
            result = execute_recovery(
                ScheduleConfig.from_env(),
                args.db_path,
                manifest,
                deployed_commit=args.deployed_commit,
                actor=args.actor,
                apply=args.apply,
            )
        if report_target is not None:
            _atomic_write_report(report_target, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] in {"validated", "applied"} else 2
    except sqlite3.Error:
        result = {
            "status": "failed",
            "error_code": "x_post_bound_drama_recovery_store_failed",
            "error_message": "短剧媒体恢复数据库事务失败，已回滚，禁止自动重试",
            "x_write_attempted": False,
        }
        if report_target is not None:
            try:
                _atomic_write_report(report_target, result)
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    except (
        BackfillError,
        CandidateSelectionError,
        DailyRunError,
        DramaPoolRejection,
        ScheduleRunError,
        XPostError,
    ) as exc:
        code, message = _safe_error(exc)
        result = {
            "status": "failed",
            "error_code": code[:64],
            "error_message": message,
            "x_write_attempted": False,
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
