"""Crash-safe X auto-template execution over the canonical X publisher.

Selection and scheduling live in the isolated ``x_auto`` database.  Account
truth, global material occupancy, media policy, queue creation and final X
publishing remain in the existing X subsystem behind ``XPostAutoBridgeClient``.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Mapping, Optional

from .client import safe_public_message
from .core import TERMINAL_TASK_STATUSES, TaskRecord, XAutoPostStore
from .selector import (
    NoEligibleMaterial,
    SelectionRequest,
    SelectionRules,
    TwoStageSelector,
)
from .x_sidecar import XPostAutoBridgeClient, XPostBridgeError


UTC = timezone.utc
STANDARD_VIDEO_MAX_DURATION_SECONDS = 140
AUTO_TEMPLATE_VIDEO_MAX_DURATION_SECONDS = 600
LONG_VIDEO_SUBSCRIPTION_TYPES = frozenset({"basic", "premium", "premium_plus"})


class AutoPostExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 500):
        self.code = str(code or "x_auto_execution_failed")[:96]
        self.status = int(status)
        super().__init__(str(message or "X automatic publishing failed")[:500])


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
    account_audit_approved: bool = False
    url_property_verified: bool = False

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "AutoLiveGates":
        import os

        source = os.environ if environ is None else environ
        return cls(
            live_enabled=_flag(source, "X_AUTO_POST_LIVE_ENABLED"),
            account_audit_approved=_flag(
                source, "X_AUTO_POST_ACCOUNT_AUDIT_APPROVED"
            ),
            url_property_verified=_flag(
                source, "X_AUTO_POST_URL_PROPERTY_VERIFIED"
            ),
        )

    @property
    def is_open(self) -> bool:
        return bool(
            self.live_enabled
            and self.account_audit_approved
            and self.url_property_verified
        )

    def as_dict(self) -> Dict[str, bool]:
        return {
            "live_enabled": bool(self.live_enabled),
            "account_audit_approved": bool(self.account_audit_approved),
            "url_property_verified": bool(self.url_property_verified),
            "is_open": self.is_open,
        }


def account_duration_limit_seconds(account: Optional[Mapping[str, Any]]) -> int:
    """Return the fail-closed auto-template duration limit for one account."""

    value = account if isinstance(account, Mapping) else {}
    subscription_type = (
        str(value.get("subscription_type", "unknown") or "unknown")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if subscription_type == "premiumplus":
        subscription_type = "premium_plus"
    if (
        value.get("long_video_eligible") is True
        and subscription_type in LONG_VIDEO_SUBSCRIPTION_TYPES
    ):
        return AUTO_TEMPLATE_VIDEO_MAX_DURATION_SECONDS
    return STANDARD_VIDEO_MAX_DURATION_SECONDS


def selector_rules(
    config: Mapping[str, Any],
    *,
    account: Optional[Mapping[str, Any]] = None,
) -> SelectionRules:
    drama = (
        config.get("drama_rule")
        if isinstance(config.get("drama_rule"), Mapping)
        else {}
    )
    material = (
        config.get("material_rule")
        if isinstance(config.get("material_rule"), Mapping)
        else {}
    )
    duration_min = material.get("duration_min_seconds")
    duration_max = material.get("duration_max_seconds")
    if account is not None:
        account_limit = Decimal(account_duration_limit_seconds(account))
        try:
            configured_min = (
                None if duration_min in (None, "") else Decimal(str(duration_min))
            )
            configured_max = (
                None if duration_max in (None, "") else Decimal(str(duration_max))
            )
        except (InvalidOperation, ValueError):
            # Preserve the existing validation error for malformed historical data.
            pass
        else:
            if configured_min is not None and configured_min > account_limit:
                raise NoEligibleMaterial({"account_duration_limit": 1})
            if configured_max is None or configured_max > account_limit:
                duration_max = account_limit
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
                        "d0_roas" if drama.get("sort_by") == "roas" else "spend"
                    ),
                    "direction": drama.get("sort_direction", "desc"),
                },
                "launch_window_days": config.get("drama_launch_window_days", 0),
                "resource_types": drama.get("resource_type_v2") or [],
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
                "sort": {
                    "field": (
                        "d0_roas" if material.get("sort_by") == "roas" else "spend"
                    ),
                    "direction": material.get("sort_direction", "desc"),
                },
                "duration_seconds": {
                    "min": duration_min,
                    "max": duration_max,
                },
            },
        }
    )


class _ClaimedSelectionStore:
    def __init__(self, store: XAutoPostStore, claim_token: str):
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


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        result = 0
    else:
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            result = 0
    if result <= 0:
        raise AutoPostExecutionError(
            "x_auto_bridge_identity_invalid", f"{label} is invalid", 502
        )
    return result


def _queue_status(queue: Mapping[str, Any]) -> str:
    return str(queue.get("queue_status") or queue.get("status") or "").strip()


class AutoPostExecutor:
    """Execute one task phase at a time with immutable cross-DB identity."""

    def __init__(
        self,
        store: XAutoPostStore,
        selector: TwoStageSelector,
        bridge: XPostAutoBridgeClient,
        candidate_loader: Callable[[TaskRecord, Mapping[str, Any]], Mapping[str, Any]],
        media_preflight: Callable[
            [TaskRecord, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
        ],
        *,
        gates: Optional[AutoLiveGates] = None,
        now_fn=lambda: datetime.now(UTC),
        lease_seconds: int = 10800,
        max_concurrent_tasks: int = 1,
    ):
        if not callable(candidate_loader) or not callable(media_preflight):
            raise AutoPostExecutionError(
                "x_auto_executor_config_invalid",
                "candidate loader and media preflight are required",
                500,
            )
        self.store = store
        self.selector = selector
        self.bridge = bridge
        self.candidate_loader = candidate_loader
        self.media_preflight = media_preflight
        self.gates = gates or AutoLiveGates.from_env()
        self.now_fn = now_fn
        self.lease_seconds = int(lease_seconds)
        self.max_concurrent_tasks = int(max_concurrent_tasks)
        self._execute_slots = threading.BoundedSemaphore(self.max_concurrent_tasks)
        if not 120 <= self.lease_seconds <= 10800 or not 1 <= self.max_concurrent_tasks <= 4:
            raise AutoPostExecutionError(
                "x_auto_executor_config_invalid", "executor limits are invalid", 500
            )

    def _now(self) -> datetime:
        value = self.now_fn()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutoPostExecutionError(
                "x_auto_clock_invalid", "automatic publishing clock is invalid", 500
            )
        return value.astimezone(UTC)

    def _phase_selector(self, claim_token: str) -> TwoStageSelector:
        return TwoStageSelector(
            self.selector.source,
            self.selector.metrics,
            self.selector.history,
            _ClaimedSelectionStore(self.store, claim_token),
            material_validator=self.selector.material_validator,
            product=self.selector.product,
            app_id=self.selector.app_id,
            material_data_source=self.selector.material_data_source,
        )

    @staticmethod
    def _selection_material(task: TaskRecord) -> Mapping[str, Any]:
        value = (
            task.selection.get("material")
            if isinstance(task.selection, Mapping)
            else None
        )
        if not isinstance(value, Mapping):
            raise AutoPostExecutionError(
                "x_auto_selection_snapshot_invalid",
                "frozen material selection is invalid",
                500,
            )
        return value

    def _select(self, task: TaskRecord, claim_token: str) -> TaskRecord:
        template = self.store.get_template(
            task.template_id, version=task.template_version
        )
        request = SelectionRequest(
            run_id=task.run_id,
            task_id=task.id,
            template_id=task.template_id,
            template_version=task.template_version,
            account_id=task.account_id,
            language=task.language,
            rules=selector_rules(template.config, account=task.account_snapshot),
            now=self._now(),
        )
        self._phase_selector(claim_token).select_and_reserve(request)
        return self.store.get_task(task.id)

    @staticmethod
    def _validate_account(task: TaskRecord, raw: Mapping[str, Any]) -> Dict[str, Any]:
        account = dict(raw or {})
        account_id = _positive_int(account.get("id"), "X account ID")
        if str(account_id) != str(task.account_id):
            raise AutoPostExecutionError(
                "x_auto_account_identity_changed",
                "verified X account does not match the frozen task",
                409,
            )
        status = str(account.get("status") or "").strip().lower()
        if status in {"disabled", "disconnected", "revoke_pending"} or not bool(
            account.get("publish_approved")
        ):
            raise AutoPostExecutionError(
                "x_auto_account_not_publishable",
                "X account is not currently publishable",
                409,
            )
        return account

    @staticmethod
    def _validate_run_identity(task: TaskRecord, raw: Mapping[str, Any]) -> Dict[str, Any]:
        run = dict(raw or {})
        if (
            str(run.get("trigger_source") or "") != "auto_template"
            or run.get("account_ids") != [int(task.account_id)]
            or run.get("material_ids") != [str(task.material_id)]
            or str(run.get("external_task_key") or "") != f"x-auto-task-{task.id}"
            or str(run.get("template_ref") or "") != f"x-auto-template-{task.template_id}"
            or int(run.get("template_version") or 0) != task.template_version
        ):
            raise AutoPostExecutionError(
                "x_auto_execution_identity_mismatch",
                "X execution envelope does not match the frozen task",
                502,
            )
        _positive_int(run.get("id"), "execution run ID")
        return run

    @staticmethod
    def _run_queue(run: Mapping[str, Any], queue_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        queues = run.get("queues")
        if not isinstance(queues, list) or len(queues) > 1:
            raise AutoPostExecutionError(
                "x_auto_execution_queue_invalid",
                "X execution queue snapshot is invalid",
                502,
            )
        if not queues:
            return None
        queue = queues[0]
        if not isinstance(queue, Mapping):
            raise AutoPostExecutionError(
                "x_auto_execution_queue_invalid",
                "X execution queue snapshot is invalid",
                502,
            )
        normalized = dict(queue)
        returned_id = _positive_int(normalized.get("id"), "execution queue ID")
        if queue_id is not None and returned_id != int(queue_id):
            raise AutoPostExecutionError(
                "x_auto_execution_queue_mismatch",
                "X execution queue identity changed",
                502,
            )
        if _positive_int(normalized.get("manual_run_id"), "execution parent ID") != int(
            run.get("id")
        ):
            raise AutoPostExecutionError(
                "x_auto_execution_queue_mismatch",
                "X execution queue parent is invalid",
                502,
            )
        return normalized

    def _create_run(self, task: TaskRecord, body_template: str) -> Dict[str, Any]:
        payload = {
            "external_task_key": f"x-auto-task-{task.id}",
            "template_ref": f"x-auto-template-{task.template_id}",
            "template_version": task.template_version,
            "account_id": int(task.account_id),
            "material_id": str(task.material_id),
            "body_template": body_template,
            "actor": "x_auto_post_service",
        }
        try:
            raw = self.bridge.create_run(payload)
        except XPostBridgeError as exc:
            if not exc.unknown_outcome:
                raise
            # The stable external task key makes this retry a read-or-create,
            # never a second execution request.
            raw = self.bridge.create_run(payload)
        return self._validate_run_identity(task, raw)

    def _query_or_plan(
        self,
        task: TaskRecord,
        run: Mapping[str, Any],
        account: Mapping[str, Any],
    ) -> Dict[str, Any]:
        existing = self._run_queue(run)
        if existing is not None:
            return dict(run)
        candidate = dict(self.candidate_loader(task, run))
        candidate["body_template"] = str(run.get("body_template") or task.body_template)
        candidate["source_type"] = "material"
        candidate["source_date"] = str(run.get("source_date") or "")
        planned = dict(self.media_preflight(task, account, candidate))
        try:
            duration = float(planned.get("preflight_duration") or 0)
        except (TypeError, ValueError, OverflowError):
            duration = 0
        if not 0 < duration <= 600:
            raise AutoPostExecutionError(
                "x_auto_duration_out_of_range",
                "automatic X video duration must be at most 600 seconds",
                409,
            )
        self.bridge.storage_preflight()
        try:
            result = self.bridge.create_plan(int(run["id"]), planned)
        except XPostBridgeError as exc:
            if not exc.unknown_outcome:
                raise
            result = self.bridge.query_run(int(run["id"]))
        return self._validate_run_identity(task, result)

    def _confirm_reservation(self, task: TaskRecord, queue_id: int, claim_token: str) -> None:
        confirm = getattr(self.store, "confirm_material_reservation", None)
        if callable(confirm):
            confirm(task.id, queue_id, claim_token=claim_token)

    def _release_provisional(
        self,
        task: TaskRecord,
        claim_token: Optional[str],
        reason: str,
        *,
        allow_recorded_run: bool = False,
    ) -> TaskRecord:
        release = getattr(self.store, "release_provisional_material", None)
        if (
            not callable(release)
            or task.execution_queue_id
            or (task.execution_run_id and not allow_recorded_run)
        ):
            return task
        return release(task.id, claim_token=claim_token, reason=reason)

    def _prepare(self, task: TaskRecord, claim_token: str) -> TaskRecord:
        template = self.store.get_template(
            task.template_id, version=task.template_version
        )
        body_template = str(template.config.get("body_template") or "")
        body_hash = hashlib.sha256(body_template.encode("utf-8")).hexdigest()
        account = self._validate_account(task, task.account_snapshot)
        if task.status != "preparing":
            task = self.store.transition_task(
                task.id,
                "preparing",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "body_template": body_template,
                    "body_sha256": body_hash,
                    "body_utf16_units": len(body_template.encode("utf-16-le")) // 2,
                    "error_code": "",
                    "error_message": "",
                },
                event_type="task_x_execution_started",
            )
        run = (
            self._validate_run_identity(
                task, self.bridge.query_run(task.execution_run_id)
            )
            if task.execution_run_id
            else self._create_run(task, body_template)
        )
        run_id = int(run["id"])
        if str(run.get("status") or "") == "failed_preflight":
            raise AutoPostExecutionError(
                "x_auto_execution_failed_preflight",
                "canonical X execution is already terminal before queue creation",
                409,
            )
        if not task.execution_run_id:
            task = self.store.transition_task(
                task.id,
                "preparing",
                expected_statuses={"preparing"},
                claim_token=claim_token,
                updates={"execution_run_id": run_id},
                event_type="task_x_execution_reserved",
            )
        run = self._query_or_plan(task, run, account)
        queue = self._run_queue(run)
        if queue is None:
            raise AutoPostExecutionError(
                "x_auto_execution_plan_missing",
                "X execution plan has no canonical queue",
                503,
            )
        queue_id = int(queue["id"])
        self._confirm_reservation(task, queue_id, claim_token)
        duration = float(queue.get("preflight_duration") or 0)
        if duration <= 0:
            # Plan endpoints may omit the value from their public DTO; the
            # strict preflight already enforced it and the X queue rechecks it.
            material = self._selection_material(task)
            duration = float(material.get("video_duration") or 0)
        return self.store.transition_task(
            task.id,
            "ready",
            expected_statuses={"preparing"},
            claim_token=claim_token,
            updates={
                "execution_queue_id": queue_id,
                "selected_duration_sec": duration,
                "claim_phase": "",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "error_code": "",
                "error_message": "",
            },
            event_type="task_x_execution_ready",
        )

    def _terminal_from_run(
        self,
        task: TaskRecord,
        run: Mapping[str, Any],
        claim_token: str,
        *,
        publish_result: Optional[Mapping[str, Any]] = None,
        force_unknown: bool = False,
    ) -> TaskRecord:
        run = self._validate_run_identity(task, run)
        queue = self._run_queue(run, task.execution_queue_id)
        if queue is None:
            raise AutoPostExecutionError(
                "x_auto_execution_plan_missing", "X execution queue is missing", 503
            )
        status = _queue_status(queue)
        canonical_unknown = bool(queue.get("unknown_outcome"))
        unknown = (
            canonical_unknown
            or status == "publishing"
            or bool(task.unknown_outcome)
            or bool(force_unknown)
        )
        result = dict(publish_result or {})
        log_id = queue.get("log_id") or result.get("log_id")
        post_id = str(queue.get("post_id") or result.get("post_id") or "")
        preview_url = str(
            queue.get("preview_url") or result.get("preview_url") or ""
        )
        if status == "published":
            if not post_id:
                raise AutoPostExecutionError(
                    "x_auto_published_identity_missing",
                    "published X queue is missing its Post ID",
                    502,
                )
            updates: Dict[str, Any] = {
                "publish_id": post_id,
                "publish_url": preview_url,
                "unknown_outcome": False,
                "claim_phase": "",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "error_code": "",
                "error_message": "",
            }
            if log_id not in (None, ""):
                updates["execution_log_id"] = _positive_int(
                    log_id, "X publish log ID"
                )
            return self.store.transition_task(
                task.id,
                "published",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates=updates,
                event_type="task_x_published_confirmed",
            )
        run_status = str(run.get("status") or "")
        if run_status in {"stopped", "needs_review"}:
            retained_unknown = bool(
                task.unknown_outcome
                or force_unknown
                or canonical_unknown
                or run_status == "needs_review"
            )
            return self.store.transition_task(
                task.id,
                "failed",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "unknown_outcome": retained_unknown,
                    "claim_phase": "",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": str(
                        run.get("error_code")
                        or (
                            "x_auto_publish_outcome_unknown"
                            if retained_unknown
                            else "x_auto_publish_interrupted"
                        )
                    )[:96],
                    "error_message": safe_public_message(
                        run.get("error_message")
                        or "Canonical X execution was safely stopped"
                    ),
                },
                event_type=(
                    "task_x_publish_needs_review"
                    if retained_unknown
                    else "task_x_publish_stopped"
                ),
            )
        if status == "failed" and not canonical_unknown:
            return self.store.transition_task(
                task.id,
                "failed",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    # Retain a previously observed transport-unknown marker as
                    # historical evidence, while the canonical failed queue is
                    # sufficient to terminate and release the account.
                    "unknown_outcome": bool(task.unknown_outcome or force_unknown),
                    "claim_phase": "",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "error_code": str(queue.get("error_code") or "x_auto_x_publish_failed")[:96],
                    "error_message": safe_public_message(queue.get("error_message")),
                },
                event_type="task_x_publish_failed",
            )
        return self.store.transition_task(
            task.id,
            "retry_wait",
            expected_statuses={task.status},
            claim_token=claim_token,
            updates={
                "unknown_outcome": bool(unknown),
                "claim_phase": "reconcile",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "next_attempt_at_utc": self._now() + timedelta(minutes=5),
                "error_code": "x_auto_publish_outcome_unknown" if unknown else "",
                "error_message": "X publish outcome requires queue reconciliation" if unknown else "",
            },
            event_type="task_x_publish_reconcile_required",
        )

    def _publish(self, task: TaskRecord, claim_token: str) -> TaskRecord:
        if not task.execution_run_id or not task.execution_queue_id:
            raise AutoPostExecutionError(
                "x_auto_execution_identity_missing",
                "canonical X execution identity is missing",
                500,
            )
        publish_result: Dict[str, Any] = {}
        transport_unknown = False
        try:
            publish_result = self.bridge.publish_queue(task.execution_queue_id)
        except XPostBridgeError as exc:
            if not exc.unknown_outcome:
                # Query after a known error too: the X queue, not the HTTP
                # status, owns terminal truth.
                run = self.bridge.query_run(task.execution_run_id)
                return self._terminal_from_run(task, run, claim_token)
            transport_unknown = True
        run = self.bridge.query_run(task.execution_run_id)
        return self._terminal_from_run(
            task,
            run,
            claim_token,
            publish_result=publish_result,
            force_unknown=transport_unknown,
        )

    def _reconcile(self, task: TaskRecord, claim_token: str) -> TaskRecord:
        if not task.execution_run_id:
            raise AutoPostExecutionError(
                "x_auto_execution_identity_missing",
                "X execution run ID is missing",
                500,
            )
        run = self._validate_run_identity(
            task, self.bridge.query_run(task.execution_run_id)
        )
        queue = self._run_queue(run, task.execution_queue_id)
        if queue is None:
            if str(run.get("status") or "") != "failed_preflight":
                try:
                    run = self.bridge.record_failure(
                        task.execution_run_id,
                        "x_auto_reconcile_without_queue",
                        "X auto execution was interrupted before queue creation",
                    )
                except XPostBridgeError as exc:
                    if not exc.unknown_outcome:
                        raise
                    run = self.bridge.query_run(task.execution_run_id)
                run = self._validate_run_identity(task, run)
            if str(run.get("status") or "") != "failed_preflight":
                raise AutoPostExecutionError(
                    "x_auto_failure_record_incomplete",
                    "canonical X execution without a queue is not terminal",
                    503,
                )
            current = self._release_provisional(
                task,
                claim_token,
                "x_auto_reconcile_without_queue",
                allow_recorded_run=True,
            )
            current = self.store.get_task(task.id) if current is None else current
            updates: Dict[str, Any] = {
                "unknown_outcome": False,
                "claim_phase": "",
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "error_code": "x_auto_reconcile_without_queue",
                "error_message": "Canonical X execution ended before queue creation",
            }
            if not current.execution_run_id:
                updates["execution_run_id"] = task.execution_run_id
            return self.store.transition_task(
                current.id,
                "failed",
                expected_statuses={current.status},
                claim_token=(
                    claim_token if current.status == task.status else None
                ),
                updates=updates,
                event_type="task_x_unqueued_execution_stopped",
            )
        if _queue_status(queue) in {"queued", "publishing"}:
            recover = getattr(self.bridge, "recover_run", None)
            if not callable(recover):
                raise AutoPostExecutionError(
                    "x_auto_recovery_unavailable",
                    "exact X execution recovery is unavailable",
                    503,
                )
            recovered = recover(task.execution_run_id)
            if not isinstance(recovered, Mapping):
                raise AutoPostExecutionError(
                    "x_auto_recovery_invalid",
                    "exact X execution recovery response is invalid",
                    502,
                )
            recovered_run = recovered.get("run")
            if not isinstance(recovered_run, Mapping):
                raise AutoPostExecutionError(
                    "x_auto_recovery_invalid",
                    "exact X execution recovery did not return the run",
                    502,
                )
            run = self._validate_run_identity(task, recovered_run)
        return self._terminal_from_run(task, run, claim_token)

    def _retry_or_fail(
        self,
        task: TaskRecord,
        phase: str,
        exc: BaseException,
        claim_token: str,
    ) -> TaskRecord:
        code = str(getattr(exc, "code", "x_auto_execution_failed"))[:96]
        status = int(getattr(exc, "status", 500) or 500)
        message = safe_public_message(exc)
        if task.execution_queue_id or task.unknown_outcome or phase == "reconcile":
            return self.store.transition_task(
                task.id,
                "retry_wait",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "claim_phase": "reconcile",
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "next_attempt_at_utc": self._now() + timedelta(minutes=5),
                    "unknown_outcome": bool(task.unknown_outcome),
                    "error_code": code,
                    "error_message": message,
                },
                event_type="task_x_reconcile_retry",
            )
        if status >= 500:
            return self.store.transition_task(
                task.id,
                "retry_wait",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "claim_phase": phase,
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "next_attempt_at_utc": self._now() + timedelta(minutes=5),
                    "error_code": code,
                    "error_message": message,
                },
                event_type="task_x_transient_failure",
            )
        canonical_failure_recorded = False
        if task.execution_run_id:
            try:
                try:
                    recorded = self.bridge.record_failure(
                        task.execution_run_id, code, message
                    )
                except XPostBridgeError as record_error:
                    if not record_error.unknown_outcome:
                        raise
                    recorded = self.bridge.query_run(task.execution_run_id)
                recorded = self._validate_run_identity(task, recorded)
                if str(recorded.get("status") or "") != "failed_preflight":
                    raise AutoPostExecutionError(
                        "x_auto_failure_record_incomplete",
                        "canonical X execution failure is not terminal",
                        503,
                    )
                canonical_failure_recorded = True
            except Exception as record_error:
                return self.store.transition_task(
                    task.id,
                    "retry_wait",
                    expected_statuses={task.status},
                    claim_token=claim_token,
                    updates={
                        "claim_phase": "prepare",
                        "claim_worker": "",
                        "claim_token": "",
                        "lease_expires_at_utc": "",
                        "next_attempt_at_utc": self._now() + timedelta(minutes=5),
                        "error_code": str(
                            getattr(
                                record_error,
                                "code",
                                "x_auto_failure_record_pending",
                            )
                        )[:96],
                        "error_message": safe_public_message(record_error),
                    },
                    event_type="task_x_failure_record_retry",
                )
        target = "no_candidate" if isinstance(exc, NoEligibleMaterial) else "failed"
        try:
            current = self._release_provisional(
                task,
                claim_token,
                code,
                allow_recorded_run=canonical_failure_recorded,
            )
        except Exception as release_error:
            return self.store.transition_task(
                task.id,
                "retry_wait",
                expected_statuses={task.status},
                claim_token=claim_token,
                updates={
                    "claim_phase": phase,
                    "claim_worker": "",
                    "claim_token": "",
                    "lease_expires_at_utc": "",
                    "next_attempt_at_utc": self._now() + timedelta(minutes=5),
                    "error_code": str(
                        getattr(
                            release_error,
                            "code",
                            "x_auto_material_release_pending",
                        )
                    )[:96],
                    "error_message": safe_public_message(release_error),
                },
                event_type="task_x_material_release_retry",
            )
        current = self.store.get_task(task.id) if current is None else current
        # A store may reset the task while releasing a provisional row.  Keep
        # the canonical failed-run identity while making the unqueued material
        # reusable, then keep a deterministic terminal outcome for this attempt.
        terminal_updates: Dict[str, Any] = {
            "claim_phase": "",
            "claim_worker": "",
            "claim_token": "",
            "lease_expires_at_utc": "",
            "error_code": code,
            "error_message": message,
        }
        if task.execution_run_id and not current.execution_run_id:
            terminal_updates["execution_run_id"] = task.execution_run_id
        return self.store.transition_task(
            current.id,
            target,
            expected_statuses={current.status},
            claim_token=(claim_token if current.status == task.status else None),
            updates=terminal_updates,
            event_type="task_x_no_candidate" if target == "no_candidate" else "task_x_failed",
        )

    def _update_run(self, run_id: int) -> None:
        tasks = self.store.list_tasks(run_id=run_id)
        if not tasks:
            return
        run = self.store.get_run(run_id)
        if not all(task.status in TERMINAL_TASK_STATUSES for task in tasks):
            if run.status == "queued":
                self.store.set_run_status(run_id, "running", expected_statuses={"queued"})
            return
        failed = [task for task in tasks if task.status in {"failed", "canceled"}]
        successful = [task for task in tasks if task.status not in {"failed", "canceled"}]
        target = "failed" if failed and not successful else "partial_failed" if failed else "completed"
        if run.status not in {"completed", "partial_failed", "failed", "canceled"}:
            self.store.set_run_status(run_id, target, expected_statuses={run.status})

    def execute_next(self, worker_id: Any) -> Dict[str, Any]:
        reconcile_only = not self.gates.is_open
        worker = str(worker_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,128}", worker):
            raise AutoPostExecutionError(
                "x_auto_worker_id_invalid", "worker identity is invalid", 400
            )
        if not self._execute_slots.acquire(blocking=False):
            return {"ok": True, "claimed": False, "busy": True}
        try:
            claim = self.store.claim_next_executable_task(
                worker_id=worker,
                lease_seconds=self.lease_seconds,
                now=self._now(),
                reconcile_only=reconcile_only,
            )
            if claim is None:
                result = {
                    "ok": True,
                    "claimed": False,
                    "gates": self.gates.as_dict(),
                }
                if reconcile_only:
                    result["held"] = "live_gates_closed"
                return result
            task = claim.task
            phase = claim.claim_phase
            if reconcile_only and phase != "reconcile":
                raise AutoPostExecutionError(
                    "x_auto_closed_gate_scope_violation",
                    "closed live gates may only reconcile existing executions",
                    500,
                )
            claim_token = claim.reveal_claim_token()
            run = self.store.get_run(task.run_id)
            if run.status == "queued":
                self.store.set_run_status(run.id, "running", expected_statuses={"queued"})
            try:
                prepared_now = False
                if phase == "selection":
                    task = self._select(task, claim_token)
                    phase = "prepare"
                if phase == "prepare" or task.status in {"reserved", "preparing"}:
                    task = self._prepare(task, claim_token)
                    prepared_now = True
                if not prepared_now and phase == "publish":
                    task = self._publish(task, claim_token)
                elif not prepared_now and phase == "reconcile":
                    task = self._reconcile(task, claim_token)
            except Exception as exc:
                current = self.store.get_task(task.id)
                effective_phase = (
                    "reconcile"
                    if current.status in {"unknown", "reconciling"}
                    else phase
                )
                task = self._retry_or_fail(
                    current, effective_phase, exc, claim_token
                )
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
    "account_duration_limit_seconds",
    "selector_rules",
]
