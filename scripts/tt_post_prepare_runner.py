#!/usr/bin/env python3
"""Prepare at most one TikTok recurring-pool material through the CPU sidecar.

This runner is deliberately separate from ``tt_post_runner.py``. Long GPU
composition must never delay a due publish or reconciliation tick. The CPU
sidecar owns the durable preparation state machine; this process only claims
one row, keeps its lease alive, and asks the sidecar to process that claim.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_posts.core import redact_text  # noqa: E402
from features.tt_posts.service import (  # noqa: E402
    DEFAULT_CPU_PORT,
    MAX_HTTP_RESPONSE_BYTES,
)


DEFAULT_INTERNAL_URL = "http://127.0.0.1:%s" % DEFAULT_CPU_PORT
DEFAULT_LOCK_PATH = "/run/tt-post/prepare-runner.lock"
DEFAULT_WORKER_ID = "tt-post-prepare-primary"
DEFAULT_LEASE_SECONDS = 180
DEFAULT_RENEW_INTERVAL_SECONDS = 30
DEFAULT_INTERNAL_TIMEOUT = 60
DEFAULT_GPU_PREPARE_TIMEOUT = 9000
DEFAULT_PROCESS_TIMEOUT = 9300
SAFE_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")


class PrepareRunnerError(RuntimeError):
    """Bounded, secret-safe runner failure."""

    def __init__(self, code: str, message: str, status: int = 500):
        self.code = str(code or "tt_post_prepare_runner_error")[:96]
        self.status = int(status)
        super().__init__(redact_text(message, 500))

    def __repr__(self) -> str:
        return "PrepareRunnerError(code=%r, status=%r)" % (
            self.code,
            self.status,
        )


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
        raise PrepareRunnerError(
            "tt_post_prepare_runner_config_invalid",
            "%s is outside its safe range" % name,
            500,
        )
    return value


@dataclass(frozen=True)
class PrepareRunnerConfig:
    internal_url: str
    internal_token: str
    worker_id: str = DEFAULT_WORKER_ID
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    renew_interval_seconds: int = DEFAULT_RENEW_INTERVAL_SECONDS
    internal_timeout: int = DEFAULT_INTERNAL_TIMEOUT
    gpu_prepare_timeout: int = DEFAULT_GPU_PREPARE_TIMEOUT
    process_timeout: int = DEFAULT_PROCESS_TIMEOUT
    lock_path: str = DEFAULT_LOCK_PATH

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "PrepareRunnerConfig":
        source = os.environ if environ is None else environ
        return cls(
            internal_url=str(
                source.get("TT_POST_INTERNAL_URL", DEFAULT_INTERNAL_URL)
            ).rstrip("/"),
            internal_token=str(source.get("TT_POST_INTERNAL_TOKEN", "")),
            worker_id=str(
                source.get("TT_POST_PREPARE_RUNNER_ID", DEFAULT_WORKER_ID)
            ).strip(),
            lease_seconds=_env_int(
                source,
                "TT_POST_PREPARE_LEASE_SECONDS",
                DEFAULT_LEASE_SECONDS,
                60,
                600,
            ),
            renew_interval_seconds=_env_int(
                source,
                "TT_POST_PREPARE_RENEW_INTERVAL_SECONDS",
                DEFAULT_RENEW_INTERVAL_SECONDS,
                5,
                600,
            ),
            internal_timeout=_env_int(
                source,
                "TT_POST_PREPARE_INTERNAL_TIMEOUT",
                DEFAULT_INTERNAL_TIMEOUT,
                5,
                600,
            ),
            gpu_prepare_timeout=_env_int(
                source,
                "TT_POST_GPU_PREPARE_TIMEOUT",
                DEFAULT_GPU_PREPARE_TIMEOUT,
                60,
                10800,
            ),
            process_timeout=_env_int(
                source,
                "TT_POST_PREPARE_PROCESS_TIMEOUT",
                DEFAULT_PROCESS_TIMEOUT,
                120,
                14400,
            ),
            lock_path=str(
                source.get(
                    "TT_POST_PREPARE_RUNNER_LOCK_PATH",
                    DEFAULT_LOCK_PATH,
                )
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
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare runner may only access the loopback CPU sidecar",
                500,
            )
        if (
            len(self.internal_token) < 32
            or len(self.internal_token) > 512
            or any(ord(char) < 33 for char in self.internal_token)
        ):
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare runner internal credential is not configured",
                500,
            )
        if not WORKER_ID_RE.fullmatch(self.worker_id):
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare runner worker identity is invalid",
                500,
            )
        if self.renew_interval_seconds * 3 > self.lease_seconds:
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare lease must cover at least three renew intervals",
                500,
            )
        if self.process_timeout < self.gpu_prepare_timeout + 60:
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare process timeout must exceed the GPU timeout",
                500,
            )
        if self.process_timeout < self.internal_timeout:
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare process timeout must cover the internal timeout",
                500,
            )
        lock = Path(self.lock_path)
        production_posix_lock = str(self.lock_path).replace(
            "\\",
            "/",
        ).startswith("/run/tt-post/")
        if not lock.is_absolute() and not (
            os.name == "nt" and production_posix_lock
        ):
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare runner lock path must be absolute",
                500,
            )
        if os.name != "nt" and not production_posix_lock:
            raise PrepareRunnerError(
                "tt_post_prepare_runner_config_invalid",
                "prepare runner lock must remain under /run/tt-post",
                500,
            )

    def __repr__(self) -> str:
        return (
            "PrepareRunnerConfig(internal_url=%r, internal_token=<redacted>, "
            "worker_id=%r, lease_seconds=%r, process_timeout=%r)"
            % (
                self.internal_url,
                self.worker_id,
                self.lease_seconds,
                self.process_timeout,
            )
        )


def _positive_id(value: Any, field: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        normalized = 0
    if normalized <= 0:
        raise PrepareRunnerError(
            "tt_post_prepare_sidecar_invalid_response",
            "%s is invalid" % field,
            502,
        )
    return normalized


def _claim_token(value: Any) -> str:
    token = str(value or "")
    if (
        len(token) < 32
        or len(token) > 512
        or any(ord(char) < 33 for char in token)
    ):
        raise PrepareRunnerError(
            "tt_post_prepare_sidecar_invalid_response",
            "preparation claim token is invalid",
            502,
        )
    return token


class PrepareSidecarClient:
    """Strict loopback client for the preparation state-machine endpoints."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int,
        process_timeout: int,
        connection_factory=None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self._token = str(token)
        self.timeout = int(timeout)
        self.process_timeout = int(process_timeout)
        self._connection_factory = connection_factory

    def __repr__(self) -> str:
        return (
            "PrepareSidecarClient(base_url=%r, token=<redacted>, "
            "timeout=%r, process_timeout=%r)"
            % (self.base_url, self.timeout, self.process_timeout)
        )

    def _connection(self, timeout: int):
        if self._connection_factory is not None:
            return self._connection_factory(
                "127.0.0.1",
                DEFAULT_CPU_PORT,
                timeout,
            )
        return http.client.HTTPConnection(
            "127.0.0.1",
            DEFAULT_CPU_PORT,
            timeout=timeout,
        )

    def _request(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        if (
            not path.startswith("/internal/tt-posts/preparations/")
            or "://" in path
            or "?" in path
            or "#" in path
        ):
            raise PrepareRunnerError(
                "tt_post_prepare_sidecar_request_invalid",
                "preparation sidecar path is invalid",
                500,
            )
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self._connection(
            self.timeout if timeout is None else int(timeout)
        )
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Accept": "application/json",
                    "Connection": "close",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                raise PrepareRunnerError(
                    "tt_post_prepare_sidecar_invalid_response",
                    "preparation sidecar response exceeds the safe limit",
                    502,
                )
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                raise PrepareRunnerError(
                    "tt_post_prepare_sidecar_invalid_response",
                    "preparation sidecar response is not valid JSON",
                    502,
                ) from None
            if not isinstance(decoded, dict):
                raise PrepareRunnerError(
                    "tt_post_prepare_sidecar_invalid_response",
                    "preparation sidecar response is not an object",
                    502,
                )
            status = int(getattr(response, "status", 0) or 0)
            if not 200 <= status < 300:
                raise PrepareRunnerError(
                    str(
                        decoded.get("code")
                        or "tt_post_prepare_sidecar_error"
                    )[:96],
                    str(
                        decoded.get("message")
                        or "preparation sidecar request failed"
                    )[:500],
                    status,
                )
            return decoded
        except PrepareRunnerError:
            raise
        except (OSError, socket.error, http.client.HTTPException):
            raise PrepareRunnerError(
                "tt_post_prepare_sidecar_unreachable",
                "preparation sidecar is temporarily unavailable",
                502,
            ) from None
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        result = self._request(
            "/internal/tt-posts/preparations/claim",
            {
                "worker_id": str(worker_id),
                "lease_seconds": int(lease_seconds),
            },
        )
        item = result.get("item")
        if item is None:
            if result.get("claim_token") not in (None, ""):
                raise PrepareRunnerError(
                    "tt_post_prepare_sidecar_invalid_response",
                    "idle preparation claim unexpectedly contains a token",
                    502,
                )
            return None
        if not isinstance(item, Mapping):
            raise PrepareRunnerError(
                "tt_post_prepare_sidecar_invalid_response",
                "preparation claim item is invalid",
                502,
            )
        normalized = dict(item)
        normalized["id"] = _positive_id(
            normalized.get("id") or normalized.get("preparation_id"),
            "preparation id",
        )
        return {
            "item": normalized,
            "claim_token": _claim_token(result.get("claim_token")),
        }

    def renew(
        self,
        preparation_id: int,
        claim_token: str,
        *,
        lease_seconds: int,
    ) -> Dict[str, Any]:
        result = self._request(
            "/internal/tt-posts/preparations/%s/renew"
            % _positive_id(preparation_id, "preparation id"),
            {
                "claim_token": _claim_token(claim_token),
                "lease_seconds": int(lease_seconds),
            },
        )
        if not isinstance(result.get("item"), Mapping):
            raise PrepareRunnerError(
                "tt_post_prepare_sidecar_invalid_response",
                "preparation renew response is invalid",
                502,
            )
        return result

    def process(
        self,
        preparation_id: int,
        claim_token: str,
    ) -> Dict[str, Any]:
        normalized_id = _positive_id(preparation_id, "preparation id")
        result = self._request(
            "/internal/tt-posts/preparations/%s/process" % normalized_id,
            {"claim_token": _claim_token(claim_token)},
            timeout=self.process_timeout,
        )
        item = result.get("item")
        if not isinstance(item, Mapping):
            raise PrepareRunnerError(
                "tt_post_prepare_sidecar_invalid_response",
                "preparation process response is invalid",
                502,
            )
        returned_id = _positive_id(
            item.get("id") or item.get("preparation_id"),
            "preparation id",
        )
        if returned_id != normalized_id:
            raise PrepareRunnerError(
                "tt_post_prepare_sidecar_invalid_response",
                "preparation process response identity does not match",
                502,
            )
        return result


class LeaseHeartbeat:
    """Renew one preparation lease while its synchronous process call runs."""

    def __init__(
        self,
        renew_fn: Callable[[], Any],
        interval_seconds: float,
    ):
        self._renew_fn = renew_fn
        self._interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._renew_count = 0
        self._last_error: Optional[PrepareRunnerError] = None

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name="tt-post-prepare-lease",
            daemon=True,
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._renew_fn()
            except PrepareRunnerError as exc:
                with self._state_lock:
                    self._last_error = exc
            except Exception as exc:
                with self._state_lock:
                    self._last_error = PrepareRunnerError(
                        "tt_post_prepare_renew_unexpected",
                        type(exc).__name__,
                        500,
                    )
            else:
                with self._state_lock:
                    self._renew_count += 1
                    self._last_error = None

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self._interval_seconds + 1.0))

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            error = self._last_error
            return {
                "renew_count": int(self._renew_count),
                "renew_error_code": error.code if error is not None else "",
            }


def _safe_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "preparation_id": item.get("id") or item.get("preparation_id"),
        "material_id": item.get("material_id"),
        "source_account_id": (
            item.get("source_account_id") or item.get("account_id")
        ),
        "preparation_status": (
            item.get("preparation_status") or item.get("status")
        ),
        "attempt_count": item.get("attempt_count"),
        "error_code": str(item.get("error_code") or "")[:96],
    }


def execute_prepare_tick(
    config: PrepareRunnerConfig,
    *,
    client: Optional[PrepareSidecarClient] = None,
    heartbeat_factory=LeaseHeartbeat,
) -> Dict[str, Any]:
    """Claim and process no more than one durable preparation row."""

    config.validate()
    sidecar = client or PrepareSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
        process_timeout=config.process_timeout,
    )
    claim = sidecar.claim(
        worker_id=config.worker_id,
        lease_seconds=config.lease_seconds,
    )
    if claim is None:
        return {
            "status": "idle",
            "claimed_count": 0,
            "processed_count": 0,
        }

    item = claim["item"]
    preparation_id = _positive_id(item.get("id"), "preparation id")
    claim_token = _claim_token(claim.get("claim_token"))
    heartbeat = heartbeat_factory(
        lambda: sidecar.renew(
            preparation_id,
            claim_token,
            lease_seconds=config.lease_seconds,
        ),
        config.renew_interval_seconds,
    )
    try:
        with heartbeat:
            result = sidecar.process(preparation_id, claim_token)
    finally:
        heartbeat.close()
    processed = dict(result["item"])
    heartbeat_state = heartbeat.snapshot()
    return {
        "status": "processed",
        "claimed_count": 1,
        "processed_count": 1,
        "item": _safe_item(processed),
        **heartbeat_state,
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
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main() -> int:
    try:
        config = PrepareRunnerConfig.from_env()
        with process_lock(config.lock_path) as acquired:
            result = (
                execute_prepare_tick(config)
                if acquired
                else {
                    "status": "skipped_locked",
                    "claimed_count": 0,
                    "processed_count": 0,
                }
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except PrepareRunnerError as exc:
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
                    "error_code": "tt_post_prepare_runner_unexpected",
                    "message": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
