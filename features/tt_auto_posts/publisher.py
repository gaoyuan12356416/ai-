"""Crash-safe execution of one TT automatic account task.

The selector freezes a material before this module performs any GPU work.  A
frozen material is never released.  The stable GPU job ID is reused for every
retry, and a task that may have reached Direct Post init can only reconcile.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

from features.tt_posts.core import (
    caption_uses_code_macro,
    caption_uses_drama_name_macro,
)
from features.tt_posts.service import GPUClientError

from .client import safe_public_message
from .core import (
    TASK_STATUSES,
    TTAutoPostStoreError,
    TTPostAutoStore,
    TaskRecord,
)
from .links import (
    build_auto_short_url,
    build_auto_w2a_url,
    render_auto_caption,
    write_auto_short_redirect,
)
from .selector import (
    NoEligibleMaterial,
    SelectionError,
    SelectionRequest,
    SelectionRules,
    TwoStageSelector,
)


UTC = timezone.utc
SOURCE_DIRECT_MEDIA_PROFILE = "tt-post-source-direct-v1"
ZERO_TRIM_MEDIA_PROFILES = frozenset(
    {
        SOURCE_DIRECT_MEDIA_PROFILE,
        "tt-post-random-overlay-hevc-720x1280-v1",
        "tt-post-random-overlay-h264-720x1280-v1",
    }
)
TERMINAL_TASK_STATUSES = frozenset(
    {"no_candidate", "published", "failed", "canceled", "skipped"}
)


class AutoPostExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 500):
        self.code = str(code or "tt_auto_execution_failed")[:96]
        self.status = int(status)
        super().__init__(str(message or "TT automatic publishing failed")[:500])


def _flag(source: Mapping[str, str], name: str) -> bool:
    return str(source.get(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class AutoLiveGates:
    live_enabled: bool = False
    direct_audit_approved: bool = False
    url_property_verified: bool = False

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "AutoLiveGates":
        source = os.environ if environ is None else environ
        return cls(
            live_enabled=_flag(source, "TT_AUTO_POST_LIVE_ENABLED"),
            direct_audit_approved=_flag(
                source, "TT_AUTO_POST_DIRECT_AUDIT_APPROVED"
            ),
            url_property_verified=_flag(
                source, "TT_AUTO_POST_URL_PROPERTY_VERIFIED"
            ),
        )

    @property
    def is_open(self) -> bool:
        return bool(
            self.live_enabled
            and self.direct_audit_approved
            and self.url_property_verified
        )

    def as_dict(self) -> Dict[str, bool]:
        return {
            "live_enabled": bool(self.live_enabled),
            "direct_audit_approved": bool(self.direct_audit_approved),
            "url_property_verified": bool(self.url_property_verified),
            "is_open": self.is_open,
        }


def selector_rules(config: Mapping[str, Any]) -> SelectionRules:
    drama = config.get("drama_rule") if isinstance(config.get("drama_rule"), Mapping) else {}
    material = (
        config.get("material_rule")
        if isinstance(config.get("material_rule"), Mapping)
        else {}
    )
    return SelectionRules.from_mapping(
        {
            "metric_window_days": config.get("metric_window_days", 7),
            "platform": config.get("platform", 0),
            "drama": {
                "spend": {
                    "min": drama.get("spend_min"),
                    "max": drama.get("spend_max"),
                },
                "d0_roas": {
                    "min": drama.get("roas_min"),
                    "max": drama.get("roas_max"),
                },
                "sort": {
                    "field": (
                        "d0_roas"
                        if drama.get("sort_by") == "roas"
                        else "spend"
                    ),
                    "direction": drama.get("sort_direction", "desc"),
                },
                "resource_types": drama.get("resource_type_v2") or [],
                "launch_window_days": config.get(
                    "drama_launch_window_days", 0
                ),
                "cooldown_days": config.get("cooldown_days", 0),
            },
            "material": {
                "spend": {
                    "min": material.get("spend_min"),
                    "max": material.get("spend_max"),
                },
                "d0_roas": {
                    "min": material.get("roas_min"),
                    "max": material.get("roas_max"),
                },
                "duration_seconds": {
                    "min": material.get("duration_min_seconds"),
                    "max": material.get("duration_max_seconds"),
                },
                "sort": {
                    "field": (
                        "d0_roas"
                        if material.get("sort_by") == "roas"
                        else "spend"
                    ),
                    "direction": material.get("sort_direction", "desc"),
                },
            },
        }
    )


def _https_url(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        parsed = None
        port = -1
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or len(text) > 4096
    ):
        raise AutoPostExecutionError(
            "tt_auto_media_url_invalid", f"{label}地址无效", 409
        )
    return text


def _public_tiktok_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    host = str(parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "tiktok.com" or host.endswith(".tiktok.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return text


def _stable_gpu_job_id(task: TaskRecord) -> str:
    if task.gpu_job_id:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{11,127}", task.gpu_job_id):
            raise AutoPostExecutionError(
                "tt_auto_gpu_job_id_invalid", "GPU任务身份无效", 500
            )
        return task.gpu_job_id
    digest = hashlib.sha256(
        (
            str(task.id)
            + "|"
            + task.account_id
            + "|"
            + task.material_id
            + "|"
            + task.content_id
        ).encode("utf-8")
    ).hexdigest()[:36]
    return "ttauto-%d-%s" % (task.id, digest)


class _ClaimedSelectionStore:
    """Inject the task claim token into the selector's atomic reservation."""

    def __init__(self, store: TTPostAutoStore, claim_token: str):
        self.store = store
        self.claim_token = claim_token

    def get_task_reservation(self, task_id: int):
        return self.store.get_task_reservation(task_id)

    def reserved_material_ids(self, material_ids):
        return self.store.reserved_material_ids(material_ids)

    def cooldown_content_ids(self, *args, **kwargs):
        return self.store.cooldown_content_ids(*args, **kwargs)

    def reserve_material(self, **kwargs):
        kwargs["claim_token"] = self.claim_token
        return self.store.reserve_material(**kwargs)


class AutoPostExecutor:
    def __init__(
        self,
        store: TTPostAutoStore,
        selector: TwoStageSelector,
        account_source: Any,
        gpu_client: Any,
        *,
        code_route_broker: Any = None,
        gates: Optional[AutoLiveGates] = None,
        now_fn=lambda: datetime.now(UTC),
        short_link_root: Any = "/mnt/data-disk/tt-auto-post-public/s2l",
        source_trim_tail_seconds: float = 4.333333,
        media_profile_version: str = "tt-post-direct-outro-hevc-720x1280-v2",
        lease_seconds: int = 10800,
        max_concurrent_tasks: int = 4,
    ):
        self.store = store
        self.selector = selector
        self.account_source = account_source
        self.gpu_client = gpu_client
        self.code_route_broker = code_route_broker
        self.gates = gates or AutoLiveGates.from_env()
        self.now_fn = now_fn
        self.short_link_root = str(short_link_root or "").strip()
        self.source_trim_tail_seconds = float(source_trim_tail_seconds)
        self.media_profile_version = str(media_profile_version or "").strip()
        self.lease_seconds = int(lease_seconds)
        self.max_concurrent_tasks = int(max_concurrent_tasks)
        self._execute_slots = threading.BoundedSemaphore(self.max_concurrent_tasks)
        if (
            self.media_profile_version == SOURCE_DIRECT_MEDIA_PROFILE
            and self.source_trim_tail_seconds != 0
        ):
            raise AutoPostExecutionError(
                "tt_auto_source_direct_trim_forbidden",
                "source-direct publishing requires zero source trim",
                500,
            )
        if (
            self.media_profile_version in ZERO_TRIM_MEDIA_PROFILES
            and self.source_trim_tail_seconds != 0
        ):
            raise AutoPostExecutionError(
                "tt_auto_media_profile_trim_forbidden",
                "%s requires zero source trim" % self.media_profile_version,
                500,
            )
        if (
            not Path(self.short_link_root).is_absolute()
            or not 0 <= self.source_trim_tail_seconds <= 60
            or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,128}", self.media_profile_version
            )
            or not 120 <= self.lease_seconds <= 10800
            or not 1 <= self.max_concurrent_tasks <= 16
        ):
            raise AutoPostExecutionError(
                "tt_auto_executor_config_invalid",
                "自动发布执行器配置无效",
                500,
            )

    def _now(self) -> datetime:
        value = self.now_fn()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutoPostExecutionError(
                "tt_auto_clock_invalid", "自动发布时钟无效", 500
            )
        return value.astimezone(UTC)

    def _phase_selector(self, claim_token: str) -> TwoStageSelector:
        return TwoStageSelector(
            self.selector.source,
            self.selector.metrics,
            self.selector.legacy_reader,
            _ClaimedSelectionStore(self.store, claim_token),
            material_validator=self.selector.material_validator,
            product=self.selector.product,
            app_id=self.selector.app_id,
            material_data_source=self.selector.material_data_source,
        )

    @staticmethod
    def _selection_material(task: TaskRecord) -> Mapping[str, Any]:
        value = task.selection.get("material") if isinstance(task.selection, Mapping) else None
        if not isinstance(value, Mapping):
            raise AutoPostExecutionError(
                "tt_auto_selection_snapshot_invalid",
                "冻结素材快照无效",
                500,
            )
        return value

    @staticmethod
    def _selection_drama(task: TaskRecord) -> Mapping[str, Any]:
        value = task.selection.get("drama") if isinstance(task.selection, Mapping) else None
        if not isinstance(value, Mapping):
            raise AutoPostExecutionError(
                "tt_auto_selection_snapshot_invalid",
                "冻结剧快照无效",
                500,
            )
        return value

    def _retry_or_fail(
        self,
        task: TaskRecord,
        phase: str,
        exc: BaseException,
        claim_token: Optional[str] = None,
    ) -> TaskRecord:
        code = str(getattr(exc, "code", "tt_auto_execution_failed"))[:96]
        status = int(getattr(exc, "status", 500) or 500)
        message = safe_public_message(exc)
        publish_evidence = bool(
            task.publish_id
            or task.unknown_outcome
            or task.status in {"unknown", "reconciling"}
            or phase == "reconcile"
        )
        retry_phase = "reconcile" if publish_evidence else phase
        transient_selection = status >= 500 and phase == "selection"
        transient_reserved = (
            status >= 500
            and bool(task.material_id)
            and phase in {"prepare", "publish"}
        )
        if publish_evidence or transient_selection or transient_reserved:
            retry_delay = timedelta(minutes=5)
            return self.store.transition_task(
                task.id,
                "retry_wait",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "claim_phase": retry_phase,
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "next_attempt_at_utc": self._now() + retry_delay,
                    "error_code": code,
                    "error_message": message,
                },
                event_type=(
                    "task_reconcile_retry_required"
                    if publish_evidence
                    else "task_transient_failure"
                ),
                message=message,
            )
        target = "no_candidate" if isinstance(exc, NoEligibleMaterial) else "failed"
        return self.store.transition_task(
            task.id,
            target,
            expected_statuses={task.status},
            claim_token=claim_token,
            updates={
                "claim_phase": "",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "error_code": code,
                "error_message": message,
            },
            event_type="task_selection_empty" if target == "no_candidate" else "task_failed",
            message=message,
        )

    def _select(self, task: TaskRecord, claim_token: str) -> TaskRecord:
        template = self.store.get_template(
            task.template_id, version=task.template_version
        )
        rules = selector_rules(template.config)
        request = SelectionRequest(
            run_id=task.run_id,
            task_id=task.id,
            template_id=task.template_id,
            template_version=task.template_version,
            account_id=task.account_id,
            language=task.drama_language,
            rules=rules,
            now=self._now(),
        )
        self._phase_selector(claim_token).select_and_reserve(request)
        return self.store.get_task(task.id)

    def _prepare(
        self, task: TaskRecord, claim_token: Optional[str] = None
    ) -> TaskRecord:
        material = self._selection_material(task)
        source_url = _https_url(
            material.get("source_media_url") or material.get("media_url"),
            "源素材",
        )
        job_id = _stable_gpu_job_id(task)
        caption = self._caption_and_link(task)
        if task.status != "preparing":
            task = self.store.transition_task(
                task.id,
                "preparing",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "gpu_job_id": job_id,
                    "source_media_url": source_url,
                    "caption": caption,
                    "error_code": "",
                    "error_message": "",
                },
                event_type="task_preparation_started",
            )
        elif not task.gpu_job_id or not task.source_media_url:
            task = self.store.transition_task(
                task.id,
                "preparing",
                expected_statuses={"preparing"},
                claim_token=claim_token,
                updates={
                    "gpu_job_id": job_id,
                    "source_media_url": source_url,
                    "caption": caption,
                },
                event_type="task_preparation_recovered",
            )
        prepared = self.gpu_client.prepare(
            job_id=job_id,
            material={
                "content_id": task.content_id,
                "source_media_url": source_url,
            },
            source_trim_tail_seconds=self.source_trim_tail_seconds,
            expected_profile=self.media_profile_version,
        )
        if not isinstance(prepared, Mapping):
            raise AutoPostExecutionError(
                "tt_auto_prepared_response_invalid", "GPU成片响应无效", 502
            )
        if str(prepared.get("job_id") or "") != job_id or str(
            prepared.get("content_id") or ""
        ) != task.content_id:
            raise AutoPostExecutionError(
                "tt_auto_prepared_identity_mismatch", "GPU成片身份不一致", 409
            )
        if str(prepared.get("profile") or "") != self.media_profile_version:
            raise AutoPostExecutionError(
                "tt_auto_prepared_profile_mismatch", "GPU成片版本不一致", 409
            )
        output_url = _https_url(
            prepared.get("output_url")
            or prepared.get("prepared_media_url")
            or prepared.get("final_media_url"),
            "最终成片",
        )
        if output_url == source_url:
            raise AutoPostExecutionError(
                "tt_auto_prepared_matches_source", "最终成片不能等于源素材", 409
            )
        output_sha = str(prepared.get("output_sha256") or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", output_sha):
            raise AutoPostExecutionError(
                "tt_auto_prepared_fingerprint_invalid", "最终成片指纹无效", 502
            )
        try:
            output_size = int(prepared.get("output_size") or 0)
            probe = prepared.get("probe") if isinstance(prepared.get("probe"), Mapping) else {}
            duration = float(probe.get("duration") or 0)
        except (TypeError, ValueError, OverflowError):
            output_size = 0
            duration = 0
        if (
            output_size <= 0
            or not math.isfinite(duration)
            or duration <= 0
            or duration > 3600
        ):
            raise AutoPostExecutionError(
                "tt_auto_prepared_metadata_invalid", "最终成片元数据无效", 502
            )
        return self.store.transition_task(
            task.id,
            "ready",
            expected_statuses={"preparing"},
            claim_token=claim_token,
            updates={
                "prepared_media_url": output_url,
                "prepared_output_sha256": output_sha,
                "prepared_output_size": output_size,
                "prepared_duration_sec": duration,
                "claim_phase": "",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "error_code": "",
                "error_message": "",
            },
            event_type="task_preparation_ready",
        )

    @staticmethod
    def _creator_info(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, Mapping) and isinstance(raw.get("creator_info"), Mapping):
            raw = raw.get("creator_info")
        if not isinstance(raw, Mapping):
            raise AutoPostExecutionError(
                "tt_auto_creator_info_invalid", "TikTok账号实时信息无效", 502
            )
        options = raw.get("privacy_level_options")
        try:
            maximum = int(raw.get("max_video_post_duration_sec") or 0)
        except (TypeError, ValueError, OverflowError):
            maximum = 0
        if (
            not isinstance(options, list)
            or not options
            or any(not isinstance(value, str) for value in options)
            or maximum <= 0
            or maximum > 3600
        ):
            raise AutoPostExecutionError(
                "tt_auto_creator_info_invalid", "TikTok账号实时信息无效", 502
            )
        return {
            "privacy_level_options": list(dict.fromkeys(options)),
            "comment_disabled": bool(raw.get("comment_disabled")),
            "duet_disabled": bool(raw.get("duet_disabled")),
            "stitch_disabled": bool(raw.get("stitch_disabled")),
            "max_video_post_duration_sec": maximum,
        }

    @staticmethod
    def _assert_creator_settings(
        creator: Mapping[str, Any], settings: Mapping[str, Any], duration: float
    ) -> None:
        if str(settings.get("privacy_level") or "") not in creator.get(
            "privacy_level_options", []
        ):
            raise AutoPostExecutionError(
                "tt_auto_privacy_unavailable", "账号当前不支持保存的隐私设置", 409
            )
        for setting, disabled, label in (
            ("allow_comment", "comment_disabled", "评论"),
            ("allow_duet", "duet_disabled", "Duet"),
            ("allow_stitch", "stitch_disabled", "Stitch"),
        ):
            if bool(settings.get(setting)) and bool(creator.get(disabled)):
                raise AutoPostExecutionError(
                    "tt_auto_creator_capability_changed",
                    f"账号当前不支持{label}",
                    409,
                )
        if duration > int(creator.get("max_video_post_duration_sec") or 0):
            raise AutoPostExecutionError(
                "tt_auto_duration_exceeds_account_limit",
                "最终成片超过账号实时长度限制",
                409,
            )

    def _caption_and_link(self, task: TaskRecord) -> str:
        template = self.store.get_template(
            task.template_id, version=task.template_version
        )
        material = self._selection_material(task)
        drama = self._selection_drama(task)
        short_url = build_auto_short_url(task.id)
        frozen_time = str(task.reserved_at_utc or task.created_at or "").strip()
        try:
            frozen_at = datetime.fromisoformat(frozen_time.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            frozen_at = None
        if frozen_at is None or frozen_at.tzinfo is None:
            raise AutoPostExecutionError(
                "tt_auto_link_timestamp_invalid",
                "自动发布任务的冻结时间无效",
                500,
            )
        caption_template = template.config.get("caption_template")
        drama_name = str(
            material.get("drama_name") or drama.get("name") or ""
        ).strip()
        if (
            caption_uses_drama_name_macro(caption_template)
            and not drama_name
        ):
            raise AutoPostExecutionError(
                "caption_drama_name_required",
                "发布文案使用{{drama_name}}时必须有有效剧名",
                409,
            )
        long_url = build_auto_w2a_url(
            link_id=task.id,
            username=task.account_username or task.account_id,
            # Keep the immutable task short link byte-identical across retries.
            timestamp=int(frozen_at.astimezone(UTC).timestamp()),
            language=task.drama_language,
            drama_name=drama_name or task.content_id,
            tag=material.get("material_tag") or "TTauto",
            page_name=task.account_display_name
            or task.account_username
            or task.account_id,
            page_id=task.account_id,
            material_name=material.get("material_name") or task.material_id,
            material_id=task.material_id,
            content_id=task.content_id,
        )
        write_auto_short_redirect(self.short_link_root, task.id, long_url)
        description = material.get("description") or drama.get("name")
        code = None
        if caption_uses_code_macro(caption_template):
            if self.code_route_broker is None:
                raise AutoPostExecutionError(
                    "tt_auto_code_service_not_configured",
                    "自动发布四位码服务尚未配置",
                    503,
                )
            code = self.code_route_broker.freeze_route(
                task.id,
                content_id=task.content_id,
                long_url=long_url,
                created_at=str(task.reserved_at_utc or task.created_at or ""),
            )
        rendered = render_auto_caption(
            caption_template,
            task.content_id,
            short_url=short_url,
            description=description,
            code=code,
            drama_name=drama_name,
        )
        if task.caption and task.caption != rendered:
            raise AutoPostExecutionError(
                "tt_auto_caption_frozen_conflict",
                "自动发布任务的冻结文案不一致",
                409,
            )
        return task.caption or rendered

    def _sync_code_state(
        self,
        task: TaskRecord,
        state: str,
        *,
        best_effort: bool = False,
    ) -> None:
        template = self.store.get_template(
            task.template_id, version=task.template_version
        )
        if not caption_uses_code_macro(template.config.get("caption_template")):
            return
        if self.code_route_broker is None:
            if best_effort:
                return
            raise AutoPostExecutionError(
                "tt_auto_code_service_not_configured",
                "自动发布四位码服务尚未配置",
                503,
            )
        try:
            self.code_route_broker.set_state(
                task.id,
                state=state,
                updated_at=self._now().isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
            )
        except Exception:
            if not best_effort:
                raise

    def _publish(
        self, task: TaskRecord, claim_token: Optional[str] = None
    ) -> TaskRecord:
        if task.publish_id or task.unknown_outcome or task.status in {
            "unknown",
            "reconciling",
        }:
            return self._reconcile(task, claim_token)
        if not self.gates.is_open:
            raise AutoPostExecutionError(
                "tt_auto_live_gates_closed", "自动发布生产门禁未全部打开", 409
            )
        job_id = _stable_gpu_job_id(task)
        with self.account_source.publish_credentials(task.account_id) as credentials:
            creator = self._creator_info(
                self.gpu_client.creator_info(
                    job_id=job_id,
                    source_account_id=task.account_id,
                    access_token=credentials.reveal_access_token(),
                )
            )
        self._assert_creator_settings(
            creator, task.account_settings, task.prepared_duration_sec
        )
        caption = self._caption_and_link(task)
        task = self.store.transition_task(
            task.id,
            "publishing",
            expected_statuses={task.status},
            claim_token=claim_token,
            updates={
                "caption": caption,
                "gpu_job_id": job_id,
                "error_code": "",
                "error_message": "",
            },
            event_type="task_publish_started",
        )
        self._sync_code_state(task, "publishing")
        queue = {
            **dict(task.account_settings),
            "caption": caption,
            "material_id": task.material_id,
        }
        try:
            with self.account_source.publish_credentials(task.account_id) as credentials:
                result = self.gpu_client.publish(
                    job_id=job_id,
                    source_account_id=task.account_id,
                    access_token=credentials.reveal_access_token(),
                    queue=queue,
                )
        except GPUClientError as exc:
            recovered_id = str(
                exc.details.get("publish_id")
                if isinstance(exc.details, Mapping)
                else ""
            ).strip()
            if exc.code == "tt_publish_reconcile_required" and recovered_id:
                return self.store.transition_task(
                    task.id,
                    "reconciling",
                    expected_statuses={"publishing"},
                    claim_token=claim_token,
                    updates={
                        "publish_id": recovered_id,
                        "unknown_outcome": False,
                        "claim_phase": "reconcile",
                        "claim_worker": "",
                        "claim_token": "",
                        "lease_expires_at_utc": "",
                        "error_code": exc.code,
                        "error_message": safe_public_message(exc),
                    },
                    event_type="task_publish_reconcile_required",
                )
            if exc.unknown_outcome or not exc.publish_was_not_created:
                return self.store.transition_task(
                    task.id,
                    "unknown",
                    expected_statuses={"publishing"},
                    claim_token=claim_token,
                    updates={
                        "unknown_outcome": True,
                        "claim_phase": "reconcile",
                        "claim_worker": "",
                        "claim_token": "",
                        "lease_expires_at_utc": "",
                        "error_code": exc.code,
                        "error_message": safe_public_message(exc),
                    },
                    event_type="task_publish_outcome_unknown",
                )
            raise AutoPostExecutionError(exc.code, str(exc), exc.status) from None
        if not isinstance(result, Mapping):
            return self.store.transition_task(
                task.id,
                "unknown",
                expected_statuses={"publishing"},
                claim_token=claim_token,
                updates={
                    "unknown_outcome": True,
                    "claim_phase": "reconcile",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": "tt_auto_publish_response_invalid",
                    "error_message": "GPU发布响应无效",
                },
                event_type="task_publish_outcome_unknown",
            )
        publish_id = str(result.get("publish_id") or "").strip()
        if not publish_id:
            return self.store.transition_task(
                task.id,
                "unknown",
                expected_statuses={"publishing"},
                claim_token=claim_token,
                updates={
                    "unknown_outcome": True,
                    "claim_phase": "reconcile",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": "tt_auto_publish_id_missing",
                    "error_message": "GPU发布结果缺少publish_id",
                },
                event_type="task_publish_outcome_unknown",
            )
        remote = str(result.get("state") or result.get("remote_status") or "").lower()
        if remote in {"published", "publish_complete"}:
            published = self.store.transition_task(
                task.id,
                "published",
                expected_statuses={"publishing"},
                claim_token=claim_token,
                updates={
                    "publish_id": publish_id,
                    "publish_url": _public_tiktok_url(result.get("publish_url")),
                    "unknown_outcome": False,
                    "claim_phase": "",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": "",
                    "error_message": "",
                },
                event_type="task_published",
            )
            self._sync_code_state(published, "published", best_effort=True)
            return published
        return self.store.transition_task(
            task.id,
            "reconciling",
            expected_statuses={"publishing"},
            claim_token=claim_token,
            updates={
                "publish_id": publish_id,
                "unknown_outcome": False,
                "claim_phase": "reconcile",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
            },
            event_type="task_publish_id_recorded",
        )

    def _reconcile(
        self, task: TaskRecord, claim_token: Optional[str] = None
    ) -> TaskRecord:
        job_id = _stable_gpu_job_id(task)
        with self.account_source.publish_credentials(task.account_id) as credentials:
            result = self.gpu_client.reconcile(
                job_id=job_id,
                source_account_id=task.account_id,
                access_token=credentials.reveal_access_token(),
            )
        if not isinstance(result, Mapping):
            raise AutoPostExecutionError(
                "tt_auto_reconcile_response_invalid", "GPU核对响应无效", 502
            )
        returned_id = str(result.get("publish_id") or "").strip()
        if not returned_id:
            raise AutoPostExecutionError(
                "tt_auto_reconcile_publish_id_missing",
                "GPU账本暂无可核对publish_id",
                503,
            )
        if task.publish_id and task.publish_id != returned_id:
            raise AutoPostExecutionError(
                "tt_auto_publish_id_conflict", "CPU与GPU publish_id不一致", 500
            )
        remote = str(result.get("state") or result.get("remote_status") or "").lower()
        if remote in {"published", "publish_complete"}:
            published = self.store.transition_task(
                task.id,
                "published",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "publish_id": returned_id,
                    "publish_url": _public_tiktok_url(result.get("publish_url")),
                    "unknown_outcome": False,
                    "claim_phase": "",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": "",
                    "error_message": "",
                },
                event_type="task_reconciled_published",
            )
            self._sync_code_state(published, "published", best_effort=True)
            return published
        if remote in {"failed", "publish_failed"}:
            return self.store.transition_task(
                task.id,
                "failed",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "publish_id": returned_id,
                    "unknown_outcome": False,
                    "claim_phase": "",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": "tt_auto_remote_publish_failed",
                    "error_message": "TikTok远端发布失败",
                },
                event_type="task_reconciled_failed",
            )
        return self.store.transition_task(
            task.id,
            "reconciling",
            expected_statuses={task.status},
            claim_token=claim_token,
            updates={
                "publish_id": returned_id,
                "unknown_outcome": False,
                "claim_phase": "reconcile",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
            },
            event_type="task_reconcile_pending",
        )

    def _update_run(self, run_id: int) -> None:
        tasks = self.store.list_tasks(run_id=run_id)
        if not tasks:
            return
        if not all(task.status in TERMINAL_TASK_STATUSES for task in tasks):
            self._ensure_run_running(run_id)
            return
        failed = [task for task in tasks if task.status in {"failed", "canceled"}]
        successful = [task for task in tasks if task.status not in {"failed", "canceled"}]
        target = (
            "failed"
            if failed and not successful
            else "partial_failed"
            if failed
            else "completed"
        )
        run = self.store.get_run(run_id)
        if run.status not in {"completed", "partial_failed", "failed", "canceled"}:
            self.store.set_run_status(
                run_id,
                target,
                expected_statuses={run.status},
            )

    def _ensure_run_running(self, run_id: int):
        """Idempotently start a run when concurrent account workers race."""
        run = self.store.get_run(run_id)
        if run.status != "queued":
            return run
        try:
            return self.store.set_run_status(
                run_id,
                "running",
                expected_statuses={"queued"},
            )
        except TTAutoPostStoreError as exc:
            current = self.store.get_run(run_id)
            if (
                exc.code == "tt_auto_run_status_conflict"
                and current.status == "running"
            ):
                return current
            raise

    def execute_next(self, worker_id: Any) -> Dict[str, Any]:
        if not self.gates.is_open:
            return {
                "ok": True,
                "claimed": False,
                "held": "live_gates_closed",
                "gates": self.gates.as_dict(),
            }
        worker = str(worker_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,128}", worker):
            raise AutoPostExecutionError(
                "tt_auto_worker_id_invalid", "执行器身份无效", 400
            )
        if not self._execute_slots.acquire(blocking=False):
            return {"ok": True, "claimed": False, "busy": True}
        try:
            claim = self.store.claim_next_executable_task(
                worker_id=worker,
                lease_seconds=self.lease_seconds,
                now=self._now(),
            )
            if claim is None:
                return {"ok": True, "claimed": False, "gates": self.gates.as_dict()}
            task = claim.task
            phase = claim.claim_phase
            claim_token = claim.reveal_claim_token()
            self._ensure_run_running(task.run_id)
            try:
                prepared_now = False
                if phase == "selection":
                    task = self._select(task, claim_token)
                    phase = "prepare"
                if phase == "prepare" or task.status in {"reserved", "preparing"}:
                    task = self._prepare(task, claim_token)
                    # `ready` deliberately releases the preparation claim.
                    # Publishing must begin in a later execute call which
                    # atomically claims ready -> publishing with a fresh lease.
                    prepared_now = True
                if not prepared_now and (
                    phase == "publish" or task.status in {"ready", "publishing"}
                ):
                    task = self._publish(task, claim_token)
                elif not prepared_now and (
                    phase == "reconcile"
                    or task.status in {"unknown", "reconciling"}
                ):
                    task = self._reconcile(task, claim_token)
            except NoEligibleMaterial as exc:
                task = self._retry_or_fail(
                    self.store.get_task(task.id), phase, exc, claim_token
                )
            except Exception as exc:
                current = self.store.get_task(task.id)
                if current.status in {"unknown", "reconciling"}:
                    phase = "reconcile"
                task = self._retry_or_fail(current, phase, exc, claim_token)
            self._update_run(task.run_id)
            return {
                "ok": True,
                "claimed": True,
                "task": task.as_dict(),
                "gates": self.gates.as_dict(),
            }
        finally:
            self._execute_slots.release()


__all__ = [
    "AutoLiveGates",
    "AutoPostExecutionError",
    "AutoPostExecutor",
    "selector_rules",
]
