#!/usr/bin/env python3
"""Run configured X Post schedule points with a strict 90-second grace window.

The runner owns no X credentials.  It uses the existing daily internal bearer,
reads ad/drama metadata through the read-only MySQL endpoint, freezes a plan in
the X sidecar, and publishes only queue identities returned by that plan.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import random
import re
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.drama_selector import (  # noqa: E402
    DRAMAWAVE_APP_ID,
    DramaPoolRejection,
    DramaQueryError,
    DramaSelectionError,
    select_drama_pool_episodes,
)
from features.x_accounts.language import (  # noqa: E402
    canonical_drama_language,
    same_drama_language,
)
from features.x_posts.selector import (  # noqa: E402
    CandidateQueryError,
    CandidateSelectionError,
    DEFAULT_SCHEMA,
    normalize_date,
    previous_source_date,
    select_pool_candidates,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    DEFAULT_SHORT_BASE_URL,
    XPostError,
    build_drama_episode_post_text,
    build_w2a_url,
    download_media,
    probe_media,
    redact_text,
)
from scripts.x_post_daily_runner import (  # noqa: E402
    DEFAULT_INTERNAL_URL,
    DEFAULT_REPAIR_PROFILE,
    MAX_DAILY_ACCOUNTS,
    CandidatePreflightError,
    MediaRepairClient,
    SidecarClient,
    SidecarError,
    _connect_from_config,
    _parse_hosts,
    _plan_candidate,
    _preflight_candidate,
    _record_pool_checks_best_effort,
    _safe_account,
    process_lock,
)


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
FIXED_TIMEZONE = "Asia/Shanghai"
DEFAULT_GRACE_SECONDS = 90
DEFAULT_DUE_PATH = "/internal/posts/schedules/due"
DEFAULT_PREVIOUS_DAY_DUE_PATH = "/internal/posts/schedules/previous-day-due"
DEFAULT_PLAN_QUERY_PATH = "/internal/posts/schedule-plan/query"
DEFAULT_PLAN_PATH = "/internal/posts/schedule-plan"
DEFAULT_FAILURE_PATH = "/internal/posts/schedule-runs/record-failure"
DEFAULT_HEARTBEAT_PATH = "/internal/posts/schedule-runs/heartbeat"
DEFAULT_DRAMA_POOL_PATH = "/internal/posts/drama-pool/available"
DEFAULT_DRAMA_CHECK_PATH = "/internal/posts/drama-pool/check"
DEFAULT_PREMIUM_RELAY_ACCOUNTS_PATH = (
    "/internal/posts/premium-relay/accounts"
)
DEFAULT_MATERIAL_POOL_PATH = "/internal/posts/material-pool/available"
DEFAULT_MATERIAL_CHECK_PATH = "/internal/posts/material-pool/check"
DEFAULT_STORAGE_PREFLIGHT_PATH = "/internal/posts/storage/preflight"
DEFAULT_PUBLISH_PATH = "/internal/posts/queue/{queue_id}/publish"
DEFAULT_WORK_DIR = "/mnt/data-disk/x-post-automation/daily-work"
DEFAULT_LOCK_PATH = "/run/x-post-daily/runner.lock"
MAX_DUE_BATCHES = 10
MAX_POOL_SCAN = 1000
SCHEDULE_MEDIA_DOWNLOAD_ATTEMPTS = 3
_SAFE_INTERNAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_SOURCE_TYPES = {"material", "drama"}
_QUEUE_STATUSES = {
    "queued",
    "reserved",
    "publishing",
    "published",
    "failed",
    "waiting_relay",
}
_DRAMA_DETERMINISTIC_REJECTION_CODES = frozenset(
    {
        "invalid_media_duration",
        "invalid_media_frame_rate",
        "invalid_media_scan",
        "invalid_media_type",
        "invalid_media_url",
        "media_host_not_allowed",
        "media_too_large",
        "source_not_repairable",
        "x_long_video_requires_premium",
        "x_post_daily_copy_validation_failed",
    }
)
MATERIAL_ASSIGNMENT_VERSION = "material-random-relay-v1"


class MaterialScheduleCandidates(list):
    """List-compatible plan carrying clean, run-scoped FIFO skip proof."""

    def __init__(self, values=(), *, fifo_capacity_skips=None):
        super().__init__(values)
        self.fifo_capacity_skips = list(fifo_capacity_skips or [])


def _is_ordered_account_subset(values, configured):
    """Return whether values is a non-empty ordered subset of configured."""
    if (
        not values
        or len(values) > len(configured)
        or len(set(values)) != len(values)
    ):
        return False
    positions = {account_id: index for index, account_id in enumerate(configured)}
    try:
        ranks = [positions[account_id] for account_id in values]
    except KeyError:
        return False
    return ranks == sorted(ranks)


class ScheduleRunError(RuntimeError):
    def __init__(
        self,
        message,
        code="x_post_schedule_failed",
        *,
        drama_pool_item_id=None,
        content_id="",
    ):
        self.code = str(code or "x_post_schedule_failed")[:64]
        self.drama_pool_item_id = drama_pool_item_id
        self.content_id = str(content_id or "")
        super().__init__(redact_text(message, 240))


def _env_value(name, fallback_name="", default=""):
    value = os.environ.get(name)
    if value is None and fallback_name:
        value = os.environ.get(fallback_name)
    return str(default if value is None else value).strip()


def _env_secret_value(name, fallback_name=""):
    value = os.environ.get(name)
    if value is None and fallback_name:
        value = os.environ.get(fallback_name)
    return "" if value is None else str(value)


def _env_int(name, fallback_name, default, minimum, maximum):
    raw = os.environ.get(name)
    if raw is None and fallback_name:
        raw = os.environ.get(fallback_name)
    try:
        value = int(str(raw if raw is not None else default))
    except (TypeError, ValueError, OverflowError):
        value = int(default)
    return max(int(minimum), min(value, int(maximum)))


def _env_bool(name, default=False):
    raw = str(
        os.environ.get(name, "1" if default else "0") or ""
    ).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise ScheduleRunError(
        "%s must be 0 or 1" % name,
        "x_post_schedule_invalid_configuration",
    )


def _endpoint_path(value, *, template=False):
    value = str(value or "").strip()
    if not value.startswith("/internal/") or "?" in value or "#" in value:
        raise ScheduleRunError("invalid sidecar endpoint path")
    if template and "{queue_id}" not in value:
        raise ScheduleRunError("publish endpoint must contain {queue_id}")
    return value


@dataclass(frozen=True)
class ScheduleConfig:
    internal_url: str
    internal_token: str
    start_date: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_connect_timeout: int
    mysql_read_timeout: int
    scan_limit: int
    candidate_pool_limit: int
    media_allowed_hosts: tuple
    max_media_bytes: int
    media_timeout: int
    internal_timeout: int
    lock_path: str
    work_dir: str
    grace_seconds: int
    max_due_batches: int
    due_path: str
    plan_query_path: str
    plan_path: str
    failure_path: str
    material_pool_path: str
    material_check_path: str
    drama_pool_path: str
    drama_check_path: str
    storage_preflight_path: str
    publish_path_template: str
    repair_url: str = ""
    repair_token: str = ""
    repair_timeout: int = 900
    repair_profile: str = DEFAULT_REPAIR_PROFILE
    max_repairs_per_run: int = 6
    drama_app_id: int = DRAMAWAVE_APP_ID
    previous_day_recovery_reason: str = ""
    previous_day_deployed_commit: str = ""
    drama_duration_routing_enabled: bool = False

    @property
    def pool_check_path(self):
        """Compatibility name used by the existing material preflight helper."""
        return self.material_check_path

    @classmethod
    def from_env(cls):
        allowed_hosts_raw = _env_value(
            "X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS",
            "X_POST_DAILY_MEDIA_ALLOWED_HOSTS",
        )
        return cls(
            internal_url=_env_value(
                "X_POST_SCHEDULE_INTERNAL_URL",
                "X_POST_DAILY_INTERNAL_URL",
                DEFAULT_INTERNAL_URL,
            ).rstrip("/"),
            internal_token=_env_value(
                "X_POST_SCHEDULE_INTERNAL_TOKEN",
                "X_POST_DAILY_INTERNAL_TOKEN",
            ),
            start_date=_env_value(
                "X_POST_SCHEDULE_START_DATE",
                "X_POST_DAILY_START_DATE",
            ),
            mysql_host=_env_value(
                "X_POST_SCHEDULE_MYSQL_HOST", "X_POST_DAILY_MYSQL_HOST"
            ),
            mysql_port=_env_int(
                "X_POST_SCHEDULE_MYSQL_PORT",
                "X_POST_DAILY_MYSQL_PORT",
                63350,
                1,
                65535,
            ),
            mysql_user=_env_value(
                "X_POST_SCHEDULE_MYSQL_USER", "X_POST_DAILY_MYSQL_USER"
            ),
            mysql_password=_env_secret_value(
                "X_POST_SCHEDULE_MYSQL_PASSWORD", "X_POST_DAILY_MYSQL_PASSWORD"
            ),
            mysql_database=_env_value(
                "X_POST_SCHEDULE_MYSQL_DATABASE",
                "X_POST_DAILY_MYSQL_DATABASE",
                DEFAULT_SCHEMA,
            ),
            mysql_connect_timeout=_env_int(
                "X_POST_SCHEDULE_MYSQL_CONNECT_TIMEOUT",
                "X_POST_DAILY_MYSQL_CONNECT_TIMEOUT",
                5,
                1,
                30,
            ),
            mysql_read_timeout=_env_int(
                "X_POST_SCHEDULE_MYSQL_READ_TIMEOUT",
                "X_POST_DAILY_MYSQL_READ_TIMEOUT",
                30,
                5,
                180,
            ),
            scan_limit=_env_int(
                "X_POST_SCHEDULE_SCAN_LIMIT",
                "X_POST_DAILY_SCAN_LIMIT",
                1000,
                1,
                MAX_POOL_SCAN,
            ),
            candidate_pool_limit=_env_int(
                "X_POST_SCHEDULE_CANDIDATE_POOL_LIMIT",
                "X_POST_DAILY_CANDIDATE_POOL_LIMIT",
                50,
                1,
                100,
            ),
            media_allowed_hosts=_parse_hosts(allowed_hosts_raw),
            max_media_bytes=_env_int(
                "X_POST_SCHEDULE_MAX_MEDIA_BYTES",
                "X_POST_DAILY_MAX_MEDIA_BYTES",
                512 * 1024 * 1024,
                1024,
                512 * 1024 * 1024,
            ),
            media_timeout=_env_int(
                "X_POST_SCHEDULE_MEDIA_TIMEOUT",
                "X_POST_DAILY_MEDIA_TIMEOUT",
                30,
                5,
                120,
            ),
            internal_timeout=_env_int(
                "X_POST_SCHEDULE_INTERNAL_TIMEOUT",
                "X_POST_DAILY_INTERNAL_TIMEOUT",
                900,
                5,
                7200,
            ),
            lock_path=_env_value(
                "X_POST_SCHEDULE_LOCK_PATH",
                "X_POST_DAILY_LOCK_PATH",
                DEFAULT_LOCK_PATH,
            ),
            work_dir=_env_value(
                "X_POST_SCHEDULE_WORK_DIR",
                "X_POST_DAILY_WORK_DIR",
                DEFAULT_WORK_DIR,
            ),
            grace_seconds=_env_int(
                "X_POST_SCHEDULE_GRACE_SECONDS",
                "",
                DEFAULT_GRACE_SECONDS,
                1,
                DEFAULT_GRACE_SECONDS,
            ),
            max_due_batches=_env_int(
                "X_POST_SCHEDULE_MAX_DUE_BATCHES",
                "",
                10,
                1,
                MAX_DUE_BATCHES,
            ),
            due_path=_env_value(
                "X_POST_SCHEDULE_DUE_PATH", "", DEFAULT_DUE_PATH
            ),
            plan_query_path=_env_value(
                "X_POST_SCHEDULE_PLAN_QUERY_PATH", "", DEFAULT_PLAN_QUERY_PATH
            ),
            plan_path=_env_value(
                "X_POST_SCHEDULE_PLAN_PATH", "", DEFAULT_PLAN_PATH
            ),
            failure_path=_env_value(
                "X_POST_SCHEDULE_FAILURE_PATH", "", DEFAULT_FAILURE_PATH
            ),
            material_pool_path=_env_value(
                "X_POST_SCHEDULE_MATERIAL_POOL_PATH",
                "X_POST_DAILY_POOL_AVAILABLE_PATH",
                DEFAULT_MATERIAL_POOL_PATH,
            ),
            material_check_path=_env_value(
                "X_POST_SCHEDULE_MATERIAL_CHECK_PATH",
                "X_POST_DAILY_POOL_CHECK_PATH",
                DEFAULT_MATERIAL_CHECK_PATH,
            ),
            drama_pool_path=_env_value(
                "X_POST_SCHEDULE_DRAMA_POOL_PATH", "", DEFAULT_DRAMA_POOL_PATH
            ),
            drama_check_path=_env_value(
                "X_POST_SCHEDULE_DRAMA_CHECK_PATH", "", DEFAULT_DRAMA_CHECK_PATH
            ),
            storage_preflight_path=_env_value(
                "X_POST_SCHEDULE_STORAGE_PREFLIGHT_PATH",
                "X_POST_DAILY_STORAGE_PREFLIGHT_PATH",
                DEFAULT_STORAGE_PREFLIGHT_PATH,
            ),
            publish_path_template=_env_value(
                "X_POST_SCHEDULE_PUBLISH_PATH_TEMPLATE",
                "X_POST_DAILY_PUBLISH_PATH_TEMPLATE",
                DEFAULT_PUBLISH_PATH,
            ),
            repair_url=_env_value(
                "X_POST_SCHEDULE_REPAIR_URL", "X_POST_DAILY_REPAIR_URL"
            ),
            repair_token=_env_value(
                "X_POST_SCHEDULE_REPAIR_TOKEN", "X_POST_DAILY_REPAIR_TOKEN"
            )
            or _env_value("X_POST_MEDIA_REPAIR_TOKEN"),
            repair_timeout=_env_int(
                "X_POST_SCHEDULE_REPAIR_TIMEOUT",
                "X_POST_DAILY_REPAIR_TIMEOUT",
                900,
                5,
                3600,
            ),
            repair_profile=_env_value(
                "X_POST_SCHEDULE_REPAIR_PROFILE",
                "X_POST_DAILY_REPAIR_PROFILE",
                DEFAULT_REPAIR_PROFILE,
            ),
            max_repairs_per_run=_env_int(
                "X_POST_SCHEDULE_MAX_REPAIRS_PER_RUN",
                "X_POST_DAILY_MAX_REPAIRS_PER_RUN",
                6,
                0,
                50,
            ),
            drama_app_id=_env_int(
                "X_POST_SCHEDULE_DRAMA_APP_ID",
                "",
                DRAMAWAVE_APP_ID,
                1,
                9223372036854775807,
            ),
            drama_duration_routing_enabled=_env_bool(
                "X_POST_DRAMA_DURATION_ROUTING_ENABLED", False
            ),
        )

    def validate(self):
        if not isinstance(self.drama_duration_routing_enabled, bool):
            raise ScheduleRunError(
                "drama duration routing flag must be boolean",
                "x_post_schedule_invalid_configuration",
            )
        parsed = urllib.parse.urlsplit(self.internal_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _SAFE_INTERNAL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ScheduleRunError("X Post sidecar URL must be loopback HTTP")
        if not self.internal_token:
            raise ScheduleRunError("existing daily internal bearer is required")
        if self.start_date:
            normalize_date(self.start_date, "start_date")
        if (
            not self.mysql_host
            or not self.mysql_user
            or self.mysql_password == ""
            or not re.fullmatch(r"[A-Za-z0-9_]+", self.mysql_database)
        ):
            raise ScheduleRunError("read-only MySQL configuration is incomplete")
        if self.candidate_pool_limit > self.scan_limit:
            raise ScheduleRunError("candidate pool limit cannot exceed scan limit")
        work_dir = Path(self.work_dir)
        if not work_dir.is_absolute():
            raise ScheduleRunError("schedule work directory must be absolute")
        if os.name != "nt" and work_dir != Path(DEFAULT_WORK_DIR):
            raise ScheduleRunError("schedule runner must use the fixed data-disk work directory")
        if self.grace_seconds != DEFAULT_GRACE_SECONDS:
            raise ScheduleRunError("schedule grace window must remain exactly 90 seconds")
        if bool(self.previous_day_recovery_reason) != bool(
            self.previous_day_deployed_commit
        ):
            raise ScheduleRunError("previous-day recovery audit scope is incomplete")
        if self.previous_day_recovery_reason and (
            self.previous_day_recovery_reason
            != "operator_previous_day_stale_claim_recovery_v1"
            or not re.fullmatch(
                r"[a-f0-9]{40}", self.previous_day_deployed_commit
            )
            or self.due_path != DEFAULT_PREVIOUS_DAY_DUE_PATH
        ):
            raise ScheduleRunError("previous-day recovery audit scope is invalid")
        for path in (
            self.due_path,
            self.plan_query_path,
            self.plan_path,
            self.failure_path,
            self.material_pool_path,
            self.material_check_path,
            self.drama_pool_path,
            self.drama_check_path,
            self.storage_preflight_path,
        ):
            _endpoint_path(path)
        _endpoint_path(self.publish_path_template, template=True)
        if self.repair_url and not self.repair_token:
            raise ScheduleRunError("media repair token is required")
        if self.repair_url and self.repair_token == self.internal_token:
            raise ScheduleRunError("media repair bearer must be independent")
        if self.repair_url:
            repair = urllib.parse.urlsplit(self.repair_url)
            try:
                repair_port = repair.port
            except ValueError:
                raise ScheduleRunError("media repair URL is invalid") from None
            if (
                repair.scheme != "http"
                or repair.hostname not in _SAFE_INTERNAL_HOSTS
                or repair.username is not None
                or repair.password is not None
                or repair.query
                or repair.fragment
                or repair_port not in {None, 18820}
                or not repair.path.startswith("/internal/")
            ):
                raise ScheduleRunError(
                    "media repair URL must be the loopback repair endpoint"
                )


def _schedule_identity(raw):
    if not isinstance(raw, dict):
        raise SidecarError(
            "x_post_schedule_invalid_response", "Due schedule item is invalid"
        )
    source_type = str(raw.get("source_type", "") or "").strip().lower()
    run_date = normalize_date(raw.get("run_date"), "run_date")
    publish_time = str(raw.get("publish_time", "") or "").strip()
    if source_type not in _SOURCE_TYPES or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", publish_time
    ):
        raise SidecarError(
            "x_post_schedule_invalid_response", "Due schedule identity is invalid"
        )
    version = raw.get("version")
    account_ids = raw.get("account_ids")
    frozen = raw.get("frozen", False)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version <= 0
        or not isinstance(account_ids, list)
        or not 1 <= len(account_ids) <= MAX_DAILY_ACCOUNTS
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in account_ids
        )
        or len(set(account_ids)) != len(account_ids)
        or not isinstance(frozen, bool)
    ):
        raise SidecarError(
            "x_post_schedule_invalid_response", "Due schedule scope is invalid"
        )
    return {
        "source_type": source_type,
        "run_date": run_date,
        "publish_time": publish_time,
        "version": version,
        "account_ids": list(account_ids),
        "frozen": frozen,
    }


def _scheduled_at(item):
    return datetime.strptime(
        item["run_date"] + " " + item["publish_time"], "%Y-%m-%d %H:%M"
    ).replace(tzinfo=BEIJING_TZ)


class ScheduleSidecarClient(SidecarClient):
    """Strict parser for the schedule-only internal API surface."""

    def verify_account(self, account_id):
        return super().verify_account(account_id, schedule_preflight=True)

    def publish_queue(self, path_template, queue_id):
        """Accept a zero-X-write relay wait while preserving publish validation."""
        path = path_template.format(queue_id=int(queue_id))
        result = self.post(path, {}, write_may_have_happened=True)
        item = (
            result.get("item")
            if isinstance(result, dict)
            and isinstance(result.get("item"), dict)
            else result
        )
        if isinstance(item, dict) and item.get("status") == "waiting_relay":
            delivery_mode = str(item.get("delivery_mode", "") or "")
            response_queue_id = item.get("queue_id")
            error_code = str(item.get("error_code", "") or "")
            duration_present = (
                "preflight_duration" in item or "final_duration" in item
            )
            duration_value = item.get(
                "preflight_duration", item.get("final_duration")
            )
            invalid_duration = False
            if duration_present:
                routing_disabled = bool(
                    error_code
                    == "x_post_drama_duration_routing_disabled"
                )
                invalid_duration = (
                    not isinstance(duration_value, (int, float))
                    or isinstance(duration_value, bool)
                    or not math.isfinite(float(duration_value))
                    or (
                        float(duration_value) < 0.0
                        if routing_disabled
                        else float(duration_value) <= 140.0
                    )
                )
            if (
                delivery_mode != "duration_pending"
                or not isinstance(response_queue_id, int)
                or isinstance(response_queue_id, bool)
                or response_queue_id != int(queue_id)
                or not duration_present
                or invalid_duration
                or (
                    bool(error_code)
                    and not re.fullmatch(
                        r"[A-Za-z0-9_.:-]{1,64}", error_code
                    )
                )
                or bool(item.get("unknown_outcome"))
                or any(
                    item.get(key) not in (None, "", 0)
                    for key in (
                        "log_id",
                        "post_id",
                        "preview_url",
                        "short_url",
                    )
                )
            ):
                raise SidecarError(
                    "x_publish_invalid_response",
                    "Relay-wait response is incomplete or inconsistent",
                    502,
                    unknown_outcome=True,
                )
            waiting = {
                "status": "waiting_relay",
                "queue_id": int(queue_id),
                "delivery_mode": "duration_pending",
                "preflight_duration": float(duration_value),
                "error_code": error_code,
            }
            return waiting

        class _ResponseReplay:
            def post(self, *_args, **_kwargs):
                return result

        # Reuse the daily publisher's strict successful-Post validator without
        # issuing a second HTTP request to the write endpoint.
        return SidecarClient.publish_queue(
            _ResponseReplay(), path_template, queue_id
        )

    def due_schedules(
        self,
        path,
        *,
        current,
        grace_seconds,
        limit,
        operator_reason="",
        deployed_commit="",
    ):
        payload = {
            "now": current.isoformat(timespec="seconds"),
            "run_date": current.date().isoformat(),
            "grace_seconds": int(grace_seconds),
            "limit": int(limit),
        }
        if operator_reason or deployed_commit:
            payload["operator_reason"] = str(operator_reason)
            payload["deployed_commit"] = str(deployed_commit)
        result = self.post(
            path,
            payload,
        )
        raw_items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(raw_items, list) or len(raw_items) > int(limit):
            raise SidecarError(
                "x_post_schedule_invalid_response",
                "Due schedule response is invalid",
            )
        items = []
        seen = set()
        for raw in raw_items:
            item = _schedule_identity(raw)
            key = (
                item["source_type"],
                item["run_date"],
                item["publish_time"],
            )
            if key in seen:
                raise SidecarError(
                    "x_post_schedule_invalid_response",
                    "Due schedule response contains duplicates",
                )
            seen.add(key)
            items.append(item)
        # Preserve the sidecar's priority order: points inside the current
        # grace window come before same-day frozen backlog.
        return items

    def query_schedule_plan(self, path, identity):
        payload = {
            key: identity[key]
            for key in ("source_type", "run_date", "publish_time", "version")
        }
        result = self.post(path, payload)
        item = result.get("item") if isinstance(result, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"found", "run", "queues"}
            or not isinstance(item.get("found"), bool)
            or not isinstance(item.get("queues"), list)
        ):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Schedule plan query response is invalid",
            )
        if not item["found"]:
            if item["run"] is not None or item["queues"]:
                raise SidecarError(
                    "x_post_schedule_plan_invalid_response",
                    "Missing schedule plan response is inconsistent",
                )
            return {"found": False, "run": None, "queues": []}
        if not isinstance(item["run"], dict):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Schedule run identity is invalid",
            )
        run = item["run"]
        if (
            run.get("source_type") != identity["source_type"]
            or run.get("run_date") != identity["run_date"]
            or run.get("publish_time") != identity["publish_time"]
            or run.get("config_version") != identity["version"]
            or run.get("account_ids") != identity["account_ids"]
            or not isinstance(run.get("expected_count"), int)
            or isinstance(run.get("expected_count"), bool)
            or not 1
            <= run.get("expected_count")
            <= len(identity["account_ids"])
        ):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Schedule run identity does not match the requested point",
            )
        queues = self._normalize_queues(
            item["queues"], identity["account_ids"]
        )
        queue_account_ids = [queue["account_id"] for queue in queues]
        if queues and (
            len(queues) != run["expected_count"]
            or not _is_ordered_account_subset(
                queue_account_ids, identity["account_ids"]
            )
        ):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Frozen queue account order is inconsistent",
            )
        return {
            "found": True,
            "run": dict(run),
            "queues": queues,
        }

    @staticmethod
    def _normalize_queues(raw_queues, account_ids):
        if not isinstance(raw_queues, list) or len(raw_queues) > len(account_ids):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Schedule queue response is invalid",
            )
        queues = []
        seen_ids = set()
        seen_accounts = set()
        previous_rank = 0
        for raw in raw_queues:
            if not isinstance(raw, dict):
                raise SidecarError(
                    "x_post_schedule_plan_invalid_response",
                    "Schedule queue identity is invalid",
                )
            queue_id = raw.get("id")
            account_id = raw.get("account_id")
            rank = raw.get("candidate_rank")
            status = raw.get("status")
            unknown = raw.get("unknown_outcome", False)
            error_code = raw.get("error_code", "")
            delivery_mode = str(
                raw.get("delivery_mode", "direct") or "direct"
            )
            relay_account_id = raw.get("relay_account_id", 0)
            repost_status = str(raw.get("repost_status", "") or "")
            duration_present = (
                "preflight_duration" in raw or "final_duration" in raw
            )
            duration_value = raw.get(
                "preflight_duration", raw.get("final_duration", 0.0)
            )
            valid_duration = (
                isinstance(duration_value, (int, float))
                and not isinstance(duration_value, bool)
                and math.isfinite(float(duration_value))
                and float(duration_value) >= 0.0
            )
            if (
                not isinstance(queue_id, int)
                or isinstance(queue_id, bool)
                or queue_id <= 0
                or queue_id in seen_ids
                or not isinstance(account_id, int)
                or isinstance(account_id, bool)
                or account_id <= 0
                or account_id in seen_accounts
                or account_id not in account_ids
                or not isinstance(rank, int)
                or isinstance(rank, bool)
                or rank <= previous_rank
                or status not in _QUEUE_STATUSES
                or not isinstance(unknown, bool)
                or not isinstance(error_code, str)
                or len(error_code) > 64
                or bool(error_code)
                and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", error_code)
                or delivery_mode
                not in {
                    "direct",
                    "duration_pending",
                    "premium_relay_repost",
                }
                or not isinstance(relay_account_id, int)
                or isinstance(relay_account_id, bool)
                or not valid_duration
                or (
                    delivery_mode == "direct"
                    and (relay_account_id != 0 or repost_status)
                )
                or (
                    delivery_mode == "duration_pending"
                    and (
                        relay_account_id != 0
                        or repost_status
                        or unknown
                        or error_code
                        or status not in {"queued", "waiting_relay"}
                        or (
                            status == "queued"
                            and float(duration_value) != 0.0
                        )
                        or (
                            status == "waiting_relay"
                            and float(duration_value) <= 140.0
                        )
                    )
                )
                or (
                    status == "waiting_relay"
                    and delivery_mode != "duration_pending"
                )
                or (
                    delivery_mode == "premium_relay_repost"
                    and (
                        relay_account_id <= 0
                        or repost_status
                        not in {
                            "reserved",
                            "source_publishing",
                            "source_published",
                            "reposting",
                            "reposted",
                            "failed",
                            "needs_review",
                        }
                    )
                )
            ):
                raise SidecarError(
                    "x_post_schedule_plan_invalid_response",
                    "Schedule queue identity is invalid",
                )
            seen_ids.add(queue_id)
            seen_accounts.add(account_id)
            previous_rank = rank
            normalized = {
                "id": queue_id,
                "account_id": account_id,
                "candidate_rank": rank,
                "status": status,
                "unknown_outcome": unknown,
                "error_code": error_code,
            }
            if delivery_mode == "duration_pending":
                normalized.update(
                    {
                        "delivery_mode": delivery_mode,
                        "preflight_duration": float(duration_value),
                    }
                )
            elif delivery_mode == "premium_relay_repost":
                normalized.update(
                    {
                        "delivery_mode": delivery_mode,
                        "relay_account_id": relay_account_id,
                        "repost_status": repost_status,
                    }
                )
                if duration_present:
                    normalized["preflight_duration"] = float(duration_value)
            elif duration_present:
                normalized["preflight_duration"] = float(duration_value)
            queues.append(normalized)
        return queues

    def create_schedule_plan(self, path, payload):
        result = self.post(
            path,
            payload,
            write_may_have_happened=True,
        )
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("queues"), list):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Schedule plan response is invalid",
            )
        return self._normalize_queues(
            item["queues"], payload["account_ids"]
        )

    def heartbeat_schedule_run(self, path, identity, *, plan_attempt=False):
        payload = {
            key: identity[key]
            for key in (
                "source_type",
                "run_date",
                "publish_time",
                "version",
                "account_ids",
            )
        }
        payload["plan_attempt"] = bool(plan_attempt)
        result = self.post(path, payload, write_may_have_happened=True)
        item = result.get("item") if isinstance(result, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("source_type") != identity["source_type"]
            or item.get("run_date") != identity["run_date"]
            or item.get("publish_time") != identity["publish_time"]
            or item.get("config_version") != identity["version"]
            or item.get("account_ids") != identity["account_ids"]
            or item.get("status") not in {"claimed", "running"}
            or not isinstance(item.get("heartbeat_recorded"), bool)
            or item.get("plan_attempt_recorded") is not bool(plan_attempt)
        ):
            raise SidecarError(
                "x_post_schedule_heartbeat_invalid_response",
                "Schedule heartbeat response is invalid",
                unknown_outcome=True,
            )
        return item

    def record_schedule_failure(self, path, identity, code, message):
        payload = {
            "source_type": identity["source_type"],
            "run_date": identity["run_date"],
            "publish_time": identity["publish_time"],
            "version": identity["version"],
            "account_ids": list(identity["account_ids"]),
            "error_code": str(code or "")[:64],
            "error_message": redact_text(message, 240),
        }
        if identity.get("drama_pool_item_id") is not None:
            payload["drama_pool_item_id"] = int(
                identity["drama_pool_item_id"]
            )
            payload["content_id"] = str(
                identity.get("content_id", "") or ""
            )
        result = self.post(
            path,
            payload,
            write_may_have_happened=True,
        )
        item = result.get("item") if isinstance(result, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("source_type") != identity["source_type"]
            or item.get("run_date") != identity["run_date"]
            or item.get("publish_time") != identity["publish_time"]
            or item.get("config_version") != identity["version"]
            or item.get("account_ids") != identity["account_ids"]
            or item.get("status") != "failed_preflight"
            or not isinstance(item.get("recorded"), bool)
        ):
            raise SidecarError(
                "x_post_schedule_failure_invalid_response",
                "Schedule failure audit response is invalid",
                502,
                unknown_outcome=True,
            )
        return dict(item)

    def available_drama_pool(self, path, limit, account_ids, *, configured_account_ids=None):
        normalized_accounts = [int(value) for value in account_ids]
        payload = {"limit": int(limit), "account_ids": normalized_accounts}
        if configured_account_ids is not None:
            payload["configured_account_ids"] = list(configured_account_ids)
        result = self.post(
            path,
            payload,
        )
        items = result.get("items") if isinstance(result, dict) else None
        if (
            not isinstance(items, list)
            or len(items) > len(normalized_accounts)
        ):
            raise SidecarError(
                "x_post_drama_pool_invalid_response",
                "Drama pool response is invalid",
            )
        # The selector performs the full identity/FIFO validation.
        return [dict(item) if isinstance(item, dict) else item for item in items]

    def premium_relay_accounts(self, run_date, drama_language="en"):
        normalized_language = canonical_drama_language(drama_language)
        result = self.post(
            DEFAULT_PREMIUM_RELAY_ACCOUNTS_PATH,
            {
                "run_date": str(run_date),
                "drama_language": normalized_language,
            },
        )
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) > MAX_DAILY_ACCOUNTS:
            raise SidecarError(
                "x_post_premium_relay_accounts_invalid_response",
                "Premium relay account response is invalid",
            )
        normalized = []
        seen = set()
        previous_load = -1
        for raw in items:
            if not isinstance(raw, dict):
                raise SidecarError(
                    "x_post_premium_relay_accounts_invalid_response",
                    "Premium relay account response is invalid",
                )
            account = _safe_account(raw)
            if account["drama_language"] != normalized_language:
                raise SidecarError(
                    "x_post_premium_relay_accounts_invalid_response",
                    "Premium relay account language is invalid",
                )
            account["publish_eligible"] = raw.get("publish_eligible") is True
            account["long_video_publish_eligible"] = (
                raw.get("long_video_publish_eligible") is True
            )
            account["protected"] = raw.get("protected") is True
            account_id = int(account["id"])
            load = raw.get("relay_assignment_count")
            if (
                account_id in seen
                or not isinstance(load, int)
                or isinstance(load, bool)
                or load < previous_load
                or not account.get("publish_eligible")
                or not account.get("long_video_eligible")
                or not account.get("long_video_publish_eligible")
                or raw.get("protected") is not False
            ):
                raise SidecarError(
                    "x_post_premium_relay_accounts_invalid_response",
                    "Premium relay account response is invalid",
                )
            account["relay_assignment_count"] = load
            account["protected"] = False
            normalized.append(account)
            seen.add(account_id)
            previous_load = load
        return normalized

    def record_drama_pool_checks(
        self,
        path,
        checks,
        *,
        validate_only=False,
    ):
        normalized = []
        for raw in checks:
            if not isinstance(raw, dict):
                raise SidecarError(
                    "x_post_drama_pool_check_invalid_response",
                    "Drama pool check is invalid",
                )
            item = {
                "pool_item_id": int(raw["pool_item_id"]),
                "content_id": str(raw["content_id"]),
                "error_code": str(raw["error_code"])[:64],
                "error_message": redact_text(
                    raw.get("error_message", ""),
                    240,
                ),
            }
            if not item["error_code"]:
                item["expected_error_code"] = str(
                    raw["expected_error_code"]
                )[:64]
                item["expected_episode_number"] = int(
                    raw["expected_episode_number"]
                )
            normalized.append(item)
        result = self.post(
            path,
            {
                "checks": normalized,
                "validate_only": bool(validate_only),
            },
            write_may_have_happened=not validate_only,
        )
        item = result.get("item") if isinstance(result, dict) else None
        validated_count = (
            item.get("validated_count", item.get("updated_count"))
            if isinstance(item, dict)
            else None
        )
        response_validate_only = (
            item.get("validate_only", False)
            if isinstance(item, dict)
            else None
        )
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("updated_count"), int)
            or item["updated_count"] < 0
            or item["updated_count"] > len(normalized)
            or not isinstance(validated_count, int)
            or validated_count < 0
            or validated_count > len(normalized)
            or response_validate_only is not bool(validate_only)
        ):
            raise SidecarError(
                "x_post_drama_pool_check_invalid_response",
                "Drama pool check response is invalid",
                502,
                unknown_outcome=True,
            )
        normalized_item = dict(item)
        normalized_item["validated_count"] = validated_count
        normalized_item["validate_only"] = response_validate_only
        return normalized_item


def _within_grace(item, current, grace_seconds):
    scheduled = _scheduled_at(item)
    late_seconds = (current - scheduled).total_seconds()
    return 0 <= late_seconds <= int(grace_seconds)


def _failure_audit_fields(exc):
    code = str(getattr(exc, "code", "") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
        code = "x_post_schedule_preflight_failed"
    message = redact_text(str(exc), 240)
    return code, message or "X schedule preflight failed"


def _record_schedule_failure_best_effort(
    sidecar,
    config,
    identity,
    exc,
):
    recorder = getattr(sidecar, "record_schedule_failure", None)
    if not callable(recorder):
        return None
    code, message = _failure_audit_fields(exc)
    failure_identity = dict(identity)
    if getattr(exc, "drama_pool_item_id", None) is not None:
        failure_identity["drama_pool_item_id"] = int(
            exc.drama_pool_item_id
        )
        failure_identity["content_id"] = str(
            getattr(exc, "content_id", "") or ""
        )
    try:
        return recorder(
            config.failure_path,
            failure_identity,
            code,
            message,
        )
    except Exception:
        # Preserve the original failure when the sidecar itself is unhealthy.
        return None


def _preflight_failure_result(identity, exc, audit):
    code, message = _failure_audit_fields(exc)
    return {
        "source_type": identity["source_type"],
        "run_date": identity["run_date"],
        "publish_time": identity["publish_time"],
        "version": identity["version"],
        "resumed_existing_plan": False,
        "planned_count": 0,
        "attempted_count": 0,
        "published_count": 0,
        "status": "failed_preflight",
        "error_code": code,
        "error_message": message,
        "failure_recorded": bool(
            isinstance(audit, dict)
            and audit.get("status") == "failed_preflight"
        ),
        "results": [],
    }


def _open_source_connection(config, connection_factory):
    factory = connection_factory or _connect_from_config
    try:
        return factory(config)
    except CandidateSelectionError:
        raise
    except Exception:
        # Driver connection errors can contain hosts, usernames or DSNs.
        # Keep the same safe operator contract as cursor query failures.
        raise CandidateQueryError("source") from None


def _close_source_connection(connection):
    close = getattr(connection, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # Cleanup cannot turn a safe query result into a credential-bearing
            # driver exception.
            pass


def _repair_client(config):
    if not config.repair_url:
        return None
    return MediaRepairClient(
        config.repair_url,
        config.repair_token,
        timeout=config.repair_timeout,
        max_output_bytes=config.max_media_bytes,
    )


def _verify_accounts(sidecar, account_ids, *, skip_blocked=False, skipped_accounts=None):
    verified = []
    skipped = []
    for account_id in account_ids:
        try:
            verified.append(_safe_account(sidecar.verify_account(account_id)))
        except SidecarError as exc:
            if not skip_blocked or exc.status != 409 or exc.code not in {
                "x_post_account_needs_review", "x_post_account_locked",
            }:
                raise
            skipped.append({"account_id": int(account_id), "error_code": exc.code, "message": str(exc)})
    skipped_ids = {item["account_id"] for item in skipped}
    if [item["id"] for item in verified] != [value for value in account_ids if value not in skipped_ids]:
        raise ScheduleRunError(
            "verified account order does not match frozen schedule",
            "x_post_schedule_account_mismatch",
        )
    if skipped_accounts is not None:
        skipped_accounts.extend(skipped)
    if not verified and skipped:
        raise ScheduleRunError(
            ("本批账号均被暂停；" + "；".join(item["message"] for item in skipped))[:240],
            skipped[0]["error_code"],
        )
    return verified


def _retrying_media_downloader(
    downloader,
    attempts=SCHEDULE_MEDIA_DOWNLOAD_ATTEMPTS,
):
    attempts = max(1, int(attempts))

    def download(*args, **kwargs):
        for attempt in range(1, attempts + 1):
            try:
                return downloader(*args, **kwargs)
            except XPostError as exc:
                if (
                    exc.code != "media_download_failed"
                    or attempt >= attempts
                ):
                    raise
        raise AssertionError("unreachable media download retry state")

    return download


def _stable_shuffled(values, seed_parts, shuffle_fn=None):
    """Return a reproducible shuffle without using process-randomized hash()."""
    payload = json.dumps(
        [MATERIAL_ASSIGNMENT_VERSION]
        + [str(value) for value in seed_parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    seed = hashlib.sha256(payload).hexdigest()
    items = list(values)
    if shuffle_fn is not None:
        shuffled = shuffle_fn(list(items), seed)
        if (
            not isinstance(shuffled, list)
            or len(shuffled) != len(items)
            or sorted(str(item) for item in shuffled)
            != sorted(str(item) for item in items)
        ):
            raise ScheduleRunError(
                "material assignment shuffler returned an invalid permutation",
                "x_post_schedule_material_assignment_invalid",
            )
        return shuffled
    random.Random(int(seed, 16)).shuffle(items)
    return items


def _material_assignment_seed_parts(identity, accounts, language, purpose):
    identity = identity if isinstance(identity, dict) else {}
    return (
        purpose,
        identity.get("source_type", "material"),
        identity.get("run_date", ""),
        identity.get("publish_time", ""),
        identity.get("version", ""),
        canonical_drama_language(language),
        ",".join(str(int(account["id"])) for account in accounts),
    )


def _preflight_material_candidates(
    config,
    sidecar,
    candidates,
    accounts,
    *,
    source_date,
    timestamp,
    downloader,
    prober,
    repair_client,
    assignment_identity,
    stable_shuffler=_stable_shuffled,
    accepted_by_account=None,
    repair_state=None,
    capacity_skips=None,
    heartbeat=None,
):
    """FIFO-preflight one page while preserving optional cross-page state."""
    target_count = len(accounts)
    if accepted_by_account is None:
        accepted_by_account = {}
    if repair_state is None:
        repair_state = {"attempted": 0}
    if capacity_skips is None:
        capacity_skips = []
    failures = []
    accounts_by_language = {}
    for account in accounts:
        language = canonical_drama_language(account.get("drama_language"))
        accounts_by_language.setdefault(language, []).append(account)
    target_order = {
        language: stable_shuffler(
            language_accounts,
            _material_assignment_seed_parts(
                assignment_identity,
                accounts,
                language,
                "target",
            ),
        )
        for language, language_accounts in accounts_by_language.items()
    }
    relay_cache = {}
    work_root = Path(config.work_dir)
    if not work_root.exists() or not work_root.is_dir() or work_root.is_symlink():
        raise ScheduleRunError(
            "schedule media work directory is unavailable",
            "x_post_storage_unavailable",
        )

    def relay_for(language, target, material_id):
        if language not in relay_cache:
            relay_cache[language] = sidecar.premium_relay_accounts(
                str(assignment_identity.get("run_date") or ""), language
            )
        relay_options = [
            option
            for option in relay_cache[language]
            if int(option["id"]) != int(target["id"])
            and bool(option.get("long_video_eligible"))
            and same_drama_language(option.get("drama_language"), language)
        ]
        relay_options = stable_shuffler(
            relay_options,
            _material_assignment_seed_parts(
                assignment_identity,
                accounts,
                language,
                "relay:%s:%s" % (int(target["id"]), material_id),
            ),
        )
        if not relay_options:
            raise CandidatePreflightError(
                "no currently eligible same-language public Premium relay account is available",
                "x_long_video_requires_premium",
            ) from None
        return relay_options[0]

    with tempfile.TemporaryDirectory(
        prefix="x-post-schedule-material-", dir=str(work_root)
    ) as temporary:
        root = Path(temporary)
        for candidate in candidates:
            if callable(heartbeat):
                heartbeat()
            if len(accepted_by_account) == target_count:
                break
            try:
                language = canonical_drama_language(
                    candidate.get("material_language")
                    or candidate.get("language")
                )
            except (AttributeError, ValueError) as exc:
                failures.append(
                    {
                        "pool_item_id": (
                            candidate.get("pool_item_id")
                            if isinstance(candidate, dict)
                            else None
                        ),
                        "material_id": str(
                            candidate.get("material_id", "")
                            if isinstance(candidate, dict)
                            else ""
                        ),
                        "error_code": "x_account_drama_language_invalid",
                        "error_message": redact_text(str(exc), 240),
                    }
                )
                continue
            language_targets = target_order.get(language, [])
            remaining_targets = [
                account
                for account in language_targets
                if int(account["id"]) not in accepted_by_account
            ]
            if not remaining_targets:
                if not language_targets:
                    failures.append(
                        {
                            "pool_item_id": candidate.get("pool_item_id"),
                            "material_id": str(
                                candidate.get("material_id", "") or ""
                            ),
                            "error_code": "material_language_not_scheduled",
                            "error_message": "当前发布账号不包含该素材语言",
                        }
                    )
                else:
                    # This clean item is before a later-language selection in
                    # the global FIFO order. Carry exact, run-scoped proof to
                    # the sidecar instead of persisting a fake pool failure.
                    capacity_skips.append(
                        {
                            "pool_item_id": candidate.get("pool_item_id"),
                            "material_id": str(
                                candidate.get("material_id", "") or ""
                            ),
                            "material_language": language,
                            "reason": "language_capacity_full",
                        }
                    )
                # The language is configured, but all targets of that language
                # already have a candidate in this batch. This is normal batch
                # capacity, so leave the item clean for a later schedule point.
                continue
            target = remaining_targets[0]
            material_id = str(candidate.get("material_id", "") or "")
            destination = root / ("%s.bin" % material_id)
            rank = len(accepted_by_account) + 1
            relay = None
            try:
                duration = float(candidate.get("source_duration") or 0)
                if candidate.get("media_kind") == "video" and duration <= 0:
                    raise CandidatePreflightError(
                        "source video duration metadata is missing",
                        code="material_duration_missing",
                    )
                route_account = target
                if duration > 140 and not target.get("long_video_eligible"):
                    relay = relay_for(language, target, material_id)
                    route_account = relay
                try:
                    item = _preflight_candidate(
                        config,
                        candidate,
                        route_account,
                        rank,
                        timestamp,
                        destination,
                        downloader,
                        prober,
                        repair_client=repair_client,
                        repair_state=repair_state,
                    )
                except (
                    XPostError,
                    CandidatePreflightError,
                    http.client.HTTPException,
                    OSError,
                    TypeError,
                    ValueError,
                ) as direct_error:
                    if not (
                        relay is None
                        and str(getattr(direct_error, "code", "") or "")
                        == "x_long_video_requires_premium"
                        and not target.get("long_video_eligible")
                    ):
                        raise
                    relay = relay_for(language, target, material_id)
                    item = _preflight_candidate(
                        config,
                        candidate,
                        relay,
                        rank,
                        timestamp,
                        destination,
                        downloader,
                        prober,
                        repair_client=repair_client,
                        repair_state=repair_state,
                    )
                item.update(
                    {
                        "media_validation_mode": "preflight",
                        "delivery_mode": "direct",
                        "relay_account_id": 0,
                        "relay_account_username": "",
                    }
                )
                if relay is not None:
                    item.update(
                        {
                            "account_id": int(target["id"]),
                            "account_username": str(target["username"]),
                            "page_name": str(
                                target.get("display_name", "")
                                or target["username"]
                            ),
                            "page_id": str(target["x_user_id"]),
                            "delivery_mode": "premium_relay_repost",
                            "relay_account_id": int(relay["id"]),
                            "relay_account_username": str(relay["username"]),
                        }
                    )
            except (
                XPostError,
                CandidatePreflightError,
                http.client.HTTPException,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                failures.append(
                    {
                        "pool_item_id": candidate.get("pool_item_id"),
                        "material_id": material_id,
                        "error_code": str(
                            getattr(exc, "code", "media_preflight_failed")
                        )[:64],
                        "error_message": redact_text(str(exc), 240),
                    }
                )
                continue
            item["source_type"] = "material"
            item["source_date"] = source_date
            accepted_by_account[int(target["id"])] = item
    accepted = []
    for account in accounts:
        item = accepted_by_account.get(int(account["id"]))
        if item is None:
            continue
        item["candidate_rank"] = len(accepted) + 1
        accepted.append(item)
    return accepted, failures


def _record_capacity_skip_proofs(sidecar, config, skips):
    """Persist current-run source identity before an atomic FIFO skip."""
    if not skips:
        return
    recorder = getattr(sidecar, "record_pool_checks", None)
    if not callable(recorder):
        raise ScheduleRunError(
            "素材容量证明接口不可用",
            "x_post_pool_fifo_conflict",
        )
    checks = [
        {
            "pool_item_id": int(item["pool_item_id"]),
            "material_id": str(item["material_id"]),
            "material_language": str(item["material_language"]),
            "proof_reason": "language_capacity_full",
            "error_code": "",
            "error_message": "",
        }
        for item in skips
    ]
    for start in range(0, len(checks), 100):
        batch = checks[start : start + 100]
        result = recorder(config.pool_check_path, batch)
        if int(result.get("updated_count") or 0) != len(batch):
            raise ScheduleRunError(
                "素材容量证明未完整写入冻结批次",
                "x_post_pool_fifo_conflict",
            )


def _material_candidates(
    config,
    sidecar,
    accounts,
    *,
    source_date,
    connection_factory,
    downloader,
    prober,
    repair_client,
    timestamp,
    assignment_identity=None,
    stable_shuffler=_stable_shuffled,
    heartbeat=None,
):
    pool_items = sidecar.available_pool_items(
        config.material_pool_path, config.scan_limit
    )
    if not pool_items:
        raise ScheduleRunError(
            "manual material pool has no available item",
            "x_post_schedule_material_shortage",
        )
    identity = dict(assignment_identity or {})
    identity.setdefault("source_type", "material")
    identity.setdefault("run_date", source_date)
    identity.setdefault("publish_time", "legacy")
    identity.setdefault("version", 1)
    accepted_by_account = {}
    repair_state = {"attempted": 0}
    capacity_skips = []
    planned = []
    connection = _open_source_connection(config, connection_factory)
    try:
        # candidate_pool_limit bounds one metadata-hydration page. The
        # fixed pool snapshot may continue through scan_limit until the frozen
        # account scope is filled or every row has been inspected.
        for start in range(0, len(pool_items), config.candidate_pool_limit):
            if len(accepted_by_account) == len(accounts):
                break
            if callable(heartbeat):
                heartbeat()
            pool_batch = pool_items[
                start : start + config.candidate_pool_limit
            ]
            try:
                candidates, selector_rejections = select_pool_candidates(
                    connection,
                    pool_batch,
                    source_date,
                    limit=config.candidate_pool_limit,
                    schema=config.mysql_database,
                )
            except CandidateQueryError:
                # Preserve the frozen SQLite pool snapshot. Reconnect once and
                # retry only this read-only hydration page.
                _close_source_connection(connection)
                connection = _open_source_connection(
                    config, connection_factory
                )
                candidates, selector_rejections = select_pool_candidates(
                    connection,
                    pool_batch,
                    source_date,
                    limit=config.candidate_pool_limit,
                    schema=config.mysql_database,
                )
            _record_pool_checks_best_effort(
                sidecar,
                config,
                selector_rejections,
            )
            capacity_skip_start = len(capacity_skips)
            planned, planning_rejections = _preflight_material_candidates(
                config,
                sidecar,
                candidates,
                accounts,
                source_date=source_date,
                timestamp=timestamp,
                downloader=downloader,
                prober=prober,
                repair_client=repair_client,
                assignment_identity=identity,
                stable_shuffler=stable_shuffler,
                accepted_by_account=accepted_by_account,
                repair_state=repair_state,
                capacity_skips=capacity_skips,
                heartbeat=heartbeat,
            )
            _record_capacity_skip_proofs(
                sidecar,
                config,
                capacity_skips[capacity_skip_start:],
            )
            _record_pool_checks_best_effort(
                sidecar,
                config,
                planning_rejections,
            )
    finally:
        _close_source_connection(connection)
    if not planned:
        raise ScheduleRunError(
            "no FIFO material candidate passed media preflight",
            "x_post_schedule_material_preflight_shortage",
        )
    return MaterialScheduleCandidates(
        planned,
        fifo_capacity_skips=capacity_skips,
    )


def _drama_candidates(
    config,
    sidecar,
    accounts,
    *,
    source_date,
    connection_factory,
    downloader,
    prober,
    repair_client,
    timestamp,
    configured_account_ids=None,
):
    del downloader, prober, repair_client
    account_ids = [int(account["id"]) for account in accounts]
    connection = _open_source_connection(config, connection_factory)

    def reject_unassigned(pool_item_id, content_id, code, message):
        result = sidecar.record_drama_pool_checks(
            config.drama_check_path,
            [
                {
                    "pool_item_id": int(pool_item_id),
                    "content_id": str(content_id),
                    "error_code": str(code or "x_post_drama_preflight_failed")[
                        :64
                    ],
                    "error_message": redact_text(message, 240),
                }
            ],
        )
        if int(result.get("updated_count") or 0) != 1:
            raise ScheduleRunError(
                "unassigned drama rejection was not recorded",
                "x_post_drama_pool_check_conflict",
                drama_pool_item_id=pool_item_id,
                content_id=content_id,
            )

    try:
        rejected_ids = set()
        refresh_accounts = False
        relay_accounts = {}
        while len(rejected_ids) < config.scan_limit:
            if refresh_accounts:
                accounts = _verify_accounts(sidecar, account_ids)
                refresh_accounts = False
            scope_options = {}
            if configured_account_ids is not None and list(configured_account_ids) != account_ids:
                scope_options["configured_account_ids"] = list(configured_account_ids)
            pool_items = sidecar.available_drama_pool(
                config.drama_pool_path,
                config.scan_limit,
                account_ids,
                **scope_options,
            )
            if not pool_items:
                raise ScheduleRunError(
                    "当前账号没有可续播的空闲剧集，请检查已绑定剧集的失败记录或同语言库存",
                    "x_post_schedule_drama_shortage",
                )
            candidates = []
            refill_required = False
            for pool_item in pool_items:
                try:
                    candidates.extend(
                        select_drama_pool_episodes(
                            connection,
                            [pool_item],
                            account_ids=account_ids,
                            schema=config.mysql_database,
                            app_id=config.drama_app_id,
                        )
                    )
                except DramaPoolRejection as exc:
                    if (
                        int(exc.pool_item_id or 0) != int(pool_item.get("id") or 0)
                        or str(exc.content_id or "")
                        != str(pool_item.get("content_id") or "")
                    ):
                        raise ScheduleRunError(
                            "short-drama selector rejection identity changed",
                            "x_post_drama_pool_check_conflict",
                        ) from None
                    if int(pool_item.get("assigned_account_id") or 0) > 0:
                        # A bound drama keeps its affinity/progress for later
                        # repair, but must not suppress healthy sibling dramas.
                        continue
                    reject_unassigned(
                        exc.pool_item_id,
                        exc.content_id,
                        exc.code,
                        str(exc),
                    )
                    rejected_ids.add(int(exc.pool_item_id))
                    refresh_accounts = True
                    refill_required = True
                    break
            if refill_required:
                continue
            candidate_account_ids = [
                int(candidate.get("candidate_account_id") or 0)
                for candidate in candidates
            ]
            if candidate_account_ids and not _is_ordered_account_subset(
                candidate_account_ids, account_ids
            ):
                raise ScheduleRunError(
                    "short-drama candidate order changed during planning",
                    "x_post_schedule_account_mismatch",
                )
            if not candidates:
                raise ScheduleRunError(
                    "short-drama pool has no eligible free episode for this schedule",
                    "x_post_schedule_drama_shortage",
                )

            account_by_id = {int(account["id"]): account for account in accounts}
            planned_by_index = {}

            def normalize_item(item, candidate):
                normalized = dict(item)
                normalized.update(
                    {
                        # Drama affinity is stored on the dedicated pool row.
                        "pool_item_id": None,
                        "pool_created_at": "",
                        "drama_pool_item_id": candidate["drama_pool_item_id"],
                        "drama_pool_created_at": candidate[
                            "drama_pool_created_at"
                        ],
                        "source_type": "drama",
                        "source_date": source_date,
                        "media_validation_mode": "deferred",
                        "preflight_sha256": "",
                        "preflight_size": 0,
                        "preflight_duration": float(
                            item.get("preflight_duration") or 0.0
                        ),
                        "preflight_width": 0,
                        "preflight_height": 0,
                        "delivery_mode": item.get("delivery_mode") or "direct",
                        "relay_account_id": int(item.get("relay_account_id") or 0),
                        "relay_account_username": str(
                            item.get("relay_account_username") or ""
                        ),
                    }
                )
                return normalized

            def normalize_relay_item(
                item, candidate, target_account, relay_account
            ):
                normalized = normalize_item(item, candidate)
                normalized.update(
                    {
                        "delivery_mode": "premium_relay_repost",
                        "relay_account_id": int(relay_account["id"]),
                        "relay_account_username": str(relay_account["username"]),
                        "account_id": int(target_account["id"]),
                        "account_username": str(target_account["username"]),
                        "page_name": str(
                            target_account.get("display_name", "")
                            or target_account["username"]
                        ),
                        "page_id": str(target_account["x_user_id"]),
                    }
                )
                return normalized

            for index, candidate in enumerate(candidates):
                rank = index + 1
                account = account_by_id.get(
                    int(candidate.get("candidate_account_id") or 0)
                )
                if account is None:
                    raise ScheduleRunError(
                        "short-drama account assignment changed during planning",
                        "x_post_schedule_account_mismatch",
                        drama_pool_item_id=candidate.get("drama_pool_item_id"),
                        content_id=candidate.get("content_id", ""),
                    )
                try:
                    build_drama_episode_post_text(
                        DEFAULT_SHORT_BASE_URL + "/1.html",
                        candidate["sub_num"],
                        candidate["name_tag"],
                        candidate["description"],
                    )
                    helper_candidate = dict(candidate)
                    helper_candidate.update(
                        {
                            "pool_item_id": candidate["drama_pool_item_id"],
                            "pool_created_at": candidate[
                                "drama_pool_created_at"
                            ],
                            "source_date": source_date,
                        }
                    )
                    drama_language = canonical_drama_language(
                        candidate.get("material_language")
                        or candidate.get("language")
                    )
                    if config.drama_duration_routing_enabled:
                        item = _plan_candidate(
                            account, helper_candidate, rank, timestamp
                        )
                        item.update(
                            {
                                "delivery_mode": "duration_pending",
                                "relay_account_id": 0,
                                "relay_account_username": "",
                                "preflight_duration": 0.0,
                            }
                        )
                        planned_by_index[index] = normalize_item(
                            item, candidate
                        )
                        continue
                    if account.get("long_video_eligible"):
                        item = _plan_candidate(
                            account, helper_candidate, rank, timestamp
                        )
                        item["preflight_duration"] = 0.0
                        planned_by_index[index] = normalize_item(item, candidate)
                        continue

                    if drama_language not in relay_accounts:
                        relay_run_date = datetime.fromtimestamp(
                            timestamp, BEIJING_TZ
                        ).date().isoformat()
                        try:
                            relay_accounts[drama_language] = (
                                sidecar.premium_relay_accounts(
                                    relay_run_date, drama_language
                                )
                            )
                        except TypeError:
                            relay_accounts[drama_language] = (
                                sidecar.premium_relay_accounts(relay_run_date)
                            )
                    relay_account = next(
                        (
                            relay
                            for relay in relay_accounts[drama_language]
                            if int(relay["id"]) != int(account["id"])
                            and bool(relay.get("long_video_eligible"))
                            and (
                                not relay.get("drama_language")
                                or same_drama_language(
                                    relay.get("drama_language"), drama_language
                                )
                            )
                        ),
                        None,
                    )
                    if relay_account is None:
                        continue
                    relay_item = _plan_candidate(
                        relay_account, helper_candidate, rank, timestamp
                    )
                    # Source duration is unavailable. 141 is only a relay hint;
                    # deferred publish probes the episode once before upload.
                    relay_item["preflight_duration"] = 141.0
                    planned_by_index[index] = normalize_relay_item(
                        relay_item, candidate, account, relay_account
                    )
                except (XPostError, TypeError, ValueError):
                    # Candidate-local metadata/copy defects must not block other
                    # already-available episodes in this frozen schedule.
                    continue

            if not planned_by_index:
                raise ScheduleRunError(
                    "no short-drama candidate passed metadata planning",
                    "x_post_schedule_drama_shortage",
                )
            return [
                planned_by_index[index]
                for index in range(len(candidates))
                if index in planned_by_index
            ]
        raise ScheduleRunError(
            "drama metadata rejection limit was reached",
            "x_post_schedule_drama_shortage",
        )
    finally:
        _close_source_connection(connection)


def _plan_payload(identity, candidates):
    payload = {
        "source_type": identity["source_type"],
        "run_date": identity["run_date"],
        "publish_time": identity["publish_time"],
        "version": identity["version"],
        "account_ids": [int(item["account_id"]) for item in candidates],
        "source_date": candidates[0]["source_date"],
        "candidates": candidates,
    }
    capacity_skips = getattr(candidates, "fifo_capacity_skips", None)
    if capacity_skips:
        payload["fifo_capacity_skips"] = list(capacity_skips)
    return payload


def _heartbeat_best_effort(sidecar, identity, *, plan_attempt=False):
    heartbeat = getattr(sidecar, "heartbeat_schedule_run", None)
    if not callable(heartbeat):
        return None
    return heartbeat(
        DEFAULT_HEARTBEAT_PATH,
        identity,
        plan_attempt=plan_attempt,
    )


def _publish_frozen_queues(config, sidecar, identity, queues, *, resumed):
    results = []
    stopped = False
    for queue in queues:
        entry = {
            "queue_id": queue["id"],
            "account_id": queue["account_id"],
            "status": queue["status"],
        }
        relay_repost_resume = bool(
            queue.get("delivery_mode") == "premium_relay_repost"
            and queue.get("repost_status") == "source_published"
            and not queue.get("unknown_outcome")
        )
        if queue.get("unknown_outcome") or (
            queue["status"] == "publishing" and not relay_repost_resume
        ):
            entry.update(
                {
                    "status": "needs_review",
                    "unknown_outcome": True,
                    "error_code": "x_post_unknown_outcome",
                }
            )
            results.append(entry)
            stopped = True
            break
        if queue["status"] == "published":
            results.append(entry)
            continue
        if queue["status"] == "failed":
            entry["error_code"] = (
                queue.get("error_code") or "x_post_retry_requires_review"
            )
            results.append(entry)
            if queue.get("error_code") == "x_post_rate_limited":
                stopped = True
                break
            continue
        try:
            published = sidecar.publish_queue(
                config.publish_path_template, queue["id"]
            )
            entry["status"] = published["status"]
            for key in (
                "log_id",
                "preview_url",
                "delivery_mode",
                "preflight_duration",
                "error_code",
            ):
                if key in published:
                    entry[key] = published[key]
        except SidecarError as exc:
            entry.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "unknown_outcome": exc.unknown_outcome,
                }
            )
            if exc.status == 429 or exc.unknown_outcome:
                stopped = True
        results.append(entry)
        if stopped:
            break
    result_statuses = [item["status"] for item in results]
    return {
        "source_type": identity["source_type"],
        "run_date": identity["run_date"],
        "publish_time": identity["publish_time"],
        "version": identity["version"],
        "resumed_existing_plan": bool(resumed),
        "planned_count": len(queues),
        "attempted_count": len(results),
        "published_count": sum(item["status"] == "published" for item in results),
        "waiting_relay_count": sum(
            item["status"] == "waiting_relay" for item in results
        ),
        "status": "stopped" if stopped else (
            "published"
            if len(results) == len(queues)
            and all(status == "published" for status in result_statuses)
            else (
                "waiting_relay"
                if len(results) == len(queues)
                and any(
                    status == "waiting_relay" for status in result_statuses
                )
                and all(
                    status in {"published", "waiting_relay"}
                    for status in result_statuses
                )
                else "completed_with_errors"
            )
        ),
        "results": results,
    }


def execute_schedule_tick(
    config,
    *,
    sidecar=None,
    connection_factory=None,
    material_candidate_loader=_material_candidates,
    drama_candidate_loader=_drama_candidates,
    downloader=download_media,
    prober=probe_media,
    repair_client=None,
    now=None,
):
    """Execute due schedule batches. Collaborators are injectable for tests."""
    config.validate()
    current = shanghai_now(now)
    run_date = current.date().isoformat()
    if config.start_date and run_date < normalize_date(
        config.start_date, "start_date"
    ):
        return {
            "status": "skipped_before_start_date",
            "run_date": run_date,
            "start_date": config.start_date,
            "batches": [],
        }
    sidecar = sidecar or ScheduleSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
    )
    if repair_client is None:
        repair_client = _repair_client(config)
    due_options = {
        "current": current,
        "grace_seconds": config.grace_seconds,
        "limit": config.max_due_batches,
    }
    if config.previous_day_recovery_reason:
        due_options.update(
            {
                "operator_reason": config.previous_day_recovery_reason,
                "deployed_commit": config.previous_day_deployed_commit,
            }
        )
    due = sidecar.due_schedules(config.due_path, **due_options)
    accepted_due = [
        item
        for item in due
        if item.get("frozen")
        or _within_grace(item, current, config.grace_seconds)
    ]
    stale_count = len(due) - len(accepted_due)
    if not accepted_due:
        return {
            "status": "no_due",
            "run_date": run_date,
            "due_count": len(due),
            "stale_ignored_count": stale_count,
            "batches": [],
        }

    batches = []
    retrying_downloader = _retrying_media_downloader(downloader)
    for identity in accepted_due:
        # Frozen state always wins. This query precedes token verification,
        # source reads, media downloads, and GPU repair.
        existing = sidecar.query_schedule_plan(
            config.plan_query_path, identity
        )
        if existing["found"] and existing["queues"]:
            result = _publish_frozen_queues(
                config,
                sidecar,
                identity,
                existing["queues"],
                resumed=True,
            )
            batches.append(result)
            continue
        if existing["found"] and existing["run"].get("plan_attempted_at"):
            # The previous worker crossed the plan-write fence but did not
            # leave queues. This successful ledger read makes it safe to
            # terminalize; never reselect and issue another plan write.
            exc = ScheduleRunError(
                "上次计划创建结果未确认，已停止重复尝试",
                "x_post_schedule_plan_unknown",
            )
            audit = _record_schedule_failure_best_effort(
                sidecar, config, identity, exc
            )
            batches.append(_preflight_failure_result(identity, exc, audit))
            continue
        if existing["found"] and existing["run"].get("status") != "claimed":
            raise ScheduleRunError(
                "frozen schedule run has no queues",
                "x_post_schedule_plan_incomplete",
            )

        try:
            _heartbeat_best_effort(sidecar, identity)
            sidecar.preflight_storage(config.storage_preflight_path)
            skipped_accounts = []
            accounts = _verify_accounts(
                sidecar, identity["account_ids"], skip_blocked=True,
                skipped_accounts=skipped_accounts,
            )
            source_date = previous_source_date(current)
            timestamp = max(1, int(current.timestamp()))
            if identity["source_type"] == "material":
                material_loader_options = {
                    "source_date": source_date,
                    "connection_factory": connection_factory,
                    "downloader": retrying_downloader,
                    "prober": prober,
                    "repair_client": repair_client,
                    "timestamp": timestamp,
                }
                if material_candidate_loader is _material_candidates:
                    material_loader_options["assignment_identity"] = identity
                    material_loader_options["heartbeat"] = lambda: (
                        _heartbeat_best_effort(sidecar, identity)
                    )
                candidates = material_candidate_loader(
                    config,
                    sidecar,
                    accounts,
                    **material_loader_options,
                )
            else:
                drama_scope_options = {}
                if drama_candidate_loader is _drama_candidates:
                    drama_scope_options["configured_account_ids"] = identity["account_ids"]
                candidates = drama_candidate_loader(
                    config,
                    sidecar,
                    accounts,
                    source_date=source_date,
                    connection_factory=connection_factory,
                    downloader=downloader,
                    prober=prober,
                    repair_client=repair_client,
                    timestamp=timestamp,
                    **drama_scope_options,
                )
            candidate_account_ids = [
                int(item["account_id"]) for item in candidates
            ]
            if not _is_ordered_account_subset(
                candidate_account_ids, identity["account_ids"]
            ):
                raise ScheduleRunError(
                    "candidate account order does not match frozen schedule",
                    "x_post_schedule_account_mismatch",
                )
            # Material preflight/repair may outlive an access token. Refresh
            # the frozen subset immediately before the atomic plan transaction;
            # each later publish also verifies its account.
            _verify_accounts(sidecar, candidate_account_ids)
            sidecar.preflight_storage(config.storage_preflight_path)
            _heartbeat_best_effort(sidecar, identity)
        except Exception as exc:
            audit = _record_schedule_failure_best_effort(
                sidecar,
                config,
                identity,
                exc,
            )
            batches.append(
                _preflight_failure_result(identity, exc, audit)
            )
            continue

        try:
            _heartbeat_best_effort(
                sidecar, identity, plan_attempt=True
            )
            queues = sidecar.create_schedule_plan(
                config.plan_path,
                _plan_payload(identity, candidates),
            )
        except SidecarError as exc:
            # A structured HTTP error proves the atomic transaction rolled
            # back and must terminalize the claimed run. For a lost response,
            # read the ledger first: publish only an already-frozen queue set;
            # never reselect or issue a second plan write in this tick.
            if exc.unknown_outcome:
                try:
                    reconciled = sidecar.query_schedule_plan(
                        config.plan_query_path, identity
                    )
                except Exception:
                    batches.append(
                        {
                            **_preflight_failure_result(identity, exc, None),
                            "status": "stopped",
                            "error_code": "x_post_schedule_plan_unknown",
                            "error_message": "计划创建结果待核对，未继续发布",
                        }
                    )
                    continue
                if reconciled["found"] and reconciled["queues"]:
                    result = _publish_frozen_queues(
                        config,
                        sidecar,
                        identity,
                        reconciled["queues"],
                        resumed=True,
                    )
                    batches.append(result)
                    continue
                exc = ScheduleRunError(
                    "计划创建结果未确认，已停止重复尝试",
                    "x_post_schedule_plan_unknown",
                )
            audit = _record_schedule_failure_best_effort(
                sidecar,
                config,
                identity,
                exc,
            )
            batches.append(_preflight_failure_result(identity, exc, audit))
            continue
        if [queue["account_id"] for queue in queues] != candidate_account_ids:
            raise ScheduleRunError(
                "created queue order does not match frozen schedule",
                "x_post_schedule_plan_mismatch",
            )
        result = _publish_frozen_queues(
            config, sidecar, identity, queues, resumed=False
        )
        if skipped_accounts:
            result["skipped_accounts"] = skipped_accounts
        batches.append(result)

    return {
        "status": (
            "stopped"
            if any(item["status"] == "stopped" for item in batches)
            else (
                "published"
                if batches and all(item["status"] == "published" for item in batches)
                else (
                    "waiting_relay"
                    if batches
                    and any(
                        item["status"] == "waiting_relay" for item in batches
                    )
                    and all(
                        item["status"] in {"published", "waiting_relay"}
                        for item in batches
                    )
                    else "completed_with_errors"
                )
            )
        ),
        "run_date": run_date,
        "due_count": len(due),
        "stale_ignored_count": stale_count,
        "processed_count": len(batches),
        "batches": batches,
    }


def main():
    try:
        config = ScheduleConfig.from_env()
        with process_lock(config.lock_path) as acquired:
            if acquired is None:
                result = {"status": "skipped_locked", "batches": []}
            else:
                result = execute_schedule_tick(config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") in {
            "published",
            "waiting_relay",
            "no_due",
            "skipped_before_start_date",
            "skipped_locked",
        } else 1
    except (
        CandidateSelectionError,
        DramaPoolRejection,
        DramaQueryError,
        DramaSelectionError,
        ScheduleRunError,
        SidecarError,
        XPostError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(
                        getattr(exc, "code", type(exc).__name__)
                    )[:64],
                    "message": redact_text(str(exc), 240),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "unexpected_error",
                    "message": redact_text(str(exc), 240),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
