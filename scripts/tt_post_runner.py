#!/usr/bin/env python3
"""Claim and execute due TikTok Post tasks through the CPU sidecar.

The runner is intentionally a one-minute oneshot.  It owns no TikTok token,
processes accounts serially, uses a fixed ten-minute grace window, and never
turns an unknown outcome back into a publish request.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import secrets
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_posts.core import redact_text  # noqa: E402
from features.tt_posts.service import (  # noqa: E402
    DEFAULT_CPU_PORT,
    DEFAULT_GRACE_SECONDS,
    MAX_HTTP_RESPONSE_BYTES,
)


DEFAULT_INTERNAL_URL = "http://127.0.0.1:%s" % DEFAULT_CPU_PORT
DEFAULT_LOCK_PATH = "/run/tt-post/runner.lock"
DEFAULT_RECONCILE_LIMIT = 5
MAX_RECONCILE_LIMIT = 10
SAFE_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RunnerError(RuntimeError):
    """Secret-safe runner error."""

    def __init__(self, code: str, message: str, status: int = 500):
        self.code = str(code or "tt_post_runner_error")[:96]
        self.status = int(status)
        super().__init__(redact_text(message, 500))

    def __repr__(self) -> str:
        return "RunnerError(code=%r, status=%r)" % (self.code, self.status)


def _env_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(str(source.get(name, default)))
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value < minimum or value > maximum:
        raise RunnerError(
            "tt_post_runner_config_invalid",
            "TT Post runner数值配置无效",
            500,
        )
    return value


@dataclass(frozen=True)
class RunnerConfig:
    internal_url: str
    internal_token: str
    worker_id: str
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    claim_limit: int = 20
    reconcile_limit: int = DEFAULT_RECONCILE_LIMIT
    timeout: int = 900
    lock_path: str = DEFAULT_LOCK_PATH

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "RunnerConfig":
        source = os.environ if environ is None else environ
        return cls(
            internal_url=str(
                source.get("TT_POST_INTERNAL_URL", DEFAULT_INTERNAL_URL)
            ).rstrip("/"),
            internal_token=str(source.get("TT_POST_INTERNAL_TOKEN", "")),
            worker_id=str(
                source.get("TT_POST_RUNNER_ID", "tt-post-runner-primary")
            ).strip(),
            grace_seconds=_env_int(
                source,
                "TT_POST_GRACE_SECONDS",
                DEFAULT_GRACE_SECONDS,
                DEFAULT_GRACE_SECONDS,
                DEFAULT_GRACE_SECONDS,
            ),
            claim_limit=_env_int(
                source,
                "TT_POST_CLAIM_LIMIT",
                20,
                1,
                100,
            ),
            reconcile_limit=_env_int(
                source,
                "TT_POST_RECONCILE_LIMIT",
                DEFAULT_RECONCILE_LIMIT,
                1,
                MAX_RECONCILE_LIMIT,
            ),
            timeout=_env_int(
                source,
                "TT_POST_INTERNAL_TIMEOUT",
                900,
                5,
                3600,
            ),
            lock_path=str(
                source.get("TT_POST_RUNNER_LOCK_PATH", DEFAULT_LOCK_PATH)
            ).strip(),
        )

    def validate(self) -> None:
        parsed = urllib.parse.urlsplit(self.internal_url)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in SAFE_HOSTS
            or port != DEFAULT_CPU_PORT
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post runner只能访问loopback CPU sidecar",
                500,
            )
        if (
            len(self.internal_token) < 32
            or len(self.internal_token) > 512
            or any(ord(char) < 33 for char in self.internal_token)
        ):
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post runner内部凭据未配置",
                500,
            )
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,128}", self.worker_id):
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post runner身份无效",
                500,
            )
        if self.grace_seconds != DEFAULT_GRACE_SECONDS:
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post宽限窗口必须固定为600秒",
                500,
            )
        if self.reconcile_limit < 1 or self.reconcile_limit > MAX_RECONCILE_LIMIT:
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post核对预算必须保持在安全上限内",
                500,
            )
        lock = Path(self.lock_path)
        if not lock.is_absolute():
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post runner锁路径必须是绝对路径",
                500,
            )
        if os.name != "nt" and not str(lock).startswith("/run/tt-post/"):
            raise RunnerError(
                "tt_post_runner_config_invalid",
                "TT Post runner锁必须位于/run/tt-post",
                500,
            )

    def __repr__(self) -> str:
        return (
            "RunnerConfig(internal_url=%r, internal_token=<redacted>, "
            "worker_id=%r, grace_seconds=%r)"
            % (
                self.internal_url,
                self.worker_id,
                self.grace_seconds,
            )
        )


class TTPostSidecarClient:
    """Strict loopback client used by the runner."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int,
        connection_factory=None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self._token = str(token)
        self.timeout = int(timeout)
        self._connection_factory = connection_factory

    def __repr__(self) -> str:
        return (
            "TTPostSidecarClient(base_url=%r, token=<redacted>, timeout=%r)"
            % (self.base_url, self.timeout)
        )

    def _connection(self):
        if self._connection_factory is not None:
            return self._connection_factory(
                "127.0.0.1",
                DEFAULT_CPU_PORT,
                self.timeout,
            )
        return http.client.HTTPConnection(
            "127.0.0.1",
            DEFAULT_CPU_PORT,
            timeout=self.timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if (
            method not in {"GET", "POST"}
            or not path.startswith("/")
            or "://" in path
            or "#" in path
        ):
            raise RunnerError(
                "tt_post_sidecar_request_invalid",
                "TT Post sidecar请求无效",
                500,
            )
        body = None
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
            "Connection": "close",
        }
        if payload is not None:
            body = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=UTF-8"
        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                raise RunnerError(
                    "tt_post_sidecar_invalid_response",
                    "TT Post sidecar响应超过安全上限",
                    502,
                )
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                raise RunnerError(
                    "tt_post_sidecar_invalid_response",
                    "TT Post sidecar响应格式无效",
                    502,
                ) from None
            if not isinstance(decoded, dict):
                raise RunnerError(
                    "tt_post_sidecar_invalid_response",
                    "TT Post sidecar响应不是对象",
                    502,
                )
            status = int(getattr(response, "status", 0) or 0)
            if not 200 <= status < 300:
                raise RunnerError(
                    str(decoded.get("code") or "tt_post_sidecar_error")[:96],
                    str(decoded.get("message") or "TT Post sidecar请求失败")[:500],
                    status,
                )
            return decoded
        except RunnerError:
            raise
        except (OSError, socket.error, http.client.HTTPException):
            raise RunnerError(
                "tt_post_sidecar_unreachable",
                "TT Post sidecar暂不可用，当前tick停止",
                502,
            ) from None
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def reconciling(self, limit: int) -> List[Dict[str, Any]]:
        result = self._request(
            "GET",
            "/internal/tt-posts/reconciling?limit=%s" % int(limit),
        )
        items = result.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise RunnerError(
                "tt_post_sidecar_invalid_response",
                "TT Post核对任务响应无效",
                502,
            )
        return [dict(item) for item in items]

    def reconcile(self, queue_id: int) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/internal/tt-posts/queue/%s/reconcile" % int(queue_id),
            {},
        )

    def claim(
        self,
        *,
        worker_id: str,
        grace_seconds: int,
        limit: int,
    ) -> List[Dict[str, Any]]:
        result = self._request(
            "POST",
            "/internal/tt-posts/claim",
            {
                "worker_id": worker_id,
                "grace_seconds": int(grace_seconds),
                "limit": int(limit),
            },
        )
        items = result.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("queue"), dict)
            for item in items
        ):
            raise RunnerError(
                "tt_post_sidecar_invalid_response",
                "TT Post领取响应无效",
                502,
            )
        return [dict(item) for item in items]

    def schedules_due(self) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/internal/tt-posts/schedules/due",
            {},
        )

    def publish(self, queue_id: int, claim_token: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/internal/tt-posts/queue/%s/publish" % int(queue_id),
            {"claim_token": str(claim_token)},
        )


def _safe_result(item: Mapping[str, Any]) -> Dict[str, Any]:
    queue = item.get("item") if isinstance(item.get("item"), Mapping) else item
    return {
        "queue_id": queue.get("id") or queue.get("queue_id"),
        "source_account_id": queue.get("source_account_id")
        or queue.get("account_id"),
        "status": queue.get("queue_status") or queue.get("status"),
        "publish_id_present": bool(queue.get("publish_id")),
        "unknown_outcome": bool(queue.get("unknown_outcome")),
        "error_code": str(queue.get("error_code") or "")[:96],
    }


def _safe_reconcile_error(
    queue: Mapping[str, Any],
    error: RunnerError,
) -> Dict[str, Any]:
    result = _safe_result(queue)
    result.update(
        {
            "operation": "reconcile_error",
            "error_code": error.code,
            "http_status": error.status,
        }
    )
    return result


def _safe_publish_request_error(
    queue: Mapping[str, Any],
    error: RunnerError,
) -> Dict[str, Any]:
    result = _safe_result(queue)
    result.update(
        {
            "operation": "publish_request_error",
            "error_code": error.code,
            "http_status": error.status,
        }
    )
    return result


def execute_runner_tick(
    config: RunnerConfig,
    *,
    client: Optional[TTPostSidecarClient] = None,
) -> Dict[str, Any]:
    """Run one bounded, globally serial runner tick."""

    config.validate()
    sidecar = client or TTPostSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.timeout,
    )
    results: List[Dict[str, Any]] = []

    def claim_and_publish(limit: int) -> int:
        claims = sidecar.claim(
            worker_id=config.worker_id,
            grace_seconds=config.grace_seconds,
            limit=limit,
        )
        for claim in claims:
            queue = claim["queue"]
            try:
                queue_id = int(
                    queue.get("id") or queue.get("queue_id") or 0
                )
            except (TypeError, ValueError, OverflowError):
                queue_id = 0
            status = str(
                queue.get("queue_status") or queue.get("status") or ""
            )
            if queue_id <= 0:
                results.append(
                    _safe_publish_request_error(
                        queue,
                        RunnerError(
                            "tt_post_sidecar_invalid_response",
                            "TT Post领取任务身份无效",
                            502,
                        ),
                    )
                )
                continue
            if (
                status in {"unknown", "needs_review"}
                or bool(queue.get("unknown_outcome"))
            ):
                results.append(
                    {
                        "operation": "skip_unknown",
                        **_safe_result(queue),
                    }
                )
                continue
            claim_token = str(claim.get("claim_token") or "")
            if not claim_token:
                # Compliance-blocked hold rows deliberately have no executable
                # claim token and therefore can never reach publish init.
                results.append(
                    {
                        "operation": "blocked_or_missed",
                        **_safe_result(queue),
                    }
                )
                continue
            try:
                result = sidecar.publish(queue_id, claim_token)
            except RunnerError as exc:
                results.append(_safe_publish_request_error(queue, exc))
                continue
            results.append({"operation": "publish", **_safe_result(result)})
        return len(claims)

    # Existing absolute/manual queues get the first claim budget so a slow
    # Creator Info check for a new daily slot cannot make them miss grace.
    claimed_before_schedule = claim_and_publish(config.claim_limit)

    # Persist and freeze current recurring daily slots next. The sidecar owns
    # all timezone/FIFO/idempotency and crash-recovery decisions.
    due_result = sidecar.schedules_due()
    due_items = due_result.get("items", [])
    if not isinstance(due_items, list):
        raise RunnerError(
            "tt_post_sidecar_invalid_response",
            "TT Post recurring schedule response is invalid",
            502,
        )
    for item in due_items:
        if not isinstance(item, Mapping):
            raise RunnerError(
                "tt_post_sidecar_invalid_response",
                "TT Post recurring schedule item is invalid",
                502,
            )
        results.append(
            {
                "operation": "schedule_due",
                "run_id": item.get("run_id") or item.get("id"),
                "queue_id": item.get("queue_id"),
                "source_account_id": str(
                    item.get("source_account_id")
                    or item.get("account_id")
                    or ""
                ),
                "material_id": str(item.get("material_id") or ""),
                "status": str(item.get("status") or ""),
                "error_code": str(item.get("error_code") or "")[:96],
                "error_message": redact_text(
                    item.get("error_message") or "",
                    500,
                ),
            }
        )

    # If due created a queue and the first phase did not exhaust the bounded
    # budget, claim only the remaining capacity so the new slot can run in the
    # same tick without doubling the per-tick maximum.
    created_due_queue = any(
        isinstance(item, Mapping) and bool(item.get("queue_id"))
        for item in due_items
    )
    remaining_claim_limit = config.claim_limit - claimed_before_schedule
    if created_due_queue and remaining_claim_limit > 0:
        claim_and_publish(remaining_claim_limit)

    # A stored publish ID is reconciled after all due claims. Each row is isolated
    # so one remote business error cannot block the rest of the tick.
    for queue in sidecar.reconciling(config.reconcile_limit):
        try:
            queue_id = int(queue.get("id") or queue.get("queue_id") or 0)
        except (TypeError, ValueError, OverflowError):
            queue_id = 0
        if queue_id <= 0 or not queue.get("publish_id"):
            results.append(
                _safe_reconcile_error(
                    queue,
                    RunnerError(
                        "tt_post_sidecar_invalid_response",
                        "TT Post核对任务身份无效",
                        502,
                    ),
                )
            )
            continue
        try:
            result = sidecar.reconcile(queue_id)
        except RunnerError as exc:
            results.append(_safe_reconcile_error(queue, exc))
            continue
        results.append({"operation": "reconcile", **_safe_result(result)})

    return {
        "status": "ok",
        "schedule_due_count": len(due_items),
        "grace_seconds": config.grace_seconds,
        "reconcile_budget": config.reconcile_limit,
        "reconcile_count": sum(
            item["operation"] == "reconcile" for item in results
        ),
        "reconcile_error_count": sum(
            item["operation"] == "reconcile_error" for item in results
        ),
        "publish_request_count": sum(
            item["operation"] == "publish" for item in results
        ),
        "publish_request_error_count": sum(
            item["operation"] == "publish_request_error" for item in results
        ),
        "results": results,
    }


@contextlib.contextmanager
def process_lock(path: str) -> Iterator[bool]:
    lock = Path(path)
    if os.name == "nt":
        yield True
        return
    import fcntl

    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        os.close(descriptor)


def main() -> int:
    try:
        config = RunnerConfig.from_env()
        with process_lock(config.lock_path) as acquired:
            result = (
                execute_runner_tick(config)
                if acquired
                else {"status": "skipped_locked", "results": []}
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except RunnerError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc),
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
                    "error_code": "tt_post_runner_unexpected",
                    "message": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
