#!/usr/bin/env python3
"""Repair and revalidate explicitly selected unpublished X pool materials."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.selector import (  # noqa: E402
    CandidateSelectionError,
    previous_source_date,
    select_pool_candidates,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    XPostError,
    download_media,
    probe_media,
    redact_text,
)
from scripts.x_post_daily_runner import (  # noqa: E402
    CandidatePreflightError,
    DailyConfig,
    DailyRunError,
    MediaRepairClient,
    SidecarClient,
    _connect_from_config,
    _failure_audit_fields,
    _preflight_candidate,
    process_lock,
)


DAILY_ENV_PATH = Path("/etc/x-post-daily.env")
REPAIR_TOKEN_PATH = Path("/etc/x-post-media-repair.token")
MAX_ENV_FILE_BYTES = 64 * 1024
MAX_BACKFILL_MATERIALS = 100
_MATERIAL_ID = re.compile(r"[1-9][0-9]*")
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_URL_TEXT = re.compile(r"(?i)\bhttps?://[^\s]+")
_SAFE_DAILY_KEYS = frozenset(
    {
        "X_POST_AUTOMATION_INTERNAL_URL",
        "X_POST_DAILY_ACCOUNT_IDS",
        "X_POST_DAILY_CANDIDATE_POOL_LIMIT",
        "X_POST_DAILY_FAILURE_PATH",
        "X_POST_DAILY_INTERNAL_TIMEOUT",
        "X_POST_DAILY_INTERNAL_TOKEN",
        "X_POST_DAILY_INTERNAL_URL",
        "X_POST_DAILY_LOCK_PATH",
        "X_POST_DAILY_MATERIAL_KEYS_PATH",
        "X_POST_DAILY_MAX_MEDIA_BYTES",
        "X_POST_DAILY_MAX_REPAIRS_PER_RUN",
        "X_POST_DAILY_MEDIA_ALLOWED_HOSTS",
        "X_POST_DAILY_MEDIA_TIMEOUT",
        "X_POST_DAILY_MYSQL_CONNECT_TIMEOUT",
        "X_POST_DAILY_MYSQL_DATABASE",
        "X_POST_DAILY_MYSQL_HOST",
        "X_POST_DAILY_MYSQL_PASSWORD",
        "X_POST_DAILY_MYSQL_PORT",
        "X_POST_DAILY_MYSQL_READ_TIMEOUT",
        "X_POST_DAILY_MYSQL_USER",
        "X_POST_DAILY_PLAN_PATH",
        "X_POST_DAILY_PLAN_QUERY_PATH",
        "X_POST_DAILY_POOL_AVAILABLE_PATH",
        "X_POST_DAILY_POOL_CHECK_PATH",
        "X_POST_DAILY_PUBLISH_PATH_TEMPLATE",
        "X_POST_DAILY_REPAIR_PROFILE",
        "X_POST_DAILY_REPAIR_TIMEOUT",
        "X_POST_DAILY_REPAIR_URL",
        "X_POST_DAILY_SCAN_LIMIT",
        "X_POST_DAILY_START_DATE",
        "X_POST_DAILY_STORAGE_PREFLIGHT_PATH",
        "X_POST_DAILY_WORK_DIR",
        "X_POST_FFPROBE_BIN",
    }
)
_BACKFILL_ACCOUNT = {
    "id": 1,
    "username": "x_media_repair",
    "x_user_id": "1",
    "display_name": "X Media Repair",
}


class BackfillError(DailyRunError):
    pass


def _read_regular_file(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise BackfillError(
            "%s could not be opened as a regular file" % path.name,
            code="x_post_backfill_config_unavailable",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 0
            or metadata.st_size > MAX_ENV_FILE_BYTES
        ):
            raise BackfillError(
                "%s is not a bounded regular file" % path.name,
                code="x_post_backfill_config_invalid",
            )
        chunks = []
        remaining = MAX_ENV_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_ENV_FILE_BYTES:
            raise BackfillError(
                "%s exceeds the file-size limit" % path.name,
                code="x_post_backfill_config_invalid",
            )
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BackfillError(
            "%s is not valid UTF-8" % path.name,
            code="x_post_backfill_config_invalid",
        ) from None


def _parse_environment_file(path, allowed):
    """Parse assignments as data; never invoke a shell or expand variables."""
    values = {}
    for line_number, line in enumerate(
        _read_regular_file(path).splitlines(), start=1
    ):
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            parts = list(lexer)
        except ValueError:
            raise BackfillError(
                "%s contains invalid quoting on line %s"
                % (Path(path).name, line_number),
                code="x_post_backfill_config_invalid",
            ) from None
        if not parts:
            continue
        if len(parts) != 1 or "=" not in parts[0]:
            raise BackfillError(
                "%s contains a non-assignment on line %s"
                % (Path(path).name, line_number),
                code="x_post_backfill_config_invalid",
            )
        key, value = parts[0].split("=", 1)
        if (
            not _ENV_KEY.fullmatch(key)
            or key not in allowed
            or key in values
            or "\x00" in value
        ):
            raise BackfillError(
                "%s contains an unsupported assignment on line %s"
                % (Path(path).name, line_number),
                code="x_post_backfill_config_invalid",
            )
        values[key] = value
    return values


def load_environment_files(
    daily_path=DAILY_ENV_PATH,
    token_path=REPAIR_TOKEN_PATH,
):
    daily = _parse_environment_file(daily_path, _SAFE_DAILY_KEYS)
    token = _parse_environment_file(
        token_path, {"X_POST_MEDIA_REPAIR_TOKEN"}
    )
    if set(token) != {"X_POST_MEDIA_REPAIR_TOKEN"}:
        raise BackfillError(
            "x-post-media-repair.token must contain exactly one token",
            code="x_post_backfill_config_invalid",
        )
    if "X_POST_MEDIA_REPAIR_TOKEN" in daily:
        raise BackfillError(
            "repair token must remain in its dedicated file",
            code="x_post_backfill_config_invalid",
        )
    result = dict(daily)
    result.update(token)
    return result


@contextlib.contextmanager
def configured_environment(values):
    """Expose parsed values without inheriting unrelated X_POST settings."""
    keys = {
        key for key in os.environ if key.startswith("X_POST_")
    }.union(values)
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(values)
        yield
    finally:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


def normalize_material_ids(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise BackfillError(
            "at least one --material-id is required",
            code="x_post_backfill_material_ids_invalid",
        )
    if len(values) > MAX_BACKFILL_MATERIALS:
        raise BackfillError(
            "too many material IDs were requested",
            code="x_post_backfill_material_ids_invalid",
        )
    normalized = []
    seen = set()
    for raw in values:
        value = str(raw or "").strip()
        if (
            not _MATERIAL_ID.fullmatch(value)
            or int(value) > 9223372036854775807
            or value in seen
        ):
            raise BackfillError(
                "material IDs must be unique positive integers",
                code="x_post_backfill_material_ids_invalid",
            )
        seen.add(value)
        normalized.append(value)
    return normalized


def _safe_error(exc):
    code, message = _failure_audit_fields(exc)
    message = _URL_TEXT.sub("[url redacted]", redact_text(message, 240))
    return code, message or "material repair or validation failed"


def _failure_result(material_id, pool_item_id, exc):
    code, message = _safe_error(exc)
    result = {
        "material_id": str(material_id),
        "status": "failed",
        "error_code": code,
        "error_message": message,
    }
    if isinstance(pool_item_id, int) and not isinstance(pool_item_id, bool):
        result["pool_item_id"] = pool_item_id
    return result


def _validate_report_path(path):
    supplied = Path(path)
    if not supplied.is_absolute():
        raise BackfillError(
            "--report-path must be absolute",
            code="x_post_backfill_report_path_invalid",
        )
    target = supplied.resolve(strict=False)
    if os.name != "nt":
        for protected in (Path("/etc"), Path("/run")):
            try:
                target.relative_to(protected)
            except ValueError:
                continue
            raise BackfillError(
                "--report-path overlaps a protected system directory",
                code="x_post_backfill_report_path_invalid",
            )
    if target in {
        DAILY_ENV_PATH.resolve(strict=False),
        REPAIR_TOKEN_PATH.resolve(strict=False),
    }:
        raise BackfillError(
            "--report-path overlaps protected configuration",
            code="x_post_backfill_report_path_invalid",
        )
    parent = target.parent
    if (
        not parent.exists()
        or not parent.is_dir()
        or parent.is_symlink()
        or (target.exists() and target.is_symlink())
    ):
        raise BackfillError(
            "--report-path is unsafe",
            code="x_post_backfill_report_path_invalid",
        )
    return target


def _atomic_write_report(path, result):
    target = _validate_report_path(path)
    parent = target.parent
    payload = (
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name,
        suffix=".tmp",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _execute_locked_backfill(
    config,
    material_ids,
    *,
    sidecar,
    repair_client,
    connection_factory,
    pool_candidate_loader,
    downloader,
    prober,
    now,
):
    sidecar.preflight_storage(config.storage_preflight_path)
    available = sidecar.available_pool_items(
        config.pool_available_path, 1000
    )
    requested = set(material_ids)
    selected_pool = [
        item for item in available if item["material_id"] in requested
    ]
    selected_ids = {item["material_id"] for item in selected_pool}
    results = [
        _failure_result(
            material_id,
            None,
            BackfillError(
                "material is not in the available unpublished pool",
                code="x_post_backfill_material_not_available",
            ),
        )
        for material_id in material_ids
        if material_id not in selected_ids
    ]
    if selected_pool:
        connection = connection_factory(config)
        try:
            candidates, selector_rejections = pool_candidate_loader(
                connection,
                selected_pool,
                previous_source_date(shanghai_now(now)),
                limit=len(selected_pool),
                schema=config.mysql_database,
            )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
    else:
        candidates, selector_rejections = [], []

    checks = []
    rejected_pool_ids = set()
    pool_by_id = {int(item["id"]): item for item in selected_pool}
    for rejection in selector_rejections:
        pool_item_id = rejection.get("pool_item_id")
        if isinstance(pool_item_id, int) and not isinstance(pool_item_id, bool):
            rejected_pool_ids.add(pool_item_id)
            material_id = str(
                pool_by_id.get(pool_item_id, {}).get("material_id", "")
                or ""
            )
            code = str(
                rejection.get("error_code", "")
                or "x_post_backfill_candidate_rejected"
            )[:64]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
                code = "x_post_backfill_candidate_rejected"
            message = _URL_TEXT.sub(
                "[url redacted]",
                redact_text(rejection.get("error_message", ""), 240),
            )
            checks.append(
                {
                    "pool_item_id": pool_item_id,
                    "error_code": code,
                    "error_message": message,
                }
            )
            results.append(
                {
                    "material_id": material_id,
                    "pool_item_id": pool_item_id,
                    "status": "failed",
                    "error_code": code,
                    "error_message": message,
                }
            )

    work_root = Path(config.work_dir)
    if not work_root.exists() or not work_root.is_dir() or work_root.is_symlink():
        raise BackfillError(
            "daily media work directory is unavailable",
            code="x_post_storage_unavailable",
        )
    repair_attempted_count = 0
    candidate_by_pool_id = {
        int(item["pool_item_id"]): item for item in candidates
    }
    with tempfile.TemporaryDirectory(
        prefix="x-post-repair-backfill-",
        dir=str(work_root),
    ) as temporary:
        root = Path(temporary)
        for pool_item in selected_pool:
            pool_item_id = int(pool_item["id"])
            if pool_item_id in rejected_pool_ids:
                continue
            candidate = candidate_by_pool_id.get(pool_item_id)
            if candidate is None:
                exc = BackfillError(
                    "material could not be hydrated from the custom source",
                    code="x_post_backfill_candidate_not_found",
                )
                result = _failure_result(
                    pool_item["material_id"], pool_item_id, exc
                )
                results.append(result)
                checks.append(
                    {
                        "pool_item_id": pool_item_id,
                        "error_code": result["error_code"],
                        "error_message": result["error_message"],
                    }
                )
                continue
            try:
                # The daily cap bounds automatic newest-first replenishment. This
                # operator command already has an explicit, bounded ID list,
                # so keep the one-attempt guard per material without making
                # later requested IDs depend on an earlier material's result.
                item_repair_state = {"attempted": 0}
                item = _preflight_candidate(
                    config,
                    candidate,
                    _BACKFILL_ACCOUNT,
                    1,
                    max(1, int(shanghai_now(now).timestamp())),
                    root / ("%s.mp4" % candidate["material_id"]),
                    downloader,
                    prober,
                    repair_client=repair_client,
                    repair_state=item_repair_state,
                )
            except (
                XPostError,
                CandidatePreflightError,
                CandidateSelectionError,
                http.client.HTTPException,
                OSError,
                ValueError,
            ) as exc:
                result = _failure_result(
                    candidate["material_id"], pool_item_id, exc
                )
                results.append(result)
                checks.append(
                    {
                        "pool_item_id": pool_item_id,
                        "error_code": result["error_code"],
                        "error_message": result["error_message"],
                    }
                )
                repair_attempted_count += int(item_repair_state["attempted"])
                continue
            repair_attempted_count += int(item_repair_state["attempted"])
            checks.append(
                {
                    "pool_item_id": pool_item_id,
                    "error_code": "",
                    "error_message": "",
                }
            )
            results.append(
                {
                    "material_id": str(item["material_id"]),
                    "pool_item_id": pool_item_id,
                    "status": (
                        "repaired_ready"
                        if item.get("media_repair_job_key")
                        else "validated_ready"
                    ),
                }
            )

    updated = 0
    for start in range(0, len(checks), 100):
        response = sidecar.record_pool_checks(
            config.pool_check_path, checks[start : start + 100]
        )
        updated += int(response["updated_count"])
    if updated != len(checks):
        raise BackfillError(
            "one or more pool rows changed before validation was recorded",
            code="x_post_backfill_pool_update_conflict",
        )
    results.sort(
        key=lambda item: material_ids.index(item["material_id"])
        if item["material_id"] in material_ids
        else len(material_ids)
    )
    failed_count = sum(item["status"] == "failed" for item in results)
    ready_count = sum(item["status"].endswith("_ready") for item in results)
    return {
        "status": (
            "completed" if failed_count == 0 else "completed_with_failures"
        ),
        "requested_count": len(material_ids),
        "available_count": len(selected_pool),
        "ready_count": ready_count,
        "failed_count": failed_count,
        "repair_attempted_count": repair_attempted_count,
        "pool_checks_updated_count": updated,
        "results": results,
    }


def execute_backfill(
    config,
    material_ids,
    *,
    sidecar=None,
    repair_client=None,
    connection_factory=None,
    pool_candidate_loader=select_pool_candidates,
    downloader=download_media,
    prober=probe_media,
    lock_factory=process_lock,
    now=None,
):
    material_ids = normalize_material_ids(material_ids)
    config.validate()
    if not config.repair_url:
        raise BackfillError(
            "X media repair is disabled",
            code="x_post_media_repair_disabled",
        )
    sidecar = sidecar or SidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
    )
    repair_client = repair_client or MediaRepairClient(
        config.repair_url,
        config.repair_token,
        timeout=config.repair_timeout,
        max_output_bytes=config.max_media_bytes,
    )
    connection_factory = connection_factory or _connect_from_config
    with lock_factory(config.lock_path) as acquired:
        if acquired is None:
            return {
                "status": "skipped_locked",
                "requested_count": len(material_ids),
                "available_count": 0,
                "ready_count": 0,
                "failed_count": 0,
                "repair_attempted_count": 0,
                "pool_checks_updated_count": 0,
                "results": [],
            }
        return _execute_locked_backfill(
            config,
            material_ids,
            sidecar=sidecar,
            repair_client=repair_client,
            connection_factory=connection_factory,
            pool_candidate_loader=pool_candidate_loader,
            downloader=downloader,
            prober=prober,
            now=now,
        )


def _argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Repair and revalidate explicitly selected available X pool "
            "materials without creating or publishing queues."
        )
    )
    parser.add_argument(
        "--material-id",
        action="append",
        required=True,
        help="Custom-source material ID; repeat for multiple materials.",
    )
    parser.add_argument(
        "--report-path",
        help="Optional absolute path for an atomic JSON report.",
    )
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    report_target = None
    try:
        if args.report_path:
            # Validate before any GPU/COS or pool mutation. A later I/O race is
            # reported separately without rewriting the completed work.
            report_target = _validate_report_path(args.report_path)
        values = load_environment_files()
        with configured_environment(values):
            config = DailyConfig.from_env()
            result = execute_backfill(
                config,
                args.material_id,
                now=datetime.now().astimezone(),
            )
    except (
        BackfillError,
        DailyRunError,
        CandidateSelectionError,
        XPostError,
    ) as exc:
        code, message = _safe_error(exc)
        result = {
            "status": "failed",
            "error_code": code,
            "error_message": message,
            "requested_count": len(args.material_id or ()),
            "available_count": 0,
            "ready_count": 0,
            "failed_count": len(args.material_id or ()),
            "repair_attempted_count": 0,
            "pool_checks_updated_count": 0,
            "results": [],
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "error_code": "x_post_backfill_unexpected_error",
            "error_message": type(exc).__name__,
            "requested_count": len(args.material_id or ()),
            "available_count": 0,
            "ready_count": 0,
            "failed_count": len(args.material_id or ()),
            "repair_attempted_count": 0,
            "pool_checks_updated_count": 0,
            "results": [],
        }
    report_write_failed = False
    try:
        if report_target is not None:
            _atomic_write_report(report_target, result)
    except (BackfillError, OSError) as exc:
        code, message = _safe_error(exc)
        result = dict(result)
        result["report_status"] = "failed"
        result["report_error_code"] = code
        result["report_error_message"] = message
        report_write_failed = True
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return (
        0
        if result["status"] == "completed" and not report_write_failed
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
