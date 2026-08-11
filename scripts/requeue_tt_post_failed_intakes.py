#!/usr/bin/env python3
"""Safely requeue terminal TT Post intake failures for a newer media profile.

The command is a dry run unless ``--apply`` is supplied. Apply mode requires
the exact candidate-set SHA-256 printed by the dry run, writes an audit row for
every changed intake, and never creates a TikTok publish request. The normal
preparation runner performs GPU work after the operator restores its timer.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.upgrade_tt_post_recurring_profile import (  # noqa: E402
    preparation_job_id,
)


PRODUCTION_DB_PATH = "/mnt/data-disk/tt-post-publisher/tt-post.sqlite3"
PRODUCTION_AUTO_DB_PATH = (
    "/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3"
)
DEFAULT_LOCK_PATH = "/run/tt-post/requeue-failed-intakes.lock"
DEFAULT_SOURCE_PROFILE = "tt-post-source-direct-v1"
DEFAULT_ERROR_CODE = "prepared_media_invalid"
DEFAULT_LANGUAGE = "en"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9._:@+-]{8,160}$")


class FailedIntakeRecoveryError(RuntimeError):
    """Bounded, secret-safe recovery failure."""

    def __init__(self, code: str, message: str):
        self.code = str(code or "tt_post_failed_intake_recovery_failed")[:96]
        super().__init__(str(message or "TT Post intake recovery failed")[:500])


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _connect(path: str, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        uri = "file:%s?mode=ro" % Path(path).resolve().as_posix()
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(Path(path).resolve()), timeout=30)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def _guard_production_path(path: str, expected: str, allow_any: bool) -> None:
    if allow_any:
        return
    if os.path.realpath(path) != os.path.realpath(expected):
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_path_invalid",
            "Recovery path is not the reviewed production database",
        )


def _normalize_profile(value: Any, field: str) -> str:
    profile = str(value or "").strip()
    if not profile or len(profile) > 128 or any(ord(char) < 32 for char in profile):
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_config_invalid",
            "%s is invalid" % field,
        )
    return profile


def _normalize_trim(value: Any) -> float:
    try:
        trim = float(value)
    except (TypeError, ValueError, OverflowError):
        trim = -1
    if not math.isfinite(trim) or trim < 0 or trim >= 86400:
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_config_invalid",
            "Target source trim is invalid",
        )
    return round(trim, 6)


def _select_candidates(
    conn: sqlite3.Connection,
    *,
    source_profile: str,
    error_code: str,
    language: str,
    limit: int,
) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM tt_post_material_intake
        WHERE status='failed'
          AND recurring_pool_id IS NULL
          AND preparation_profile=?
          AND error_code=?
          AND lower(trim(material_language))=?
        ORDER BY id
        LIMIT ?
        """,
        (source_profile, error_code, language.lower(), limit),
    ).fetchall()


def _candidate_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = []
    for row in rows:
        payload.append(
            {
                "account_id": str(row["account_id"] or ""),
                "attempt_count": int(row["attempt_count"] or 0),
                "caption_sha256": hashlib.sha256(
                    str(row["caption"] or "").encode("utf-8")
                ).hexdigest(),
                "content_id": str(row["content_id"] or ""),
                "error_code": str(row["error_code"] or ""),
                "gpu_job_id": str(row["gpu_job_id"] or ""),
                "id": int(row["id"]),
                "material_id": str(row["material_id"] or ""),
                "preparation_profile": str(row["preparation_profile"] or ""),
                "request_sha256": str(row["request_sha256"] or ""),
                "source_media_url_sha256": hashlib.sha256(
                    str(row["source_media_url"] or "").encode("utf-8")
                ).hexdigest(),
                "status": str(row["status"] or ""),
            }
        )
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _material_conflicts(
    post_conn: sqlite3.Connection,
    auto_db_path: str,
    material_ids: Sequence[str],
) -> List[Dict[str, str]]:
    if not material_ids:
        return []
    placeholders = ",".join("?" for _ in material_ids)
    conflicts: List[Dict[str, str]] = []
    for table in (
        "tt_post_recurring_pool",
        "tt_post_material_pool",
        "tt_post_queue",
        "tt_post_direct_test",
    ):
        rows = post_conn.execute(
            "SELECT material_id FROM %s WHERE material_id IN (%s)"
            % (table, placeholders),
            tuple(material_ids),
        ).fetchall()
        conflicts.extend(
            {"material_id": str(row[0]), "source": table} for row in rows
        )
    with contextlib.closing(_connect(auto_db_path, read_only=True)) as auto_conn:
        rows = auto_conn.execute(
            """
            SELECT material_id
            FROM tt_auto_material_ledger
            WHERE material_id IN (%s)
            """
            % placeholders,
            tuple(material_ids),
        ).fetchall()
        conflicts.extend(
            {"material_id": str(row[0]), "source": "tt_auto_material_ledger"}
            for row in rows
        )
    return sorted(conflicts, key=lambda item: (int(item["material_id"]), item["source"]))


def _new_request_sha256(
    row: Mapping[str, Any],
    *,
    target_profile: str,
    target_job_id: str,
    source_trim_tail_seconds: float,
) -> str:
    frozen_payload = {
        "account_id": str(row["account_id"] or ""),
        "caption": str(row["caption"] or ""),
        "caption_template": str(row["caption_template"] or ""),
        "consent_version": str(row["consent_version"] or ""),
        "consented_at_utc": str(row["consented_at_utc"] or ""),
        "content_id": str(row["content_id"] or ""),
        "description": str(row["description"] or ""),
        "drama_name": str(row["drama_name"] or ""),
        "gpu_job_id": target_job_id,
        "is_aigc": bool(row["is_aigc"]),
        "material_id": str(row["material_id"] or ""),
        "material_language": str(row["material_language"] or ""),
        "material_name": str(row["material_name"] or ""),
        "material_tag": str(row["material_tag"] or ""),
        "preparation_profile": target_profile,
        "source_media_url": str(row["source_media_url"] or ""),
        "source_trim_tail_seconds": source_trim_tail_seconds,
    }
    return hashlib.sha256(
        json.dumps(
            frozen_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _public_plan(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_profile: str,
    target_profile: str,
    source_trim_tail_seconds: float,
    error_code: str,
    language: str,
    applied: bool,
) -> Dict[str, Any]:
    return {
        "applied": bool(applied),
        "candidate_count": len(rows),
        "candidate_intake_ids": [int(row["id"]) for row in rows],
        "candidate_material_ids": [str(row["material_id"]) for row in rows],
        "candidate_set_sha256": _candidate_fingerprint(rows),
        "error_code": error_code,
        "language": language,
        "source_profile": source_profile,
        "source_trim_tail_seconds": source_trim_tail_seconds,
        "target_profile": target_profile,
    }


def plan_recovery(
    db_path: str,
    auto_db_path: str,
    *,
    source_profile: str,
    target_profile: str,
    source_trim_tail_seconds: float,
    error_code: str = DEFAULT_ERROR_CODE,
    language: str = DEFAULT_LANGUAGE,
    limit: int = 500,
    allow_any_db_path: bool = False,
) -> Dict[str, Any]:
    _guard_production_path(db_path, PRODUCTION_DB_PATH, allow_any_db_path)
    _guard_production_path(auto_db_path, PRODUCTION_AUTO_DB_PATH, allow_any_db_path)
    source_profile = _normalize_profile(source_profile, "Source profile")
    target_profile = _normalize_profile(target_profile, "Target profile")
    trim = _normalize_trim(source_trim_tail_seconds)
    if source_profile == target_profile or limit < 1 or limit > 500:
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_config_invalid",
            "Recovery source/target profile or limit is invalid",
        )
    with contextlib.closing(_connect(db_path, read_only=True)) as conn:
        rows = _select_candidates(
            conn,
            source_profile=source_profile,
            error_code=error_code,
            language=language,
            limit=limit,
        )
        conflicts = _material_conflicts(
            conn,
            auto_db_path,
            [str(row["material_id"]) for row in rows],
        )
    if conflicts:
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_lineage_conflict",
            "Candidate material already exists in a publish ledger: %s"
            % json.dumps(conflicts, sort_keys=True),
        )
    return _public_plan(
        rows,
        source_profile=source_profile,
        target_profile=target_profile,
        source_trim_tail_seconds=trim,
        error_code=error_code,
        language=language,
        applied=False,
    )


def _ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tt_post_material_intake_recovery_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            candidate_set_sha256 TEXT NOT NULL,
            intake_id INTEGER NOT NULL,
            material_id TEXT NOT NULL,
            from_status TEXT NOT NULL,
            from_profile TEXT NOT NULL,
            from_gpu_job_id TEXT NOT NULL,
            from_request_sha256 TEXT NOT NULL,
            from_attempt_count INTEGER NOT NULL,
            from_error_code TEXT NOT NULL,
            from_error_message TEXT NOT NULL,
            to_profile TEXT NOT NULL,
            to_gpu_job_id TEXT NOT NULL,
            to_request_sha256 TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(operation_id, intake_id),
            FOREIGN KEY(intake_id) REFERENCES tt_post_material_intake(id)
        )
        """
    )


@contextlib.contextmanager
def process_lock(path: str) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            yield
            return
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FailedIntakeRecoveryError(
                "tt_post_failed_intake_recovery_busy",
                "Another failed-intake recovery is running",
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def apply_recovery(
    db_path: str,
    auto_db_path: str,
    *,
    expected_candidate_sha256: str,
    source_profile: str,
    target_profile: str,
    source_trim_tail_seconds: float,
    error_code: str = DEFAULT_ERROR_CODE,
    language: str = DEFAULT_LANGUAGE,
    limit: int = 500,
    actor: str = "codex-operator",
    operation_id: str = "",
    lock_path: str = DEFAULT_LOCK_PATH,
    allow_any_db_path: bool = False,
) -> Dict[str, Any]:
    _guard_production_path(db_path, PRODUCTION_DB_PATH, allow_any_db_path)
    _guard_production_path(auto_db_path, PRODUCTION_AUTO_DB_PATH, allow_any_db_path)
    if not SHA256_RE.fullmatch(str(expected_candidate_sha256 or "")):
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_hash_required",
            "Exact dry-run candidate SHA-256 is required",
        )
    actor = str(actor or "").strip()
    if not OPERATION_ID_RE.fullmatch(actor):
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_actor_invalid",
            "Recovery actor is invalid",
        )
    operation_id = str(operation_id or "").strip() or (
        "tt-post-failed-intake-recovery-"
        + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    if not OPERATION_ID_RE.fullmatch(operation_id):
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_operation_invalid",
            "Recovery operation ID is invalid",
        )
    source_profile = _normalize_profile(source_profile, "Source profile")
    target_profile = _normalize_profile(target_profile, "Target profile")
    trim = _normalize_trim(source_trim_tail_seconds)
    if source_profile == target_profile or limit < 1 or limit > 500:
        raise FailedIntakeRecoveryError(
            "tt_post_failed_intake_config_invalid",
            "Recovery source/target profile or limit is invalid",
        )
    with process_lock(lock_path):
        with contextlib.closing(_connect(db_path, read_only=False)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM tt_post_material_intake
                    WHERE status IN ('queued','preparing','retry_wait')
                    """
                ).fetchone()[0]
                if int(active) != 0:
                    raise FailedIntakeRecoveryError(
                        "tt_post_failed_intake_inflight",
                        "Preparation work is still active",
                    )
                rows = _select_candidates(
                    conn,
                    source_profile=source_profile,
                    error_code=error_code,
                    language=language,
                    limit=limit,
                )
                if not rows:
                    raise FailedIntakeRecoveryError(
                        "tt_post_failed_intake_candidates_empty",
                        "No failed intake matches the reviewed recovery set",
                    )
                conflicts = _material_conflicts(
                    conn,
                    auto_db_path,
                    [str(row["material_id"]) for row in rows],
                )
                if conflicts:
                    raise FailedIntakeRecoveryError(
                        "tt_post_failed_intake_lineage_conflict",
                        "Candidate material already exists in a publish ledger",
                    )
                actual_hash = _candidate_fingerprint(rows)
                if not hmac.compare_digest(actual_hash, expected_candidate_sha256):
                    raise FailedIntakeRecoveryError(
                        "tt_post_failed_intake_candidate_changed",
                        "Candidate set changed after dry run",
                    )
                _ensure_audit_table(conn)
                timestamp = _utc_now()
                requeued_ids: List[int] = []
                for row in rows:
                    target_job_id = preparation_job_id(row, target_profile, trim)
                    target_request_sha = _new_request_sha256(
                        row,
                        target_profile=target_profile,
                        target_job_id=target_job_id,
                        source_trim_tail_seconds=trim,
                    )
                    conn.execute(
                        """
                        INSERT INTO tt_post_material_intake_recovery_audit(
                            operation_id,candidate_set_sha256,intake_id,material_id,
                            from_status,from_profile,from_gpu_job_id,
                            from_request_sha256,from_attempt_count,
                            from_error_code,from_error_message,to_profile,
                            to_gpu_job_id,to_request_sha256,actor,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            operation_id,
                            actual_hash,
                            int(row["id"]),
                            str(row["material_id"]),
                            str(row["status"]),
                            str(row["preparation_profile"]),
                            str(row["gpu_job_id"]),
                            str(row["request_sha256"]),
                            int(row["attempt_count"] or 0),
                            str(row["error_code"] or ""),
                            str(row["error_message"] or ""),
                            target_profile,
                            target_job_id,
                            target_request_sha,
                            actor,
                            timestamp,
                        ),
                    )
                    cursor = conn.execute(
                        """
                        UPDATE tt_post_material_intake
                        SET request_sha256=?,gpu_job_id=?,
                            source_trim_tail_seconds=?,preparation_profile=?,
                            status='queued',attempt_count=0,next_attempt_at_utc='',
                            claim_worker='',claim_token='',
                            lease_expires_at_utc='',prepared_media_url='',
                            prepared_output_sha256='',prepared_output_size=0,
                            prepared_duration_sec=0,recurring_pool_id=NULL,
                            error_code='',error_message='',claimed_at_utc='',
                            ready_at_utc='',failed_at_utc='',canceled_at_utc='',
                            updated_by_user_id=?,
                            updated_by_name=?,updated_at=?
                        WHERE id=? AND status='failed'
                          AND recurring_pool_id IS NULL
                          AND preparation_profile=?
                          AND gpu_job_id=?
                          AND request_sha256=?
                          AND error_code=?
                        """,
                        (
                            target_request_sha,
                            target_job_id,
                            trim,
                            target_profile,
                            actor,
                            actor,
                            timestamp,
                            int(row["id"]),
                            str(row["preparation_profile"]),
                            str(row["gpu_job_id"]),
                            str(row["request_sha256"]),
                            error_code,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise FailedIntakeRecoveryError(
                            "tt_post_failed_intake_fence_failed",
                            "Failed intake changed during recovery",
                        )
                    requeued_ids.append(int(row["id"]))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    result = _public_plan(
        rows,
        source_profile=source_profile,
        target_profile=target_profile,
        source_trim_tail_seconds=trim,
        error_code=error_code,
        language=language,
        applied=True,
    )
    result.update(
        {
            "operation_id": operation_id,
            "requeued_count": len(requeued_ids),
            "requeued_intake_ids": requeued_ids,
        }
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=os.environ.get("TT_POST_DB_PATH", PRODUCTION_DB_PATH))
    parser.add_argument("--auto-db-path", default=PRODUCTION_AUTO_DB_PATH)
    parser.add_argument("--from-profile", default=DEFAULT_SOURCE_PROFILE)
    parser.add_argument("--to-profile", default=os.environ.get("TT_POST_MEDIA_PROFILE_VERSION", ""), required=not bool(os.environ.get("TT_POST_MEDIA_PROFILE_VERSION")))
    parser.add_argument("--source-trim-tail-seconds", type=float, default=float(os.environ.get("TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS", "0")))
    parser.add_argument("--error-code", default=DEFAULT_ERROR_CODE)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--expected-candidate-sha256", default="")
    parser.add_argument("--actor", default="codex-operator")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    parser.add_argument("--allow-any-db-path", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    common = dict(
        source_profile=args.from_profile,
        target_profile=args.to_profile,
        source_trim_tail_seconds=args.source_trim_tail_seconds,
        error_code=args.error_code,
        language=args.language,
        limit=args.limit,
        allow_any_db_path=args.allow_any_db_path,
    )
    if args.apply:
        result = apply_recovery(
            args.db_path,
            args.auto_db_path,
            expected_candidate_sha256=args.expected_candidate_sha256,
            actor=args.actor,
            operation_id=args.operation_id,
            lock_path=args.lock_path,
            **common,
        )
    else:
        result = plan_recovery(args.db_path, args.auto_db_path, **common)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FailedIntakeRecoveryError as exc:
        print(
            json.dumps(
                {"code": exc.code, "error": str(exc), "ok": False},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
