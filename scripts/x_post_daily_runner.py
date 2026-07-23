#!/usr/bin/env python3
"""Publish one compliant Dramawave video to each configured X account daily."""

from __future__ import annotations

import contextlib
import http.client
import json
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
    ranked_material_ids,
    select_candidates,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    XPostError,
    build_post_text,
    build_w2a_url,
    download_media,
    probe_media,
    redact_text,
)


DEFAULT_INTERNAL_URL = "http://127.0.0.1:8810"
MAX_ERROR_BODY_BYTES = 64 * 1024
_SAFE_INTERNAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
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
        raise DailyRunError("X_POST_DAILY_ACCOUNT_IDS must contain three integer IDs") from None
    if len(items) != 3 or len(set(items)) != 3 or any(item <= 0 for item in items):
        raise DailyRunError("X_POST_DAILY_ACCOUNT_IDS must contain three unique positive IDs")
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
        work_dir = Path(self.work_dir)
        if not work_dir.is_absolute():
            raise DailyRunError("X_POST_DAILY_WORK_DIR must be absolute")
        if os.name != "nt" and work_dir != Path(
            "/mnt/data-disk/x-post-automation/daily-work"
        ):
            raise DailyRunError(
                "X_POST_DAILY_WORK_DIR must use the fixed data-disk work directory"
            )
        for path in (
            self.material_keys_path,
            self.storage_preflight_path,
            self.failure_path,
            self.plan_path,
        ):
            if not path.startswith("/internal/") or "?" in path or "#" in path:
                raise DailyRunError("invalid sidecar endpoint path")
        if (
            "{queue_id}" not in self.publish_path_template
            or not self.publish_path_template.startswith("/internal/")
        ):
            raise DailyRunError("invalid publish endpoint path template")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _file_pointer, _code, _message, _headers, _url):
        return None


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
                raw = response.read(MAX_ERROR_BODY_BYTES + 1)
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
        if len(raw) > MAX_ERROR_BODY_BYTES:
            raise SidecarError(
                "x_sidecar_invalid_response",
                "X sidecar response is too large",
                502,
                unknown_outcome=write_may_have_happened,
            )
        data = self._decode(raw)
        if status < 200 or status >= 300 or not isinstance(data, dict):
            raise SidecarError(
                "x_sidecar_invalid_response",
                "X sidecar returned an invalid response",
                502,
                unknown_outcome=write_may_have_happened,
            )
        return data

    @staticmethod
    def _decode(raw):
        if len(raw or b"") > MAX_ERROR_BODY_BYTES:
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

    def create_plan(self, path, payload):
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
            or item.get("status") not in {
                "queued",
                "running",
                "stopped",
                "completed",
                "completed_with_errors",
                "needs_review",
            }
            or not isinstance(queues, list)
            or len(queues) != 3
        ):
            raise SidecarError(
                "x_daily_plan_invalid_response",
                "Daily plan response is invalid",
                unknown_outcome=True,
            )
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 3:
            raise SidecarError(
                "x_daily_plan_invalid_response",
                "Daily plan request identity is invalid",
                unknown_outcome=True,
            )
        requested_account_ids = []
        requested_material_ids = []
        for candidate in candidates:
            account_id = candidate.get("account_id") if isinstance(candidate, dict) else None
            material_id = candidate.get("material_id") if isinstance(candidate, dict) else None
            if (
                not isinstance(account_id, int)
                or isinstance(account_id, bool)
                or account_id <= 0
                or not re.fullmatch(r"[1-9][0-9]*", str(material_id or ""))
            ):
                raise SidecarError(
                    "x_daily_plan_invalid_response",
                    "Daily plan request account identity is invalid",
                    unknown_outcome=True,
                )
            requested_account_ids.append(account_id)
            requested_material_ids.append(str(material_id))
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

    def record_run_failure(self, path, run_date, source_date, error_code, error_message):
        result = self.post(
            path,
            {
                "run_date": run_date,
                "source_date": source_date,
                "error_code": error_code,
                "error_message": error_message,
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
    return {
        "id": int(item["id"]),
        "username": str(item["username"]),
        "x_user_id": str(item["x_user_id"]),
        "display_name": str(item.get("display_name", "") or item["username"]),
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
        item["description"],
    )
    return item


def _preflight_candidates(
    config, candidates, verified_accounts, timestamp, downloader, prober
):
    accepted = []
    failures = []
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
            if len(accepted) == 3:
                break
            account = verified_accounts[len(accepted)]
            try:
                item = _plan_candidate(
                    account,
                    candidate,
                    len(accepted) + 1,
                    timestamp,
                )
            except (XPostError, KeyError, TypeError, ValueError) as exc:
                failures.append(
                    {
                        "material_id": str(
                            candidate.get("material_id", "")
                            if isinstance(candidate, dict)
                            else ""
                        ),
                        "error_code": "x_post_daily_copy_validation_failed",
                    }
                )
                continue
            destination = root / ("%s.mp4" % item["material_id"])
            try:
                media = downloader(
                    item["material_url"],
                    destination,
                    config.media_allowed_hosts,
                    max_bytes=config.max_media_bytes,
                    timeout=config.media_timeout,
                )
                probe = prober(
                    destination,
                    max_bytes=config.max_media_bytes,
                    timeout=config.media_timeout,
                )
            except (
                XPostError,
                http.client.HTTPException,
                OSError,
                ValueError,
            ) as exc:
                failures.append(
                    {
                        "material_id": candidate["material_id"],
                        "error_code": str(getattr(exc, "code", "media_preflight_failed"))[:64],
                    }
                )
                continue
            else:
                item["preflight_sha256"] = str(media.get("sha256", "") or "")
                item["preflight_size"] = int(media.get("size", 0) or 0)
                item["preflight_duration"] = float(probe.get("duration", 0) or 0)
                item["preflight_width"] = int(probe.get("width", 0) or 0)
                item["preflight_height"] = int(probe.get("height", 0) or 0)
                accepted.append(item)
            finally:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise DailyRunError(
                        "daily media preflight cleanup failed: %s" % exc,
                        code="x_post_storage_unavailable",
                    ) from None
    if len(accepted) != 3:
        raise DailyRunError(
            "only %s candidates passed all local preflight gates (required 3)"
            % len(accepted),
            code="x_post_daily_candidate_preflight_shortage",
        )
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
        )
    except Exception:
        # This audit is best effort because the original failure may itself be
        # a sidecar outage. Never mask or rewrite that original exception.
        return


def execute_daily_run(
    config,
    *,
    sidecar=None,
    connection_factory=None,
    candidate_loader=select_candidates,
    ranked_loader=ranked_material_ids,
    downloader=download_media,
    prober=probe_media,
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

        connection_factory = connection_factory or _connect_from_config
        connection = connection_factory(config)
        try:
            ranked_ids = ranked_loader(
                connection,
                source_date,
                scan_limit=config.scan_limit,
                schema=config.mysql_database,
            )
            excluded = sidecar.used_material_keys(config.material_keys_path, ranked_ids)
            candidates = candidate_loader(
                connection,
                source_date,
                excluded_material_keys=excluded,
                limit=config.candidate_pool_limit,
                scan_limit=config.scan_limit,
                schema=config.mysql_database,
            )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        if len(candidates) < 3:
            raise DailyRunError(
                "fewer than three compliant unused candidates were found",
                code="x_post_daily_candidate_shortage",
            )

        planned_candidates, preflight_failures = _preflight_candidates(
            config,
            candidates,
            verified,
            max(1, int(current.timestamp())),
            downloader,
            prober,
        )
        # Re-check immediately before the sidecar's transactional plan call.
        # The sidecar performs the same point-of-use guard again server-side.
        sidecar.preflight_storage(config.storage_preflight_path)
    except Exception as exc:
        _record_failure_best_effort(
            sidecar, config, run_date, source_date, exc
        )
        raise

    # From this point onward the plan call may have committed all three queues.
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

    results = []
    stopped = False
    for account, queue in zip(verified, queues):
        entry = {
            "account_id": account["id"],
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
            if len(results) == 3 and all(item.get("status") == "published" for item in results)
            else ("stopped" if stopped else "completed_with_failures")
        ),
        "run_date": run_date,
        "source_date": source_date,
        "planned_count": len(queues),
        "published_count": sum(item.get("status") == "published" for item in results),
        "preflight_rejected_count": len(preflight_failures),
        "results": results,
    }


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
