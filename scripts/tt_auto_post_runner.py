#!/usr/bin/env python3
"""Create due TT auto-template runs or execute bounded parallel account workers."""

from __future__ import annotations

import contextlib
import concurrent.futures
import argparse
import http.client
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_auto_posts.validation import valid_internal_bearer  # noqa: E402


DEFAULT_URL = "http://127.0.0.1:18831"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class RunnerError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 500):
        self.code = str(code or "tt_auto_post_runner_error")[:80]
        self.status = status if isinstance(status, int) else 500
        super().__init__(str(message or "TT auto runner failed")[:500])


@dataclass(frozen=True)
class RunnerConfig:
    internal_url: str
    internal_token: str
    worker_id: str
    timeout: int
    execute_timeout: int
    lock_path: str
    scheduler_lock_path: str
    worker_count: int
    max_tasks_per_worker: int
    publish_poll_seconds: int

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "RunnerConfig":
        source = os.environ if environ is None else environ
        try:
            timeout = int(source.get("TT_AUTO_POST_INTERNAL_TIMEOUT", "120"))
            execute_timeout = int(
                source.get("TT_AUTO_POST_EXECUTE_TIMEOUT", "9300")
            )
            worker_count = int(source.get("TT_AUTO_POST_WORKER_COUNT", "4"))
            max_tasks_per_worker = int(
                source.get("TT_AUTO_POST_MAX_TASKS_PER_WORKER", "1")
            )
            publish_poll_seconds = int(
                source.get("TT_AUTO_POST_PUBLISH_POLL_SECONDS", "15")
            )
        except (TypeError, ValueError, OverflowError):
            timeout = execute_timeout = worker_count = max_tasks_per_worker = 0
            publish_poll_seconds = 0
        return cls(
            internal_url=str(
                source.get("TT_AUTO_POST_INTERNAL_URL", DEFAULT_URL)
            ).rstrip("/"),
            internal_token=str(
                source.get("TT_AUTO_POST_INTERNAL_TOKEN", "") or ""
            ),
            worker_id=str(
                source.get(
                    "TT_AUTO_POST_RUNNER_ID", "tt-auto-post-runner-primary"
                )
            ).strip(),
            timeout=timeout,
            execute_timeout=execute_timeout,
            lock_path=str(
                source.get(
                    "TT_AUTO_POST_RUNNER_LOCK_PATH",
                    "/run/tt-auto-post/runner.lock",
                )
            ).strip(),
            scheduler_lock_path=str(
                source.get(
                    "TT_AUTO_POST_SCHEDULER_LOCK_PATH",
                    "/run/tt-auto-post/scheduler.lock",
                )
            ).strip(),
            worker_count=worker_count,
            max_tasks_per_worker=max_tasks_per_worker,
            publish_poll_seconds=publish_poll_seconds,
        )

    def validate(self) -> None:
        parsed = urlsplit(self.internal_url)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or port != 18831
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RunnerError(
                "tt_auto_post_runner_config_invalid",
                "runner may only access loopback port 18831",
            )
        if not valid_internal_bearer(self.internal_token):
            raise RunnerError(
                "tt_auto_post_runner_config_invalid",
                "runner bearer is not configured",
            )
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,120}", self.worker_id):
            raise RunnerError(
                "tt_auto_post_runner_config_invalid", "worker id is invalid"
            )
        if not 5 <= self.timeout <= 600 or not 120 <= self.execute_timeout <= 14400:
            raise RunnerError(
                "tt_auto_post_runner_config_invalid", "timeout is invalid"
            )
        if not 1 <= self.worker_count <= 16 or self.max_tasks_per_worker != 1:
            raise RunnerError(
                "tt_auto_post_runner_config_invalid", "worker limits are invalid"
            )
        if not 2 <= self.publish_poll_seconds <= 60:
            raise RunnerError(
                "tt_auto_post_runner_config_invalid",
                "publish poll interval is invalid",
            )
        for raw_lock in (self.lock_path, self.scheduler_lock_path):
            lock = Path(raw_lock)
            if not lock.is_absolute() or (
                os.name != "nt" and not str(lock).startswith("/run/tt-auto-post/")
            ):
                raise RunnerError(
                    "tt_auto_post_runner_config_invalid", "lock path is invalid"
                )


class SidecarClient:
    def __init__(self, config: RunnerConfig):
        config.validate()
        self.config = config

    def post(self, path: str, payload: Mapping[str, Any], timeout: int) -> Dict[str, Any]:
        if path not in {
            "/internal/tt-auto-post/tick",
            "/internal/tt-auto-post/execute-next",
        }:
            raise RunnerError("tt_auto_post_runner_route_invalid", "route denied")
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", 18831, timeout=timeout)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": "Bearer " + self.config.internal_token,
                    "Content-Type": "application/json; charset=UTF-8",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RunnerError(
                    "tt_auto_post_runner_response_too_large",
                    "sidecar response exceeds limit",
                    502,
                )
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                raise RunnerError(
                    "tt_auto_post_runner_response_invalid",
                    "sidecar response is invalid",
                    502,
                ) from None
            if not isinstance(decoded, dict):
                raise RunnerError(
                    "tt_auto_post_runner_response_invalid",
                    "sidecar response is invalid",
                    502,
                )
            if not 200 <= int(response.status) < 300:
                raise RunnerError(
                    str(decoded.get("code") or decoded.get("error") or "sidecar_error"),
                    str(decoded.get("message") or "sidecar request failed"),
                    int(response.status),
                )
            return decoded
        except RunnerError:
            raise
        except (OSError, http.client.HTTPException):
            raise RunnerError(
                "tt_auto_post_sidecar_unavailable",
                "TT auto sidecar is unavailable",
                503,
            ) from None
        finally:
            connection.close()


@contextlib.contextmanager
def exclusive_lock(path: str) -> Iterator[bool]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def tick_once(config: RunnerConfig) -> Dict[str, Any]:
    client = SidecarClient(config)
    return client.post(
        "/internal/tt-auto-post/tick", {}, timeout=config.timeout
    )


def execute_pending(config: RunnerConfig) -> list[list[Dict[str, Any]]]:
    executed = []
    failures = []

    def worker(
        worker_id: str,
        phases: Optional[list[str]] = None,
    ) -> list[Dict[str, Any]]:
        worker_client = SidecarClient(config)
        results = []
        for _ in range(config.max_tasks_per_worker):
            payload: Dict[str, Any] = {"worker_id": worker_id}
            if phases is not None:
                payload["phases"] = list(phases)
            result = worker_client.post(
                "/internal/tt-auto-post/execute-next",
                payload,
                timeout=config.execute_timeout,
            )
            results.append(result)
            if not bool(result.get("claimed")):
                break
        return results

    if config.worker_count == 1:
        try:
            return [worker(config.worker_id + "-1")]
        except RunnerError as exc:
            raise RunnerError(
                "tt_auto_post_runner_partial_failure",
                "runner phase failed (execute:%s)" % exc.code,
                exc.status,
            ) from None

    prepare_worker_count = config.worker_count - 1
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=prepare_worker_count,
        thread_name_prefix="tt-auto-post",
    ) as pool:
        futures = [
            pool.submit(
                worker,
                "%s-prepare-%d" % (config.worker_id, index + 1),
                ["selection", "prepare"],
            )
            for index in range(prepare_worker_count)
        ]
        publish_results: list[Dict[str, Any]] = []
        publish_client = SidecarClient(config)
        while True:
            try:
                result = publish_client.post(
                    "/internal/tt-auto-post/execute-next",
                    {
                        "worker_id": config.worker_id + "-publish",
                        "phases": ["publish", "reconcile"],
                    },
                    timeout=config.execute_timeout,
                )
            except RunnerError as exc:
                failures.append(("publish", exc.code, exc.status))
                break
            claimed = bool(result.get("claimed"))
            if claimed:
                publish_results.append(result)
            all_done = all(future.done() for future in futures)
            if all_done and not claimed:
                break
            if not claimed:
                pending = [future for future in futures if not future.done()]
                if pending:
                    concurrent.futures.wait(
                        pending,
                        timeout=config.publish_poll_seconds,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
        for future in futures:
            try:
                executed.append(future.result())
            except RunnerError as exc:
                failures.append(("execute", exc.code, exc.status))
        executed.append(publish_results)
    if failures:
        summary = ",".join("%s:%s" % (phase, code) for phase, code, _ in failures)
        raise RunnerError(
            "tt_auto_post_runner_partial_failure",
            "runner phase failed (%s)" % summary,
            max(status for _, _, status in failures),
        )
    return executed


def run_once(config: RunnerConfig) -> Dict[str, Any]:
    tick: Dict[str, Any] = {}
    executed = []
    failures = []
    try:
        tick = tick_once(config)
    except RunnerError as exc:
        failures.append(("tick", exc.code, exc.status))
    # Existing tasks, especially unknown outcomes awaiting reconciliation,
    # continue even when creation of a new scheduled run fails.
    try:
        executed = execute_pending(config)
    except RunnerError as exc:
        failures.append(("execute", exc.code, exc.status))
    if failures:
        summary = ",".join("%s:%s" % (phase, code) for phase, code, _ in failures)
        raise RunnerError(
            "tt_auto_post_runner_partial_failure",
            "runner phase failed (%s)" % summary,
            max(status for _, _, status in failures),
        )
    return {"ok": True, "tick": tick, "execute": executed}


def main(argv=None) -> int:
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--mode", choices=("tick", "execute", "all"), default="all"
        )
        args = parser.parse_args(argv)
        config = RunnerConfig.from_env()
        config.validate()
        lock_path = (
            config.scheduler_lock_path if args.mode == "tick" else config.lock_path
        )
        with exclusive_lock(lock_path) as acquired:
            if not acquired:
                return 0
            if args.mode == "tick":
                tick_once(config)
            elif args.mode == "execute":
                execute_pending(config)
            else:
                run_once(config)
        return 0
    except RunnerError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("tt_auto_post_runner_unexpected: runner failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
