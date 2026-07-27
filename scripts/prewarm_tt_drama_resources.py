#!/usr/bin/env python3
"""Prewarm W2A HTML metadata for recently active DramaWave content IDs."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_drama_resources import (  # noqa: E402
    DATA_DISK_UUID,
    ResourceStorageError,
    normalize_content_id,
    validate_resource_cache_path,
)
from features.tt_drama_prewarm import (  # noqa: E402
    ActiveDramaCandidateRepository,
    CandidateOverflowError,
    PrewarmCandidateConfig,
    PrewarmSourceError,
    recent_shanghai_date_window,
)


DEFAULT_DB_PATH = (
    "/mnt/data-disk/tt-drama-resource-cache/state/resources.sqlite3"
)
DEFAULT_CURSOR_PATH = (
    "/mnt/data-disk/tt-drama-resource-cache/state/prewarm-cursor.json"
)
DEFAULT_LOCK_PATH = "/run/tt-drama-resource-prewarm/prewarm.lock"
DEFAULT_BATCH_LIMIT = 500
BOOTSTRAP_BATCH_LIMIT = 3000
MAX_BATCH_LIMIT = BOOTSTRAP_BATCH_LIMIT
MAX_WORKERS = 4
MAX_QPS = 2.0
CURSOR_SCHEMA_VERSION = 2
MAX_RETRY_PER_RUN = 100
MAX_RETRY_BACKLOG = 5000


class PrewarmRunError(RuntimeError):
    """The runner could not safely execute or persist its plan."""

    error_code = "prewarm_run_error"


def _env(name, default=""):
    return str(os.environ.get(name, default) or default).strip()


def _env_int(name, default, minimum, maximum):
    try:
        value = int(_env(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(value, int(maximum)))


def _env_float(name, default, minimum, maximum):
    try:
        value = float(_env(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), min(value, float(maximum)))


def _mysql_settings():
    return {
        "host": _env("DRAMA_DB_HOST") or _env("ADMIN_MAPPING_MYSQL_HOST"),
        "port": (
            _env("DRAMA_DB_PORT")
            or _env("ADMIN_MAPPING_MYSQL_PORT")
            or "0"
        ),
        "user": _env("DRAMA_DB_USER") or _env("ADMIN_MAPPING_MYSQL_USER"),
        "password": (
            os.environ.get("DRAMA_DB_PASSWORD")
            or os.environ.get("ADMIN_MAPPING_MYSQL_PASSWORD")
            or ""
        ),
        "database": (
            _env("DRAMA_DB_NAME")
            or _env("ADMIN_MAPPING_MYSQL_DATABASE")
            or "kunlunads_dev"
        ),
    }


def _build_repository():
    settings = _mysql_settings()
    config = PrewarmCandidateConfig(
        database=settings["database"],
        insight_table=_env(
            "TT_DRAMA_RESOURCE_PREWARM_INSIGHT_TABLE",
            "ads_custom_source_insight",
        ),
        insight_index=_env(
            "TT_DRAMA_RESOURCE_PREWARM_INSIGHT_INDEX",
            "as",
        ),
        product=_env(
            "TT_DRAMA_RESOURCE_PREWARM_PRODUCT",
            "Dramawave",
        ),
        source_app_id=_env(
            "TT_DRAMA_RESOURCE_PREWARM_SOURCE_APP_ID",
            "[w2a]drama-double",
        ),
        data_source=_env_int(
            "TT_DRAMA_RESOURCE_PREWARM_DATA_SOURCE",
            6,
            0,
            100,
        ),
    )
    return ActiveDramaCandidateRepository(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        config=config,
        connect_timeout=_env_int(
            "TT_DRAMA_RESOURCE_PREWARM_DB_CONNECT_TIMEOUT_SECONDS",
            5,
            1,
            10,
        ),
        read_timeout=_env_int(
            "TT_DRAMA_RESOURCE_PREWARM_DB_READ_TIMEOUT_SECONDS",
            30,
            5,
            60,
        ),
    )


def _cover_hosts():
    value = _env("TT_DRAMA_RESOURCE_COVER_HOSTS", "cdn.usrgrow.com")
    return tuple(
        item.strip().lower().rstrip(".")
        for item in value.split(",")
        if item.strip()
    )


def _build_resource_service(db_path):
    from features.tt_drama_resources import (
        SQLiteResourceCache,
        W2AHTMLClient,
        W2AResourceService,
    )

    landing_id = _env_int(
        "TT_DRAMA_RESOURCE_LANDING_ID",
        2049,
        1,
        999999,
    )
    if landing_id != 2049:
        raise PrewarmRunError("W2A landing_id must remain 2049")
    cache = SQLiteResourceCache(
        db_path,
        busy_timeout_seconds=_env_float(
            "TT_DRAMA_RESOURCE_SQLITE_BUSY_TIMEOUT_SECONDS",
            5,
            1,
            30,
        ),
    )
    client = W2AHTMLClient(
        landing_id=landing_id,
        timeout_seconds=_env_float(
            "TT_DRAMA_RESOURCE_HTTP_TIMEOUT_SECONDS",
            5,
            1,
            15,
        ),
        max_html_bytes=_env_int(
            "TT_DRAMA_RESOURCE_HTTP_MAX_BYTES",
            512 * 1024,
            64 * 1024,
            2 * 1024 * 1024,
        ),
        allowed_cover_hosts=_cover_hosts(),
    )
    return W2AResourceService(
        cache=cache,
        client=client,
        landing_id=landing_id,
        positive_ttl_seconds=_env_int(
            "TT_DRAMA_RESOURCE_POSITIVE_TTL_SECONDS",
            86400,
            300,
            7 * 86400,
        ),
        negative_ttl_seconds=_env_int(
            "TT_DRAMA_RESOURCE_NEGATIVE_TTL_SECONDS",
            900,
            60,
            86400,
        ),
        stale_ttl_seconds=_env_int(
            "TT_DRAMA_RESOURCE_STALE_TTL_SECONDS",
            7 * 86400,
            3600,
            30 * 86400,
        ),
        lease_seconds=_env_int(
            "TT_DRAMA_RESOURCE_LEASE_SECONDS",
            15,
            5,
            60,
        ),
        wait_timeout_seconds=_env_float(
            "TT_DRAMA_RESOURCE_WAIT_TIMEOUT_SECONDS",
            5,
            1,
            15,
        ),
    )


def _validate_state_paths(db_path, cursor_path):
    if os.path.normpath(str(db_path)) != os.path.normpath(DEFAULT_DB_PATH):
        raise PrewarmRunError(
            "resource database path must remain on the fixed data disk"
        )
    if os.path.normpath(str(cursor_path)) != os.path.normpath(
        DEFAULT_CURSOR_PATH
    ):
        raise PrewarmRunError(
            "prewarm cursor path must remain on the fixed data disk"
        )
    for target_path in (db_path, cursor_path):
        try:
            target = validate_resource_cache_path(
                target_path,
                expected_mount_uuid=DATA_DISK_UUID,
            )
        except ResourceStorageError as exc:
            raise PrewarmRunError(str(exc)) from None
        parent = target.parent
        if not parent.is_dir():
            raise PrewarmRunError(
                "resource state directory must be provisioned before prewarm"
            )
        if parent.is_symlink():
            raise PrewarmRunError(
                "resource state directory must not be a symlink"
            )
        if not os.access(str(parent), os.R_OK | os.W_OK | os.X_OK):
            raise PrewarmRunError(
                "resource state directory is not writable"
            )


@contextmanager
def _exclusive_lock(path):
    try:
        import fcntl
    except ImportError:
        raise PrewarmRunError(
            "fcntl is required for the prewarm lock"
        ) from None
    lock_path = Path(path)
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(lock_path.parent), 0o700)
        handle = lock_path.open("a+")
    except OSError as exc:
        raise PrewarmRunError(
            "prewarm lock setup failed: %s" % type(exc).__name__
        ) from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        error = PrewarmRunError("another resource prewarm is already running")
        error.error_code = "prewarm_already_running"
        raise error from None
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_cursor(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != CURSOR_SCHEMA_VERSION
    ):
        return {}
    next_content_id = str(value.get("next_content_id") or "")
    try:
        next_index = max(0, int(value.get("next_index") or 0))
    except (TypeError, ValueError):
        next_index = 0
    retry_content_ids = []
    seen = set()
    for candidate in value.get("retry_content_ids") or []:
        try:
            content_id = normalize_content_id(candidate)
        except ValueError:
            continue
        if content_id in seen:
            continue
        seen.add(content_id)
        retry_content_ids.append(content_id)
        if len(retry_content_ids) >= MAX_RETRY_BACKLOG:
            break
    return {
        "next_content_id": next_content_id,
        "next_index": next_index,
        "retry_content_ids": retry_content_ids,
    }


def _select_batch(candidates, batch_limit, cursor, bootstrap=False):
    content_ids = list(candidates or [])
    if not content_ids:
        return [], ""
    limit = max(1, min(int(batch_limit), MAX_BATCH_LIMIT, len(content_ids)))
    state = cursor or {}
    requested = "" if bootstrap else str(
        state.get("next_content_id") or ""
    )
    if bootstrap:
        start = 0
    elif requested in content_ids:
        start = content_ids.index(requested)
    else:
        try:
            start = int(state.get("next_index") or 0) % len(content_ids)
        except (TypeError, ValueError):
            start = 0

    selected = []
    selected_set = set()
    if not bootstrap:
        active = set(content_ids)
        retry_candidates = [
            content_id
            for content_id in state.get("retry_content_ids") or []
            if content_id in active
        ]
        retry_limit = min(
            len(retry_candidates),
            MAX_RETRY_PER_RUN,
            limit if limit == 1 else max(1, limit - 1),
        )
        for content_id in retry_candidates[:retry_limit]:
            if content_id in selected_set:
                continue
            selected.append(content_id)
            selected_set.add(content_id)

    scanned = 0
    while len(selected) < limit and scanned < len(content_ids):
        content_id = content_ids[(start + scanned) % len(content_ids)]
        scanned += 1
        if content_id in selected_set:
            continue
        selected.append(content_id)
        selected_set.add(content_id)
    next_content_id = content_ids[(start + scanned) % len(content_ids)]
    return selected, next_content_id


def _atomic_write_cursor(
    path,
    next_content_id,
    candidate_count,
    retry_content_ids=(),
    next_index=0,
):
    target = Path(path)
    parent = target.parent
    payload = (
        json.dumps(
            {
                "schema_version": CURSOR_SCHEMA_VERSION,
                "next_content_id": str(next_content_id or ""),
                "next_index": max(0, int(next_index)),
                "candidate_count": int(candidate_count),
                "retry_content_ids": list(retry_content_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".prewarm-cursor.",
            suffix=".tmp",
            dir=str(parent),
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
        temporary = None
    except Exception as exc:
        raise PrewarmRunError(
            "prewarm cursor write failed: %s" % type(exc).__name__
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class _StartRateGate:
    """Bound request starts even when several cache workers are active."""

    def __init__(self, qps, clock=None, sleep=None):
        self.interval = 1.0 / max(0.1, min(float(qps), MAX_QPS))
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self):
        with self.lock:
            now = float(self.clock())
            delay = max(0.0, self.next_start - now)
            if delay:
                self.sleep(delay)
                now = float(self.clock())
            self.next_start = max(now, self.next_start) + self.interval


def execute_prewarm(
    *,
    repository,
    service,
    start_date,
    end_date,
    batch_limit=DEFAULT_BATCH_LIMIT,
    cursor_path=DEFAULT_CURSOR_PATH,
    bootstrap=False,
    dry_run=False,
    workers=MAX_WORKERS,
    qps=MAX_QPS,
    clock=None,
    sleep=None,
    resource_error_types=(Exception,),
):
    candidates = repository.fetch(start_date, end_date)
    effective_limit = (
        BOOTSTRAP_BATCH_LIMIT
        if bootstrap
        else min(int(batch_limit), DEFAULT_BATCH_LIMIT)
    )
    cursor = {} if bootstrap else _load_cursor(cursor_path)
    selected, next_content_id = _select_batch(
        candidates,
        effective_limit,
        cursor,
        bootstrap=bootstrap,
    )
    base_result = {
        "window_start": start_date,
        "window_end": end_date,
        "candidate_count": len(candidates),
        "planned_count": len(selected),
        "batch_limit": effective_limit,
        "bootstrap": bool(bootstrap),
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return dict(base_result, status="dry_run")
    if service is None:
        raise PrewarmRunError("resource service is required")
    service.warmup()
    gate = _StartRateGate(qps=qps, clock=clock, sleep=sleep)
    states = Counter()
    found_count = 0
    not_found_count = 0
    failures = []

    def resolve_one(content_id):
        cache = getattr(service, "cache", None)
        landing_id = int(getattr(service, "landing_id", 2049))
        if cache is not None and hasattr(cache, "peek"):
            cached = cache.peek(
                landing_id,
                content_id,
                allow_stale=True,
            )
            cached_state = str(
                getattr(cached, "cache_state", "") or ""
            )
            if cached_state in {"DISK_HIT", "NEGATIVE_HIT"}:
                return cached
        gate.wait()
        return service.resolve(
            content_id,
            force_refresh=False,
            allow_stale=True,
        )

    worker_count = max(1, min(int(workers), MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        pending = {
            pool.submit(resolve_one, content_id): content_id
            for content_id in selected
        }
        for future in as_completed(pending):
            content_id = pending[future]
            try:
                outcome = future.result()
            except resource_error_types as exc:
                failures.append(
                    {
                        "content_id": content_id,
                        "error": type(exc).__name__,
                    }
                )
                continue
            state = str(getattr(outcome, "cache_state", "") or "unknown")
            states[state] += 1
            if bool(getattr(outcome, "found", False)):
                found_count += 1
            else:
                not_found_count += 1
            if state == "STALE":
                failures.append(
                    {
                        "content_id": content_id,
                        "error": "stale_fallback",
                    }
                )

    failed_content_ids = [item["content_id"] for item in failures]
    failed_set = set(failed_content_ids)
    selected_set = set(selected)
    active_set = set(candidates)
    retry_content_ids = []
    retry_seen = set()
    for content_id in list(cursor.get("retry_content_ids") or []) + failed_content_ids:
        if (
            content_id not in active_set
            or (
                content_id in selected_set
                and content_id not in failed_set
            )
            or content_id in retry_seen
        ):
            continue
        retry_seen.add(content_id)
        retry_content_ids.append(content_id)
        if len(retry_content_ids) >= MAX_RETRY_BACKLOG:
            break
    _atomic_write_cursor(
        cursor_path,
        next_content_id=next_content_id,
        candidate_count=len(candidates),
        retry_content_ids=retry_content_ids,
        next_index=(
            candidates.index(next_content_id)
            if next_content_id in candidates
            else 0
        ),
    )
    status = "ok" if not failures else "partial_error"
    return dict(
        base_result,
        status=status,
        processed_count=len(selected),
        found_count=found_count,
        not_found_count=not_found_count,
        error_count=len(failures),
        cache_states=dict(sorted(states.items())),
        errors=failures[:20],
        retry_count=len(retry_content_ids),
        cursor_advanced=True,
    )


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Prewarm cached W2A HTML metadata for recent active dramas."
        )
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=_env_int(
            "TT_DRAMA_RESOURCE_PREWARM_BATCH_LIMIT",
            DEFAULT_BATCH_LIMIT,
            1,
            DEFAULT_BATCH_LIMIT,
        ),
        help="Maximum candidates in a normal run (1..500; default 500).",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Plan/process up to 3000 candidates from the start of the ranking.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and plan only; do not fetch W2A or write SQLite/cursor.",
    )
    parser.add_argument(
        "--db-path",
        default=_env("TT_DRAMA_RESOURCE_DB_PATH", DEFAULT_DB_PATH),
    )
    parser.add_argument(
        "--cursor-path",
        default=_env(
            "TT_DRAMA_RESOURCE_PREWARM_CURSOR_PATH",
            DEFAULT_CURSOR_PATH,
        ),
    )
    parser.add_argument(
        "--lock-path",
        default=_env(
            "TT_DRAMA_RESOURCE_PREWARM_LOCK_PATH",
            DEFAULT_LOCK_PATH,
        ),
    )
    return parser


def _error_payload(exc):
    error_code = str(
        getattr(exc, "error_code", "unexpected_error")
    )
    if isinstance(exc, ResourceStorageError):
        error_code = "resource_storage_error"
    payload = {
        "status": "error",
        "error_code": error_code,
        "error": (
            str(exc)
            if isinstance(
                exc,
                (
                    CandidateOverflowError,
                    PrewarmSourceError,
                    PrewarmRunError,
                    ResourceStorageError,
                ),
            )
            else type(exc).__name__
        ),
    }
    if isinstance(exc, CandidateOverflowError):
        payload["candidate_count_lower_bound"] = exc.count_lower_bound
        payload["candidate_limit"] = 5000
    return payload


def main(argv=None):
    args = _parser().parse_args(argv)
    maximum_batch_limit = (
        MAX_BATCH_LIMIT if args.bootstrap else DEFAULT_BATCH_LIMIT
    )
    if not 1 <= int(args.batch_limit) <= maximum_batch_limit:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "invalid_batch_limit",
                    "error": (
                        "batch-limit must be between 1 and %d"
                        % maximum_batch_limit
                    ),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    start_date, end_date = recent_shanghai_date_window()
    service = None
    try:
        repository = _build_repository()
        with _exclusive_lock(args.lock_path):
            if args.dry_run:
                resource_errors = (Exception,)
            else:
                _validate_state_paths(args.db_path, args.cursor_path)
                service = _build_resource_service(args.db_path)
                from features.tt_drama_resources import (
                    ResourceBusyError,
                    ResourceSourceError,
                )

                resource_errors = (
                    ResourceSourceError,
                    ResourceBusyError,
                )
            result = execute_prewarm(
                repository=repository,
                service=service,
                start_date=start_date,
                end_date=end_date,
                batch_limit=args.batch_limit,
                cursor_path=args.cursor_path,
                bootstrap=args.bootstrap,
                dry_run=args.dry_run,
                workers=_env_int(
                    "TT_DRAMA_RESOURCE_PREWARM_WORKERS",
                    MAX_WORKERS,
                    1,
                    MAX_WORKERS,
                ),
                qps=_env_float(
                    "TT_DRAMA_RESOURCE_PREWARM_QPS",
                    MAX_QPS,
                    0.1,
                    MAX_QPS,
                ),
                resource_error_types=resource_errors,
            )
    except Exception as exc:
        print(
            json.dumps(
                _error_payload(exc),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                pass
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"ok", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
