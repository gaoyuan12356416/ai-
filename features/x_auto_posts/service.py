"""Loopback admin API, scheduler and executor wiring for X auto templates."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from features.x_posts.selector import connect_read_only
from features.x_accounts.language import same_drama_language

from .client import X_AUTO_ADMIN_PREFIX, contains_sensitive_key, safe_public_message
from .core import AuditActor, XAutoPostStore
from .publisher import AutoLiveGates, AutoPostExecutor, selector_rules
from .repositories import BEIJING_TZ, MetricWindowRepository, ReadOnlyMySQLRepository
from .selector import ResolverMaterialValidator, SelectionRequest, TwoStageSelector
from .validation import (
    expected_version,
    normalize_template_payload,
    valid_internal_bearer,
)
from .x_sidecar import (
    DEFAULT_X_POST_INTERNAL_URL,
    XPostAutoBridgeClient,
    XPostMaterialHistory,
)


UTC = timezone.utc
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18833
MAX_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AutoPostServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "x_auto_post_error")[:96]
        self.status = int(status)
        super().__init__(str(message or "X automatic publishing request failed")[:500])


def _positive_id(value: Any, label: str = "ID") -> int:
    if isinstance(value, bool):
        result = 0
    else:
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            result = 0
    if result <= 0:
        raise AutoPostServiceError("invalid_request", f"{label} is invalid", 400)
    return result


def _actor(payload: Dict[str, Any]) -> AuditActor:
    raw = payload.pop("_actor", {})
    if not isinstance(raw, Mapping) or set(raw).difference({"user_id", "name"}):
        raise AutoPostServiceError("invalid_request", "operator identity is invalid", 400)
    return AuditActor.from_values(raw.get("user_id"), raw.get("name"))


def _query_one(query: Mapping[str, Sequence[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if values is None:
        return default
    if not isinstance(values, list) or len(values) != 1:
        raise AutoPostServiceError("invalid_request", "query parameter is invalid", 400)
    return str(values[0] or "").strip()


def _limit_offset(query: Mapping[str, Sequence[str]]) -> Tuple[int, int]:
    try:
        limit = int(_query_one(query, "limit", "50"))
        offset = int(_query_one(query, "offset", "0"))
    except ValueError:
        raise AutoPostServiceError("invalid_request", "pagination is invalid", 400) from None
    if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
        raise AutoPostServiceError("invalid_request", "pagination is invalid", 400)
    return limit, offset


def _beijing_date_boundary(value: str, *, next_day: bool = False) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value or "")):
        raise AutoPostServiceError("invalid_request", "run date is invalid", 400)
    try:
        local = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        raise AutoPostServiceError("invalid_request", "run date is invalid", 400) from None
    if next_day:
        local += timedelta(days=1)
    return local.astimezone(UTC).isoformat(timespec="seconds")


def _blacklist_dict(snapshot: Any) -> Dict[str, Any]:
    return {
        "loaded_at_utc": str(snapshot.loaded_at_utc),
        "source_row_count": int(snapshot.source_row_count),
        "sha256": str(snapshot.sha256),
    }


def _public_selection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _public_selection(item)
            for key, item in value.items()
            if str(key)
            not in {
                "media_url",
                "material_url",
                "source_media_url",
                "prepared_media_url",
                "original_material_url",
            }
        }
    if isinstance(value, list):
        return [_public_selection(item) for item in value]
    return value


def _safe_x_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        host = str(parsed.hostname or "").lower()
        valid = (
            parsed.scheme == "https"
            and (
                host in {"x.com", "twitter.com"}
                or host.endswith(".x.com")
                or host.endswith(".twitter.com")
            )
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    return text if valid else ""


class _PreviewStore:
    def __init__(self, store: XAutoPostStore):
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


class XAutoPostService:
    def __init__(
        self,
        store: XAutoPostStore,
        account_bridge: XPostAutoBridgeClient,
        selector: TwoStageSelector,
        executor: AutoPostExecutor,
        *,
        now_fn=lambda: datetime.now(UTC),
        runner_kick_path: Any = "/run/x-auto-post/manual-kick",
        schedule_grace_seconds: int = 600,
    ):
        self.store = store
        self.account_bridge = account_bridge
        self.selector = selector
        self.executor = executor
        self.now_fn = now_fn
        self.runner_kick_path = str(runner_kick_path or "").strip()
        self.schedule_grace_seconds = int(schedule_grace_seconds)
        self._schedule_lock = threading.Lock()
        self._run_create_lock = threading.Lock()
        if not 60 <= self.schedule_grace_seconds <= 3600:
            raise AutoPostServiceError(
                "x_auto_schedule_config_invalid", "schedule grace is invalid", 500
            )

    def _now(self) -> datetime:
        value = self.now_fn()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutoPostServiceError("x_auto_clock_invalid", "service clock is invalid", 500)
        return value.astimezone(UTC)

    def _template_item(self, snapshot: Any) -> Dict[str, Any]:
        item = self._template_snapshot_item(snapshot)
        last_run = self.store.get_latest_run_for_template(snapshot.id)
        next_run_at = ""
        if snapshot.enabled:
            now = self._now()
            shanghai_now = now.astimezone(BEIJING_TZ)
            try:
                enabled_at = datetime.fromisoformat(
                    str(snapshot.enabled_at_utc).replace("Z", "+00:00")
                ).astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                enabled_at = None
            schedule = snapshot.config.get("schedule")
            schedule = schedule if isinstance(schedule, Mapping) else {}
            for day_offset in (range(2) if enabled_at is not None else ()):
                local_day = (shanghai_now + timedelta(days=day_offset)).date()
                day = local_day.isoformat()
                if schedule.get("mode") == "fixed":
                    publish_times = list(schedule.get("times") or [])
                else:
                    publish_times = self.store.get_random_plan(
                        snapshot.id, snapshot.version, day
                    ) or []
                for publish_time in publish_times:
                    hour, minute = (int(value) for value in publish_time.split(":"))
                    slot = datetime(
                        local_day.year,
                        local_day.month,
                        local_day.day,
                        hour,
                        minute,
                        tzinfo=BEIJING_TZ,
                    ).astimezone(UTC)
                    if slot >= now and slot >= enabled_at:
                        next_run_at = slot.isoformat(timespec="seconds")
                        break
                if next_run_at:
                    break
        if next_run_at:
            item["next_run_at"] = next_run_at
        if last_run is not None:
            item["last_run"] = self._run_item(last_run)
            item["last_run_status"] = last_run.status
            item["last_run_at"] = last_run.created_at
        return item

    @staticmethod
    def _run_item(run: Any, *, template: Optional[Any] = None, tasks=None) -> Dict[str, Any]:
        item = run.as_dict()
        item["run_id"] = run.id
        if item.get("error_message"):
            item["error_message"] = safe_public_message(item["error_message"])
        snapshot = item.get("blacklist_snapshot")
        if isinstance(snapshot, Mapping):
            item["blacklist_snapshot"] = {
                "loaded_at_utc": str(snapshot.get("loaded_at_utc") or ""),
                "source_row_count": int(snapshot.get("source_row_count") or 0),
                "sha256": str(snapshot.get("sha256") or ""),
            }
        if template is not None:
            item["template_name"] = template.name
            item["template_snapshot"] = XAutoPostService._template_snapshot_item(template)
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
            item["attention_task_count"] = sum(
                task.status in {"failed", "canceled", "unknown"} for task in values
            )
        return item

    @staticmethod
    def _template_snapshot_item(snapshot: Any) -> Dict[str, Any]:
        """Return the frozen template DTO without querying live run state."""

        config = dict(snapshot.config)
        schedule = config.get("schedule") if isinstance(config.get("schedule"), Mapping) else {}
        if schedule.get("mode") == "fixed":
            schedule_summary = ", ".join(schedule.get("times") or [])
        else:
            schedule_summary = "random %s/day" % int(schedule.get("daily_count") or 0)
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
    def _task_item(task: Any) -> Dict[str, Any]:
        item = task.as_dict()
        item["prepared"] = bool(item.get("execution_queue_id"))
        item["selection"] = _public_selection(item.get("selection"))
        item.pop("account_snapshot", None)
        item["publish_url"] = _safe_x_url(item.get("publish_url"))
        if item.get("error_message"):
            item["error_message"] = safe_public_message(item["error_message"])
        return item

    def accounts(self) -> Dict[str, Any]:
        items = self.account_bridge.accounts()
        return {"ok": True, "accounts": items, "items": items, "total": len(items)}

    def refresh_account(self, account_id: Any) -> Dict[str, Any]:
        expected_id = _positive_id(account_id, "account ID")
        account = self.account_bridge.verify_account(
            expected_id,
            only_refresh_required=True,
            preserve_transient_status=True,
            require_publish_approved=True,
        )
        if str(account.get("id") or "") != str(expected_id):
            raise AutoPostServiceError(
                "x_auto_x_bridge_response_invalid",
                "X account refresh returned an invalid account",
                502,
            )
        if not bool(account.get("publish_eligible")):
            raise AutoPostServiceError(
                "x_auto_account_not_publishable",
                "X account is still not currently publishable",
                409,
            )
        return {"ok": True, "account": account, "item": account}

    def templates(self, query: Mapping[str, Sequence[str]]) -> Dict[str, Any]:
        status = _query_one(query, "status")
        if status not in {"", "enabled", "disabled"}:
            raise AutoPostServiceError("invalid_request", "template status is invalid", 400)
        enabled = None if not status else status == "enabled"
        q = _query_one(query, "q").casefold()
        limit, offset = _limit_offset(query)
        values = self.store.list_templates(enabled=enabled)
        if q:
            values = [item for item in values if q in item.name.casefold()]
        total = len(values)
        page = values[offset : offset + limit]
        items = [self._template_item(item) for item in page]
        all_values = self.store.list_templates()
        return {
            "ok": True,
            "templates": items,
            "items": items,
            "total": total,
            "summary": {
                "enabled": sum(item.enabled for item in all_values),
                "disabled": sum(not item.enabled for item in all_values),
                "running": sum(
                    run.status in {"queued", "running"} for run in self.store.list_runs()
                ),
            },
        }

    def template(self, template_id: Any) -> Dict[str, Any]:
        item = self._template_item(self.store.get_template(template_id))
        return {"ok": True, "template": item, "item": item}

    def _validate_accounts(self, config: Mapping[str, Any]) -> None:
        for account_id in config.get("account_ids") or []:
            account = self.account_bridge.verify_account(account_id)
            if str(account.get("id") or "") != str(account_id) or not bool(
                account.get("publish_eligible")
            ):
                raise AutoPostServiceError(
                    "x_auto_account_not_publishable",
                    "selected X account is not currently publishable",
                    409,
                )
            self._assert_account_language(account, config.get("language"))

    @staticmethod
    def _assert_account_language(
        account: Mapping[str, Any], template_language: Any
    ) -> None:
        if not same_drama_language(
            account.get("drama_language"), template_language
        ):
            raise AutoPostServiceError(
                "x_auto_account_language_mismatch",
                "selected X account drama language does not match the template language",
                409,
            )

    @staticmethod
    def _confirmation(actor: AuditActor, now: datetime) -> Dict[str, Any]:
        return {
            "accepted": True,
            "scope": "x_auto_publish_template_version",
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
        public = self._template_item(item)
        return {"ok": True, "template": public, "item": public}

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
        public = self._template_item(item)
        return {"ok": True, "template": public, "item": public}

    def copy_template(self, template_id: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        if set(payload).difference({"expected_version", "name"}):
            raise AutoPostServiceError("invalid_request", "copy request is invalid", 400)
        source = self.store.get_template(template_id)
        if expected_version(payload.get("expected_version")) != source.version:
            raise AutoPostServiceError(
                "x_auto_template_version_conflict", "template version changed", 409
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
        public = self._template_item(item)
        return {"ok": True, "template": public, "item": public}

    def set_enabled(self, template_id: Any, enabled: bool, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        actor = _actor(payload)
        if set(payload) != {"expected_version"}:
            raise AutoPostServiceError("invalid_request", "enable request is invalid", 400)
        version = expected_version(payload.get("expected_version"))
        template = self.store.get_template(template_id)
        if template.version != version:
            raise AutoPostServiceError(
                "x_auto_template_version_conflict", "template version changed", 409
            )
        if bool(template.enabled) is bool(enabled):
            public = self._template_item(template)
            return {"ok": True, "template": public, "item": public}
        if enabled:
            self._validate_accounts(template.config)
        item = self.store.set_template_enabled(
            template_id,
            enabled=bool(enabled),
            expected_version=version,
            actor=actor,
        )
        public = self._template_item(item)
        return {"ok": True, "template": public, "item": public}

    def _preview_selector(self) -> TwoStageSelector:
        return TwoStageSelector(
            self.selector.source,
            self.selector.metrics,
            self.selector.history,
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
            raise AutoPostServiceError("invalid_request", "preview request is invalid", 400)
        template = self.store.get_template(template_id)
        if expected_version(payload.get("expected_version")) != template.version:
            raise AutoPostServiceError(
                "x_auto_template_version_conflict", "template version changed", 409
            )
        requested = str(payload.get("account_id") or "").strip()
        account_ids = list(template.config.get("account_ids") or [])
        if requested:
            if requested not in account_ids:
                raise AutoPostServiceError(
                    "invalid_request", "preview account is not in this template", 400
                )
            account_ids = [requested]
        results = []
        for index, account_id in enumerate(account_ids, start=1):
            try:
                account = self.account_bridge.verify_account(account_id)
                if not bool(account.get("publish_eligible")):
                    raise AutoPostServiceError(
                        "x_auto_account_not_publishable",
                        "selected X account is not currently publishable",
                        409,
                    )
                self._assert_account_language(
                    account, template.config.get("language")
                )
                selection = self._preview_selector().select_and_reserve(
                    SelectionRequest(
                        run_id=index,
                        task_id=index,
                        template_id=template.id,
                        template_version=template.version,
                        account_id=account_id,
                        language=str(template.config.get("language") or ""),
                        rules=selector_rules(template.config, account=account),
                        now=self._now(),
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "account_id": account_id,
                        "ok": False,
                        "error_code": str(getattr(exc, "code", "x_auto_preview_failed")),
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
        return {"ok": True, "preview": results, "items": results, "reserved": False}

    def _run_tasks(self, run: Any, config: Mapping[str, Any]) -> List[Any]:
        existing = {task.account_id: task for task in self.store.list_tasks(run_id=run.id)}
        tasks = []
        for account_id in config.get("account_ids") or []:
            if account_id in existing:
                tasks.append(existing[account_id])
                continue
            try:
                account = self.account_bridge.verify_account(account_id)
                if not bool(account.get("publish_eligible")):
                    raise AutoPostServiceError(
                        "x_auto_account_not_publishable",
                        "selected X account is not currently publishable",
                        409,
                    )
                self._assert_account_language(
                    account, config.get("language")
                )
                task = self.store.create_task(
                    run_id=run.id,
                    account_id=account_id,
                    account_username=str(account.get("username") or ""),
                    account_display_name=str(account.get("display_name") or ""),
                    language=config.get("language"),
                    body_template=config.get("body_template"),
                    account_snapshot=dict(account),
                    account_snapshot_version=1,
                )
            except Exception as exc:
                task = self.store.create_task(
                    run_id=run.id,
                    account_id=account_id,
                    language=config.get("language"),
                    body_template=config.get("body_template"),
                    account_snapshot={},
                    account_snapshot_version=0,
                )
                task = self.store.transition_task(
                    task.id,
                    "failed",
                    expected_statuses={"pending"},
                    updates={
                        "error_code": str(getattr(exc, "code", "x_auto_account_unavailable")),
                        "error_message": safe_public_message(exc),
                    },
                    event_type="task_account_snapshot_failed",
                )
            tasks.append(task)
        if tasks and all(task.status == "failed" for task in tasks):
            current = self.store.get_run(run.id)
            if current.status == "queued":
                self.store.set_run_status(run.id, "failed", expected_statuses={"queued"})
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
        with self._run_create_lock:
            existing = self.store.get_run_by_key(run_key)
            if existing is not None:
                frozen = self.store.get_template(
                    existing.template_id, version=existing.template_version
                )
                if (
                    existing.template_id != template.id
                    or existing.trigger_type != trigger_type
                    or existing.template_version != template.version
                    or frozen.config_sha256 != template.config_sha256
                ):
                    raise AutoPostServiceError(
                        "x_auto_run_idempotency_conflict",
                        "run idempotency key belongs to different frozen facts",
                        409,
                    )
                self._run_tasks(existing, frozen.config)
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
            raise AutoPostServiceError("invalid_request", "run-now request is invalid", 400)
        if payload.get("confirmed") is not True:
            raise AutoPostServiceError(
                "x_auto_run_confirmation_required", "explicit run confirmation is required", 409
            )
        if not self.executor.gates.is_open:
            raise AutoPostServiceError(
                "x_auto_live_gates_closed", "X 自动发布生产门禁未全部开启", 409
            )
        template = self.store.get_template(template_id)
        if expected_version(payload.get("expected_version")) != template.version:
            raise AutoPostServiceError(
                "x_auto_template_version_conflict", "template version changed", 409
            )
        self._validate_accounts(template.config)
        request_key = str(payload.get("idempotency_key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{8,128}", request_key):
            raise AutoPostServiceError("invalid_request", "idempotency key is invalid", 400)
        now = self._now()
        digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:32]
        run, created = self._create_run(
            template,
            trigger_type="manual",
            scheduled_at=now,
            publish_time=now.astimezone(BEIJING_TZ).strftime("%H:%M"),
            run_key=f"x-auto:manual:v1:{digest}",
            actor=actor,
        )
        public = self._run_item(
            run, template=template, tasks=self.store.list_tasks(run_id=run.id)
        )
        return {
            "ok": True,
            "run_id": run.id,
            "run": public,
            "item": public,
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
        return ["%02d:%02d" % divmod(value, 60) for value in sorted(generator.sample(range(1440), count))]

    def _schedule_times(self, template: Any, shanghai_date: str) -> List[str]:
        schedule = template.config.get("schedule")
        if not isinstance(schedule, Mapping):
            raise AutoPostServiceError("x_auto_schedule_invalid", "template schedule is invalid", 500)
        if schedule.get("mode") == "fixed":
            return list(schedule.get("times") or [])
        existing = self.store.get_random_plan(template.id, template.version, shanghai_date)
        if existing is not None:
            return existing
        times = self._random_times(template, shanghai_date, int(schedule.get("daily_count") or 0))
        return self.store.put_random_plan(template.id, template.version, shanghai_date, times)

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
            day = shanghai.date().isoformat()
            created = []
            for template in self.store.list_templates(enabled=True):
                for publish_time in self._schedule_times(template, day):
                    hour, minute = (int(value) for value in publish_time.split(":"))
                    slot = shanghai.replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    ).astimezone(UTC)
                    try:
                        enabled_at = datetime.fromisoformat(
                            str(template.enabled_at_utc).replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except (TypeError, ValueError, OverflowError):
                        raise AutoPostServiceError(
                            "x_auto_template_timestamp_invalid",
                            "template enabled timestamp is invalid",
                            500,
                        ) from None
                    age = (now - slot).total_seconds()
                    if slot < enabled_at or age < 0 or age > self.schedule_grace_seconds:
                        continue
                    try:
                        run, was_created = self._create_run(
                            template,
                            trigger_type="auto",
                            scheduled_at=slot,
                            publish_time=publish_time,
                            run_key=(
                                f"x-auto:auto:v1:{template.id}:"
                                f"{day}:{publish_time.replace(':', '')}"
                            ),
                        )
                    except Exception as exc:
                        if str(getattr(exc, "code", "")) in {
                            "x_auto_template_not_enabled_for_slot",
                            "x_auto_template_version_conflict",
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
            template_id=None if not template_id else _positive_id(template_id),
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
        items = []
        for run in values[offset : offset + limit]:
            try:
                template = self.store.get_template(run.template_id, version=run.template_version)
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
            "items": items,
            "total": total,
            "summary": {
                "active": sum(run.status in {"queued", "running"} for run in values),
                "completed": sum(run.status == "completed" for run in values),
                "attention": sum(run.status in {"partial_failed", "failed"} for run in values),
            },
        }

    def run(self, run_id: Any) -> Dict[str, Any]:
        run = self.store.get_run(run_id)
        template = self.store.get_template(run.template_id, version=run.template_version)
        tasks = self.store.list_tasks(run_id=run.id)
        public_run = self._run_item(run, template=template, tasks=tasks)
        public_tasks = [self._task_item(task) for task in tasks]
        return {
            "ok": True,
            "run": public_run,
            "item": public_run,
            "tasks": public_tasks,
            "account_tasks": public_tasks,
            "events": self.store.list_events(run_id=run.id),
        }

    def execute_next(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(raw)
        if set(payload) != {"worker_id"}:
            raise AutoPostServiceError("invalid_request", "executor request is invalid", 400)
        return self.executor.execute_next(payload.get("worker_id"))


def _required_env(source: Mapping[str, str], name: str, fallback: str = "") -> str:
    value = str(source.get(name, "") or source.get(fallback, "") or "")
    if not value:
        raise AutoPostServiceError(
            "x_auto_config_missing", f"required configuration {name} is missing", 500
        )
    return value


def _env_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    fallback: str = "",
) -> int:
    raw = source.get(name, source.get(fallback, str(default)))
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if not minimum <= value <= maximum:
        raise AutoPostServiceError(
            "x_auto_config_invalid", f"configuration {name} is invalid", 500
        )
    return value


def _isolated_db_path(source: Mapping[str, str]) -> str:
    root = Path(
        str(source.get("X_AUTO_POST_STATE_ROOT", "/mnt/data-disk/x-auto-post-publisher"))
    ).expanduser()
    path = Path(
        str(
            source.get(
                "X_AUTO_POST_DB_PATH",
                "/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3",
            )
        )
    ).expanduser()
    x_path = Path(
        str(
            source.get(
                "X_POST_DB_PATH",
                "/mnt/data-disk/x-post-automation/x-posts.sqlite3",
            )
        )
    ).expanduser()
    if not root.is_absolute() or not path.is_absolute() or not x_path.is_absolute():
        raise AutoPostServiceError("x_auto_db_path_invalid", "database paths must be absolute", 500)
    try:
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        resolved_x = x_path.resolve(strict=False)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise AutoPostServiceError(
            "x_auto_db_path_invalid", "X auto database must stay inside its state root", 500
        ) from None
    if resolved_path == resolved_x:
        raise AutoPostServiceError(
            "x_auto_db_path_collision", "X auto and canonical X databases must be separate", 500
        )
    return str(resolved_path)


def _allowed_hosts(value: Any) -> Tuple[str, ...]:
    hosts = tuple(
        dict.fromkeys(
            item.strip().lower().rstrip(".")
            for item in str(value or "").replace(",", " ").split()
            if item.strip()
        )
    )
    if not hosts or any(
        not re.fullmatch(r"(?:[a-z0-9-]+\.)*[a-z0-9-]+", host) for host in hosts
    ):
        raise AutoPostServiceError(
            "x_auto_media_hosts_invalid", "media allowlist is invalid", 500
        )
    return hosts


@dataclass(frozen=True)
class _MediaConfig:
    media_allowed_hosts: Tuple[str, ...]
    max_media_bytes: int
    media_timeout: int
    work_dir: str
    repair_profile: str
    max_repairs_per_run: int


def build_service_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> XAutoPostService:
    source = os.environ if environ is None else environ
    internal_token = _required_env(source, "X_AUTO_POST_INTERNAL_TOKEN")
    bridge_token = _required_env(source, "X_POST_AUTO_INTERNAL_TOKEN")
    if not valid_internal_bearer(internal_token) or not valid_internal_bearer(bridge_token):
        raise AutoPostServiceError(
            "x_auto_internal_bearer_invalid", "internal bearer is invalid", 500
        )
    if secrets.compare_digest(internal_token, bridge_token):
        raise AutoPostServiceError(
            "x_auto_bearer_reuse_denied", "admin and X execution bearers must differ", 500
        )
    for existing_name in ("X_POST_DAILY_INTERNAL_TOKEN", "X_POST_SCHEDULE_INTERNAL_TOKEN"):
        existing = str(source.get(existing_name, "") or "")
        if existing and secrets.compare_digest(existing, bridge_token):
            raise AutoPostServiceError(
                "x_auto_bearer_reuse_denied",
                "X auto execution bearer must not reuse an existing runner bearer",
                500,
            )

    publish_timeout = _env_int(
        source, "X_AUTO_POST_X_PUBLISH_TIMEOUT", 9000, 600, 10200
    )
    execute_timeout = _env_int(
        source, "X_AUTO_POST_EXECUTE_TIMEOUT", 10200, 120, 14400
    )
    lease_seconds = _env_int(
        source, "X_AUTO_POST_LEASE_SECONDS", 10800, 120, 10800
    )
    if (
        publish_timeout + 300 > execute_timeout
        or execute_timeout + 300 > lease_seconds
    ):
        raise AutoPostServiceError(
            "x_auto_timeout_budget_invalid",
            "publish, execute and lease timeout budgets are unsafe",
            500,
        )
    bridge = XPostAutoBridgeClient(
        str(source.get("X_POST_AUTOMATION_INTERNAL_URL", DEFAULT_X_POST_INTERNAL_URL)),
        bridge_token,
        timeout=_env_int(source, "X_AUTO_POST_X_BRIDGE_TIMEOUT", 120, 5, 600),
        publish_timeout=publish_timeout,
    )
    db_path = _isolated_db_path(source)
    mysql_host = _required_env(source, "X_AUTO_POST_MYSQL_HOST", "X_POST_SCHEDULE_MYSQL_HOST")
    mysql_port = _env_int(
        source,
        "X_AUTO_POST_MYSQL_PORT",
        63350,
        1,
        65535,
        "X_POST_SCHEDULE_MYSQL_PORT",
    )
    mysql_user = _required_env(source, "X_AUTO_POST_MYSQL_USER", "X_POST_SCHEDULE_MYSQL_USER")
    mysql_password = _required_env(
        source, "X_AUTO_POST_MYSQL_PASSWORD", "X_POST_SCHEDULE_MYSQL_PASSWORD"
    )
    material_schema = str(
        source.get(
            "X_AUTO_POST_MYSQL_DATABASE",
            source.get("X_POST_SCHEDULE_MYSQL_DATABASE", "kunlunads_dev"),
        )
    ).strip()
    blacklist_schema = str(
        source.get("X_AUTO_POST_BLACKLIST_DATABASE", "ads_setting")
    ).strip()
    connect_timeout = _env_int(source, "X_AUTO_POST_MYSQL_CONNECT_TIMEOUT", 5, 1, 30)
    read_timeout = _env_int(source, "X_AUTO_POST_MYSQL_READ_TIMEOUT", 120, 5, 180)

    def source_connection():
        return connect_read_only(
            host=mysql_host,
            port=mysql_port,
            user=mysql_user,
            password=mysql_password,
            database=material_schema,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    store = XAutoPostStore(db_path)
    repository = ReadOnlyMySQLRepository(
        source_connection,
        schema=material_schema,
        blacklist_schema=blacklist_schema,
    )
    class XStrictMaterialResolver:
        def resolve(self, material_id):
            from features.x_posts.selector import (
                previous_source_date,
                select_auto_template_candidates,
            )

            connection = source_connection()
            try:
                candidates, rejections = select_auto_template_candidates(
                    connection,
                    [material_id],
                    previous_source_date(datetime.now(UTC)),
                    limit=1,
                    schema=material_schema,
                    now=datetime.now(UTC),
                )
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
            if rejections or len(candidates) != 1:
                first = rejections[0] if rejections else {}
                raise AutoPostServiceError(
                    str(first.get("error_code") or "x_auto_material_validation_failed"),
                    str(first.get("error_message") or "material failed strict X validation"),
                    409,
                )
            item = dict(candidates[0])
            return {
                "material_id": str(item.get("material_id") or ""),
                "content_id": str(item.get("content_id") or ""),
                "material_language": str(item.get("material_language") or ""),
                "description": str(item.get("description") or ""),
                "drama_name": str(item.get("drama_name") or ""),
                "material_tag": str(item.get("tag") or ""),
            }

    resolver = XStrictMaterialResolver()
    selector = TwoStageSelector(
        repository,
        MetricWindowRepository(store),
        XPostMaterialHistory(bridge),
        store,
        material_validator=ResolverMaterialValidator(resolver),
    )

    from features.x_posts.service import download_media, probe_media
    from scripts.x_post_daily_runner import (
        DEFAULT_REPAIR_PROFILE,
        MediaRepairClient,
        _preflight_candidates,
        _safe_account,
    )

    hosts_raw = source.get(
        "X_AUTO_POST_MEDIA_ALLOWED_HOSTS",
        source.get(
            "X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS",
            source.get("X_POST_DAILY_MEDIA_ALLOWED_HOSTS", ""),
        ),
    )
    media_config = _MediaConfig(
        media_allowed_hosts=_allowed_hosts(hosts_raw),
        max_media_bytes=_env_int(
            source, "X_AUTO_POST_MAX_MEDIA_BYTES", 512 * 1024 * 1024, 1024, 512 * 1024 * 1024
        ),
        media_timeout=_env_int(source, "X_AUTO_POST_MEDIA_TIMEOUT", 30, 5, 120),
        work_dir=str(
            source.get(
                "X_AUTO_POST_WORK_DIR", "/mnt/data-disk/x-post-automation/daily-work"
            )
        ),
        repair_profile=str(
            source.get("X_AUTO_POST_REPAIR_PROFILE", DEFAULT_REPAIR_PROFILE)
        ),
        max_repairs_per_run=_env_int(
            source, "X_AUTO_POST_MAX_REPAIRS_PER_RUN", 1, 0, 1
        ),
    )
    repair_url = str(
        source.get(
            "X_AUTO_POST_REPAIR_URL",
            source.get("X_POST_SCHEDULE_REPAIR_URL", ""),
        )
        or ""
    ).strip()
    repair_token = str(
        source.get(
            "X_AUTO_POST_REPAIR_TOKEN",
            source.get(
                "X_POST_SCHEDULE_REPAIR_TOKEN",
                source.get("X_POST_MEDIA_REPAIR_TOKEN", ""),
            ),
        )
        or ""
    )
    repair_client = (
        MediaRepairClient(
            repair_url,
            repair_token,
            timeout=_env_int(source, "X_AUTO_POST_REPAIR_TIMEOUT", 900, 5, 3600),
            max_output_bytes=media_config.max_media_bytes,
        )
        if repair_url
        else None
    )

    def candidate_loader(task, execution_run):
        from features.x_posts.selector import select_auto_template_candidates

        connection = source_connection()
        try:
            candidates, rejections = select_auto_template_candidates(
                connection,
                [task.material_id],
                str(execution_run.get("source_date") or ""),
                limit=1,
                schema=material_schema,
                now=datetime.now(UTC),
            )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        if rejections or len(candidates) != 1:
            first = rejections[0] if rejections else {}
            raise AutoPostServiceError(
                str(first.get("error_code") or "x_auto_source_preflight_failed"),
                str(first.get("error_message") or "selected material failed X compliance"),
                409,
            )
        candidate = dict(candidates[0])
        if str(candidate.get("material_id") or "") != str(task.material_id):
            raise AutoPostServiceError(
                "x_auto_material_identity_mismatch", "X candidate identity changed", 409
            )
        candidate["manual_item_id"] = int(task.id)
        candidate["source_type"] = "material"
        return candidate

    def media_preflight(task, account, candidate):
        timestamp = max(1, int(datetime.now(UTC).timestamp()))
        planned, rejections = _preflight_candidates(
            media_config,
            [dict(candidate)],
            [_safe_account(dict(account))],
            timestamp,
            download_media,
            probe_media,
            repair_client,
        )
        if rejections or len(planned) != 1:
            first = rejections[0] if rejections else {}
            raise AutoPostServiceError(
                str(first.get("error_code") or "x_auto_media_preflight_failed"),
                str(first.get("error_message") or "selected material failed media preflight"),
                409,
            )
        return dict(planned[0])

    executor = AutoPostExecutor(
        store,
        selector,
        bridge,
        candidate_loader,
        media_preflight,
        gates=AutoLiveGates.from_env(source),
        lease_seconds=lease_seconds,
        max_concurrent_tasks=_env_int(source, "X_AUTO_POST_WORKER_COUNT", 1, 1, 4),
    )
    return XAutoPostService(
        store,
        bridge,
        selector,
        executor,
        runner_kick_path=str(
            source.get("X_AUTO_POST_RUNNER_KICK_PATH", "/run/x-auto-post/manual-kick")
        ),
        schedule_grace_seconds=_env_int(
            source, "X_AUTO_POST_SCHEDULE_GRACE_SECONDS", 600, 60, 3600
        ),
    )


class XAutoPostHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        service: XAutoPostService,
        internal_token: str,
    ):
        host, port = server_address
        if host != DEFAULT_HOST or int(port) != DEFAULT_PORT:
            raise AutoPostServiceError(
                "x_auto_listen_invalid", "service may listen only on 127.0.0.1:18833", 500
            )
        if not valid_internal_bearer(internal_token):
            raise AutoPostServiceError(
                "x_auto_internal_bearer_invalid", "internal bearer is invalid", 500
            )
        self.x_auto_service = service
        self.internal_token = str(internal_token)
        super().__init__(server_address, XAutoPostRequestHandler)


class XAutoPostRequestHandler(BaseHTTPRequestHandler):
    server: XAutoPostHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        if contains_sensitive_key(payload):
            status = 500
            payload = {
                "ok": False,
                "error": "x_auto_unsafe_response",
                "code": "x_auto_unsafe_response",
                "message": "response contains non-public fields",
            }
        body = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
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
            raise AutoPostServiceError("invalid_request", "request body is too large", 413)
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError):
            raise AutoPostServiceError("invalid_request", "request body is invalid JSON", 400) from None
        if not isinstance(value, dict) or contains_sensitive_key(value):
            raise AutoPostServiceError("invalid_request", "request body is invalid", 400)
        return value

    def _dispatch(self) -> Tuple[int, Mapping[str, Any]]:
        parsed = urlsplit(self.path)
        path = parsed.path
        service = self.server.x_auto_service
        if self.command == "GET" and path == "/health":
            return 200, {
                "ok": True,
                "service": "x-auto-post",
                "gates": service.executor.gates.as_dict(),
            }
        if not self._authorized():
            return 403, {
                "ok": False,
                "error": "x_auto_internal_required",
                "code": "x_auto_internal_required",
                "message": "access denied",
            }
        query = parse_qs(parsed.query, keep_blank_values=True)
        if self.command == "GET" and path == X_AUTO_ADMIN_PREFIX + "/accounts":
            return 200, service.accounts()
        match = re.fullmatch(
            re.escape(X_AUTO_ADMIN_PREFIX) + r"/accounts/([1-9][0-9]*)/verify", path
        )
        if self.command == "POST" and match:
            if self._body():
                raise AutoPostServiceError(
                    "invalid_request", "account refresh body must be empty", 400
                )
            return 200, service.refresh_account(match.group(1))
        if self.command == "GET" and path == X_AUTO_ADMIN_PREFIX + "/templates":
            return 200, service.templates(query)
        match = re.fullmatch(
            re.escape(X_AUTO_ADMIN_PREFIX) + r"/templates/([1-9][0-9]*)", path
        )
        if self.command == "GET" and match:
            return 200, service.template(match.group(1))
        if self.command == "GET" and path == X_AUTO_ADMIN_PREFIX + "/runs":
            return 200, service.runs(query)
        match = re.fullmatch(
            re.escape(X_AUTO_ADMIN_PREFIX) + r"/runs/([1-9][0-9]*)", path
        )
        if self.command == "GET" and match:
            return 200, service.run(match.group(1))
        if self.command == "POST" and path == X_AUTO_ADMIN_PREFIX + "/templates":
            return 200, service.create_template(self._body())
        match = re.fullmatch(
            re.escape(X_AUTO_ADMIN_PREFIX)
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
        if self.command == "POST" and path == "/internal/x-auto-post/tick":
            if self._body():
                raise AutoPostServiceError("invalid_request", "tick body must be empty", 400)
            return 200, service.tick()
        if self.command == "POST" and path == "/internal/x-auto-post/execute-next":
            return 200, service.execute_next(self._body())
        raise AutoPostServiceError("not_found", "route not found", 404)

    def _handle(self) -> None:
        try:
            status, payload = self._dispatch()
        except Exception as exc:
            code = str(getattr(exc, "code", "x_auto_internal_error"))
            if not re.fullmatch(r"[a-z0-9_]{1,96}", code):
                code = "x_auto_internal_error"
            status = int(getattr(exc, "status", 500) or 500)
            if not 400 <= status <= 599:
                status = 500
            payload = {
                "ok": False,
                "error": code,
                "code": code,
                "message": safe_public_message(exc),
            }
        self._json(status, payload)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


def serve(environ: Optional[Mapping[str, str]] = None) -> None:
    source = os.environ if environ is None else environ
    host = str(source.get("X_AUTO_POST_SERVICE_HOST", DEFAULT_HOST)).strip()
    try:
        port = int(source.get("X_AUTO_POST_SERVICE_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError, OverflowError):
        port = 0
    service = build_service_from_env(source)
    server = XAutoPostHTTPServer(
        (host, port), service, _required_env(source, "X_AUTO_POST_INTERNAL_TOKEN")
    )
    shutting_down = threading.Event()

    def graceful_shutdown(_signum: int, _frame: Any) -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()
        threading.Thread(
            target=server.shutdown, name="x-auto-post-shutdown", daemon=True
        ).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, graceful_shutdown)
        signal.signal(signal.SIGINT, graceful_shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


__all__ = [
    "AutoPostServiceError",
    "XAutoPostHTTPServer",
    "XAutoPostService",
    "build_service_from_env",
    "serve",
]
