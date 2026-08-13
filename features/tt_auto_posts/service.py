"""Loopback admin API and scheduler for TT automatic publishing templates."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import signal
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from features.tt_posts.service import (
    DramawaveMaterialResolver,
    GPUClient,
    MySQLSnapshotAccountRepository,
    SnapshotMySQLConfig,
)
from features.x_posts.selector import connect_read_only

from .client import (
    TT_AUTO_ADMIN_PREFIX,
    contains_sensitive_key,
    safe_public_message,
)
from .code_broker import synthetic_queue_id
from .code_broker_client import AutoCodeBrokerClient, DEFAULT_BROKER_URL
from .core import AuditActor, PUBLISH_LOG_STATUS_GROUPS, TTPostAutoStore
from .legacy_reader import LegacyTTPostReader, LegacyTTPostReaderError
from .publisher import (
    DIRECT_OUTRO_MEDIA_PROFILE,
    RANDOM_OVERLAY_MEDIA_PROFILE,
    AutoLiveGates,
    AutoPostExecutor,
    VideoTemplateRoute,
    selector_rules,
)
from .repositories import (
    BEIJING_TZ,
    MetricWindowRepository,
    ReadOnlyMySQLRepository,
)
from .selector import (
    ResolverMaterialValidator,
    SelectionRequest,
    TwoStageSelector,
)
from .validation import (
    VIDEO_TEMPLATE_DIRECT_OUTRO,
    VIDEO_TEMPLATE_RANDOM_OVERLAY,
    ValidationError,
    expected_version,
    normalize_template_payload,
    valid_internal_bearer,
)


UTC = timezone.utc
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18831
MAX_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AutoPostServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "tt_auto_post_error")[:96]
        self.status = int(status)
        super().__init__(str(message or "TT 自动发布请求失败")[:500])


def _positive_id(value: Any, label: str = "ID") -> int:
    if isinstance(value, bool):
        raise AutoPostServiceError("invalid_request", f"{label}无效", 400)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = 0
    if result <= 0:
        raise AutoPostServiceError("invalid_request", f"{label}无效", 400)
    return result


def _actor(payload: Dict[str, Any]) -> AuditActor:
    raw = payload.pop("_actor", {})
    if not isinstance(raw, Mapping) or set(raw).difference({"user_id", "name"}):
        raise AutoPostServiceError("invalid_request", "操作人信息无效", 400)
    return AuditActor.from_values(raw.get("user_id"), raw.get("name"))


def _query_one(query: Mapping[str, Sequence[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if values is None:
        return default
    if not isinstance(values, list) or len(values) != 1:
        raise AutoPostServiceError("invalid_request", "查询参数无效", 400)
    return str(values[0] or "").strip()


def _limit_offset(query: Mapping[str, Sequence[str]]) -> Tuple[int, int]:
    try:
        limit = int(_query_one(query, "limit", "50"))
        offset = int(_query_one(query, "offset", "0"))
    except ValueError:
        raise AutoPostServiceError("invalid_request", "分页参数无效", 400) from None
    if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
        raise AutoPostServiceError("invalid_request", "分页参数无效", 400)
    return limit, offset


def _publish_log_limit_offset(
    query: Mapping[str, Sequence[str]],
) -> Tuple[int, int]:
    limit, offset = _limit_offset(query)
    if offset > 10_000:
        raise AutoPostServiceError(
            "invalid_request", "发布日志最多翻阅前10000条", 400
        )
    return limit, offset


def _beijing_date_boundary(value: str, *, next_day: bool = False) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value or "")):
        raise AutoPostServiceError("invalid_request", "运行日期筛选无效", 400)
    try:
        local = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        raise AutoPostServiceError("invalid_request", "运行日期筛选无效", 400) from None
    if next_day:
        local += timedelta(days=1)
    return local.astimezone(UTC).isoformat(timespec="seconds")


def _publish_log_order(item: Mapping[str, Any]) -> Tuple[float, str, int]:
    raw = str(item.get("task_at_utc") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
        timestamp = parsed.astimezone(UTC).timestamp()
    except (OverflowError, TypeError, ValueError):
        timestamp = float("-inf")
    return (
        timestamp,
        str(item.get("publish_source") or ""),
        int(item.get("task_id") or 0),
    )


def _blacklist_dict(snapshot: Any) -> Dict[str, Any]:
    return {
        "drama_series_codes": sorted(snapshot.drama_series_codes),
        "material_data_source_ids": sorted(snapshot.material_data_source_ids),
        "loaded_at_utc": snapshot.loaded_at_utc,
        "source_row_count": snapshot.source_row_count,
        "sha256": snapshot.sha256,
    }


def _public_selection(value: Any) -> Any:
    """Remove source/prepared media URLs from browser-facing snapshots."""

    if isinstance(value, Mapping):
        return {
            str(key): _public_selection(item)
            for key, item in value.items()
            if str(key) not in {"media_url", "source_media_url", "prepared_media_url"}
        }
    if isinstance(value, list):
        return [_public_selection(item) for item in value]
    return value


class _PreviewStore:
    def __init__(self, store: TTPostAutoStore):
        self.store = store

    def get_task_reservation(self, _task_id: int):
        return None

    def reserved_material_ids(self, material_ids):
        return self.store.reserved_material_ids(material_ids)

    def cooldown_content_ids(self, *args, **kwargs):
        return self.store.cooldown_content_ids(*args, **kwargs)

    @staticmethod
    def reserve_material(**kwargs):
        return {
            "preview": True,
            "material_id": str(kwargs.get("material_id") or ""),
            "content_id": str(kwargs.get("content_id") or ""),
            "task_id": int(kwargs.get("task_id") or 1),
            "run_id": int(kwargs.get("run_id") or 1),
            "template_id": int(kwargs.get("template_id") or 1),
            "reserved_at_utc": str(kwargs.get("reserved_at_utc") or ""),
        }


class TTAutoPostService:
    def __init__(
        self,
        store: TTPostAutoStore,
        legacy_reader: LegacyTTPostReader,
        account_repository: MySQLSnapshotAccountRepository,
        selector: TwoStageSelector,
        executor: AutoPostExecutor,
        *,
        now_fn=lambda: datetime.now(UTC),
        runner_kick_path: Any = "/run/tt-auto-post/manual-kick",
        schedule_grace_seconds: int = 600,
        prepare_ahead_seconds: int = 0,
    ):
        self.store = store
        self.legacy_reader = legacy_reader
        self.account_repository = account_repository
        self.account_source = account_repository.as_account_source()
        self.selector = selector
        self.executor = executor
        self.now_fn = now_fn
        self.runner_kick_path = str(runner_kick_path or "").strip()
        self.schedule_grace_seconds = int(schedule_grace_seconds)
        self.prepare_ahead_seconds = int(prepare_ahead_seconds)
        self._schedule_lock = threading.Lock()
        self._run_create_lock = threading.Lock()
        if not 60 <= self.schedule_grace_seconds <= 3600:
            raise AutoPostServiceError(
                "tt_auto_schedule_config_invalid", "调度宽限时间无效", 500
            )
        if not 0 <= self.prepare_ahead_seconds <= 43200:
            raise AutoPostServiceError(
                "tt_auto_schedule_config_invalid",
                "prepare ahead window is invalid",
                500,
            )

    def health(self) -> Dict[str, Any]:
        summaries = getattr(self.executor, "video_template_summaries", None)
        video_templates = (
            summaries()
            if callable(summaries)
            else [
                {
                    "key": VIDEO_TEMPLATE_RANDOM_OVERLAY,
                    "profile": self.executor.media_profile_version,
                    "source_trim_tail_seconds": (
                        self.executor.source_trim_tail_seconds
                    ),
                }
            ]
        )
        return {
            "ok": True,
            "service": "tt-auto-post",
            "gates": self.executor.gates.as_dict(),
            "profile": self.executor.media_profile_version,
            "source_trim_tail_seconds": self.executor.source_trim_tail_seconds,
            "prepare_ahead_seconds": self.prepare_ahead_seconds,
            "video_templates": video_templates,
        }

    def _now(self) -> datetime:
        value = self.now_fn()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutoPostServiceError("tt_auto_clock_invalid", "服务时钟无效", 500)
        return value.astimezone(UTC)

    @staticmethod
    def _template_item(snapshot: Any) -> Dict[str, Any]:
        config = dict(snapshot.config)
        schedule = config.get("schedule") if isinstance(config.get("schedule"), Mapping) else {}
        if schedule.get("mode") == "fixed":
            schedule_summary = "、".join(schedule.get("times") or [])
        else:
            schedule_summary = "随机 %s 次/天" % schedule.get("daily_count", 0)
        return {
            "id": snapshot.id,
            "template_id": snapshot.id,
            "name": snapshot.name,
            "description": snapshot.description,
            "enabled": bool(snapshot.enabled),
            "enabled_at_utc": snapshot.enabled_at_utc,
            "status": "enabled" if snapshot.enabled else "disabled",
            "version": snapshot.version,
            "current_version": snapshot.version,
            "config": config,
            "account_count": len(config.get("account_ids") or []),
            "schedule_summary": schedule_summary,
            "confirmed": bool(snapshot.confirmed),
            "config_sha256": snapshot.config_sha256,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
        }

    @staticmethod
    def _run_item(run: Any, *, template: Optional[Any] = None, tasks=None) -> Dict[str, Any]:
        item = run.as_dict()
        item["error_message"] = (
            safe_public_message(item.get("error_message"))
            if item.get("error_message")
            else ""
        )
        snapshot = item.get("blacklist_snapshot")
        if isinstance(snapshot, Mapping):
            item["blacklist_snapshot"] = {
                "loaded_at_utc": str(snapshot.get("loaded_at_utc") or ""),
                "source_row_count": int(snapshot.get("source_row_count") or 0),
                "sha256": str(snapshot.get("sha256") or ""),
            }
        item["run_id"] = run.id
        if template is not None:
            item["template_name"] = template.name
            item["template_snapshot"] = TTAutoPostService._template_item(template)
        if tasks is not None:
            values = list(tasks)
            item["task_count"] = len(values)
            item["completed_task_count"] = sum(
                task.status in {"published", "no_candidate", "skipped"}
                for task in values
            )
            item["failed_task_count"] = sum(
                task.status in {"failed", "canceled"} for task in values
            )
        return item

    @staticmethod
    def _task_item(task: Any) -> Dict[str, Any]:
        item = task.as_dict()
        item["prepared"] = bool(item.get("prepared_media_url"))
        item["selection"] = _public_selection(item.get("selection"))
        item.pop("source_media_url", None)
        item.pop("prepared_media_url", None)
        publish_url = str(item.get("publish_url") or "").strip()
        if publish_url:
            try:
                parsed = urlsplit(publish_url)
                host = str(parsed.hostname or "").lower()
                trusted = (
                    parsed.scheme == "https"
                    and (host == "tiktok.com" or host.endswith(".tiktok.com"))
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.port in (None, 443)
                    and not parsed.query
                    and not parsed.fragment
                )
            except ValueError:
                trusted = False
            if not trusted:
                item["publish_url"] = ""
        return item

    def accounts(self) -> Dict[str, Any]:
        items = []
        for account in self.account_repository.list_public_accounts():
            account_id = str(account.get("source_account_id") or account.get("account_id") or "")
            item = dict(account)
            try:
                setting = self.legacy_reader.get_account_setting(account_id)
            except Exception as exc:
                item.update(
                    {
                        "drama_language": "",
                        "settings_ready": False,
                        "account_settings_configured": False,
                        "settings_error_code": str(
                            getattr(exc, "code", "tt_auto_account_setting_unavailable")
                        ),
                    }
                )
            else:
                item.update(
                    {
                        "drama_language": setting.drama_language,
                        "settings_ready": True,
                        "account_settings_configured": True,
                        "account_setting_version": setting.version,
                    }
                )
            items.append(item)
        return {"ok": True, "accounts": items, "total": len(items)}

    def templates(self, query: Mapping[str, Sequence[str]]) -> Dict[str, Any]:
        status = _query_one(query, "status")
        if status not in {"", "enabled", "disabled"}:
            raise AutoPostServiceError("invalid_request", "模板状态无效", 400)
        enabled = None if not status else status == "enabled"
        q = _query_one(query, "q").casefold()
        limit, offset = _limit_offset(query)
        values = self.store.list_templates(enabled=enabled)
        if q:
            values = [item for item in values if q in item.name.casefold()]
        total = len(values)
        page = values[offset : offset + limit]
        all_values = self.store.list_templates()
        return {
            "ok": True,
            "templates": [self._template_item(item) for item in page],
            "total": total,
            "summary": {
                "enabled": sum(item.enabled for item in all_values),
                "disabled": sum(not item.enabled for item in all_values),
                "running": sum(
                    run.status in {"queued", "running"}
                    for run in self.store.list_runs()
                ),
            },
        }

    def template(self, template_id: Any) -> Dict[str, Any]:
        return {
            "ok": True,
            "template": self._template_item(self.store.get_template(template_id)),
        }

    def _validate_accounts(self, config: Mapping[str, Any]) -> None:
        for account_id in config.get("account_ids") or []:
            self.account_repository.get_public_account(account_id)
            self.legacy_reader.get_account_setting(account_id)

    @staticmethod
    def _confirmation(actor: AuditActor, now: datetime) -> Dict[str, Any]:
        return {
            "accepted": True,
            "scope": "tt_auto_publish_template_version",
            "confirmed_by_user_id": actor.user_id,
            "confirmed_at_utc": now.isoformat(timespec="seconds"),
        }

    def create_template(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        normalized = normalize_template_payload(payload)
        name = normalized.pop("name")
        self._validate_accounts(normalized)
        item = self.store.create_template(
            name=name,
            config=normalized,
            actor=actor,
            confirmation=self._confirmation(actor, self._now()),
        )
        return {"ok": True, "template": self._template_item(item)}

    def update_template(self, template_id: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        version = expected_version(payload.pop("expected_version", None))
        normalized = normalize_template_payload(payload)
        name = normalized.pop("name")
        self._validate_accounts(normalized)
        item = self.store.update_template(
            template_id,
            expected_version=version,
            config=normalized,
            name=name,
            actor=actor,
            confirmation=self._confirmation(actor, self._now()),
        )
        return {"ok": True, "template": self._template_item(item)}

    def copy_template(self, template_id: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        if set(payload).difference({"expected_version", "name"}):
            raise AutoPostServiceError("invalid_request", "复制参数无效", 400)
        source = self.store.get_template(template_id)
        if expected_version(payload.get("expected_version")) != source.version:
            raise AutoPostServiceError(
                "tt_auto_template_version_conflict", "模板版本已变化", 409
            )
        raw_name = payload.get("name")
        name = str(raw_name).strip() if raw_name not in (None, "") else None
        item = self.store.copy_template(template_id, name=name, actor=actor)
        item = self.store.confirm_template_version(
            item.id,
            item.version,
            confirmation=self._confirmation(actor, self._now()),
            actor=actor,
        )
        return {"ok": True, "template": self._template_item(item)}

    def set_enabled(
        self, template_id: Any, enabled: bool, raw: Mapping[str, Any]
    ) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        if set(payload) != {"expected_version"}:
            raise AutoPostServiceError("invalid_request", "启停参数无效", 400)
        version = expected_version(payload.get("expected_version"))
        template = self.store.get_template(template_id)
        if template.version != version:
            raise AutoPostServiceError(
                "tt_auto_template_version_conflict", "模板版本已变化", 409
            )
        if bool(template.enabled) is bool(enabled):
            return {"ok": True, "template": self._template_item(template)}
        if enabled:
            self._validate_accounts(template.config)
        item = self.store.set_template_enabled(
            template_id,
            enabled=bool(enabled),
            expected_version=version,
            actor=actor,
        )
        return {"ok": True, "template": self._template_item(item)}

    def _preview_selector(self) -> TwoStageSelector:
        return TwoStageSelector(
            self.selector.source,
            self.selector.metrics,
            self.selector.legacy_reader,
            _PreviewStore(self.store),
            material_validator=self.selector.material_validator,
            product=self.selector.product,
            app_id=self.selector.app_id,
            material_data_source=self.selector.material_data_source,
        )

    def preview(self, template_id: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        _actor(payload)
        if set(payload).difference({"expected_version", "account_id"}):
            raise AutoPostServiceError("invalid_request", "预览参数无效", 400)
        template = self.store.get_template(template_id)
        if expected_version(payload.get("expected_version")) != template.version:
            raise AutoPostServiceError(
                "tt_auto_template_version_conflict", "模板版本已变化", 409
            )
        requested = str(payload.get("account_id") or "").strip()
        account_ids = list(template.config.get("account_ids") or [])
        if requested:
            if requested not in account_ids:
                raise AutoPostServiceError(
                    "invalid_request", "预览账号不属于模板", 400
                )
            account_ids = [requested]
        results = []
        for index, account_id in enumerate(account_ids, start=1):
            try:
                setting = self.legacy_reader.get_account_setting(account_id)
                selection = self._preview_selector().select_and_reserve(
                    SelectionRequest(
                        run_id=index,
                        task_id=index,
                        template_id=template.id,
                        template_version=template.version,
                        account_id=account_id,
                        language=setting.drama_language,
                        rules=selector_rules(template.config),
                        now=self._now(),
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "account_id": account_id,
                        "ok": False,
                        "error_code": str(
                            getattr(exc, "code", "tt_auto_preview_failed")
                        ),
                        "error_message": safe_public_message(exc),
                    }
                )
            else:
                results.append(
                    {
                        "account_id": account_id,
                        "ok": True,
                        "selection": _public_selection(selection.as_dict()),
                    }
                )
        return {"ok": True, "preview": results, "reserved": False}

    def _run_tasks(self, run: Any, config: Mapping[str, Any]) -> List[Any]:
        existing = {
            task.account_id: task for task in self.store.list_tasks(run_id=run.id)
        }
        tasks = []
        for account_id in config.get("account_ids") or []:
            if account_id in existing:
                tasks.append(existing[account_id])
                continue
            try:
                public_account = self.account_repository.get_public_account(account_id)
                setting = self.legacy_reader.get_account_setting(account_id)
            except Exception as exc:
                task = self.store.create_task(
                    run_id=run.id,
                    account_id=account_id,
                    drama_language="und",
                    account_settings={"configured": False},
                    account_setting_version=0,
                )
                task = self.store.transition_task(
                    task.id,
                    "failed",
                    expected_statuses={"pending"},
                    updates={
                        "error_code": str(
                            getattr(exc, "code", "tt_auto_account_unavailable")
                        ),
                        "error_message": safe_public_message(exc),
                    },
                    event_type="task_account_snapshot_failed",
                )
            else:
                settings = setting.as_dict()
                settings.pop("account_id", None)
                settings.pop("updated_at", None)
                settings.pop("version", None)
                settings.pop("drama_language", None)
                task = self.store.create_task(
                    run_id=run.id,
                    account_id=account_id,
                    account_username=str(public_account.get("username") or ""),
                    account_display_name=str(
                        public_account.get("display_name") or ""
                    ),
                    drama_language=setting.drama_language,
                    account_settings=settings,
                    account_setting_version=setting.version,
                )
            tasks.append(task)
        if tasks and all(task.status == "failed" for task in tasks):
            current = self.store.get_run(run.id)
            if current.status == "queued":
                self.store.set_run_status(
                    run.id, "failed", expected_statuses={"queued"}
                )
        return tasks

    def _create_run(
        self,
        template: Any,
        *,
        trigger_type: str,
        scheduled_at: datetime,
        publish_time: str,
        run_key: str,
        actor: AuditActor = AuditActor(),
    ) -> Tuple[Any, bool]:
        # Serialize manual and scheduled creation so the first caller freezes
        # the blacklist/account facts.  Replays return before any external
        # read, which keeps an existing run executable during source outages.
        with self._run_create_lock:
            existing = self.store.get_run_by_key(run_key)
            if existing is not None:
                frozen_template = self.store.get_template(
                    existing.template_id, version=existing.template_version
                )
                if (
                    existing.template_id != template.id
                    or existing.trigger_type != trigger_type
                    or (
                        trigger_type != "auto"
                        and (
                            existing.template_version != template.version
                            or frozen_template.config_sha256
                            != template.config_sha256
                        )
                    )
                ):
                    raise AutoPostServiceError(
                        "tt_auto_run_idempotency_conflict",
                        "运行幂等键已绑定到其他事实",
                        409,
                    )
                # Normally complete already; this only repairs a process crash
                # between run insertion and account-task creation, without
                # rereading settings for accounts whose snapshot already exists.
                self._run_tasks(existing, frozen_template.config)
                return self.store.get_run(existing.id), False

            blacklist = self.selector.source.blacklist_snapshot()
            shanghai_date = scheduled_at.astimezone(BEIJING_TZ).date().isoformat()
            run = self.store.create_run(
                run_key=run_key,
                template_id=template.id,
                template_version=template.version,
                trigger_type=trigger_type,
                scheduled_at_utc=scheduled_at.astimezone(UTC),
                shanghai_date=shanghai_date,
                publish_time=publish_time,
                blacklist_snapshot=_blacklist_dict(blacklist),
                actor=actor,
            )
            self._run_tasks(run, template.config)
            return self.store.get_run(run.id), True

    def _kick(self) -> bool:
        if not self.runner_kick_path:
            return False
        try:
            path = Path(self.runner_kick_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            return True
        except OSError:
            return False

    def run_now(self, template_id: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        if set(payload).difference({"expected_version", "confirmed", "idempotency_key"}):
            raise AutoPostServiceError("invalid_request", "立即执行参数无效", 400)
        if payload.get("confirmed") is not True:
            raise AutoPostServiceError(
                "tt_auto_run_confirmation_required", "必须确认立即执行", 409
            )
        template = self.store.get_template(template_id)
        if expected_version(payload.get("expected_version")) != template.version:
            raise AutoPostServiceError(
                "tt_auto_template_version_conflict", "模板版本已变化", 409
            )
        now = self._now()
        request_key = str(payload.get("idempotency_key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{8,128}", request_key):
            raise AutoPostServiceError("invalid_request", "幂等键无效", 400)
        digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:32]
        run, created = self._create_run(
            template,
            trigger_type="manual",
            scheduled_at=now,
            publish_time=now.astimezone(BEIJING_TZ).strftime("%H:%M"),
            run_key=f"tt-auto:manual:v1:{digest}",
            actor=actor,
        )
        return {
            "ok": True,
            "run_id": run.id,
            "run": self._run_item(
                run,
                template=template,
                tasks=self.store.list_tasks(run_id=run.id),
            ),
            "runner_wakeup_requested": self._kick(),
            "idempotent": not created,
            "gates": self.executor.gates.as_dict(),
        }

    @staticmethod
    def _random_times(template: Any, shanghai_date: str, count: int) -> List[str]:
        digest = hashlib.sha256(
            (
                str(template.id)
                + "|"
                + str(template.version)
                + "|"
                + template.config_sha256
                + "|"
                + shanghai_date
            ).encode("utf-8")
        ).digest()
        generator = random.Random(int.from_bytes(digest[:16], "big"))
        minutes = sorted(generator.sample(range(24 * 60), count))
        return ["%02d:%02d" % divmod(value, 60) for value in minutes]

    def _schedule_times(self, template: Any, shanghai_date: str) -> List[str]:
        schedule = template.config.get("schedule")
        if not isinstance(schedule, Mapping):
            raise AutoPostServiceError(
                "tt_auto_schedule_invalid", "模板发布时间无效", 500
            )
        if schedule.get("mode") == "fixed":
            return list(schedule.get("times") or [])
        existing = self.store.get_random_plan(
            template.id, template.version, shanghai_date
        )
        if existing is not None:
            return existing
        times = self._random_times(
            template, shanghai_date, int(schedule.get("daily_count") or 0)
        )
        return self.store.put_random_plan(
            template.id, template.version, shanghai_date, times
        )

    def tick(self) -> Dict[str, Any]:
        if not self.executor.gates.is_open:
            return {
                "ok": True,
                "created_runs": [],
                "held": "live_gates_closed",
                "gates": self.executor.gates.as_dict(),
            }
        if not self._schedule_lock.acquire(blocking=False):
            return {"ok": True, "created_runs": [], "busy": True}
        try:
            now = self._now()
            shanghai = now.astimezone(BEIJING_TZ)
            cutoff_shanghai = (
                now + timedelta(seconds=self.prepare_ahead_seconds)
            ).astimezone(BEIJING_TZ)
            days = [shanghai.date().isoformat()]
            if cutoff_shanghai.date() != shanghai.date():
                days.append(cutoff_shanghai.date().isoformat())
            created = []
            for template in self.store.list_templates(enabled=True):
                for day, publish_time in (
                    (day, publish_time)
                    for day in days
                    for publish_time in self._schedule_times(template, day)
                ):
                    hour, minute = (int(value) for value in publish_time.split(":"))
                    slot = datetime.fromisoformat(
                        day + "T%02d:%02d:00" % (hour, minute)
                    ).replace(tzinfo=BEIJING_TZ).astimezone(UTC)
                    try:
                        enabled_at = datetime.fromisoformat(
                            str(template.enabled_at_utc).replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except (TypeError, ValueError, OverflowError):
                        raise AutoPostServiceError(
                            "tt_auto_template_timestamp_invalid",
                            "模板启用时间无效",
                            500,
                        ) from None
                    if slot < enabled_at:
                        continue
                    age = (now - slot).total_seconds()
                    if (
                        age < -self.prepare_ahead_seconds
                        or age > self.schedule_grace_seconds
                    ):
                        continue
                    try:
                        run, was_created = self._create_run(
                            template,
                            trigger_type="auto",
                            scheduled_at=slot,
                            publish_time=publish_time,
                            run_key=(
                                f"tt-auto:auto:v1:{template.id}:"
                                f"{day}:{publish_time.replace(':', '')}"
                            ),
                        )
                    except Exception as exc:
                        if str(getattr(exc, "code", "")) in {
                            "tt_auto_template_not_enabled_for_slot",
                            "tt_auto_template_version_conflict",
                        }:
                            continue
                        raise
                    if was_created:
                        created.append(run.id)
            return {
                "ok": True,
                "created_runs": sorted(set(created)),
                "current_shanghai_minute": shanghai.strftime("%Y-%m-%d %H:%M"),
                "grace_seconds": self.schedule_grace_seconds,
                "prepare_ahead_seconds": self.prepare_ahead_seconds,
                "gates": self.executor.gates.as_dict(),
            }
        finally:
            self._schedule_lock.release()

    def runs(self, query: Mapping[str, Sequence[str]]) -> Dict[str, Any]:
        template_id = _query_one(query, "template_id")
        trigger_type = _query_one(query, "trigger_type")
        status = _query_one(query, "status")
        limit, offset = _limit_offset(query)
        values = self.store.list_runs(
            template_id=(None if not template_id else _positive_id(template_id)),
            trigger_type=trigger_type or None,
            status=status or None,
        )
        start = _query_one(query, "from")
        end = _query_one(query, "to")
        if start:
            lower = _beijing_date_boundary(start)
            values = [run for run in values if run.scheduled_at_utc >= lower]
        if end:
            upper = _beijing_date_boundary(end, next_day=True)
            values = [run for run in values if run.scheduled_at_utc < upper]
        total = len(values)
        page = values[offset : offset + limit]
        items = []
        for run in page:
            try:
                template = self.store.get_template(
                    run.template_id, version=run.template_version
                )
            except Exception:
                template = None
            items.append(
                self._run_item(
                    run,
                    template=template,
                    tasks=self.store.list_tasks(run_id=run.id),
                )
            )
        return {
            "ok": True,
            "runs": items,
            "total": total,
            "summary": {
                "active": sum(run.status in {"queued", "running"} for run in values),
                "completed": sum(run.status == "completed" for run in values),
                "attention": sum(
                    run.status in {"partial_failed", "failed"} for run in values
                ),
            },
        }

    def publish_logs(
        self, query: Mapping[str, Sequence[str]]
    ) -> Dict[str, Any]:
        """Merge both immutable ledgers into one task-level read model."""

        source = _query_one(query, "publish_source")
        if source not in {"", "material_pool", "auto_template"}:
            raise AutoPostServiceError("invalid_request", "发布来源无效", 400)
        trigger = _query_one(query, "trigger_type")
        if trigger not in {"", "scheduled", "direct_test", "auto", "manual"}:
            raise AutoPostServiceError("invalid_request", "触发方式无效", 400)
        status = _query_one(query, "status")
        if status and status not in PUBLISH_LOG_STATUS_GROUPS:
            raise AutoPostServiceError("invalid_request", "发布状态无效", 400)
        limit, offset = _publish_log_limit_offset(query)
        fetch_limit = offset + limit
        account_id = _query_one(query, "source_account_id")
        material_id = _query_one(query, "material_id")
        content_id = _query_one(query, "content_id")
        raw_template_id = _query_one(query, "template_id")
        template_id = (
            None if not raw_template_id else _positive_id(raw_template_id, "模板ID")
        )
        from_value = _query_one(query, "from")
        to_value = _query_one(query, "to")
        from_utc = _beijing_date_boundary(from_value) if from_value else ""
        to_utc = (
            _beijing_date_boundary(to_value, next_day=True) if to_value else ""
        )
        if from_utc and to_utc and from_utc >= to_utc:
            raise AutoPostServiceError("invalid_request", "发布日期范围无效", 400)

        empty = {
            "items": [],
            "total": 0,
            "summary": {
                key: 0
                for key in (
                    "total",
                    "scheduled",
                    "processing",
                    "published",
                    "needs_review",
                    "failed",
                    "canceled",
                    "no_candidate",
                    "hold",
                )
            },
        }
        legacy_trigger = trigger if trigger in {"scheduled", "direct_test"} else ""
        auto_trigger = trigger if trigger in {"auto", "manual"} else ""
        include_legacy = (
            source != "auto_template"
            and template_id is None
            and trigger not in {"auto", "manual"}
        )
        include_auto = source != "material_pool" and trigger not in {
            "scheduled",
            "direct_test",
        }
        legacy = (
            self.legacy_reader.list_publish_logs(
                trigger_type=legacy_trigger,
                account_id=account_id,
                material_id=material_id,
                content_id=content_id,
                status_group=status,
                from_utc=from_utc,
                to_utc=to_utc,
                limit=fetch_limit,
            )
            if include_legacy
            else dict(empty)
        )
        automatic = (
            self.store.list_publish_logs(
                trigger_type=auto_trigger,
                account_id=account_id,
                template_id=template_id,
                material_id=material_id,
                content_id=content_id,
                status_group=status,
                from_utc=from_utc,
                to_utc=to_utc,
                limit=fetch_limit,
            )
            if include_auto
            else dict(empty)
        )

        automatic_items = automatic.get("items", [])
        if automatic_items:
            route_to_task = {
                synthetic_queue_id(item.get("task_id")): item
                for item in automatic_items
            }
            try:
                automatic_codes = self.legacy_reader.code_routes_for_queue_ids(
                    route_to_task
                )
            except LegacyTTPostReaderError as exc:
                if int(exc.status) < 500:
                    raise AutoPostServiceError(exc.code, str(exc), exc.status) from None
                automatic_codes = {}
            for route_id, code in automatic_codes.items():
                route_to_task[route_id]["code"] = code

        values = [
            dict(item)
            for result in (legacy, automatic)
            for item in result.get("items", [])
        ]
        for item in values:
            item["selection"] = _public_selection(item.get("selection", {}))
            publish_url = str(item.get("publish_url") or "").strip()
            if publish_url:
                try:
                    parsed = urlsplit(publish_url)
                    host = str(parsed.hostname or "").lower()
                    trusted = (
                        parsed.scheme == "https"
                        and (host == "tiktok.com" or host.endswith(".tiktok.com"))
                        and parsed.username is None
                        and parsed.password is None
                        and parsed.port in (None, 443)
                        and not parsed.query
                        and not parsed.fragment
                    )
                except ValueError:
                    trusted = False
                if not trusted:
                    item["publish_url"] = ""
        values.sort(key=_publish_log_order, reverse=True)
        page = values[offset : offset + limit]
        summary_keys = (
            "total",
            "scheduled",
            "processing",
            "published",
            "needs_review",
            "failed",
            "canceled",
            "no_candidate",
            "hold",
        )
        summary = {
            key: sum(
                int((result.get("summary") or {}).get(key) or 0)
                for result in (legacy, automatic)
            )
            for key in summary_keys
        }
        total = int(legacy.get("total") or 0) + int(automatic.get("total") or 0)
        summary["total"] = total
        return {
            "ok": True,
            "items": page,
            "pagination": {"limit": limit, "offset": offset, "total": total},
            "summary": summary,
            "sources": {
                "material_pool": int(legacy.get("total") or 0),
                "auto_template": int(automatic.get("total") or 0),
            },
        }

    def run(self, run_id: Any) -> Dict[str, Any]:
        run = self.store.get_run(run_id)
        template = self.store.get_template(
            run.template_id, version=run.template_version
        )
        tasks = self.store.list_tasks(run_id=run.id)
        return {
            "ok": True,
            "run": self._run_item(run, template=template, tasks=tasks),
            "tasks": [self._task_item(task) for task in tasks],
            "events": self.store.list_events(run_id=run.id),
        }

    def force_close_task(
        self, task_id: Any, raw: Mapping[str, Any]
    ) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        if set(payload) != {"reason"}:
            raise AutoPostServiceError("invalid_request", "强制关闭参数无效", 400)
        reason = str(payload.get("reason") or "").strip()
        if not reason or len(reason) > 200:
            raise AutoPostServiceError("invalid_request", "强制关闭原因无效", 400)
        audit_reason = "%s（操作人：%s / %s）" % (
            reason,
            actor.name or "未知",
            actor.user_id or "未知",
        )
        task = self.executor.force_close_task(task_id, reason=audit_reason)
        return {"ok": True, "task": self._task_item(task)}

    def execute_next(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        if (
            set(payload).difference({"worker_id", "phases"})
            or "worker_id" not in payload
        ):
            raise AutoPostServiceError("invalid_request", "执行器参数无效", 400)
        phases = payload.get("phases")
        if phases is not None and (
            isinstance(phases, (str, bytes))
            or not isinstance(phases, list)
            or not phases
            or any(not isinstance(value, str) for value in phases)
        ):
            raise AutoPostServiceError(
                "invalid_request", "executor phases are invalid", 400
            )
        return self.executor.execute_next(
            payload.get("worker_id"), allowed_phases=phases
        )


def _required_env(source: Mapping[str, str], name: str) -> str:
    value = str(source.get(name, "") or "")
    if not value:
        raise AutoPostServiceError(
            "tt_auto_config_missing", "自动发布服务配置不完整", 500
        )
    return value


def _isolated_db_paths(source: Mapping[str, str]) -> Tuple[str, str]:
    auto_root = Path(
        str(
            source.get(
                "TT_AUTO_POST_STATE_ROOT",
                "/mnt/data-disk/tt-auto-post-publisher",
            )
        ).strip()
    ).expanduser()
    legacy_root = Path(
        str(
            source.get(
                "TT_AUTO_POST_LEGACY_STATE_ROOT",
                "/mnt/data-disk/tt-post-publisher",
            )
        ).strip()
    ).expanduser()
    auto_path = Path(
        str(
            source.get(
                "TT_AUTO_POST_DB_PATH",
                "/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3",
            )
        ).strip()
    ).expanduser()
    legacy_path = Path(
        str(
            source.get(
                "TT_AUTO_POST_LEGACY_DB_PATH",
                "/mnt/data-disk/tt-post-publisher/tt-post.sqlite3",
            )
        ).strip()
    ).expanduser()
    values = (auto_root, legacy_root, auto_path, legacy_path)
    if any(not value.is_absolute() for value in values):
        raise AutoPostServiceError(
            "tt_auto_db_path_invalid", "自动发布数据库路径无效", 500
        )
    try:
        resolved_auto_root = auto_root.resolve(strict=False)
        resolved_legacy_root = legacy_root.resolve(strict=False)
        resolved_auto_path = auto_path.resolve(strict=False)
        resolved_legacy_path = legacy_path.resolve(strict=False)
        resolved_auto_path.relative_to(resolved_auto_root)
        resolved_legacy_path.relative_to(resolved_legacy_root)
    except (OSError, RuntimeError, ValueError):
        raise AutoPostServiceError(
            "tt_auto_db_path_invalid", "自动发布数据库路径无效", 500
        ) from None
    roots_overlap = False
    for candidate, parent in (
        (resolved_auto_root, resolved_legacy_root),
        (resolved_legacy_root, resolved_auto_root),
    ):
        try:
            candidate.relative_to(parent)
        except ValueError:
            continue
        roots_overlap = True
        break
    paths_collide = resolved_auto_path == resolved_legacy_path
    if not paths_collide and resolved_auto_path.exists() and resolved_legacy_path.exists():
        try:
            paths_collide = os.path.samefile(resolved_auto_path, resolved_legacy_path)
        except OSError:
            paths_collide = True
    if roots_overlap or paths_collide:
        raise AutoPostServiceError(
            "tt_auto_db_path_collision",
            "自动发布数据库必须与旧 TT 发布池物理隔离",
            500,
        )
    return str(resolved_auto_path), str(resolved_legacy_path)


def build_service_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> TTAutoPostService:
    source = os.environ if environ is None else environ
    internal_token = _required_env(source, "TT_AUTO_POST_INTERNAL_TOKEN")
    if not valid_internal_bearer(internal_token):
        raise AutoPostServiceError(
            "tt_auto_internal_bearer_invalid", "internal bearer is invalid", 500
        )
    db_path, legacy_path = _isolated_db_paths(source)
    mysql = SnapshotMySQLConfig.from_env(source)
    account_repository = MySQLSnapshotAccountRepository(mysql)
    material_schema = str(
        source.get("TT_POST_MATERIAL_MYSQL_DATABASE", "kunlunads_dev")
    ).strip()

    def source_connection():
        return connect_read_only(
            host=mysql.host,
            port=mysql.port,
            user=mysql.user,
            password=mysql.password,
            database=material_schema,
            connect_timeout=5,
            read_timeout=120,
        )

    repository = ReadOnlyMySQLRepository(
        source_connection,
        schema=material_schema,
    )
    store = TTPostAutoStore(db_path)
    legacy = LegacyTTPostReader(legacy_path)
    legacy.validate_schema()
    resolver = DramawaveMaterialResolver(
        lambda: connect_read_only(
            host=mysql.host,
            port=mysql.port,
            user=mysql.user,
            password=mysql.password,
            database=material_schema,
            connect_timeout=5,
            read_timeout=30,
        ),
        schema=material_schema,
    )
    selector = TwoStageSelector(
        repository,
        MetricWindowRepository(store),
        legacy,
        store,
        material_validator=ResolverMaterialValidator(resolver),
    )
    gpu_token = _required_env(source, "TT_POST_GPU_INTERNAL_TOKEN")
    legacy_internal_token = str(source.get("TT_POST_INTERNAL_TOKEN", "") or "")
    if secrets.compare_digest(internal_token, gpu_token) or (
        legacy_internal_token
        and secrets.compare_digest(internal_token, legacy_internal_token)
    ):
        raise AutoPostServiceError(
            "tt_auto_bearer_reuse_denied",
            "自动发布内部凭据必须与旧 TT 服务及 GPU 凭据独立",
            500,
        )
    gpu_seal_key = _required_env(
        source, "TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64"
    )
    gpu_timeout = int(source.get("TT_POST_GPU_TIMEOUT", "900"))
    gpu_prepare_timeout = int(
        source.get("TT_POST_GPU_PREPARE_TIMEOUT", "9000")
    )
    gpu = GPUClient(
        str(source.get("TT_POST_GPU_URL", "http://127.0.0.1:18830")),
        gpu_token,
        gpu_seal_key,
        timeout=gpu_timeout,
        prepare_timeout=gpu_prepare_timeout,
    )
    direct_outro_gpu = GPUClient(
        str(
            source.get(
                "TT_AUTO_POST_DIRECT_OUTRO_GPU_URL",
                "http://127.0.0.1:18834",
            )
        ),
        gpu_token,
        gpu_seal_key,
        timeout=gpu_timeout,
        prepare_timeout=gpu_prepare_timeout,
        allowed_loopback_ports=(18834,),
    )
    primary_trim = float(
        source.get("TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS", "0")
    )
    primary_profile = str(
        source.get(
            "TT_POST_MEDIA_PROFILE_VERSION",
            RANDOM_OVERLAY_MEDIA_PROFILE,
        )
        or ""
    ).strip()
    if primary_profile != RANDOM_OVERLAY_MEDIA_PROFILE or primary_trim != 0:
        raise AutoPostServiceError(
            "tt_auto_random_overlay_route_invalid",
            "随机排重GPU路由的profile或trim配置不一致",
            500,
        )
    prepare_ahead_seconds = int(
        source.get("TT_AUTO_POST_PREPARE_AHEAD_SECONDS", "0")
    )
    executor = AutoPostExecutor(
        store,
        selector,
        account_repository.as_account_source(),
        gpu,
        code_route_broker=AutoCodeBrokerClient(
            str(
                source.get(
                    "TT_AUTO_CODE_ROUTE_SERVICE_URL",
                    DEFAULT_BROKER_URL,
                )
            ),
            internal_token,
            timeout_seconds=float(
                source.get("TT_AUTO_CODE_ROUTE_TIMEOUT_SECONDS", "5")
            ),
        ),
        gates=AutoLiveGates.from_env(source),
        short_link_root=str(
            source.get(
                "TT_AUTO_POST_SHORT_LINK_ROOT",
                "/mnt/data-disk/tt-auto-post-public/s2l",
            )
        ),
        source_trim_tail_seconds=primary_trim,
        media_profile_version=primary_profile,
        video_template_routes={
            VIDEO_TEMPLATE_RANDOM_OVERLAY: VideoTemplateRoute(
                gpu,
                RANDOM_OVERLAY_MEDIA_PROFILE,
                0.0,
            ),
            VIDEO_TEMPLATE_DIRECT_OUTRO: VideoTemplateRoute(
                direct_outro_gpu,
                DIRECT_OUTRO_MEDIA_PROFILE,
                4.333333,
            ),
        },
        max_concurrent_tasks=int(source.get("TT_AUTO_POST_WORKER_COUNT", "4")),
        prepare_ahead_seconds=prepare_ahead_seconds,
    )
    return TTAutoPostService(
        store,
        legacy,
        account_repository,
        selector,
        executor,
        runner_kick_path=str(
            source.get(
                "TT_AUTO_POST_RUNNER_KICK_PATH",
                "/run/tt-auto-post/manual-kick",
            )
        ),
        schedule_grace_seconds=int(
            source.get("TT_AUTO_POST_SCHEDULE_GRACE_SECONDS", "600")
        ),
        prepare_ahead_seconds=prepare_ahead_seconds,
    )


class TTAutoPostHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        service: TTAutoPostService,
        internal_token: str,
    ):
        host, port = server_address
        if host != DEFAULT_HOST or int(port) != DEFAULT_PORT:
            raise AutoPostServiceError(
                "tt_auto_listen_invalid", "服务只能监听127.0.0.1:18831", 500
            )
        token = str(internal_token or "")
        if not valid_internal_bearer(token):
            raise AutoPostServiceError(
                "tt_auto_internal_bearer_invalid", "内部凭据无效", 500
            )
        self.tt_auto_service = service
        self.internal_token = token
        super().__init__(server_address, TTAutoPostRequestHandler)


class TTAutoPostRequestHandler(BaseHTTPRequestHandler):
    server: TTAutoPostHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        if contains_sensitive_key(payload):
            status = 500
            payload = {
                "ok": False,
                "error": "tt_auto_unsafe_response",
                "code": "tt_auto_unsafe_response",
                "message": "响应包含非公开字段",
            }
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            status = 500
            body = b'{"ok":false,"error":"response_too_large","code":"response_too_large","message":"response too large"}'
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        peer = str(self.client_address[0] if self.client_address else "")
        supplied = str(self.headers.get("Authorization") or "")
        return bool(
            peer in {"127.0.0.1", "::1"}
            and supplied.startswith("Bearer ")
            and secrets.compare_digest(
                supplied[len("Bearer ") :], self.server.internal_token
            )
        )

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(str(self.headers.get("Content-Length") or "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            raise AutoPostServiceError(
                "invalid_request", "请求体超过安全上限", 413
            )
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError):
            raise AutoPostServiceError("invalid_request", "请求体不是有效JSON", 400) from None
        if not isinstance(value, dict) or contains_sensitive_key(value):
            raise AutoPostServiceError("invalid_request", "请求体包含无效字段", 400)
        return value

    def _dispatch(self) -> Tuple[int, Mapping[str, Any]]:
        parsed = urlsplit(self.path)
        path = parsed.path
        service = self.server.tt_auto_service
        if self.command == "GET" and path == "/health":
            return 200, service.health()
        if not self._authorized():
            return 403, {
                "ok": False,
                "error": "tt_auto_internal_required",
                "code": "tt_auto_internal_required",
                "message": "拒绝访问",
            }
        query = parse_qs(parsed.query, keep_blank_values=True)
        if self.command == "GET" and path == TT_AUTO_ADMIN_PREFIX + "/accounts":
            return 200, service.accounts()
        if self.command == "GET" and path == TT_AUTO_ADMIN_PREFIX + "/templates":
            return 200, service.templates(query)
        match = re.fullmatch(
            re.escape(TT_AUTO_ADMIN_PREFIX) + r"/templates/([1-9][0-9]*)", path
        )
        if self.command == "GET" and match:
            return 200, service.template(match.group(1))
        if self.command == "GET" and path == TT_AUTO_ADMIN_PREFIX + "/runs":
            return 200, service.runs(query)
        if self.command == "GET" and path == TT_AUTO_ADMIN_PREFIX + "/publish-logs":
            return 200, service.publish_logs(query)
        match = re.fullmatch(
            re.escape(TT_AUTO_ADMIN_PREFIX) + r"/runs/([1-9][0-9]*)", path
        )
        if self.command == "GET" and match:
            return 200, service.run(match.group(1))
        if self.command == "POST" and path == TT_AUTO_ADMIN_PREFIX + "/templates":
            return 200, service.create_template(self._body())
        match = re.fullmatch(
            re.escape(TT_AUTO_ADMIN_PREFIX)
            + r"/templates/([1-9][0-9]*)(?:/(copy|enable|disable|preview|run-now))?",
            path,
        )
        if self.command == "POST" and match:
            template_id, action = match.groups()
            body = self._body()
            if not action:
                return 200, service.update_template(template_id, body)
            if action == "copy":
                return 200, service.copy_template(template_id, body)
            if action == "enable":
                return 200, service.set_enabled(template_id, True, body)
            if action == "disable":
                return 200, service.set_enabled(template_id, False, body)
            if action == "preview":
                return 200, service.preview(template_id, body)
            if action == "run-now":
                return 202, service.run_now(template_id, body)
        if self.command == "POST" and path == "/internal/tt-auto-post/tick":
            if self._body():
                raise AutoPostServiceError("invalid_request", "调度参数无效", 400)
            return 200, service.tick()
        if self.command == "POST" and path == "/internal/tt-auto-post/execute-next":
            return 200, service.execute_next(self._body())
        match = re.fullmatch(
            re.escape(TT_AUTO_ADMIN_PREFIX)
            + r"/tasks/([1-9][0-9]*)/force-close",
            path,
        )
        if self.command == "POST" and match:
            return 200, service.force_close_task(match.group(1), self._body())
        raise AutoPostServiceError("not_found", "路由不存在", 404)

    def _handle(self) -> None:
        try:
            status, payload = self._dispatch()
        except Exception as exc:
            code = str(getattr(exc, "code", "tt_auto_internal_error"))
            if not re.fullmatch(r"[a-z0-9_]{1,96}", code):
                code = "tt_auto_internal_error"
            status = int(getattr(exc, "status", 500) or 500)
            if status < 400 or status > 599:
                status = 500
            message = safe_public_message(exc)
            payload = {
                "ok": False,
                "error": code,
                "code": code,
                "message": message,
            }
        self._json(status, payload)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


def serve(environ: Optional[Mapping[str, str]] = None) -> None:
    source = os.environ if environ is None else environ
    host = str(source.get("TT_AUTO_POST_SERVICE_HOST", DEFAULT_HOST)).strip()
    try:
        port = int(source.get("TT_AUTO_POST_SERVICE_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError, OverflowError):
        port = 0
    service = build_service_from_env(source)
    server = TTAutoPostHTTPServer(
        (host, port), service, _required_env(source, "TT_AUTO_POST_INTERNAL_TOKEN")
    )
    shutting_down = threading.Event()

    def graceful_shutdown(_signum: int, _frame: Any) -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()
        # BaseServer.shutdown must run outside the serve_forever thread.
        threading.Thread(
            target=server.shutdown,
            name="tt-auto-post-shutdown",
            daemon=True,
        ).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, graceful_shutdown)
        signal.signal(signal.SIGINT, graceful_shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        # ThreadingMixIn waits for non-daemon request threads here, preserving
        # publish/reconcile state until an in-flight request records its result.
        server.server_close()


__all__ = [
    "AutoPostServiceError",
    "TTAutoPostHTTPServer",
    "TTAutoPostService",
    "build_service_from_env",
    "serve",
]
