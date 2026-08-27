#!/usr/bin/env python3
"""One explicitly authorized, permanently unlisted deployment canary.

``prepare`` freezes the one operation using an existing completed job output;
it never calls YouTube. ``run`` advances only the supplied canary task once and
then drains at most its three outbox entities. Repeat the exact command to
reconcile processing; a repeat never creates a replacement operation/session.
``status`` opens the already-provisioned ledger read-only and needs no secrets.

Persisted states use the existing ledger (no DDL in this script):
queued -> uploading + video_attempt_count=1 (intent committed BEFORE insert)
-> submitted -> processing -> published + comment published -> sync synced.
An unknown upload with a session/video identity is reconcile-only; an unknown
without identity and any unknown comment remain blocked for manual review.

Credentials come only from inherited environment/one private JSON environment
file and the existing account repository. Output is an allowlisted state DTO;
raw exceptions, account JSON and resumable identities are never printed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import logging
import os
import re
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import (  # noqa: E402
    CANARY_ACCOUNT_ID, CANARY_APP_ID, CANARY_CHANNEL_ID,
    CANARY_CHANNEL_LOCAL_ID, CANARY_DESCRIPTION, CANARY_OPERATION_ID,
    JOB_ID_RE, DramaSynthesisError, is_youtube_canary,
)
from features.drama_synthesis.unified_youtube import (  # noqa: E402
    build_unified_youtube_writer_from_env, read_secure_owned_file, run_sync_outbox_once,
)
from features.drama_synthesis.youtube import YouTubeHTTPClient, YouTubeHTTPError, YouTubePublishEngine  # noqa: E402


CONFIG_KEYS = frozenset({
    "DRAMA_DB_HOST", "DRAMA_DB_PORT", "DRAMA_DB_USER", "DRAMA_DB_PASSWORD", "DRAMA_DB_NAME",
    "DRAMA_JOB_DB_PATH", "DRAMA_SHORT_LINK_ROOT", "DRAMA_SHORT_LINK_OWNER",
    "DRAMA_YOUTUBE_SOURCE_HOSTS", "DRAMA_YOUTUBE_WORK_ROOT", "DRAMA_YOUTUBE_HTTP_TIMEOUT",
    "DRAMA_YOUTUBE_FFPROBE", "DRAMA_YOUTUBE_UNIFIED_RPC_URL",
    "DRAMA_YOUTUBE_UNIFIED_RPC_CREDENTIAL_FILE", "DRAMA_YOUTUBE_UNIFIED_RPC_TIMEOUT",
    "YOUTUBE_LIVE_ENABLED", "DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED",
})
SOURCE_KINDS = ("concat_video", "no_bgm_video", "random_template")
STATES = frozenset({"queued", "validating", "downloading", "uploading", "submitted", "processing", "published", "unknown", "failed", "skipped", "pending", "synced", "retry", "publishing"})


class CanaryCLIError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse's default error can echo an accidentally supplied secret.
        raise CanaryCLIError("canary_arguments_invalid")


def parser() -> argparse.ArgumentParser:
    result = SafeArgumentParser(description=__doc__)
    result.add_argument("--action", required=True, choices=("prepare", "run", "status"))
    result.add_argument("--authorize-unlisted-canary", action="store_true")
    result.add_argument("--operation-id")
    result.add_argument("--confirm-app-id")
    result.add_argument("--confirm-channel-local-id")
    result.add_argument("--confirm-channel-id")
    result.add_argument("--confirm-account-id")
    result.add_argument("--operator-user-id")
    result.add_argument("--job-id")
    result.add_argument("--source-kind", choices=SOURCE_KINDS)
    result.add_argument("--canary-task-id", type=int)
    result.add_argument("--config-file", help="Absolute path to a current-user-owned 0600 JSON environment file")
    return result


def validate_authorization(args: argparse.Namespace) -> None:
    if args.operation_id not in {None, CANARY_OPERATION_ID}:
        raise CanaryCLIError("canary_operation_mismatch")
    if args.canary_task_id is not None and not 1 <= args.canary_task_id <= 2_147_483_647:
        raise CanaryCLIError("canary_task_id_invalid")
    if args.action == "status":
        return
    if not args.authorize_unlisted_canary:
        raise CanaryCLIError("canary_explicit_authorization_required")
    if (
        args.operation_id != CANARY_OPERATION_ID or args.confirm_app_id != CANARY_APP_ID
        or args.confirm_channel_local_id != CANARY_CHANNEL_LOCAL_ID
        or args.confirm_channel_id != CANARY_CHANNEL_ID or args.confirm_account_id != CANARY_ACCOUNT_ID
    ):
        raise CanaryCLIError("canary_target_confirmation_mismatch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", str(args.operator_user_id or "")):
        raise CanaryCLIError("canary_operator_required")
    if args.action == "prepare":
        if not JOB_ID_RE.fullmatch(str(args.job_id or "")) or args.source_kind not in SOURCE_KINDS or args.canary_task_id is not None:
            raise CanaryCLIError("canary_job_source_required")
    elif args.canary_task_id is None or args.job_id is not None or args.source_kind is not None:
        raise CanaryCLIError("canary_exact_task_required")


def load_environment(config_file: str | None, inherited: Mapping[str, str]) -> dict[str, str]:
    result = dict(inherited)
    path = str(config_file or inherited.get("DRAMA_YOUTUBE_CANARY_ENV_FILE") or "").strip()
    if path:
        try:
            value = json.loads(read_secure_owned_file(path, max_bytes=65536))
        except (RuntimeError, ValueError, UnicodeDecodeError, OSError):
            raise CanaryCLIError("canary_config_invalid") from None
        if not isinstance(value, dict) or any(key not in CONFIG_KEYS or type(item) is not str for key, item in value.items()):
            raise CanaryCLIError("canary_config_invalid")
        result.update(value)
    return result


def ledger_path(env: Mapping[str, str]) -> Path:
    path = Path(str(env.get("DRAMA_JOB_DB_PATH") or ""))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CanaryCLIError("canary_ledger_not_ready")
    return path


def read_canary_record(path: Path, task_id: int | None = None) -> dict[str, Any] | None:
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM drama_youtube_publish WHERE operation_id=?", (CANARY_OPERATION_ID,)
            ).fetchone()
    except sqlite3.Error:
        raise CanaryCLIError("canary_ledger_not_ready") from None
    if row is None:
        if task_id is not None:
            raise CanaryCLIError("canary_task_mismatch")
        return None
    record = dict(row)
    if not is_youtube_canary(record) or task_id is not None and int(record["id"]) != task_id:
        raise CanaryCLIError("canary_task_mismatch")
    return record


def validate_execution_env(env: Mapping[str, str]) -> tuple[Path, tuple[str, ...]]:
    # Only this CLI opts into the internal lane. Neither formal switch is opened.
    if env.get("YOUTUBE_LIVE_ENABLED") != "0" or env.get("DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED") != "0":
        raise CanaryCLIError("canary_formal_gates_must_remain_disabled")
    work_root = Path(str(env.get("DRAMA_YOUTUBE_WORK_ROOT") or ""))
    if not work_root.is_absolute() or work_root.is_symlink():
        raise CanaryCLIError("canary_work_root_invalid")
    hosts = tuple(item.strip().lower() for item in str(env.get("DRAMA_YOUTUBE_SOURCE_HOSTS") or "").split(",") if item.strip())
    if not hosts or any(not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host for host in hosts):
        raise CanaryCLIError("canary_source_allowlist_required")
    return work_root, hosts


def _source_from_job(app: Any, job_id: str, source_kind: str, hosts: tuple[str, ...]) -> tuple[dict[str, Any], str]:
    job = dict(app.require_completed_drama_job(job_id))
    if str(job.get("job_id") or "") != job_id or str(job.get("app_id") or "") != CANARY_APP_ID:
        raise CanaryCLIError("canary_job_identity_mismatch")
    kind, source_url = app.drama_youtube_source(job, source_kind)
    parsed = urlsplit(str(source_url or ""))
    if kind != source_kind or parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.fragment:
        raise CanaryCLIError("canary_source_not_allowed")
    return job, str(source_url)


def _assert_frozen_source(record: Mapping[str, Any], job: Mapping[str, Any], source_kind: str, source_url: str) -> None:
    if any(str(record.get(key) or "") != str(expected) for key, expected in (
        ("job_id", job.get("job_id") or ""), ("content_id", job.get("content_id") or ""),
        ("source_kind", source_kind), ("source_url", source_url),
    )):
        raise CanaryCLIError("canary_frozen_source_conflict")


def prepare_canary(app: Any, args: argparse.Namespace, *, hosts: tuple[str, ...]) -> dict[str, Any]:
    validate_authorization(args)
    store = app.DRAMA_SYNTHESIS_STORE
    job, source_url = _source_from_job(app, args.job_id, args.source_kind, hosts)
    existing = store.youtube_canary_task()
    if existing is not None:
        _assert_frozen_source(existing, job, args.source_kind, source_url)
        return existing
    credential = app.drama_youtube_repository().credential(
        app_id=CANARY_APP_ID, channel_local_id=CANARY_CHANNEL_LOCAL_ID,
        account_id=CANARY_ACCOUNT_ID, expected_channel_id=CANARY_CHANNEL_ID,
    )
    if (
        credential.account_id != CANARY_ACCOUNT_ID or credential.channel_local_id != CANARY_CHANNEL_LOCAL_ID
        or credential.channel_id != CANARY_CHANNEL_ID or credential.channel_status != 1
        or not credential.capabilities["eligible"] or not credential.capabilities["comment_eligible"]
    ):
        raise CanaryCLIError("canary_credential_identity_invalid")
    link = store.ensure_short_link(args.job_id, args.source_kind, str(job.get("content_id") or ""), app.DRAMA_SHORT_LINK_PUBLISHER)
    short_url = str(link.get("short_url") or "")
    if not re.fullmatch(r"https://gy\.g2flow\.com/s2l/youtube/[1-9][0-9]*\.html", short_url):
        raise CanaryCLIError("canary_short_link_invalid")
    return store.enqueue_youtube_canary(
        job_id=args.job_id, content_id=str(job.get("content_id") or ""), source_kind=args.source_kind,
        source_url=source_url, description_rendered=CANARY_DESCRIPTION.replace("{{url}}", short_url),
        scopes=credential.scopes, operator_user_id=args.operator_user_id,
        operator_name="internal-deployment-canary",
    )


def run_canary(app: Any, args: argparse.Namespace, *, env: Mapping[str, str], engine=None, writer=None) -> dict[str, Any]:
    validate_authorization(args)
    work_root, hosts = validate_execution_env(env)
    store = app.DRAMA_SYNTHESIS_STORE
    record = store.youtube_canary_task()
    if record is None or int(record["id"]) != args.canary_task_id:
        raise CanaryCLIError("canary_task_mismatch")
    job, source_url = _source_from_job(app, record["job_id"], record["source_kind"], hosts)
    _assert_frozen_source(record, job, record["source_kind"], source_url)
    if writer is None:
        writer = build_unified_youtube_writer_from_env(env)
        if writer.executor is None:
            raise CanaryCLIError("canary_unified_writer_required")
    # Authentication, current primary identity, exact grants, schema and indexes
    # are checked read-only BEFORE any claim, OAuth refresh or upload attempt.
    writer.preflight()
    if engine is None:
        try:
            timeout = max(30, min(int(env.get("DRAMA_YOUTUBE_HTTP_TIMEOUT", "120")), 600))
        except (ValueError, TypeError):
            raise CanaryCLIError("canary_config_invalid") from None
        engine = YouTubePublishEngine(
            store, app.drama_youtube_repository(), YouTubeHTTPClient(timeout=timeout),
            work_root=work_root, allowed_source_hosts=hosts,
            ffprobe=env.get("DRAMA_YOUTUBE_FFPROBE", "/usr/bin/ffprobe"),
        )
    worker_id = "canary:%s:%s:%s" % (socket.gethostname(), os.getpid(), args.operator_user_id)
    engine.run_once(worker_id, canary_task_id=args.canary_task_id)
    record = store.youtube_canary_task()
    if (record["status"] != "published" or record["video_state"] != "published"
            or record["comment_status"] != "published" or int(record["unknown_outcome"])):
        return record
    verified_token = None
    for _ in range(3):
        record = store.youtube_canary_task()
        if record["sync_status"] == "synced":
            break
        try:
            verified_token = engine.verify_canary_sync(args.canary_task_id, token=verified_token)
        except (YouTubeHTTPError, DramaSynthesisError) as exc:
            store.hold_youtube_canary_sync(args.canary_task_id, exc.code)
            break
        except Exception:
            store.hold_youtube_canary_sync(args.canary_task_id, "youtube_canary_sync_preflight_unknown")
            break
        result = run_sync_outbox_once(store, writer, worker_id + ":sync", canary_task_id=args.canary_task_id)
        if not result.get("claimed") or result.get("status") != "synced":
            break
    return store.youtube_canary_task()


def safe_result(record: Mapping[str, Any] | None, action: str) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "action": action, "operation_id": CANARY_OPERATION_ID}
    if record is None:
        result.update(status="not_prepared", complete=False)
        return result
    if not is_youtube_canary(record):
        raise CanaryCLIError("canary_task_mismatch")
    result.update(
        task_id=int(record["id"]), app_id=int(CANARY_APP_ID), channel_local_id=int(CANARY_CHANNEL_LOCAL_ID),
        channel_id=CANARY_CHANNEL_ID, youtube_account_id=int(CANARY_ACCOUNT_ID), privacy_status="unlisted",
    )
    for key in ("status", "video_state", "comment_status", "sync_status"):
        result[key] = record.get(key) if record.get(key) in STATES else "unknown"
    for key in ("video_id", "comment_id"):
        value = str(record.get(key) or "")
        result[key] = value if re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value) else ""
    result["unknown_outcome"] = bool(record.get("unknown_outcome"))
    result["video_attempt_count"] = int(record.get("video_attempt_count") or 0)
    result["comment_attempt_count"] = int(record.get("comment_attempt_count") or 0)
    result["complete"] = bool(
        result["status"] == result["video_state"] == result["comment_status"] == "published"
        and result["sync_status"] == "synced" and not result["unknown_outcome"]
    )
    result["ok"] = not result["unknown_outcome"] and result["status"] != "failed" and result["sync_status"] != "failed"
    if not result["ok"]:
        if result["comment_status"] == "unknown":
            reason = "comment_requires_manual_review"
        elif record.get("error_code") == "youtube_canary_privacy_mismatch":
            reason = "unlisted_privacy_not_confirmed"
        elif result["unknown_outcome"]:
            reason = "original_upload_requires_reconciliation" if record.get("video_id") or record.get("resumable_session_uri") else "upload_identity_missing_no_replacement"
        elif result["sync_status"] == "failed":
            reason = "unified_sync_retry_required"
        else:
            reason = "operator_review_required"
        result["blocked_reason"] = reason
    return result


@contextlib.contextmanager
def quiet_execution(env: Mapping[str, str]):
    previous = dict(os.environ)
    disabled = logging.root.manager.disable
    try:
        os.environ.clear()
        os.environ.update(env)
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield
    finally:
        logging.disable(disabled)
        os.environ.clear()
        os.environ.update(previous)


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        validate_authorization(args)  # Before config/app/database access.
        env = load_environment(args.config_file, os.environ)
        path = ledger_path(env)
        record = read_canary_record(path, args.canary_task_id)
        if args.action != "status":
            _, hosts = validate_execution_env(env)
            with quiet_execution(env):
                app = importlib.import_module("app")
                if Path(app.JOB_DB_PATH).resolve() != path.resolve():
                    raise CanaryCLIError("canary_ledger_path_mismatch")
                if args.action == "prepare":
                    record = prepare_canary(app, args, hosts=hosts)
                else:
                    record = run_canary(app, args, env=env)
        result = safe_result(record, args.action)
    except CanaryCLIError as exc:
        result = {"ok": False, "code": exc.code}
    except DramaSynthesisError:
        result = {"ok": False, "code": "canary_operation_rejected"}
    except Exception:
        result = {"ok": False, "code": "canary_execution_failed"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
