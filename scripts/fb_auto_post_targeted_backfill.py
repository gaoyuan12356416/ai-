#!/usr/bin/env python3
"""Validate and enqueue one exact historical FB Page backfill.

This command never calls Graph directly.  A successful apply only creates a
manual run which the existing prepare/execute/reconcile state machine owns.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.fb_auto_posts.core import (  # noqa: E402
    ActorScope,
    FBAutoPostStore,
    StoreError,
    utc_iso,
)
from features.fb_auto_posts.repositories import (  # noqa: E402
    MaterialRepository,
    PagePoolRepository,
    RepositoryError,
)
from features.fb_auto_posts.service import build_runtime  # noqa: E402


UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
REPORT_ROOT = Path("/mnt/data-disk/fb-auto-post-publisher/recoveries")
LOCK_PATH = Path("/run/fb-auto-post/targeted-backfill.lock")
RECOVERY_VERSION = 1
_PAGE_ID = re.compile(r"\A[1-9][0-9]{3,40}\Z")
_OPERATION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{7,63}\Z")
_FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")


class RecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(message)


def normalize_page_ids(values: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise RecoveryError("fb_auto_backfill_page_scope_invalid", "回补Page范围无效")
    result = tuple(sorted(str(item or "").strip() for item in values))
    if len(result) != len(set(result)) or not all(_PAGE_ID.fullmatch(item) for item in result):
        raise RecoveryError("fb_auto_backfill_page_scope_invalid", "回补Page范围无效")
    return result


def normalize_operation_id(value: Any) -> str:
    operation = str(value or "").strip()
    if not _OPERATION_ID.fullmatch(operation):
        raise RecoveryError("fb_auto_backfill_operation_invalid", "回补操作号无效")
    return operation


def normalize_utc(value: Any, *, code: str) -> str:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise RecoveryError(code, "来源发布时间无效")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def normalize_beijing_date(value: Any) -> str:
    try:
        parsed = date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise RecoveryError("fb_auto_backfill_date_invalid", "来源北京时间日期无效") from None
    return parsed.isoformat()


def recovery_slot_key(source_run_id: int, operation_id: str) -> str:
    slot = f"manual-backfill:v{RECOVERY_VERSION}:s{int(source_run_id)}:{operation_id}"
    if len(slot) > 120:
        raise RecoveryError("fb_auto_backfill_operation_invalid", "回补操作号过长")
    return slot


def _canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rows_by_page(rows: Sequence[Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for row in rows:
        page_id = str(row.page_id)
        if page_id in result:
            raise RecoveryError("fb_auto_backfill_page_scope_changed", "当前模板Page范围存在重复，未创建回补")
        result[page_id] = row
    return result


def inspect_backfill(
    store: FBAutoPostStore,
    pages: PagePoolRepository,
    *,
    source_run_id: int,
    expected_source_planned_at_utc: str,
    expected_beijing_date: str,
    page_ids: Sequence[str],
    operation_id: str,
    now: datetime | None = None,
) -> Dict[str, Any]:
    try:
        source_id = int(source_run_id)
    except (TypeError, ValueError, OverflowError):
        source_id = 0
    if source_id <= 0:
        raise RecoveryError("fb_auto_backfill_source_invalid", "来源运行ID无效")
    targets = normalize_page_ids(page_ids)
    operation = normalize_operation_id(operation_id)
    planned_at = normalize_utc(
        expected_source_planned_at_utc,
        code="fb_auto_backfill_source_time_invalid",
    )
    expected_day = normalize_beijing_date(expected_beijing_date)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    slot_key = recovery_slot_key(source_id, operation)

    with store.connect() as conn:
        source = conn.execute(
            """
            SELECT r.*,t.status AS template_status,t.current_version,
                   t.owner_user_id,t.scope_is_admin,
                   v.config_json AS current_config_json
              FROM fb_auto_run r
              JOIN fb_auto_template t ON t.id=r.template_id
              JOIN fb_auto_template_version v
                ON v.template_id=t.id AND v.version=t.current_version
             WHERE r.id=?
            """,
            (source_id,),
        ).fetchone()
        if source is None:
            raise RecoveryError("fb_auto_backfill_source_not_found", "来源运行不存在", 404)
        stored_planned_at = normalize_utc(
            source["planned_publish_at_utc"],
            code="fb_auto_backfill_source_time_invalid",
        )
        source_day = datetime.fromisoformat(stored_planned_at).astimezone(BEIJING).date().isoformat()
        today = current.astimezone(BEIJING).date().isoformat()
        if (
            str(source["trigger_type"]) != "auto"
            or str(source["status"]) != "completed"
            or stored_planned_at != planned_at
            or source_day != expected_day
            or expected_day != today
        ):
            raise RecoveryError("fb_auto_backfill_source_mismatch", "来源运行、发布时间或今天日期不匹配，未创建回补", 409)
        if (
            str(source["template_status"]) != "enabled"
            or int(source["template_version"]) != int(source["current_version"])
            or str(source["config_json"]) != str(source["current_config_json"])
        ):
            raise RecoveryError("fb_auto_backfill_template_changed", "来源模板已停用或版本已变化，未创建回补", 409)

        source_tasks = conn.execute(
            "SELECT id,page_id,status,skip_reason,attempt_count,graph_post_id,unknown_outcome FROM fb_auto_task WHERE run_id=? ORDER BY page_id",
            (source_id,),
        ).fetchall()
        missing_tasks = [
            row for row in source_tasks
            if str(row["skip_reason"]) == "fb_page_missing_eligible_token"
        ]
        missing_ids = tuple(sorted(str(row["page_id"]) for row in missing_tasks))
        if missing_ids != targets or len(missing_tasks) != len(targets):
            raise RecoveryError("fb_auto_backfill_source_scope_mismatch", "目标Page与来源运行全部缺凭证Page不完全一致，未创建回补", 409)
        for row in missing_tasks:
            if (
                str(row["status"]) != "skipped"
                or int(row["attempt_count"]) != 0
                or str(row["graph_post_id"] or "")
                or int(row["unknown_outcome"]) != 0
            ):
                raise RecoveryError("fb_auto_backfill_source_not_pristine", "来源任务已有尝试或非安全跳过状态，未创建回补", 409)
        task_ids = [int(row["id"]) for row in missing_tasks]
        placeholders = ",".join("?" for _ in task_ids)
        attempts = int(conn.execute(
            f"SELECT COUNT(*) FROM fb_auto_publish_attempt WHERE task_id IN ({placeholders})",
            task_ids,
        ).fetchone()[0])
        ledger = int(conn.execute(
            f"SELECT COUNT(*) FROM fb_auto_publish_ledger WHERE task_id IN ({placeholders})",
            task_ids,
        ).fetchone()[0])
        if attempts or ledger:
            raise RecoveryError("fb_auto_backfill_source_not_pristine", "来源任务已有尝试或发布账本，未创建回补", 409)

        recovery_prefix = f"manual-backfill:v{RECOVERY_VERSION}:s{source_id}:"
        recovery_runs = conn.execute(
            "SELECT id,slot_key,status FROM fb_auto_run WHERE template_id=? AND slot_key LIKE ? ORDER BY id",
            (int(source["template_id"]), recovery_prefix + "%"),
        ).fetchall()
        exact_runs = [row for row in recovery_runs if str(row["slot_key"]) == slot_key]
        other_runs = [row for row in recovery_runs if str(row["slot_key"]) != slot_key]
        if other_runs or len(exact_runs) > 1:
            raise RecoveryError("fb_auto_backfill_already_exists", "该来源运行已存在其他回补，拒绝重复回补", 409)
        existing_run_id = int(exact_runs[0]["id"]) if exact_runs else 0
        if existing_run_id:
            existing_ids = tuple(sorted(str(row[0]) for row in conn.execute(
                "SELECT page_id FROM fb_auto_task WHERE run_id=? ORDER BY page_id",
                (existing_run_id,),
            )))
            if existing_ids != targets:
                raise RecoveryError("fb_auto_backfill_existing_scope_mismatch", "既有回补运行的Page范围异常，需人工处理", 409)

        now_text = utc_iso(current)
        conflicting = conn.execute(
            f"""
            SELECT x.run_id,x.page_id,x.status,x.attempt_count,x.unknown_outcome
              FROM fb_auto_task x
             WHERE x.run_id<>?
               AND x.page_id IN ({','.join('?' for _ in targets)})
               AND x.planned_publish_at_utc>=?
               AND x.planned_publish_at_utc<=?
               AND (x.status IN ('running','submitted','unknown','published') OR x.attempt_count>0)
             ORDER BY x.run_id,x.page_id
            """,
            (source_id, *targets, planned_at, now_text),
        ).fetchall()
        if conflicting and not existing_run_id:
            raise RecoveryError("fb_auto_backfill_target_already_attempted", "目标Page在来源时隙后已有发布尝试，拒绝重复回补", 409)
        backlog = conn.execute(
            """
            SELECT DISTINCT r.id
              FROM fb_auto_run r
              JOIN fb_auto_task x ON x.run_id=r.id
             WHERE r.template_id=?
               AND x.status IN ('planned','preparing','ready','running')
               AND x.planned_publish_at_utc<=?
             ORDER BY r.id
            """,
            (int(source["template_id"]), now_text),
        ).fetchall()
        if backlog and not existing_run_id:
            raise RecoveryError("fb_auto_previous_run_backlog", "当前已有到期Page任务未完成，暂不叠加回补", 409)

        config = json.loads(str(source["config_json"]))
        source_snapshot = {
            "source_run_id": source_id,
            "template_id": int(source["template_id"]),
            "template_version": int(source["template_version"]),
            "planned_publish_at_utc": planned_at,
            "beijing_date": expected_day,
            "operation_id": operation,
            "slot_key": slot_key,
            "target_page_ids": list(targets),
            "source_task_ids": sorted(task_ids),
        }
        actor = ActorScope(
            "fb-auto-targeted-backfill",
            "FB定向回补",
            bool(source["scope_is_admin"]),
            str(source["owner_user_id"]),
        )

    current_rows = pages.list_pages(
        config["group_ids"],
        is_admin=actor.is_admin,
        owner_user_id=actor.owner_user_id,
    )
    current_by_page = _rows_by_page(current_rows)
    absent = sorted(set(targets).difference(current_by_page))
    if absent:
        raise RecoveryError("fb_auto_backfill_page_scope_changed", "目标Page已不在当前模板范围，未创建回补", 409)
    page_snapshot = [
        {
            "page_id": page_id,
            "group_id": str(current_by_page[page_id].group_id),
            "group_ids": sorted(str(item) for item in current_by_page[page_id].group_ids),
            "eligible_token_count": int(current_by_page[page_id].eligible_token_count),
        }
        for page_id in targets
    ]
    fingerprint_payload = {
        "schema_version": RECOVERY_VERSION,
        "source": source_snapshot,
        "page_snapshot": page_snapshot,
        "existing_run_id": existing_run_id,
    }
    fingerprint = _canonical_fingerprint(fingerprint_payload)
    return {
        "source": source_snapshot,
        "actor": actor,
        "config": config,
        "target_page_ids": targets,
        "page_snapshot": page_snapshot,
        "eligible_page_count": sum(item["eligible_token_count"] > 0 for item in page_snapshot),
        "blocked_page_count": sum(item["eligible_token_count"] <= 0 for item in page_snapshot),
        "existing_run_id": existing_run_id,
        "fingerprint": fingerprint,
        "fingerprint_payload": fingerprint_payload,
    }


def execute_backfill(
    store: FBAutoPostStore,
    pages: PagePoolRepository,
    materials: MaterialRepository,
    *,
    source_run_id: int,
    expected_source_planned_at_utc: str,
    expected_beijing_date: str,
    page_ids: Sequence[str],
    operation_id: str,
    apply: bool = False,
    expected_fingerprint: str = "",
    now: datetime | None = None,
    capacity_limits: Mapping[str, int] | None = None,
) -> Dict[str, Any]:
    if not isinstance(apply, bool):
        raise RecoveryError("fb_auto_backfill_mode_invalid", "回补执行模式无效")
    expected = str(expected_fingerprint or "").strip().lower()
    if apply and not _FINGERPRINT.fullmatch(expected):
        raise RecoveryError("fb_auto_backfill_fingerprint_required", "真实回补必须提供有效dry-run指纹")
    if not apply and expected:
        raise RecoveryError("fb_auto_backfill_fingerprint_unexpected", "dry-run不应提供执行指纹")
    current = now or datetime.now(UTC)
    inspection = inspect_backfill(
        store,
        pages,
        source_run_id=source_run_id,
        expected_source_planned_at_utc=expected_source_planned_at_utc,
        expected_beijing_date=expected_beijing_date,
        page_ids=page_ids,
        operation_id=operation_id,
        now=current,
    )
    public = {
        "source_run_id": inspection["source"]["source_run_id"],
        "template_id": inspection["source"]["template_id"],
        "template_version": inspection["source"]["template_version"],
        "source_planned_publish_at_utc": inspection["source"]["planned_publish_at_utc"],
        "source_beijing_date": inspection["source"]["beijing_date"],
        "operation_id": inspection["source"]["operation_id"],
        "slot_key": inspection["source"]["slot_key"],
        "target_page_ids": list(inspection["target_page_ids"]),
        "page_snapshot": inspection["page_snapshot"],
        "eligible_page_count": inspection["eligible_page_count"],
        "blocked_page_count": inspection["blocked_page_count"],
        "fingerprint": inspection["fingerprint"],
    }
    if not apply:
        return {"ok": True, "status": "validated", "validate_only": True, **public}
    if inspection["existing_run_id"]:
        return {
            "ok": True,
            "status": "already_created",
            "validate_only": False,
            "idempotent": True,
            "run_id": inspection["existing_run_id"],
            **public,
        }
    if expected != inspection["fingerprint"]:
        raise RecoveryError("fb_auto_backfill_fingerprint_changed", "回补范围或实时状态已变化，请重新执行dry-run", 409)
    limits = dict(capacity_limits or {})
    allowed_limit_names = {"max_publishable_pages", "max_jobs_per_slot", "max_daily_jobs"}
    if set(limits).difference(allowed_limit_names):
        raise RecoveryError("fb_auto_backfill_capacity_invalid", "回补容量参数无效", 500)
    result = store.create_run(
        inspection["source"]["template_id"],
        inspection["source"]["slot_key"],
        "manual",
        inspection["actor"],
        pages,
        materials,
        planned_publish_at_utc=utc_iso(current),
        required_template_version=inspection["source"]["template_version"],
        target_page_ids=inspection["target_page_ids"],
        **limits,
    )
    run_id = int(result["run_id"])
    with store.connect() as conn:
        tasks = conn.execute(
            "SELECT page_id,status,skip_reason,material_id,content_id FROM fb_auto_task WHERE run_id=? ORDER BY page_id",
            (run_id,),
        ).fetchall()
    actual_ids = tuple(str(row["page_id"]) for row in tasks)
    if actual_ids != inspection["target_page_ids"]:
        raise RecoveryError("fb_auto_backfill_created_scope_mismatch", "回补建单范围异常，已停止后续操作", 500)
    return {
        "ok": True,
        "status": "created",
        "validate_only": False,
        "idempotent": bool(result.get("idempotent")),
        "run_id": run_id,
        "run_summary": dict(result.get("summary") or {}),
        "tasks": [dict(row) for row in tasks],
        **public,
    }


def _validate_report_path(path: Any, report_root: Path | None = None) -> Path:
    supplied = Path(str(path or ""))
    root = Path(report_root or REPORT_ROOT)
    if not supplied.is_absolute() or not root.is_absolute() or supplied.suffix.lower() != ".json":
        raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告必须是专用目录下的绝对JSON路径")
    lexical_root = Path(os.path.abspath(root))
    lexical_target = Path(os.path.abspath(supplied))
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError:
        relative = None
    if relative is None or not relative.parts:
        raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告路径超出专用目录")
    if not lexical_root.exists() or not lexical_root.is_dir() or lexical_root.is_symlink():
        raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告目录不可用或不安全")
    cursor = lexical_target.parent
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告路径包含软链接")
        if cursor == lexical_root:
            break
        if cursor == cursor.parent:
            raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告路径超出专用目录")
        cursor = cursor.parent
    resolved_root = lexical_root.resolve(strict=True)
    resolved_target = lexical_target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告路径解析到专用目录外") from None
    if (
        not lexical_target.parent.exists()
        or not lexical_target.parent.is_dir()
        or lexical_target.exists()
        or lexical_target.is_symlink()
    ):
        raise RecoveryError("fb_auto_backfill_report_path_invalid", "审计报告必须写入已存在目录中的新文件")
    return resolved_target


def _atomic_write_report(path: Path, result: Mapping[str, Any]) -> None:
    target = _validate_report_path(path)
    payload = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".fb-backfill-", suffix=".tmp", dir=target.parent)
        temporary_path = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary_path, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, target)
        if os.name != "nt":
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextlib.contextmanager
def process_lock(path: Path = LOCK_PATH):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验或创建一个精确Page白名单的FB历史回补运行；不直接调用Graph。")
    parser.add_argument("--source-run-id", required=True, type=int)
    parser.add_argument("--expected-source-planned-at-utc", required=True)
    parser.add_argument("--expected-beijing-date", required=True)
    parser.add_argument("--page-id", action="append", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--report-path")
    return parser


def _safe_failure(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, (RecoveryError, StoreError, RepositoryError)):
        return {
            "ok": False,
            "status": "failed",
            "error_code": str(exc.code),
            "error_message": str(exc),
        }
    return {
        "ok": False,
        "status": "failed",
        "error_code": "fb_auto_backfill_unexpected_error",
        "error_message": type(exc).__name__,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report_target: Path | None = None
    try:
        if args.apply and not args.report_path:
            raise RecoveryError("fb_auto_backfill_report_required", "真实回补必须提供独立审计报告路径")
        if args.report_path:
            report_target = _validate_report_path(args.report_path)
        with process_lock() as acquired:
            if not acquired:
                raise RecoveryError("fb_auto_backfill_locked", "另一个定向回补正在执行，请稍后重试", 409)
            runtime = build_runtime()
            if not runtime.prebuild_enabled or not runtime.executor.live_enabled:
                raise RecoveryError("fb_auto_backfill_live_gate_closed", "FB预制或真实发布门禁未开启，未创建回补", 409)
            result = execute_backfill(
                runtime.store,
                runtime.pages,
                runtime.materials,
                source_run_id=args.source_run_id,
                expected_source_planned_at_utc=args.expected_source_planned_at_utc,
                expected_beijing_date=args.expected_beijing_date,
                page_ids=args.page_id,
                operation_id=args.operation_id,
                apply=bool(args.apply),
                expected_fingerprint=str(args.expected_fingerprint or ""),
                now=datetime.now(UTC),
                capacity_limits={
                    "max_publishable_pages": runtime.max_publishable_pages,
                    "max_jobs_per_slot": runtime.max_jobs_per_slot,
                    "max_daily_jobs": runtime.max_daily_jobs,
                },
            )
    except Exception as exc:
        result = _safe_failure(exc)
    if report_target is not None:
        try:
            _atomic_write_report(report_target, result)
            result = {**result, "report_path": str(report_target), "report_status": "written"}
        except Exception as exc:
            result = {
                **result,
                "report_status": "failed",
                "report_error": type(exc).__name__,
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True and result.get("report_status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
