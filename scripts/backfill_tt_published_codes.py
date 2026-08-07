#!/usr/bin/env python3
"""Backfill durable four-character routes for pre-migration TT posts.

The default mode is read-only discovery.  Apply is deliberately guarded by
explicit queue IDs, an exact candidate count, a plan SHA-256, and a verified
SQLite backup.  Only published rows in ``tt_post_queue`` are eligible;
``tt_post_direct_test`` is never read or written.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_posts.code_routes import (  # noqa: E402
    CODE_ALPHABET,
    CODE_LENGTH,
    allocate_code_route,
)
from features.tt_posts.links import (  # noqa: E402
    TTPostLinkError,
    build_w2a_url_from_fields,
    validate_w2a_url,
)


UTC = timezone.utc
_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ROUTE_QUERY_FIELDS = (
    "af_dp",
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
)
_QUEUE_COLUMNS = {
    "id",
    "scheduled_at_utc",
    "account_id",
    "account_username",
    "creator_username_snapshot",
    "content_id",
    "material_id",
    "long_url",
    "code",
    "status",
    "publish_id",
    "created_at",
    "updated_at",
}
_EVENT_COLUMNS = {"id", "queue_id", "to_status", "created_at"}
_ROUTE_COLUMNS = {
    "code",
    "queue_id",
    "content_id",
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
    "long_url",
    "state",
    "created_at",
    "published_at",
    "updated_at",
}


class TTCodeBackfillError(RuntimeError):
    """Stable fail-closed error for the one-shot backfill."""

    def __init__(self, code: str, message: str):
        self.code = str(code or "tt_code_backfill_failed")[:96]
        super().__init__(str(message or "TT code backfill failed")[:500])


def _database_path(value: Any, *, must_exist: bool = True) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise TTCodeBackfillError(
            "tt_code_backfill_db_path_invalid",
            "database path must be absolute",
        )
    resolved = path.resolve()
    if must_exist and not resolved.is_file():
        raise TTCodeBackfillError(
            "tt_code_backfill_db_missing",
            "database file does not exist",
        )
    return resolved


@contextlib.contextmanager
def _connection(path: Path, *, read_only: bool) -> Iterable[sqlite3.Connection]:
    uri = path.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
    try:
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=10,
            isolation_level=None,
        )
    except sqlite3.Error:
        raise TTCodeBackfillError(
            "tt_code_backfill_db_unavailable",
            "database is unavailable",
        ) from None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("PRAGMA query_only").fetchone()
            if row is None or int(row[0]) != 1:
                raise TTCodeBackfillError(
                    "tt_code_backfill_query_only_failed",
                    "database did not enter query-only mode",
                )
        yield conn
    except TTCodeBackfillError:
        raise
    except sqlite3.Error:
        raise TTCodeBackfillError(
            "tt_code_backfill_db_error",
            "database operation failed",
        ) from None
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _validate_schema(conn: sqlite3.Connection) -> None:
    names = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "tt_post_queue": _QUEUE_COLUMNS,
        "tt_post_event": _EVENT_COLUMNS,
        "tt_post_code_route": _ROUTE_COLUMNS,
    }
    for table, columns in required.items():
        if table not in names or not columns.issubset(_table_columns(conn, table)):
            raise TTCodeBackfillError(
                "tt_code_backfill_schema_invalid",
                "required TT post schema is unavailable",
            )
    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or str(quick_check[0]).lower() != "ok":
        raise TTCodeBackfillError(
            "tt_code_backfill_integrity_failed",
            "database quick_check failed",
        )


def _queue_ids(values: Iterable[Any]) -> List[int]:
    normalized: List[int] = []
    seen = set()
    for value in values:
        if isinstance(value, bool):
            queue_id = 0
        else:
            try:
                queue_id = int(value)
            except (TypeError, ValueError, OverflowError):
                queue_id = 0
        if queue_id <= 0:
            raise TTCodeBackfillError(
                "tt_code_backfill_queue_id_invalid",
                "queue IDs must be positive integers",
            )
        if queue_id not in seen:
            seen.add(queue_id)
            normalized.append(queue_id)
    if len(normalized) > 10_000:
        raise TTCodeBackfillError(
            "tt_code_backfill_queue_batch_too_large",
            "too many queue IDs were requested",
        )
    return sorted(normalized)


def _utc_iso(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise TTCodeBackfillError(
            "tt_code_backfill_timestamp_invalid",
            "%s is invalid" % label,
        )
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _route_from_queue(row: Mapping[str, Any]) -> Dict[str, str]:
    queue_id = int(row["id"])
    content_id = str(row["content_id"] or "").strip()
    original_long_url = str(row["long_url"] or "").strip()
    if not original_long_url:
        raise TTCodeBackfillError(
            "tt_code_backfill_long_url_missing",
            "queue %s has no frozen W2A URL" % queue_id,
        )
    try:
        validated = validate_w2a_url(original_long_url)
        pairs = parse_qsl(urlsplit(validated).query, keep_blank_values=True)
    except (TTPostLinkError, TypeError, ValueError):
        raise TTCodeBackfillError(
            "tt_code_backfill_long_url_invalid",
            "queue %s has an invalid frozen W2A URL" % queue_id,
        ) from None
    if len(pairs) != len(_ROUTE_QUERY_FIELDS) or set(
        key for key, _value in pairs
    ) != set(_ROUTE_QUERY_FIELDS):
        raise TTCodeBackfillError(
            "tt_code_backfill_long_url_invalid",
            "queue %s has incomplete frozen attribution" % queue_id,
        )
    fields = dict(pairs)
    if fields.get("af_channel") != "AIpost":
        raise TTCodeBackfillError(
            "tt_code_backfill_channel_invalid",
            "queue %s is not a pre-migration AIpost route" % queue_id,
        )
    if fields.get("af_dp") != content_id or fields.get("af_c_id") != str(queue_id):
        raise TTCodeBackfillError(
            "tt_code_backfill_identity_mismatch",
            "queue %s frozen attribution identity does not match" % queue_id,
        )
    try:
        target = build_w2a_url_from_fields(
            {
                "af_dp": fields["af_dp"],
                "c": fields["c"],
                "af_adset": fields["af_adset"],
                "af_adset_id": fields["af_adset_id"],
                "af_ad": fields["af_ad"],
                "af_ad_id": fields["af_ad_id"],
                "af_c_id": fields["af_c_id"],
            },
            channel="TT",
        )
    except (TTPostLinkError, TypeError, ValueError):
        raise TTCodeBackfillError(
            "tt_code_backfill_route_invalid",
            "queue %s attribution route cannot be rebuilt" % queue_id,
        ) from None
    target_fields = dict(parse_qsl(urlsplit(target).query, keep_blank_values=True))
    created_at = _utc_iso(row["created_at"], "queue created_at")
    published_at = _utc_iso(row["route_published_at"], "queue published_at")
    return {
        "content_id": content_id,
        "c": target_fields["c"],
        "af_adset": target_fields["af_adset"],
        "af_adset_id": target_fields["af_adset_id"],
        "af_ad": target_fields["af_ad"],
        "af_ad_id": target_fields["af_ad_id"],
        "af_channel": "TT",
        "af_c_id": target_fields["af_c_id"],
        "long_url": target,
        "state": "published",
        "created_at": created_at,
        "published_at": published_at,
        "updated_at": published_at,
    }


def build_plan(
    conn: sqlite3.Connection,
    *,
    queue_ids: Sequence[Any] = (),
) -> List[Dict[str, Any]]:
    """Build a deterministic plan without mutating SQLite."""

    _validate_schema(conn)
    normalized_ids = _queue_ids(queue_ids)
    clauses = ["q.status='published'", "trim(q.publish_id)<>''", "trim(q.code)='' "]
    params: List[Any] = []
    if normalized_ids:
        clauses.append("q.id IN (%s)" % ",".join("?" for _item in normalized_ids))
        params.extend(normalized_ids)
    rows = conn.execute(
        """
        SELECT q.id,q.scheduled_at_utc,q.account_id,q.account_username,
               q.creator_username_snapshot,q.content_id,q.material_id,
               q.long_url,q.code,q.status,q.publish_id,q.created_at,q.updated_at,
               COALESCE(
                   (SELECT e.created_at FROM tt_post_event e
                    WHERE e.queue_id=q.id AND e.to_status='published'
                    ORDER BY e.id DESC LIMIT 1),
                   q.updated_at
               ) AS route_published_at
        FROM tt_post_queue q
        WHERE %s
        ORDER BY q.id
        """ % " AND ".join(clauses),
        tuple(params),
    ).fetchall()
    if normalized_ids and [int(row["id"]) for row in rows] != normalized_ids:
        raise TTCodeBackfillError(
            "tt_code_backfill_candidate_changed",
            "one or more explicit queue IDs are no longer eligible",
        )

    plan: List[Dict[str, Any]] = []
    for row in rows:
        queue_id = int(row["id"])
        if conn.execute(
            "SELECT 1 FROM tt_post_code_route WHERE queue_id=?", (queue_id,)
        ).fetchone() is not None:
            raise TTCodeBackfillError(
                "tt_code_backfill_route_conflict",
                "queue %s already has a route while its code is empty" % queue_id,
            )
        plan.append(
            {
                "queue_id": queue_id,
                "scheduled_at_utc": str(row["scheduled_at_utc"] or ""),
                "account_id": str(row["account_id"] or ""),
                "account_username": str(
                    row["creator_username_snapshot"]
                    or row["account_username"]
                    or ""
                ),
                "content_id": str(row["content_id"] or ""),
                "material_id": str(row["material_id"] or ""),
                "publish_id": str(row["publish_id"] or ""),
                "frozen_long_url": str(row["long_url"] or ""),
                "route": _route_from_queue(row),
            }
        )
    return plan


def plan_hash(plan: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def public_plan(plan: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    keys = (
        "queue_id",
        "scheduled_at_utc",
        "account_id",
        "account_username",
        "content_id",
        "material_id",
        "publish_id",
    )
    return [{key: item[key] for key in keys} for item in plan]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source_path: Any, backup_path: Any) -> Dict[str, Any]:
    source = _database_path(source_path)
    backup = _database_path(backup_path, must_exist=False)
    if backup.parent.resolve() == source.parent.resolve() and backup.name == source.name:
        raise TTCodeBackfillError(
            "tt_code_backfill_backup_invalid",
            "backup path must differ from the source database",
        )
    if not backup.parent.is_dir():
        raise TTCodeBackfillError(
            "tt_code_backfill_backup_parent_missing",
            "backup parent directory does not exist",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(str(backup), flags, 0o600)
    except FileExistsError:
        raise TTCodeBackfillError(
            "tt_code_backfill_backup_exists",
            "backup path already exists",
        ) from None
    except OSError:
        raise TTCodeBackfillError(
            "tt_code_backfill_backup_create_failed",
            "backup file cannot be created",
        ) from None
    os.close(descriptor)
    created_stat = backup.stat()
    try:
        with _connection(source, read_only=True) as source_conn:
            _validate_schema(source_conn)
            with contextlib.closing(sqlite3.connect(str(backup))) as backup_conn:
                source_conn.backup(backup_conn)
                check = backup_conn.execute("PRAGMA quick_check").fetchone()
                if check is None or str(check[0]).lower() != "ok":
                    raise TTCodeBackfillError(
                        "tt_code_backfill_backup_integrity_failed",
                        "backup quick_check failed",
                    )
    except Exception:
        try:
            current_stat = backup.stat()
            if (
                current_stat.st_dev == created_stat.st_dev
                and current_stat.st_ino == created_stat.st_ino
            ):
                backup.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return {
        "path": str(backup),
        "sha256": _file_sha256(backup),
        "size": backup.stat().st_size,
    }


def apply_plan(
    db_path: Any,
    *,
    queue_ids: Sequence[Any],
    expected_count: Any,
    expected_hash: Any,
    choice_fn: Optional[Callable[[Sequence[str]], str]] = None,
) -> List[Dict[str, Any]]:
    path = _database_path(db_path)
    normalized_ids = _queue_ids(queue_ids)
    if not normalized_ids:
        raise TTCodeBackfillError(
            "tt_code_backfill_queue_ids_required",
            "apply requires explicit queue IDs",
        )
    try:
        count = int(expected_count)
    except (TypeError, ValueError, OverflowError):
        count = -1
    digest = str(expected_hash or "").strip().lower()
    if count < 0 or not _SHA256_RE.fullmatch(digest):
        raise TTCodeBackfillError(
            "tt_code_backfill_guard_invalid",
            "expected count and SHA-256 are required",
        )

    written: List[Dict[str, Any]] = []
    with _connection(path, read_only=False) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = build_plan(conn, queue_ids=normalized_ids)
            current_hash = plan_hash(plan)
            if len(plan) != count or current_hash != digest:
                raise TTCodeBackfillError(
                    "tt_code_backfill_plan_changed",
                    "candidate plan changed; refusing to apply",
                )
            route_count = int(
                conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0]
            )
            if route_count + len(plan) > len(CODE_ALPHABET) ** CODE_LENGTH:
                raise TTCodeBackfillError(
                    "tt_code_backfill_capacity_exhausted",
                    "code capacity is insufficient; refusing to recycle a route",
                )
            for item in plan:
                kwargs = {} if choice_fn is None else {"choice_fn": choice_fn}
                route = allocate_code_route(
                    conn,
                    int(item["queue_id"]),
                    item["route"],
                    **kwargs,
                )
                code = str(route.get("code") or "").strip().upper()
                if not _CODE_RE.fullmatch(code):
                    raise TTCodeBackfillError(
                        "tt_code_backfill_code_invalid",
                        "allocator returned an invalid code",
                    )
                updated = conn.execute(
                    """
                    UPDATE tt_post_queue SET code=?
                    WHERE id=? AND status='published' AND publish_id=?
                      AND content_id=? AND long_url=? AND code=''
                    """,
                    (
                        code,
                        int(item["queue_id"]),
                        item["publish_id"],
                        item["content_id"],
                        item["frozen_long_url"],
                    ),
                )
                if updated.rowcount != 1:
                    raise TTCodeBackfillError(
                        "tt_code_backfill_update_conflict",
                        "queue %s changed during apply" % item["queue_id"],
                    )
                written.append(
                    {
                        **public_plan([item])[0],
                        "code": code,
                    }
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return written


def inspect_database(db_path: Any, *, queue_ids: Sequence[Any] = ()) -> Dict[str, Any]:
    path = _database_path(db_path)
    with _connection(path, read_only=True) as conn:
        plan = build_plan(conn, queue_ids=queue_ids)
    return {
        "mode": "dry-run",
        "candidate_count": len(plan),
        "plan_sha256": plan_hash(plan),
        "candidates": public_plan(plan),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-path",
        default=os.environ.get("TT_POST_DB_PATH", ""),
        required=not bool(os.environ.get("TT_POST_DB_PATH")),
    )
    parser.add_argument("--queue-id", action="append", type=int, default=[])
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-hash")
    parser.add_argument("--backup-path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    try:
        if not args.apply:
            result = inspect_database(args.db_path, queue_ids=args.queue_id)
        else:
            if (
                not args.queue_id
                or args.expected_count is None
                or not args.expected_hash
                or not args.backup_path
            ):
                raise TTCodeBackfillError(
                    "tt_code_backfill_apply_guard_missing",
                    "apply requires queue IDs, expected count/hash, and backup path",
                )
            backup = backup_database(args.db_path, args.backup_path)
            written = apply_plan(
                args.db_path,
                queue_ids=args.queue_id,
                expected_count=args.expected_count,
                expected_hash=args.expected_hash,
            )
            result = {
                "mode": "apply",
                "written_count": len(written),
                "plan_sha256": str(args.expected_hash).lower(),
                "backup": backup,
                "items": written,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except TTCodeBackfillError as exc:
        print(
            json.dumps(
                {"ok": False, "error_code": exc.code, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "tt_code_backfill_unexpected_error",
                    "message": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
