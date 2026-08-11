#!/usr/bin/env python3
"""Publish one compliant Dramawave video to each configured X account daily."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.selector import (  # noqa: E402
    CandidateSelectionError,
    DEFAULT_SCHEMA,
    connect_read_only,
    normalize_date,
    previous_source_date,
    select_pool_candidates,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    PREMIUM_MAX_DURATION_SECONDS,
    PREMIUM_SUBSCRIPTION_TYPES,
    STANDARD_MAX_DURATION_SECONDS,
    XPostError,
    build_post_text,
    build_w2a_url,
    download_media,
    probe_media,
    redact_text,
)


DEFAULT_INTERNAL_URL = "http://127.0.0.1:8810"
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_SIDECAR_RESPONSE_BYTES = 1024 * 1024
MAX_REPAIR_RESPONSE_BYTES = 64 * 1024
_SAFE_INTERNAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
FIXED_DAILY_WORK_DIR = Path("/mnt/data-disk/x-post-automation/daily-work")
REPAIRABLE_MEDIA_CODES = frozenset(
    {
        "invalid_media_codec",
        "invalid_media_dimensions",
        "invalid_media_duration",
    }
)
DEFAULT_REPAIR_PROFILE = "x-h264-nvenc-720-duration-policy-v3"
MAX_DAILY_ACCOUNTS = 50
POSITIVE_REPAIR_MATERIAL_ID_RE = re.compile(r"\A[1-9][0-9]{0,30}\Z")
DRAMA_RESOURCE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


class DailyRunError(RuntimeError):
    def __init__(self, message, code="x_post_daily_preflight_failed"):
        self.code = str(code or "x_post_daily_preflight_failed")
        super().__init__(str(message))


class SidecarError(DailyRunError):
    def __init__(self, code, message, status=503, unknown_outcome=False):
        self.code = str(code or "x_sidecar_error")[:64]
        self.status = int(status or 503)
        self.unknown_outcome = bool(unknown_outcome)
        super().__init__(
            str(message or "X sidecar request failed")[:240],
            code=self.code,
        )


class CandidatePreflightError(DailyRunError):
    """A known candidate-local failure that may safely replenish from FIFO."""


class MediaRepairError(CandidatePreflightError):
    def __init__(self, code, message, status=503):
        self.status = int(status or 503)
        super().__init__(
            str(message or "X media repair request failed")[:240],
            code=str(code or "x_post_media_repair_failed")[:64],
        )


def _normalize_repair_material_id(value):
    value = str(value or "").strip()
    if POSITIVE_REPAIR_MATERIAL_ID_RE.fullmatch(value):
        return value
    if DRAMA_RESOURCE_ID_RE.fullmatch(value):
        return value
    raise MediaRepairError(
        "x_post_media_repair_invalid_request",
        "material_id must be a positive integer or a 32-character hexadecimal resource ID",
        400,
    )


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(value, maximum))


def _parse_account_ids(value):
    raw = str(value or "").replace(" ", "")
    try:
        items = tuple(int(item) for item in raw.split(",") if item)
    except (TypeError, ValueError, OverflowError):
        raise DailyRunError(
            "X_POST_DAILY_ACCOUNT_IDS must contain comma-separated integer IDs"
        ) from None
    if (
        not items
        or len(items) > MAX_DAILY_ACCOUNTS
        or len(set(items)) != len(items)
        or any(item <= 0 for item in items)
    ):
        raise DailyRunError(
            "X_POST_DAILY_ACCOUNT_IDS must contain 1 to %s unique positive IDs"
            % MAX_DAILY_ACCOUNTS
        )
    return items


def _parse_hosts(value):
    values = tuple(
        dict.fromkeys(
            item.strip().lower().rstrip(".")
            for item in str(value or "").replace(",", " ").split()
            if item.strip()
        )
    )
    if not values:
        raise DailyRunError("X_POST_DAILY_MEDIA_ALLOWED_HOSTS is empty")
    if any(not re.fullmatch(r"(?:[a-z0-9-]+\.)*[a-z0-9-]+", item) for item in values):
        raise DailyRunError("X_POST_DAILY_MEDIA_ALLOWED_HOSTS is invalid")
    return values


@dataclass(frozen=True)
class DailyConfig:
    internal_url: str
    internal_token: str
    account_ids: tuple
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
    material_keys_path: str
    storage_preflight_path: str
    failure_path: str
    plan_path: str
    publish_path_template: str
    pool_available_path: str = "/internal/posts/material-pool/available"
    pool_check_path: str = "/internal/posts/material-pool/check"
    repair_url: str = ""
    repair_token: str = ""
    repair_timeout: int = 900
    repair_profile: str = DEFAULT_REPAIR_PROFILE
    max_repairs_per_run: int = 6
    plan_query_path: str = "/internal/posts/daily-plan/query"

    @classmethod
    def from_env(cls):
        token = os.environ.get("X_POST_DAILY_INTERNAL_TOKEN", "").strip()
        return cls(
            internal_url=os.environ.get(
                "X_POST_DAILY_INTERNAL_URL",
                os.environ.get("X_POST_AUTOMATION_INTERNAL_URL", DEFAULT_INTERNAL_URL),
            ).strip().rstrip("/"),
            internal_token=token,
            account_ids=_parse_account_ids(
                os.environ.get("X_POST_DAILY_ACCOUNT_IDS", "")
            ),
            start_date=os.environ.get("X_POST_DAILY_START_DATE", "").strip(),
            mysql_host=os.environ.get("X_POST_DAILY_MYSQL_HOST", "").strip(),
            mysql_port=_env_int("X_POST_DAILY_MYSQL_PORT", 63350, 1, 65535),
            mysql_user=os.environ.get("X_POST_DAILY_MYSQL_USER", "").strip(),
            mysql_password=os.environ.get("X_POST_DAILY_MYSQL_PASSWORD", ""),
            mysql_database=os.environ.get(
                "X_POST_DAILY_MYSQL_DATABASE", DEFAULT_SCHEMA
            ).strip(),
            mysql_connect_timeout=_env_int(
                "X_POST_DAILY_MYSQL_CONNECT_TIMEOUT", 5, 1, 30
            ),
            mysql_read_timeout=_env_int(
                "X_POST_DAILY_MYSQL_READ_TIMEOUT", 30, 5, 180
            ),
            scan_limit=_env_int("X_POST_DAILY_SCAN_LIMIT", 1000, 3, 1000),
            candidate_pool_limit=_env_int(
                "X_POST_DAILY_CANDIDATE_POOL_LIMIT", 50, 3, 100
            ),
            media_allowed_hosts=_parse_hosts(
                os.environ.get("X_POST_DAILY_MEDIA_ALLOWED_HOSTS", "")
            ),
            max_media_bytes=_env_int(
                "X_POST_DAILY_MAX_MEDIA_BYTES",
                512 * 1024 * 1024,
                1024,
                512 * 1024 * 1024,
            ),
            media_timeout=_env_int("X_POST_DAILY_MEDIA_TIMEOUT", 30, 5, 120),
            internal_timeout=_env_int("X_POST_DAILY_INTERNAL_TIMEOUT", 900, 5, 900),
            lock_path=os.environ.get(
                "X_POST_DAILY_LOCK_PATH", "/run/x-post-daily/runner.lock"
            ).strip(),
            work_dir=os.environ.get(
                "X_POST_DAILY_WORK_DIR",
                "/mnt/data-disk/x-post-automation/daily-work",
            ).strip(),
            material_keys_path=os.environ.get(
                "X_POST_DAILY_MATERIAL_KEYS_PATH",
                "/internal/posts/material-keys/query",
            ).strip(),
            storage_preflight_path=os.environ.get(
                "X_POST_DAILY_STORAGE_PREFLIGHT_PATH",
                "/internal/posts/storage/preflight",
            ).strip(),
            failure_path=os.environ.get(
                "X_POST_DAILY_FAILURE_PATH",
                "/internal/posts/runs/record-failure",
            ).strip(),
            plan_path=os.environ.get(
                "X_POST_DAILY_PLAN_PATH", "/internal/posts/daily-plan"
            ).strip(),
            publish_path_template=os.environ.get(
                "X_POST_DAILY_PUBLISH_PATH_TEMPLATE",
                "/internal/posts/queue/{queue_id}/publish",
            ).strip(),
            pool_available_path=os.environ.get(
                "X_POST_DAILY_POOL_AVAILABLE_PATH",
                "/internal/posts/material-pool/available",
            ).strip(),
            pool_check_path=os.environ.get(
                "X_POST_DAILY_POOL_CHECK_PATH",
                "/internal/posts/material-pool/check",
            ).strip(),
            repair_url=os.environ.get(
                "X_POST_DAILY_REPAIR_URL", ""
            ).strip(),
            repair_token=os.environ.get(
                "X_POST_DAILY_REPAIR_TOKEN",
                os.environ.get("X_POST_MEDIA_REPAIR_TOKEN", ""),
            ).strip(),
            repair_timeout=_env_int(
                "X_POST_DAILY_REPAIR_TIMEOUT", 900, 5, 3600
            ),
            repair_profile=os.environ.get(
                "X_POST_DAILY_REPAIR_PROFILE",
                DEFAULT_REPAIR_PROFILE,
            ).strip(),
            max_repairs_per_run=_env_int(
                "X_POST_DAILY_MAX_REPAIRS_PER_RUN", 6, 0, 50
            ),
            plan_query_path=os.environ.get(
                "X_POST_DAILY_PLAN_QUERY_PATH",
                "/internal/posts/daily-plan/query",
            ).strip(),
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
            raise DailyRunError("X Post sidecar URL must be loopback HTTP")
        if not self.internal_token:
            raise DailyRunError("X_POST_DAILY_INTERNAL_TOKEN is required")
        if self.start_date:
            normalize_date(self.start_date, "start_date")
        if not self.mysql_host or not self.mysql_user or self.mysql_password == "":
            raise DailyRunError("read-only MySQL configuration is incomplete")
        if self.candidate_pool_limit > self.scan_limit:
            raise DailyRunError("candidate pool limit cannot exceed scan limit")
        if self.scan_limit < len(self.account_ids):
            raise DailyRunError(
                "scan limit cannot be smaller than the configured account count"
            )
        if self.candidate_pool_limit < len(self.account_ids):
            raise DailyRunError(
                "candidate pool limit cannot be smaller than the configured account count"
            )
        work_dir = Path(self.work_dir)
        if not work_dir.is_absolute():
            raise DailyRunError("X_POST_DAILY_WORK_DIR must be absolute")
        if os.name != "nt" and work_dir != FIXED_DAILY_WORK_DIR:
            raise DailyRunError(
                "X_POST_DAILY_WORK_DIR must use the fixed data-disk work directory"
            )
        for path in (
            self.material_keys_path,
            self.storage_preflight_path,
            self.failure_path,
            self.plan_query_path,
            self.plan_path,
            self.pool_available_path,
            self.pool_check_path,
        ):
            if not path.startswith("/internal/") or "?" in path or "#" in path:
                raise DailyRunError("invalid sidecar endpoint path")
        if (
            "{queue_id}" not in self.publish_path_template
            or not self.publish_path_template.startswith("/internal/")
        ):
            raise DailyRunError("invalid publish endpoint path template")
        if self.repair_url:
            repair = urllib.parse.urlsplit(self.repair_url)
            try:
                repair_port = repair.port
            except ValueError:
                raise DailyRunError("X Post media repair URL is invalid") from None
            if (
                repair.scheme != "http"
                or repair.hostname not in _SAFE_INTERNAL_HOSTS
                or repair.username is not None
                or repair.password is not None
                or repair.query
                or repair.fragment
                or (
                    repair_port is not None
                    and not 1 <= repair_port <= 65535
                )
                or not repair.path.startswith("/")
                or repair.path == "/"
            ):
                raise DailyRunError(
                    "X Post media repair URL must be a loopback HTTP endpoint"
                )
            if not self.repair_token:
                raise DailyRunError("X_POST_DAILY_REPAIR_TOKEN is required")
            if self.repair_token == self.internal_token:
                raise DailyRunError(
                    "X Post media repair token must be independent"
                )
            if not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,64}", self.repair_profile
            ):
                raise DailyRunError("X_POST_DAILY_REPAIR_PROFILE is invalid")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _file_pointer, _code, _message, _headers, _url):
        return None


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if math.isfinite(normalized) else None


def _validate_repair_probe(
    raw, output_size, max_duration_seconds=STANDARD_MAX_DURATION_SECONDS
):
    try:
        max_duration_seconds = float(max_duration_seconds)
    except (TypeError, ValueError, OverflowError):
        raise MediaRepairError(
            "x_post_media_repair_invalid_response",
            "X media repair duration policy is invalid",
            502,
        ) from None
    if max_duration_seconds not in {
        STANDARD_MAX_DURATION_SECONDS,
        PREMIUM_MAX_DURATION_SECONDS,
    }:
        raise MediaRepairError(
            "x_post_media_repair_invalid_response",
            "X media repair duration policy is invalid",
            502,
        )
    if not isinstance(raw, dict):
        raise MediaRepairError(
            "x_post_media_repair_invalid_response",
            "X media repair probe is invalid",
            502,
        )
    width = raw.get("width")
    height = raw.get("height")
    size = raw.get("size")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size != output_size
        or width < 32
        or height < 32
        or width > 1280
        or height > 1280
    ):
        raise MediaRepairError(
            "x_post_media_repair_invalid_response",
            "X media repair dimensions or size are invalid",
            502,
        )
    ratio = float(width) / float(height)
    frame_rate = _finite_number(raw.get("frame_rate"))
    duration = _finite_number(raw.get("duration"))
    if (
        ratio < (1.0 / 3.0)
        or ratio > 3.0
        or frame_rate is None
        or frame_rate <= 0
        or frame_rate > 60
        or duration is None
        or duration < 0.5
        or duration > max_duration_seconds
        or str(raw.get("codec", "") or "").lower() != "h264"
        or str(raw.get("pixel_format", "") or "").lower() != "yuv420p"
        or str(raw.get("audio_codec", "") or "").lower() != "aac"
    ):
        raise MediaRepairError(
            "x_post_media_repair_invalid_response",
            "X media repair probe does not meet the X video contract",
            502,
        )
    return {
        "codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "width": width,
        "height": height,
        "frame_rate": frame_rate,
        "duration": duration,
        "size": size,
    }


class MediaRepairClient:
    """Strict loopback client for the independently authenticated GPU worker."""

    def __init__(
        self,
        url,
        token,
        *,
        timeout=900,
        max_output_bytes=512 * 1024 * 1024,
        opener=None,
    ):
        self.url = str(url or "")
        self.token = str(token or "")
        self.timeout = int(timeout)
        self.max_output_bytes = int(max_output_bytes)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    @staticmethod
    def _decode(raw):
        if len(raw or b"") > MAX_REPAIR_RESPONSE_BYTES:
            return {}
        try:
            value = json.loads(bytes(raw or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def repair(self, payload):
        if not isinstance(payload, dict):
            raise MediaRepairError(
                "x_post_media_repair_invalid_request",
                "X media repair request must be an object",
                400,
            )
        payload = dict(payload)
        duration_policy = str(
            payload.get("duration_policy", "") or ""
        ).strip().lower()
        if duration_policy not in {"standard", "premium"}:
            raise MediaRepairError(
                "x_post_media_repair_invalid_request",
                "X media repair duration policy is invalid",
                400,
            )
        payload["material_id"] = _normalize_repair_material_id(
            payload.get("material_id")
        )
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            method="POST",
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            with response:
                raw = response.read(MAX_REPAIR_RESPONSE_BYTES + 1)
                status = int(
                    getattr(response, "status", response.getcode())
                )
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                try:
                    raw = exc.read(MAX_REPAIR_RESPONSE_BYTES + 1)
                except (
                    http.client.HTTPException,
                    TimeoutError,
                    OSError,
                ):
                    raise MediaRepairError(
                        "x_post_media_repair_unreachable",
                        "X media repair error response was interrupted",
                        503,
                    ) from None
            finally:
                exc.close()
            data = self._decode(raw)
            raw_error = data.get("error") if isinstance(data, dict) else None
            if isinstance(raw_error, dict):
                code = raw_error.get("code") or raw_error.get("error")
                message = raw_error.get("message") or data.get("message")
            else:
                code = raw_error or (
                    data.get("code") if isinstance(data, dict) else ""
                )
                message = data.get("message") if isinstance(data, dict) else ""
            code = str(code or "")
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
                code = "x_post_media_repair_http_error"
            raise MediaRepairError(
                code,
                redact_text(message or "X media repair worker rejected the job", 240),
                status,
            ) from None
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ):
            raise MediaRepairError(
                "x_post_media_repair_unreachable",
                "X media repair worker network request failed",
                503,
            ) from None
        if len(raw) > MAX_REPAIR_RESPONSE_BYTES:
            raise MediaRepairError(
                "x_post_media_repair_invalid_response",
                "X media repair response is too large",
                502,
            )
        data = self._decode(raw)
        if status < 200 or status >= 300 or not isinstance(data, dict):
            raise MediaRepairError(
                "x_post_media_repair_invalid_response",
                "X media repair worker returned an invalid response",
                502,
            )
        expected_job_key = str(payload.get("job_key", "") or "")
        expected_profile = str(payload.get("profile", "") or "")
        output_url = str(data.get("output_url", "") or "")
        output_sha256 = str(data.get("output_sha256", "") or "").lower()
        output_size = data.get("output_size")
        parsed = urllib.parse.urlsplit(output_url)
        try:
            output_port = parsed.port
        except ValueError:
            output_port = -1
        if (
            data.get("status") != "ready"
            or str(data.get("job_key", "") or "") != expected_job_key
            or str(data.get("profile", "") or "") != expected_profile
            or not isinstance(data.get("reused"), bool)
            or not re.fullmatch(r"[a-f0-9]{64}", output_sha256)
            or not isinstance(output_size, int)
            or isinstance(output_size, bool)
            or output_size <= 0
            or output_size > self.max_output_bytes
            or len(output_url) > 2048
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or output_port not in {None, 443}
        ):
            raise MediaRepairError(
                "x_post_media_repair_invalid_response",
                "X media repair worker response identity is invalid",
                502,
            )
        probe = _validate_repair_probe(
            data.get("probe"),
            output_size,
            max_duration_seconds=(
                PREMIUM_MAX_DURATION_SECONDS
                if duration_policy == "premium"
                else STANDARD_MAX_DURATION_SECONDS
            ),
        )
        return {
            "status": "ready",
            "job_key": expected_job_key,
            "profile": expected_profile,
            "reused": data["reused"],
            "output_url": output_url,
            "output_sha256": output_sha256,
            "output_size": output_size,
            "probe": probe,
        }


class SidecarClient:
    def __init__(self, base_url, token, timeout=900, opener=None):
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout = int(timeout)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def post(self, path, payload, write_may_have_happened=False):
        url = self.base_url + str(path)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            method="POST",
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            with response:
                raw = response.read(MAX_SIDECAR_RESPONSE_BYTES + 1)
                status = int(getattr(response, "status", response.getcode()))
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                try:
                    raw = exc.read(MAX_ERROR_BODY_BYTES + 1)
                except (
                    http.client.HTTPException,
                    TimeoutError,
                    OSError,
                ):
                    raise SidecarError(
                        "x_sidecar_unreachable",
                        "X sidecar error response was interrupted",
                        503,
                        unknown_outcome=write_may_have_happened,
                    ) from None
            finally:
                exc.close()
            payload_data = self._decode(raw)
            raw_error = payload_data.get("error") if isinstance(payload_data, dict) else None
            if isinstance(raw_error, dict):
                code = raw_error.get("code") or raw_error.get("error")
                message = raw_error.get("message") or payload_data.get("message")
                explicit_unknown = raw_error.get("unknown_outcome")
            else:
                # The production sidecar uses the flat
                # {"error":"code","message":"..."} contract.
                code = raw_error
                message = payload_data.get("message") if isinstance(payload_data, dict) else ""
                explicit_unknown = (
                    payload_data.get("unknown_outcome")
                    if isinstance(payload_data, dict)
                    else False
                )
            explicit_known = (
                payload_data.get("outcome_known")
                if isinstance(payload_data, dict)
                else False
            )
            normalized_code = str(code or "")
            unknown = bool(
                write_may_have_happened
                and (
                    normalized_code == "x_publish_unknown"
                    or not (
                        explicit_known is True
                        and explicit_unknown is False
                        and normalized_code
                    )
                )
            )
            raise SidecarError(code or "x_sidecar_http_error", message, status, unknown) from None
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ):
            raise SidecarError(
                "x_sidecar_unreachable",
                "X sidecar network request failed",
                503,
                unknown_outcome=write_may_have_happened,
            ) from None
        if len(raw) > MAX_SIDECAR_RESPONSE_BYTES:
            raise SidecarError(
                "x_sidecar_invalid_response",
                "X sidecar response is too large",
                502,
                unknown_outcome=write_may_have_happened,
            )
        data = self._decode(raw, MAX_SIDECAR_RESPONSE_BYTES)
        if status < 200 or status >= 300 or not isinstance(data, dict):
            raise SidecarError(
                "x_sidecar_invalid_response",
                "X sidecar returned an invalid response",
                502,
                unknown_outcome=write_may_have_happened,
            )
        return data

    @staticmethod
    def _decode(raw, max_bytes=MAX_ERROR_BODY_BYTES):
        if len(raw or b"") > int(max_bytes):
            return {}
        try:
            value = json.loads(bytes(raw or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def verify_account(self, account_id):
        result = self.post(
            "/internal/posts/accounts/%s/verify" % int(account_id),
            {
                "actor": {
                    "tenant_key": "internal",
                    "user_id": "x-post-daily",
                    "name": "X Post Daily",
                    "email": "",
                    "role": "admin",
                },
                "scope": "all",
            },
        )
        item = result.get("item")
        if not isinstance(item, dict):
            raise SidecarError("x_account_invalid_response", "X account verification is invalid")
        if item.get("status") != "active" or item.get("publish_eligible") is not True:
            raise SidecarError(
                "x_account_not_publishable",
                "X account %s is not publishable" % int(account_id),
                409,
            )
        if int(item.get("id") or 0) != int(account_id):
            raise SidecarError("x_identity_mismatch", "X account verification identity mismatch", 409)
        for field in ("username", "x_user_id"):
            if not str(item.get(field, "") or "").strip():
                raise SidecarError("x_account_invalid_response", "X account identity is incomplete")
        return item

    def used_material_keys(self, path, material_ids):
        if (
            not isinstance(material_ids, list)
            or not 1 <= len(material_ids) <= 1000
            or any(not re.fullmatch(r"[1-9][0-9]*", str(value or "")) for value in material_ids)
        ):
            raise DailyRunError("material-key occupancy query must contain 1..1000 IDs")
        result = self.post(path, {"material_keys": material_ids})
        item = result.get("item") if isinstance(result.get("item"), dict) else result
        values = item.get("material_keys", []) if isinstance(item, dict) else []
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise SidecarError(
                "x_material_keys_invalid_response",
                "Published material-key response is invalid",
            )
        requested = set(material_ids)
        occupied = set(values)
        if (
            any(not re.fullmatch(r"[1-9][0-9]*", value) for value in occupied)
            or not occupied.issubset(requested)
        ):
            raise SidecarError(
                "x_material_keys_invalid_response",
                "Published material-key response is invalid",
            )
        return occupied

    def available_pool_items(self, path, limit):
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise DailyRunError("pool candidate limit is invalid") from None
        if limit <= 0 or limit > 1000:
            raise DailyRunError("pool candidate limit is out of range")
        result = self.post(path, {"limit": limit})
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) > limit:
            raise SidecarError(
                "x_post_pool_invalid_response",
                "Material pool response is invalid",
            )
        normalized = []
        seen_ids = set()
        seen_materials = set()
        previous_order = None
        for raw in items:
            if not isinstance(raw, dict):
                raise SidecarError(
                    "x_post_pool_invalid_response",
                    "Material pool item is invalid",
                )
            pool_item_id = raw.get("id")
            material_id = str(raw.get("material_id", "") or "")
            material_key = str(raw.get("material_key", "") or "")
            created_at = str(raw.get("created_at", "") or "")
            if (
                not isinstance(pool_item_id, int)
                or isinstance(pool_item_id, bool)
                or pool_item_id <= 0
                or pool_item_id in seen_ids
                or not re.fullmatch(r"[1-9][0-9]*", material_id)
                or material_key != material_id
                or material_id in seen_materials
                or not created_at
                or len(created_at) > 64
            ):
                raise SidecarError(
                    "x_post_pool_invalid_response",
                    "Material pool item identity is invalid",
                )
            order = (created_at, pool_item_id)
            if previous_order is not None and order >= previous_order:
                raise SidecarError(
                    "x_post_pool_invalid_response",
                    "Material pool FIFO order is invalid",
                )
            previous_order = order
            seen_ids.add(pool_item_id)
            seen_materials.add(material_id)
            normalized.append(
                {
                    "id": pool_item_id,
                    "material_id": material_id,
                    "material_key": material_key,
                    "created_at": created_at,
                }
            )
        return normalized

    def record_pool_checks(self, path, checks):
        if not checks:
            return {"updated_count": 0}
        result = self.post(path, {"checks": checks})
        item = result.get("item") if isinstance(result.get("item"), dict) else result
        updated_count = item.get("updated_count") if isinstance(item, dict) else None
        if (
            not isinstance(updated_count, int)
            or isinstance(updated_count, bool)
            or updated_count < 0
            or updated_count > len(checks)
        ):
            raise SidecarError(
                "x_post_pool_check_invalid_response",
                "Material pool check response is invalid",
            )
        return {"updated_count": updated_count}

    def preflight_storage(self, path):
        result = self.post(path, {})
        item = result.get("item") if isinstance(result.get("item"), dict) else result
        if (
            not isinstance(item, dict)
            or item.get("ready") is not True
            or item.get("mounted") is not True
            or item.get("atomic_write") is not True
        ):
            raise SidecarError(
                "x_post_storage_preflight_invalid_response",
                "X Post storage preflight response is invalid",
            )
        return item

    def query_daily_plan(self, path, run_date):
        """Strictly parse the identity-only snapshot used for same-day recovery."""
        requested_date = normalize_date(run_date, "run_date")
        result = self.post(path, {"run_date": requested_date})
        item = result.get("item")
        if not isinstance(item, dict) or set(item) != {"found", "run", "queues"}:
            raise SidecarError(
                "x_daily_plan_query_invalid_response",
                "Daily plan query response is invalid",
            )
        found = item.get("found")
        queues = item.get("queues")
        run = item.get("run")
        if not isinstance(found, bool) or not isinstance(queues, list):
            raise SidecarError(
                "x_daily_plan_query_invalid_response",
                "Daily plan query response is invalid",
            )
        if not found:
            if run is not None or queues:
                raise SidecarError(
                    "x_daily_plan_query_invalid_response",
                    "Missing daily plan response is inconsistent",
                )
            return {"found": False, "run": None, "queues": []}

        run_fields = {
            "id",
            "run_date",
            "source_date",
            "status",
            "expected_count",
            "queued_count",
            "published_count",
            "failed_count",
            "unknown_count",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        }
        queue_fields = {
            "id",
            "run_id",
            "run_date",
            "source_date",
            "account_id",
            "candidate_rank",
            "status",
            "created_at",
            "updated_at",
        }
        run_statuses = {
            "queued",
            "running",
            "completed",
            "completed_with_errors",
            "needs_review",
            "stopped",
            "failed_preflight",
        }
        queue_statuses = {"queued", "publishing", "published", "failed"}
        if not isinstance(run, dict) or set(run) != run_fields:
            raise SidecarError(
                "x_daily_plan_query_invalid_response",
                "Daily plan identity is invalid",
            )
        run_id = run.get("id")
        expected_count = run.get("expected_count")
        counters = [
            expected_count,
            run.get("queued_count"),
            run.get("published_count"),
            run.get("failed_count"),
            run.get("unknown_count"),
        ]
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or run.get("run_date") != requested_date
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(run.get("source_date") or ""))
            or run.get("status") not in run_statuses
            or not isinstance(expected_count, int)
            or isinstance(expected_count, bool)
            or expected_count < 1
            or expected_count > MAX_DAILY_ACCOUNTS
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > expected_count
                for value in counters
            )
            or any(
                not isinstance(run.get(field), str)
                or len(run.get(field)) > 64
                or any(ord(char) < 32 for char in run.get(field))
                for field in ("started_at", "finished_at", "created_at", "updated_at")
            )
            or len(queues) > expected_count
        ):
            raise SidecarError(
                "x_daily_plan_query_invalid_response",
                "Daily plan identity is invalid",
            )

        normalized = []
        queue_ids = set()
        account_ids = set()
        candidate_ranks = set()
        previous_rank = 0
        for queue in queues:
            if not isinstance(queue, dict) or set(queue) != queue_fields:
                raise SidecarError(
                    "x_daily_plan_query_invalid_response",
                    "Daily plan queue identity is invalid",
                )
            queue_id = queue.get("id")
            account_id = queue.get("account_id")
            candidate_rank = queue.get("candidate_rank")
            if (
                not isinstance(queue_id, int)
                or isinstance(queue_id, bool)
                or queue_id <= 0
                or queue_id in queue_ids
                or not isinstance(account_id, int)
                or isinstance(account_id, bool)
                or account_id <= 0
                or account_id in account_ids
                or not isinstance(candidate_rank, int)
                or isinstance(candidate_rank, bool)
                or candidate_rank < 1
                or candidate_rank > expected_count
                or candidate_rank in candidate_ranks
                or candidate_rank <= previous_rank
                or queue.get("run_id") != run_id
                or queue.get("run_date") != requested_date
                or queue.get("source_date") != run.get("source_date")
                or queue.get("status") not in queue_statuses
                or any(
                    not isinstance(queue.get(field), str)
                    or len(queue.get(field)) > 64
                    or any(ord(char) < 32 for char in queue.get(field))
                    for field in ("created_at", "updated_at")
                )
            ):
                raise SidecarError(
                    "x_daily_plan_query_invalid_response",
                    "Daily plan queue identity is invalid",
                )
            queue_ids.add(queue_id)
            account_ids.add(account_id)
            candidate_ranks.add(candidate_rank)
            previous_rank = candidate_rank
            normalized.append(dict(queue))
        return {"found": True, "run": dict(run), "queues": normalized}

    def create_plan(self, path, payload):
        candidates = payload.get("candidates")
        expected_count = len(candidates) if isinstance(candidates, list) else 0
        if (
            not isinstance(candidates, list)
            or expected_count < 1
            or expected_count > MAX_DAILY_ACCOUNTS
        ):
            raise SidecarError(
                "x_daily_plan_invalid_request",
                "Daily plan request identity is invalid",
            )
        requested_account_ids = []
        requested_material_ids = []
        requested_pool_item_ids = []
        for candidate in candidates:
            account_id = candidate.get("account_id") if isinstance(candidate, dict) else None
            material_id = candidate.get("material_id") if isinstance(candidate, dict) else None
            pool_item_id = candidate.get("pool_item_id") if isinstance(candidate, dict) else None
            if (
                not isinstance(account_id, int)
                or isinstance(account_id, bool)
                or account_id <= 0
                or not isinstance(pool_item_id, int)
                or isinstance(pool_item_id, bool)
                or pool_item_id <= 0
                or not re.fullmatch(r"[1-9][0-9]*", str(material_id or ""))
            ):
                raise SidecarError(
                    "x_daily_plan_invalid_request",
                    "Daily plan request account identity is invalid",
                )
            requested_account_ids.append(account_id)
            requested_material_ids.append(str(material_id))
            requested_pool_item_ids.append(pool_item_id)
        if (
            len(set(requested_account_ids)) != expected_count
            or len(set(requested_material_ids)) != expected_count
            or len(set(requested_pool_item_ids)) != expected_count
        ):
            raise SidecarError(
                "x_daily_plan_invalid_request",
                "Daily plan request identity is not unique",
            )
        result = self.post(path, payload, write_may_have_happened=True)
        item = result.get("item") if isinstance(result.get("item"), dict) else result
        queues = item.get("queues") if isinstance(item, dict) else None
        run_id = item.get("id") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or item.get("run_date") != payload.get("run_date")
            or item.get("source_date") != payload.get("source_date")
            or item.get("expected_count") != expected_count
            or item.get("status") not in {
                "queued",
                "running",
                "stopped",
                "completed",
                "completed_with_errors",
                "needs_review",
            }
            or not isinstance(queues, list)
            or len(queues) != expected_count
        ):
            raise SidecarError(
                "x_daily_plan_invalid_response",
                "Daily plan response is invalid",
                unknown_outcome=True,
            )
        normalized = []
        queue_ids = set()
        response_account_ids = []
        for queue in queues:
            if not isinstance(queue, dict):
                raise SidecarError(
                    "x_daily_plan_invalid_response",
                    "Daily plan queue is invalid",
                    unknown_outcome=True,
                )
            queue_id = queue.get("id")
            account_id = queue.get("account_id")
            if (
                not isinstance(queue_id, int)
                or isinstance(queue_id, bool)
                or queue_id <= 0
                or queue_id in queue_ids
            ):
                raise SidecarError(
                    "x_daily_plan_invalid_response",
                    "Daily plan queue identity is invalid",
                    unknown_outcome=True,
                )
            if (
                not isinstance(account_id, int)
                or isinstance(account_id, bool)
                or account_id <= 0
                or account_id in response_account_ids
                or queue.get("run_id") != run_id
                or queue.get("run_date") != payload.get("run_date")
                or queue.get("source_date") != payload.get("source_date")
                or str(queue.get("material_id") or "")
                != requested_material_ids[len(response_account_ids)]
                or queue.get("pool_item_id")
                != requested_pool_item_ids[len(response_account_ids)]
            ):
                raise SidecarError(
                    "x_daily_plan_invalid_response",
                    "Daily plan queue account/date is invalid",
                    unknown_outcome=True,
                )
            queue_ids.add(queue_id)
            response_account_ids.append(account_id)
            normalized.append(dict(queue))
        if response_account_ids != requested_account_ids:
            raise SidecarError(
                "x_daily_plan_invalid_response",
                "Daily plan queue account order is invalid",
                unknown_outcome=True,
            )
        return normalized

    def record_run_failure(
        self,
        path,
        run_date,
        source_date,
        error_code,
        error_message,
        expected_count,
    ):
        result = self.post(
            path,
            {
                "run_date": run_date,
                "source_date": source_date,
                "error_code": error_code,
                "error_message": error_message,
                "expected_count": int(expected_count),
            },
        )
        item = result.get("item")
        if (
            not isinstance(item, dict)
            or item.get("run_date") != run_date
            or item.get("source_date") != source_date
            or not isinstance(item.get("recorded"), bool)
        ):
            raise SidecarError(
                "x_daily_failure_invalid_response",
                "Daily preflight-failure response is invalid",
            )
        return item

    def publish_queue(self, path_template, queue_id):
        path = path_template.format(queue_id=int(queue_id))
        result = self.post(path, {}, write_may_have_happened=True)
        item = result.get("item") if isinstance(result.get("item"), dict) else result
        if not isinstance(item, dict):
            raise SidecarError(
                "x_publish_invalid_response",
                "X publish response is invalid",
                502,
                unknown_outcome=True,
            )
        try:
            log_id = int(item.get("log_id"))
        except (TypeError, ValueError, OverflowError):
            log_id = 0
        post_id = str(item.get("post_id", "") or "")
        preview_url = str(item.get("preview_url", "") or "")
        short_url = str(item.get("short_url", "") or "")
        valid = item.get("status") == "published" and log_id > 0
        valid = valid and bool(re.fullmatch(r"[0-9]{1,32}", post_id))
        try:
            preview = urllib.parse.urlsplit(preview_url)
            short = urllib.parse.urlsplit(short_url)
            preview_port = preview.port
            short_port = short.port
        except ValueError:
            valid = False
            preview = short = None
            preview_port = short_port = None
        if valid:
            preview_match = re.fullmatch(
                r"/[A-Za-z0-9_]{1,50}/status/([0-9]{1,32})",
                preview.path,
            )
            valid = (
                preview.scheme == "https"
                and preview.hostname == "x.com"
                and preview_port is None
                and not preview.username
                and not preview.password
                and not preview.query
                and not preview.fragment
                and preview_match is not None
                and preview_match.group(1) == post_id
                and short.scheme == "https"
                and short.hostname == "ai.yingliangads.com"
                and short_port is None
                and not short.username
                and not short.password
                and not short.query
                and not short.fragment
                and short.path == "/s2l/%s.html" % log_id
            )
        if not valid:
            raise SidecarError(
                "x_publish_invalid_response",
                "X publish response is incomplete or inconsistent",
                502,
                unknown_outcome=True,
            )
        return {
            "status": "published",
            "log_id": log_id,
            "short_url": short_url,
            "post_id": post_id,
            "preview_url": preview_url,
        }


def _connect_from_config(config):
    return connect_read_only(
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
        database=config.mysql_database,
        connect_timeout=config.mysql_connect_timeout,
        read_timeout=config.mysql_read_timeout,
    )


def _safe_account(item):
    subscription_type = str(
        item.get("subscription_type", "unknown") or "unknown"
    ).strip().lower()
    long_video_eligible = (
        subscription_type in PREMIUM_SUBSCRIPTION_TYPES
        and bool(item.get("long_video_eligible"))
    )
    return {
        "id": int(item["id"]),
        "username": str(item["username"]),
        "x_user_id": str(item["x_user_id"]),
        "display_name": str(item.get("display_name", "") or item["username"]),
        "subscription_type": subscription_type,
        "premium_subscriber": long_video_eligible,
        "long_video_eligible": long_video_eligible,
    }


def _plan_candidate(account, candidate, rank, timestamp):
    item = dict(candidate)
    item.update(
        {
            "account_id": account["id"],
            "account_username": account["username"],
            "page_name": account["display_name"],
            "page_id": account["x_user_id"],
            "candidate_rank": rank,
        }
    )
    build_w2a_url(
        {
            "username": item["account_username"],
            "timestamp": timestamp,
            "material_language": item["material_language"],
            "drama_name": item["drama_name"],
            "tag": item["tag"],
            "log_id": 1,
            "page_name": item["page_name"],
            "page_id": item["page_id"],
            "material_name": item["material_name"],
            "material_id": item["material_id"],
            "queue_id": 1,
            "content_id": item["content_id"],
        }
    )
    build_post_text(
        "https://ai.yingliangads.com/s2l/1.html",
        item["drama_name"],
        item["description"],
        item.get("body_template"),
    )
    return item


def _media_fingerprint(media):
    if not isinstance(media, dict):
        raise CandidatePreflightError(
            "media download fingerprint is invalid",
            code="media_preflight_failed",
        )
    sha256 = str(media.get("sha256", "") or "").lower()
    size = media.get("size")
    if (
        not re.fullmatch(r"[a-f0-9]{64}", sha256)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        raise CandidatePreflightError(
            "media download fingerprint is invalid",
            code="media_preflight_failed",
        )
    return sha256, size


def _duration_policy(account):
    return "premium" if account.get("long_video_eligible") else "standard"


def _duration_limit(account):
    return (
        PREMIUM_MAX_DURATION_SECONDS
        if _duration_policy(account) == "premium"
        else STANDARD_MAX_DURATION_SECONDS
    )


def _repair_job_key(item, source_sha256, profile, duration_policy):
    material_id = _normalize_repair_material_id(item["material_id"])
    repair_item_id = item.get("pool_item_id")
    if repair_item_id in (None, ""):
        repair_item_id = item.get("manual_item_id")
    if (
        isinstance(repair_item_id, bool)
        or not str(repair_item_id or "").isdigit()
        or int(repair_item_id) <= 0
    ):
        raise CandidatePreflightError(
            "media repair item identity is invalid",
            code="media_preflight_failed",
        )
    identity = "\0".join(
        (
            "x-post-media-repair-v3",
            material_id,
            str(int(repair_item_id)),
            str(source_sha256),
            str(profile),
            str(duration_policy),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _remove_preflight_file(destination):
    try:
        Path(destination).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DailyRunError(
            "daily media preflight cleanup failed: %s" % exc,
            code="x_post_storage_unavailable",
        ) from None


def _verify_repaired_download(
    worker_result,
    media,
    probe,
    *,
    max_duration_seconds=STANDARD_MAX_DURATION_SECONDS,
):
    sha256, size = _media_fingerprint(media)
    if (
        sha256 != worker_result["output_sha256"]
        or size != worker_result["output_size"]
    ):
        raise MediaRepairError(
            "x_post_media_repair_fingerprint_mismatch",
            "Repaired media fingerprint does not match the GPU worker response",
            502,
        )
    local_probe = _validate_repair_probe(
        probe,
        size,
        max_duration_seconds=max_duration_seconds,
    )
    worker_probe = worker_result["probe"]
    if (
        local_probe["codec"] != worker_probe["codec"]
        or local_probe["pixel_format"] != worker_probe["pixel_format"]
        or local_probe["audio_codec"] != worker_probe["audio_codec"]
        or local_probe["width"] != worker_probe["width"]
        or local_probe["height"] != worker_probe["height"]
        or local_probe["size"] != worker_probe["size"]
        or abs(local_probe["frame_rate"] - worker_probe["frame_rate"]) > 0.01
        or abs(local_probe["duration"] - worker_probe["duration"]) > 0.05
    ):
        raise MediaRepairError(
            "x_post_media_repair_probe_mismatch",
            "Repaired media probe does not match the GPU worker response",
            502,
        )
    return sha256, size, local_probe


def _preflight_candidate(
    config,
    candidate,
    account,
    rank,
    timestamp,
    destination,
    downloader,
    prober,
    *,
    repair_client=None,
    repair_state=None,
):
    """Validate one FIFO candidate and perform at most one repair attempt."""
    try:
        item = _plan_candidate(
            account,
            candidate,
            rank,
            timestamp,
        )
    except (XPostError, KeyError, TypeError, ValueError) as exc:
        raise CandidatePreflightError(
            redact_text(str(exc), 240),
            code="x_post_daily_copy_validation_failed",
        ) from None

    try:
        media = downloader(
            item["material_url"],
            destination,
            config.media_allowed_hosts,
            max_bytes=config.max_media_bytes,
            timeout=config.media_timeout,
        )
        try:
            probe = prober(
                destination,
                max_bytes=config.max_media_bytes,
                timeout=config.media_timeout,
                max_duration_seconds=_duration_limit(account),
            )
        except XPostError as exc:
            trigger_code = str(getattr(exc, "code", "") or "")
            state = repair_state if isinstance(repair_state, dict) else {}
            repairs_attempted = int(state.get("attempted", 0) or 0)
            if (
                trigger_code not in REPAIRABLE_MEDIA_CODES
                or repair_client is None
                or repairs_attempted >= config.max_repairs_per_run
            ):
                raise
            source_sha256, source_size = _media_fingerprint(media)
            state["attempted"] = repairs_attempted + 1
            original_url = str(item["material_url"])
            duration_policy = _duration_policy(account)
            job_key = _repair_job_key(
                item,
                source_sha256,
                config.repair_profile,
                duration_policy,
            )
            repaired = repair_client.repair(
                {
                    "job_key": job_key,
                    "material_id": str(item["material_id"]),
                    "pool_item_id": int(
                        item.get("pool_item_id")
                        or item.get("manual_item_id")
                    ),
                    "source_url": original_url,
                    "source_sha256": source_sha256,
                    "source_size": source_size,
                    "trigger_code": trigger_code,
                    "profile": config.repair_profile,
                    "duration_policy": duration_policy,
                }
            )
            _remove_preflight_file(destination)
            item["material_url"] = repaired["output_url"]
            repaired_media = downloader(
                item["material_url"],
                destination,
                config.media_allowed_hosts,
                max_bytes=config.max_media_bytes,
                timeout=config.media_timeout,
            )
            repaired_probe = prober(
                destination,
                max_bytes=config.max_media_bytes,
                timeout=config.media_timeout,
                max_duration_seconds=_duration_limit(account),
            )
            final_sha256, final_size, probe = _verify_repaired_download(
                repaired,
                repaired_media,
                repaired_probe,
                max_duration_seconds=_duration_limit(account),
            )
            media = {
                "sha256": final_sha256,
                "size": final_size,
            }
            item.update(
                {
                    "original_material_url": original_url,
                    "media_repair_trigger_code": trigger_code,
                    "media_repair_job_key": job_key,
                    "media_repair_profile": config.repair_profile,
                    "media_repair_source_sha256": source_sha256,
                }
            )
        final_sha256, final_size = _media_fingerprint(media)
        item["preflight_sha256"] = final_sha256
        item["preflight_size"] = final_size
        item["preflight_duration"] = float(
            probe.get("duration", 0) or 0
        )
        item["preflight_width"] = int(probe.get("width", 0) or 0)
        item["preflight_height"] = int(probe.get("height", 0) or 0)
        return item
    finally:
        _remove_preflight_file(destination)


def _preflight_candidates(
    config,
    candidates,
    verified_accounts,
    timestamp,
    downloader,
    prober,
    repair_client=None,
):
    accepted_by_account = {}
    failures = []
    target_count = len(verified_accounts)
    if target_count < 1 or target_count > MAX_DAILY_ACCOUNTS:
        raise DailyRunError(
            "configured account count is outside the supported daily batch range"
        )
    repair_state = {"attempted": 0}
    remaining_accounts = list(verified_accounts)
    material_routing = all(
        str(candidate.get("source_type", "material") or "material")
        .strip()
        .lower()
        != "drama"
        for candidate in candidates
        if isinstance(candidate, dict)
    )
    work_root = Path(config.work_dir)
    if not work_root.exists() or not work_root.is_dir() or work_root.is_symlink():
        raise DailyRunError(
            "daily media work directory is unavailable",
            code="x_post_storage_unavailable",
        )
    with tempfile.TemporaryDirectory(
        prefix="x-post-daily-",
        dir=str(work_root),
    ) as temporary:
        root = Path(temporary)
        for candidate in candidates:
            if len(accepted_by_account) == target_count:
                break
            if material_routing:
                account = next(
                    (
                        item
                        for item in remaining_accounts
                        if not item.get("long_video_eligible")
                    ),
                    remaining_accounts[0],
                )
            else:
                account = remaining_accounts[0]
            material_id = str(
                candidate.get("material_id", "")
                if isinstance(candidate, dict)
                else ""
            )
            destination = root / ("%s.mp4" % material_id)
            try:
                try:
                    item = _preflight_candidate(
                        config,
                        candidate,
                        account,
                        len(accepted_by_account) + 1,
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
                ) as first_error:
                    premium_account = None
                    if (
                        material_routing
                        and str(
                            getattr(first_error, "code", "") or ""
                        )
                        == "x_long_video_requires_premium"
                        and not account.get("long_video_eligible")
                    ):
                        premium_account = next(
                            (
                                candidate_account
                                for candidate_account in remaining_accounts
                                if candidate_account.get(
                                    "long_video_eligible"
                                )
                            ),
                            None,
                        )
                    if premium_account is None:
                        raise
                    account = premium_account
                    item = _preflight_candidate(
                        config,
                        candidate,
                        account,
                        len(accepted_by_account) + 1,
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
            ) as exc:
                failures.append(
                    {
                        "pool_item_id": candidate.get("pool_item_id")
                        if isinstance(candidate, dict)
                        else None,
                        "manual_item_id": candidate.get("manual_item_id")
                        if isinstance(candidate, dict)
                        else None,
                        "material_id": material_id,
                        "error_code": str(getattr(exc, "code", "media_preflight_failed"))[:64],
                        "error_message": redact_text(str(exc), 240),
                    }
                )
                continue
            remaining_accounts = [
                remaining
                for remaining in remaining_accounts
                if int(remaining["id"]) != int(account["id"])
            ]
            accepted_by_account[int(account["id"])] = item
    accepted = []
    for account in verified_accounts:
        item = accepted_by_account.get(int(account["id"]))
        if item is None:
            continue
        item["candidate_rank"] = len(accepted) + 1
        accepted.append(item)
    return accepted, failures


def _failure_audit_fields(exc):
    raw_code = str(getattr(exc, "code", "") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", raw_code):
        raw_code = (
            "x_post_daily_candidate_selection_failed"
            if isinstance(exc, CandidateSelectionError)
            else "x_post_daily_preflight_failed"
        )
    message = redact_text(str(exc), 240)
    if not message:
        message = "X daily preflight failed"
    return raw_code, message


def _record_failure_best_effort(sidecar, config, run_date, source_date, exc):
    recorder = getattr(sidecar, "record_run_failure", None)
    if not callable(recorder):
        return
    code, message = _failure_audit_fields(exc)
    try:
        recorder(
            config.failure_path,
            run_date,
            source_date,
            code,
            message,
            len(config.account_ids),
        )
    except Exception:
        # This audit is best effort because the original failure may itself be
        # a sidecar outage. Never mask or rewrite that original exception.
        return


def _record_pool_checks_best_effort(sidecar, config, checks):
    recorder = getattr(sidecar, "record_pool_checks", None)
    if not callable(recorder) or not checks:
        return
    normalized = []
    seen = set()
    for raw in checks:
        if not isinstance(raw, dict):
            continue
        pool_item_id = raw.get("pool_item_id")
        if (
            not isinstance(pool_item_id, int)
            or isinstance(pool_item_id, bool)
            or pool_item_id <= 0
            or pool_item_id in seen
        ):
            continue
        code = str(raw.get("error_code", "") or "")[:64]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
            code = "x_post_pool_material_check_failed"
        seen.add(pool_item_id)
        normalized.append(
            {
                "pool_item_id": pool_item_id,
                "error_code": code,
                "error_message": redact_text(raw.get("error_message", ""), 240),
            }
        )
    if not normalized:
        return
    for start in range(0, len(normalized), 100):
        try:
            recorder(config.pool_check_path, normalized[start : start + 100])
        except Exception:
            return


def _publish_daily_queues(
    config,
    sidecar,
    queues,
    *,
    run_date,
    source_date,
    preflight_rejected_count,
    resumed_existing_plan=False,
):
    """Run the one-at-a-time publish loop for a newly frozen or recovered plan."""
    results = []
    stopped = False
    for queue in queues:
        account_id = int(queue.get("account_id") or 0)
        entry = {
            "account_id": account_id,
            "queue_id": queue["id"],
            "status": "failed",
        }
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
                    "error_code": exc.code,
                    "unknown_outcome": exc.unknown_outcome,
                }
            )
            if exc.status == 429 or exc.unknown_outcome:
                stopped = True
        results.append(entry)
        if stopped:
            break

    return {
        "status": (
            "published"
            if queues
            and len(results) == len(queues)
            and all(item.get("status") == "published" for item in results)
            else ("stopped" if stopped else "completed_with_failures")
        ),
        "run_date": run_date,
        "source_date": source_date,
        "planned_count": len(queues),
        "published_count": sum(
            item.get("status") == "published" for item in results
        ),
        "preflight_rejected_count": int(preflight_rejected_count),
        "resumed_existing_plan": bool(resumed_existing_plan),
        "results": results,
    }


def execute_daily_run(
    config,
    *,
    sidecar=None,
    connection_factory=None,
    pool_candidate_loader=select_pool_candidates,
    downloader=download_media,
    prober=probe_media,
    repair_client=None,
    now=None,
):
    """Execute one run. Collaborators are injectable for offline unit tests."""
    config.validate()
    current = shanghai_now(now)
    run_date = current.date().isoformat()
    source_date = previous_source_date(current)
    if config.start_date and run_date < normalize_date(config.start_date, "start_date"):
        return {
            "status": "skipped_before_start_date",
            "run_date": run_date,
            "source_date": source_date,
            "start_date": config.start_date,
        }

    sidecar = sidecar or SidecarClient(
        config.internal_url, config.internal_token, timeout=config.internal_timeout
    )
    if repair_client is None and config.repair_url:
        repair_client = MediaRepairClient(
            config.repair_url,
            config.repair_token,
            timeout=config.repair_timeout,
            max_output_bytes=config.max_media_bytes,
        )

    # Read the durable plan before account refresh, pool selection, source
    # queries, downloads, or GPU repair. A frozen same-day plan is the only
    # source of truth for recovery and must never be regenerated.
    existing_plan = sidecar.query_daily_plan(config.plan_query_path, run_date)
    if existing_plan["found"]:
        existing_run = existing_plan["run"]
        existing_queues = existing_plan["queues"]
        if existing_run.get("source_date") != source_date:
            raise DailyRunError(
                "existing daily plan source_date does not match this run",
                code="x_post_daily_resume_conflict",
            )
        existing_expected_count = int(existing_run.get("expected_count") or 0)
        if existing_run.get("status") == "failed_preflight":
            if (
                existing_expected_count != len(config.account_ids)
                or existing_queues
            ):
                raise DailyRunError(
                    "failed-preflight daily run scope is inconsistent",
                    code="x_post_daily_resume_conflict",
                )
        else:
            existing_account_ids = tuple(
                int(queue.get("account_id") or 0)
                for queue in existing_queues
            )
            if (
                len(existing_queues) != existing_expected_count
                or len(set(existing_account_ids)) != existing_expected_count
                or not set(existing_account_ids).issubset(set(config.account_ids))
            ):
                raise DailyRunError(
                    "existing daily plan queue identity is inconsistent",
                    code="x_post_daily_resume_conflict",
                )
            return _publish_daily_queues(
                config,
                sidecar,
                existing_queues,
                run_date=run_date,
                source_date=source_date,
                preflight_rejected_count=0,
                resumed_existing_plan=True,
            )

    try:
        sidecar.preflight_storage(config.storage_preflight_path)
        verified = []
        for account_id in config.account_ids:
            verified.append(_safe_account(sidecar.verify_account(account_id)))
        if tuple(item["id"] for item in verified) != tuple(config.account_ids):
            raise DailyRunError(
                "verified account order does not match configured order",
                code="x_post_daily_account_mismatch",
            )

        pool_items = sidecar.available_pool_items(
            config.pool_available_path,
            config.scan_limit,
        )
        required_count = len(config.account_ids)
        if len(pool_items) < required_count:
            raise DailyRunError(
                "fewer than %s unused materials are available in the manual pool"
                % required_count,
                code="x_post_daily_pool_shortage",
            )
        connection_factory = connection_factory or _connect_from_config
        connection = connection_factory(config)
        try:
            candidates, selector_rejections = pool_candidate_loader(
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
        if len(candidates) < required_count:
            raise DailyRunError(
                "fewer than %s compliant manual-pool candidates were found"
                % required_count,
                code="x_post_daily_candidate_shortage",
            )

        planned_candidates, preflight_failures = _preflight_candidates(
            config,
            candidates,
            verified,
            max(1, int(current.timestamp())),
            downloader,
            prober,
            repair_client,
        )
        _record_pool_checks_best_effort(
            sidecar,
            config,
            preflight_failures,
        )
        if len(planned_candidates) != required_count:
            raise DailyRunError(
                "only %s manual-pool candidates passed all local preflight gates "
                "(required %s)" % (len(planned_candidates), required_count),
                code="x_post_daily_candidate_preflight_shortage",
            )
        # Re-check immediately before the sidecar's transactional plan call.
        # The sidecar performs the same point-of-use guard again server-side.
        sidecar.preflight_storage(config.storage_preflight_path)
    except Exception as exc:
        _record_failure_best_effort(
            sidecar, config, run_date, source_date, exc
        )
        raise

    # From this point onward the plan call may have committed the whole batch.
    # Never write a preflight failure over a possibly-created formal run.
    try:
        queues = sidecar.create_plan(
            config.plan_path,
            {
                "run_date": run_date,
                "source_date": source_date,
                "candidates": planned_candidates,
            },
        )
    except SidecarError as exc:
        # A structured rollback is safe to audit as a failed run; a lost or
        # malformed response may have committed the plan, so it must be
        # reconciled manually and is never overwritten.
        if not exc.unknown_outcome:
            _record_failure_best_effort(
                sidecar, config, run_date, source_date, exc
            )
        raise
    if tuple(int(queue.get("account_id") or 0) for queue in queues) != tuple(
        config.account_ids
    ):
        raise DailyRunError("daily plan queue order does not match configured accounts")

    return _publish_daily_queues(
        config,
        sidecar,
        queues,
        run_date=run_date,
        source_date=source_date,
        preflight_rejected_count=len(preflight_failures),
    )


@contextlib.contextmanager
def process_lock(path):
    if not path:
        raise DailyRunError("X_POST_DAILY_LOCK_PATH is empty")
    try:
        import fcntl
    except ImportError:
        raise DailyRunError("daily runner locking requires fcntl") from None
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield handle
    finally:
        handle.close()


def main():
    try:
        config = DailyConfig.from_env()
        with process_lock(config.lock_path) as acquired:
            if acquired is None:
                result = {"status": "skipped_locked"}
            else:
                result = execute_daily_run(config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") in {
            "published",
            "skipped_before_start_date",
            "skipped_locked",
        } else 1
    except (DailyRunError, CandidateSelectionError, XPostError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(getattr(exc, "code", type(exc).__name__))[:64],
                    "message": str(exc)[:240],
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
                    "message": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
