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
                900,
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
        )

    def validate(self):
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
            or run.get("expected_count") != len(identity["account_ids"])
        ):
            raise SidecarError(
                "x_post_schedule_plan_invalid_response",
                "Schedule run identity does not match the requested point",
            )
        queues = self._normalize_queues(
            item["queues"], identity["account_ids"]
        )
        if queues and [queue["account_id"] for queue in queues] != identity[
            "account_ids"
        ]:
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
            delivery_mode = str(
                raw.get("delivery_mode", "direct") or "direct"
            )
            relay_account_id = raw.get("relay_account_id", 0)
            repost_status = str(raw.get("repost_status", "") or "")
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
                or delivery_mode
                not in {"direct", "premium_relay_repost"}
                or not isinstance(relay_account_id, int)
                or isinstance(relay_account_id, bool)
                or (
                    delivery_mode == "direct"
                    and (relay_account_id != 0 or repost_status)
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
            }
            if delivery_mode == "premium_relay_repost":
                normalized.update(
                    {
                        "delivery_mode": delivery_mode,
                        "relay_account_id": relay_account_id,
                        "repost_status": repost_status,
                    }
                )
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

    def available_drama_pool(self, path, limit, account_ids):
        normalized_accounts = [int(value) for value in account_ids]
        result = self.post(
            path,
            {
                "limit": int(limit),
                "account_ids": normalized_accounts,
            },
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
    return factory(config)


def _repair_client(config):
    if not config.repair_url:
        return None
    return MediaRepairClient(
        config.repair_url,
        config.repair_token,
        timeout=config.repair_timeout,
        max_output_bytes=config.max_media_bytes,
    )


def _verify_accounts(sidecar, account_ids):
    verified = []
    for account_id in account_ids:
        verified.append(_safe_account(sidecar.verify_account(account_id)))
    if [item["id"] for item in verified] != list(account_ids):
        raise ScheduleRunError(
            "verified account order does not match frozen schedule",
            "x_post_schedule_account_mismatch",
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
):
    """FIFO-scan material and stably randomize same-language target pairing."""
    target_count = len(accounts)
    accepted_by_account = {}
    failures = []
    repair_state = {"attempted": 0}
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
    with tempfile.TemporaryDirectory(
        prefix="x-post-schedule-material-", dir=str(work_root)
    ) as temporary:
        root = Path(temporary)
        for candidate in candidates:
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
            remaining_targets = [
                account
                for account in target_order.get(language, [])
                if int(account["id"]) not in accepted_by_account
            ]
            if not remaining_targets:
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
                continue
            target = remaining_targets[0]
            material_id = str(candidate.get("material_id", "") or "")
            destination = root / ("%s.mp4" % material_id)
            rank = len(accepted_by_account) + 1
            try:
                try:
                    item = _preflight_candidate(
                        config,
                        candidate,
                        target,
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
                    ValueError,
                ) as direct_error:
                    if not (
                        str(getattr(direct_error, "code", "") or "")
                        == "x_long_video_requires_premium"
                        and not target.get("long_video_eligible")
                    ):
                        raise
                    if language not in relay_cache:
                        relay_cache[language] = sidecar.premium_relay_accounts(
                            str(assignment_identity.get("run_date") or ""),
                            language,
                        )
                    relay_options = [
                        relay
                        for relay in relay_cache[language]
                        if int(relay["id"]) != int(target["id"])
                        and same_drama_language(
                            relay.get("drama_language"), language
                        )
                    ]
                    relay_options = stable_shuffler(
                        relay_options,
                        _material_assignment_seed_parts(
                            assignment_identity,
                            accounts,
                            language,
                            "relay:%s:%s"
                            % (int(target["id"]), material_id),
                        ),
                    )
                    if not relay_options:
                        raise ScheduleRunError(
                            "no currently eligible same-language public Premium relay account is available",
                            "x_post_premium_relay_unavailable",
                        ) from None
                    relay = relay_options[0]
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
):
    pool_items = sidecar.available_pool_items(
        config.material_pool_path, config.scan_limit
    )
    if len(pool_items) < len(accounts):
        raise ScheduleRunError(
            "manual material pool has fewer items than this schedule requires",
            "x_post_schedule_material_shortage",
        )
    connection = _open_source_connection(config, connection_factory)
    try:
        candidates, selector_rejections = select_pool_candidates(
            connection,
            pool_items,
            source_date,
            limit=config.candidate_pool_limit,
            schema=config.mysql_database,
        )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    _record_pool_checks_best_effort(
        sidecar,
        config,
        selector_rejections,
    )
    identity = dict(assignment_identity or {})
    identity.setdefault("source_type", "material")
    identity.setdefault("run_date", source_date)
    identity.setdefault("publish_time", "legacy")
    identity.setdefault("version", 1)
    planned, preflight_rejections = _preflight_material_candidates(
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
    )
    _record_pool_checks_best_effort(
        sidecar,
        config,
        preflight_rejections,
    )
    if len(planned) != len(accounts):
        raise ScheduleRunError(
            "not enough FIFO material candidates passed media preflight",
            "x_post_schedule_material_preflight_shortage",
        )
    return planned


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
):
    account_ids = [int(account["id"]) for account in accounts]
    connection = _open_source_connection(config, connection_factory)
    work_root = Path(config.work_dir)
    if not work_root.exists() or not work_root.is_dir() or work_root.is_symlink():
        close = getattr(connection, "close", None)
        if callable(close):
            close()
        raise ScheduleRunError(
            "schedule media work directory is unavailable",
            "x_post_storage_unavailable",
        )

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
        repair_state = {"attempted": 0}
        rejected_ids = set()
        preflight_cache = {}
        refresh_accounts = False
        relay_accounts = {}

        def preflight_one(candidate, account, rank, temporary):
            cache_key = (
                int(account["id"]),
                int(candidate["drama_pool_item_id"]),
                str(candidate["episode_key"]),
                str(candidate["material_id"]),
                str(candidate["material_url"]),
                str(candidate["drama_pool_created_at"]),
                str(candidate["description"]),
                str(candidate["name_tag"]),
                str(candidate["tag"]),
            )
            cached = preflight_cache.get(cache_key)
            if cached is not None:
                return dict(cached)
            build_drama_episode_post_text(
                DEFAULT_SHORT_BASE_URL + "/1.html",
                candidate["sub_num"],
                candidate["name_tag"],
                candidate["description"],
            )
            helper_candidate = dict(candidate)
            helper_candidate["pool_item_id"] = candidate[
                "drama_pool_item_id"
            ]
            helper_candidate["pool_created_at"] = candidate[
                "drama_pool_created_at"
            ]
            helper_candidate["source_date"] = source_date
            destination = Path(temporary) / (
                "%s-%s.mp4"
                % (
                    candidate["drama_pool_item_id"],
                    candidate["sub_num"],
                )
            )
            item = _preflight_candidate(
                config,
                helper_candidate,
                account,
                rank,
                timestamp,
                destination,
                downloader,
                prober,
                repair_client=repair_client,
                repair_state=repair_state,
            )
            preflight_cache[cache_key] = dict(item)
            return item

        with tempfile.TemporaryDirectory(
            prefix="x-post-schedule-drama-", dir=str(work_root)
        ) as temporary:
            while len(rejected_ids) < config.scan_limit:
                if refresh_accounts:
                    accounts = _verify_accounts(sidecar, account_ids)
                    refresh_accounts = False
                pool_items = sidecar.available_drama_pool(
                    config.drama_pool_path,
                    config.scan_limit,
                    account_ids,
                )
                if len(pool_items) < len(accounts):
                    raise ScheduleRunError(
                        "short-drama pool has fewer free episodes than this "
                        "schedule requires",
                        "x_post_schedule_drama_shortage",
                    )
                pool_by_id = {
                    int(item["id"]): item
                    for item in pool_items
                    if isinstance(item, dict) and item.get("id") is not None
                }
                try:
                    candidates = select_drama_pool_episodes(
                        connection,
                        pool_items,
                        account_ids=account_ids,
                        schema=config.mysql_database,
                        app_id=config.drama_app_id,
                    )
                except DramaPoolRejection as exc:
                    pool = pool_by_id.get(int(exc.pool_item_id or 0), {})
                    if int(pool.get("assigned_account_id") or 0) > 0:
                        raise ScheduleRunError(
                            str(exc),
                            exc.code,
                            drama_pool_item_id=exc.pool_item_id,
                            content_id=exc.content_id,
                        ) from None
                    reject_unassigned(
                        exc.pool_item_id,
                        exc.content_id,
                        exc.code,
                        str(exc),
                    )
                    rejected_ids.add(int(exc.pool_item_id))
                    refresh_accounts = True
                    continue
                if len(candidates) != len(accounts):
                    raise ScheduleRunError(
                        "short-drama pool has fewer free episodes than this "
                        "schedule requires",
                        "x_post_schedule_drama_shortage",
                    )

                planned_by_index = {}
                rejected = False
                processing_indexes = range(len(accounts))

                def normalize_item(item, candidate):
                    normalized = dict(item)
                    # pool_item_id is used only by the GPU repair worker.
                    # The durable affinity is stored on the drama pool row.
                    normalized["pool_item_id"] = None
                    normalized["pool_created_at"] = ""
                    normalized["drama_pool_item_id"] = candidate[
                        "drama_pool_item_id"
                    ]
                    normalized["drama_pool_created_at"] = candidate[
                        "drama_pool_created_at"
                    ]
                    normalized["source_type"] = "drama"
                    normalized["source_date"] = source_date
                    return normalized

                def normalize_relay_item(
                    item, candidate, target_account, relay_account
                ):
                    normalized = normalize_item(item, candidate)
                    normalized.update(
                        {
                            "delivery_mode": "premium_relay_repost",
                            "relay_account_id": int(relay_account["id"]),
                            "relay_account_username": str(
                                relay_account["username"]
                            ),
                            "account_id": int(target_account["id"]),
                            "account_username": str(
                                target_account["username"]
                            ),
                            "page_name": str(
                                target_account.get("display_name", "")
                                or target_account["username"]
                            ),
                            "page_id": str(target_account["x_user_id"]),
                        }
                    )
                    return normalized

                for index in processing_indexes:
                    rank = index + 1
                    candidate = candidates[index]
                    account = accounts[index]
                    if int(candidate.get("candidate_account_id") or 0) != int(
                        account["id"]
                    ):
                        raise ScheduleRunError(
                            "short-drama account assignment changed during preflight",
                            "x_post_schedule_account_mismatch",
                            drama_pool_item_id=candidate.get(
                                "drama_pool_item_id"
                            ),
                            content_id=candidate.get("content_id", ""),
                        )
                    try:
                        item = preflight_one(
                            candidate,
                            account,
                            rank,
                            temporary,
                        )
                    except (
                        CandidatePreflightError,
                        XPostError,
                        http.client.HTTPException,
                        OSError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        code = str(
                            getattr(
                                exc,
                                "code",
                                "x_post_drama_preflight_failed",
                            )
                        )
                        message = "episode %s media preflight failed: %s" % (
                            candidate["episode_key"],
                            exc,
                        )
                        if (
                            code == "x_long_video_requires_premium"
                            and not account.get("long_video_eligible")
                        ):
                            drama_language = canonical_drama_language(
                                candidate.get("material_language")
                                or candidate.get("language")
                            )
                            if drama_language not in relay_accounts:
                                relay_run_date = datetime.fromtimestamp(
                                    timestamp, BEIJING_TZ
                                ).date().isoformat()
                                try:
                                    relay_accounts[drama_language] = sidecar.premium_relay_accounts(
                                        relay_run_date,
                                        drama_language,
                                    )
                                except TypeError:
                                    # Backward-compatible injected sidecar
                                    # adapters used by offline operators/tests.
                                    relay_accounts[drama_language] = sidecar.premium_relay_accounts(
                                        relay_run_date
                                    )
                            relay_account = next(
                                (
                                    relay
                                    for relay in relay_accounts[drama_language]
                                    if int(relay["id"])
                                    != int(account["id"])
                                ),
                                None,
                            )
                            if relay_account is None:
                                raise ScheduleRunError(
                                    "no currently eligible public Premium relay account is available",
                                    "x_post_premium_relay_unavailable",
                                    drama_pool_item_id=candidate[
                                        "drama_pool_item_id"
                                    ],
                                    content_id=candidate["content_id"],
                                ) from None
                            try:
                                relay_item = preflight_one(
                                    candidate,
                                    relay_account,
                                    rank,
                                    temporary,
                                )
                            except (
                                CandidatePreflightError,
                                XPostError,
                                http.client.HTTPException,
                                OSError,
                                TypeError,
                                ValueError,
                            ) as route_exc:
                                raise ScheduleRunError(
                                    "Premium relay drama preflight failed: %s"
                                    % route_exc,
                                    str(
                                        getattr(
                                            route_exc,
                                            "code",
                                            "x_post_drama_preflight_failed",
                                        )
                                    ),
                                ) from None
                            planned_by_index[index] = normalize_relay_item(
                                relay_item,
                                candidate,
                                account,
                                relay_account,
                            )
                            continue
                        if code not in _DRAMA_DETERMINISTIC_REJECTION_CODES:
                            raise ScheduleRunError(
                                message,
                                code,
                            ) from None
                        if int(candidate.get("assigned_account_id") or 0) > 0:
                            raise ScheduleRunError(
                                message,
                                code,
                                drama_pool_item_id=candidate[
                                    "drama_pool_item_id"
                                ],
                                content_id=candidate["content_id"],
                            ) from None
                        reject_unassigned(
                            candidate["drama_pool_item_id"],
                            candidate["content_id"],
                            code,
                            message,
                        )
                        rejected_ids.add(
                            int(candidate["drama_pool_item_id"])
                        )
                        refresh_accounts = True
                        rejected = True
                        break
                    planned_by_index[index] = normalize_item(item, candidate)
                if rejected:
                    continue
                if len(planned_by_index) != len(accounts):
                    raise ScheduleRunError(
                        "short-drama Premium routing did not fill every account",
                        "x_post_schedule_drama_shortage",
                    )
                return [
                    planned_by_index[index]
                    for index in range(len(accounts))
                ]
            raise ScheduleRunError(
                "drama preflight rejection limit was reached",
                "x_post_schedule_drama_shortage",
            )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def _plan_payload(identity, candidates):
    return {
        "source_type": identity["source_type"],
        "run_date": identity["run_date"],
        "publish_time": identity["publish_time"],
        "version": identity["version"],
        "account_ids": list(identity["account_ids"]),
        "source_date": candidates[0]["source_date"],
        "candidates": candidates,
    }


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
            entry["error_code"] = "x_post_retry_requires_review"
            results.append(entry)
            if identity["source_type"] == "drama":
                stopped = True
                break
            continue
        try:
            published = sidecar.publish_queue(
                config.publish_path_template, queue["id"]
            )
            entry.update(
                {
                    "status": published["status"],
                    "log_id": published["log_id"],
                    "preview_url": published["preview_url"],
                }
            )
        except SidecarError as exc:
            entry.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "unknown_outcome": exc.unknown_outcome,
                }
            )
            if (
                identity["source_type"] == "drama"
                or exc.status == 429
                or exc.unknown_outcome
            ):
                stopped = True
        results.append(entry)
        if stopped:
            break
    return {
        "source_type": identity["source_type"],
        "run_date": identity["run_date"],
        "publish_time": identity["publish_time"],
        "version": identity["version"],
        "resumed_existing_plan": bool(resumed),
        "planned_count": len(queues),
        "attempted_count": len(results),
        "published_count": sum(item["status"] == "published" for item in results),
        "status": "stopped" if stopped else (
            "published"
            if len(results) == len(queues)
            and all(item["status"] == "published" for item in results)
            else "completed_with_errors"
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
        # Frozen state always wins.  This query precedes token verification,
        # MySQL reads, downloads and GPU repair.
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
        if existing["found"] and existing["run"].get("status") != "claimed":
            raise ScheduleRunError(
                "frozen schedule run has no queues",
                "x_post_schedule_plan_incomplete",
            )

        try:
            sidecar.preflight_storage(config.storage_preflight_path)
            accounts = _verify_accounts(sidecar, identity["account_ids"])
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
                candidates = material_candidate_loader(
                    config,
                    sidecar,
                    accounts,
                    **material_loader_options,
                )
            else:
                candidates = drama_candidate_loader(
                    config,
                    sidecar,
                    accounts,
                    source_date=source_date,
                    connection_factory=connection_factory,
                    downloader=retrying_downloader,
                    prober=prober,
                    repair_client=repair_client,
                    timestamp=timestamp,
                )
            if [int(item["account_id"]) for item in candidates] != identity[
                "account_ids"
            ]:
                raise ScheduleRunError(
                    "candidate account order does not match frozen schedule",
                    "x_post_schedule_account_mismatch",
                )
            # Media download/repair may outlive a two-hour X access token.
            # Refresh the frozen scope again immediately before the atomic
            # plan transaction; each later publish also verifies its account.
            accounts = _verify_accounts(sidecar, identity["account_ids"])
            sidecar.preflight_storage(config.storage_preflight_path)
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

        queues = sidecar.create_schedule_plan(
            config.plan_path,
            _plan_payload(identity, candidates),
        )
        if [queue["account_id"] for queue in queues] != identity["account_ids"]:
            raise ScheduleRunError(
                "created queue order does not match frozen schedule",
                "x_post_schedule_plan_mismatch",
            )
        result = _publish_frozen_queues(
            config, sidecar, identity, queues, resumed=False
        )
        batches.append(result)

    return {
        "status": (
            "stopped"
            if any(item["status"] == "stopped" for item in batches)
            else (
                "published"
                if batches and all(item["status"] == "published" for item in batches)
                else "completed_with_errors"
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
