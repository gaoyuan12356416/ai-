#!/usr/bin/env python3
"""Backfill durable four-character routes for pre-migration TT posts.

The default mode is read-only discovery.  Apply is deliberately guarded by
explicit queue IDs, an exact candidate count, a plan SHA-256, and a verified
SQLite backup.  Rows that predate frozen links additionally require exact,
per-ID ledger reconstruction opt-in.  Only published rows in
``tt_post_queue`` are eligible; ``tt_post_direct_test`` is never read or
written.
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
    TT_SHORT_LINK_NAMESPACE,
    TTPostLinkError,
    build_w2a_url,
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
    "account_display_name",
    "creator_nickname_snapshot",
    "creator_username_snapshot",
    "content_id",
    "material_id",
    "long_url",
    "short_link_id",
    "short_url",
    "material_name",
    "drama_name",
    "material_language",
    "material_tag",
    "code",
    "status",
    "publish_id",
    "created_at",
    "updated_at",
}
_EVENT_COLUMNS = {
    "id",
    "queue_id",
    "event_type",
    "to_status",
    "details_json",
    "created_at",
}
_RECURRING_COLUMNS = {
    "id",
    "queue_id",
    "run_id",
    "material_id",
    "account_id",
    "content_id",
    "material_name",
    "drama_name",
    "material_language",
    "routing_language",
    "material_tag",
    "status",
}
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
        "tt_post_recurring_pool": _RECURRING_COLUMNS,
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


def _canonical_routing_language(value: Any, queue_id: int) -> str:
    text = str(value or "").strip()
    normalized = text.casefold().replace("_", "-")
    if (
        not text
        or text != normalized
        or len(text) > 32
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", text) is None
    ):
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_language_invalid",
            "queue %s recurring routing language is not canonical" % queue_id,
        )
    return text


def _frozen_or_fallback(
    row: Mapping[str, Any],
    recurring: Mapping[str, Any],
    field: str,
    fallback_value: str,
    fallback_source: str,
) -> tuple[str, str]:
    queue_id = int(row["id"])
    queue_value = str(row[field] or "").strip()
    recurring_value = str(recurring[field] or "").strip()
    if queue_value and recurring_value and queue_value != recurring_value:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_metadata_conflict",
            "queue %s frozen %s values conflict" % (queue_id, field),
        )
    if queue_value:
        return queue_value, "queue.%s" % field
    if recurring_value:
        return recurring_value, "recurring_pool.%s" % field
    return fallback_value, fallback_source


def _event_details(value: Any, queue_id: int) -> tuple[Mapping[str, Any], str]:
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, Mapping):
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_event_invalid",
            "queue %s publish event details are invalid" % queue_id,
        )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return parsed, digest


def _route_from_target(
    row: Mapping[str, Any],
    target: Any,
    *,
    published_at_value: Any = None,
) -> Dict[str, str]:
    queue_id = int(row["id"])
    content_id = str(row["content_id"] or "").strip()
    try:
        validated = validate_w2a_url(target)
        target_pairs = parse_qsl(urlsplit(validated).query, keep_blank_values=True)
    except (TTPostLinkError, TypeError, ValueError):
        raise TTCodeBackfillError(
            "tt_code_backfill_route_invalid",
            "queue %s reconstructed route is invalid" % queue_id,
        ) from None
    if len(target_pairs) != len(_ROUTE_QUERY_FIELDS) or tuple(
        key for key, _value in target_pairs
    ) != _ROUTE_QUERY_FIELDS:
        raise TTCodeBackfillError(
            "tt_code_backfill_route_invalid",
            "queue %s reconstructed route fields are invalid" % queue_id,
        )
    target_fields = dict(target_pairs)
    if (
        target_fields.get("af_channel") != "TT"
        or target_fields.get("af_dp") != content_id
        or target_fields.get("af_c_id") != str(queue_id)
        or target_fields.get("af_ad_id") != str(row["material_id"] or "")
        or target_fields.get("af_adset_id") != str(row["account_id"] or "")
    ):
        raise TTCodeBackfillError(
            "tt_code_backfill_identity_mismatch",
            "queue %s reconstructed attribution identity does not match" % queue_id,
        )
    created_at = _utc_iso(row["created_at"], "queue created_at")
    published_at = _utc_iso(
        row["route_published_at"]
        if published_at_value is None
        else published_at_value,
        "queue published_at",
    )
    return {
        "content_id": content_id,
        "c": target_fields["c"],
        "af_adset": target_fields["af_adset"],
        "af_adset_id": target_fields["af_adset_id"],
        "af_ad": target_fields["af_ad"],
        "af_ad_id": target_fields["af_ad_id"],
        "af_channel": "TT",
        "af_c_id": target_fields["af_c_id"],
        "long_url": validated,
        "state": "published",
        "created_at": created_at,
        "published_at": published_at,
        "updated_at": published_at,
    }


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
    return _route_from_target(row, target)


def _route_from_ledger(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> tuple[Dict[str, str], Dict[str, Any]]:
    queue_id = int(row["id"])
    if (
        str(row["long_url"] or "")
        or int(row["short_link_id"] or 0) != 0
        or str(row["short_url"] or "")
    ):
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_not_legacy",
            "queue %s is not a pre-link ledger row" % queue_id,
        )

    recurring_rows = conn.execute(
        """
        SELECT id,queue_id,run_id,material_id,account_id,content_id,
               material_name,drama_name,material_language,routing_language,
               material_tag,status
        FROM tt_post_recurring_pool WHERE queue_id=?
        """,
        (queue_id,),
    ).fetchall()
    if len(recurring_rows) != 1:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_recurring_missing",
            "queue %s has no unique recurring-pool evidence" % queue_id,
        )
    recurring = recurring_rows[0]
    try:
        run_id = int(recurring["run_id"])
    except (TypeError, ValueError, OverflowError):
        run_id = 0
    identity_matches = (
        int(recurring["queue_id"] or 0) == queue_id
        and str(recurring["material_id"] or "") == str(row["material_id"] or "")
        and str(recurring["account_id"] or "") == str(row["account_id"] or "")
        and str(recurring["content_id"] or "") == str(row["content_id"] or "")
    )
    if (
        not identity_matches
        or run_id <= 0
        or str(recurring["status"] or "") != "consumed"
    ):
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_recurring_mismatch",
            "queue %s recurring-pool evidence does not match" % queue_id,
        )

    publish_events = conn.execute(
        """
        SELECT id,details_json,created_at
        FROM tt_post_event
        WHERE queue_id=? AND event_type='publish_reconciled'
          AND to_status='published'
        ORDER BY id
        """,
        (queue_id,),
    ).fetchall()
    if len(publish_events) != 1:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_event_missing",
            "queue %s has no unique publish completion event" % queue_id,
        )
    publish_event = publish_events[0]
    details, details_sha256 = _event_details(
        publish_event["details_json"], queue_id
    )
    if str(details.get("publish_id") or "") != str(row["publish_id"] or ""):
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_publish_id_mismatch",
            "queue %s publish event identity does not match" % queue_id,
        )
    published_at = _utc_iso(
        publish_event["created_at"], "queue publish event created_at"
    )

    creator_username = str(row["creator_username_snapshot"] or "").strip()
    page_name = str(
        row["creator_nickname_snapshot"] or row["account_display_name"] or ""
    ).strip()
    if not creator_username or not page_name:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_account_invalid",
            "queue %s frozen account snapshot is incomplete" % queue_id,
        )

    routing_language = _canonical_routing_language(
        recurring["routing_language"], queue_id
    )
    material_name, material_name_source = _frozen_or_fallback(
        row,
        recurring,
        "material_name",
        str(row["material_id"] or ""),
        "queue.material_id_surrogate",
    )
    drama_name, drama_name_source = _frozen_or_fallback(
        row,
        recurring,
        "drama_name",
        str(row["content_id"] or ""),
        "queue.content_id_surrogate",
    )
    material_language, material_language_source = _frozen_or_fallback(
        row,
        recurring,
        "material_language",
        routing_language,
        "recurring_pool.routing_language",
    )
    material_tag, material_tag_source = _frozen_or_fallback(
        row,
        recurring,
        "material_tag",
        "none",
        "literal.none",
    )
    if _canonical_routing_language(material_language, queue_id) != routing_language:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_language_mismatch",
            "queue %s frozen material language conflicts with routing" % queue_id,
        )

    created_at = _utc_iso(row["created_at"], "queue created_at")
    timestamp = int(
        datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    )
    legacy_link_id = TT_SHORT_LINK_NAMESPACE + queue_id
    try:
        target = build_w2a_url(
            {
                "username": creator_username,
                "timestamp": timestamp,
                "material_language": routing_language,
                "drama_name": drama_name,
                "tag": material_tag,
                "link_id": legacy_link_id,
                "page_name": page_name,
                "page_id": str(row["account_id"] or ""),
                "material_name": material_name,
                "material_id": str(row["material_id"] or ""),
                "queue_id": queue_id,
                "content_id": str(row["content_id"] or ""),
                "channel": "TT",
                "af_dp_first": True,
            }
        )
    except (TTPostLinkError, TypeError, ValueError):
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_route_invalid",
            "queue %s ledger evidence cannot build a route" % queue_id,
        ) from None
    route = _route_from_target(
        row,
        target,
        published_at_value=published_at,
    )
    evidence = {
        "source": "publish_recurring_v1",
        "recurring_pool_id": int(recurring["id"]),
        "run_id": run_id,
        "publish_event_id": int(publish_event["id"]),
        "publish_event_created_at": published_at,
        "publish_event_details_sha256": details_sha256,
        "fallback_fields": {
            "campaign_timestamp": {
                "source": "queue.created_at_surrogate",
                "value": created_at,
            },
            "username": {
                "source": "queue.creator_username_snapshot",
                "value": creator_username,
            },
            "page_name": {
                "source": (
                    "queue.creator_nickname_snapshot"
                    if str(row["creator_nickname_snapshot"] or "").strip()
                    else "queue.account_display_name"
                ),
                "value": page_name,
            },
            "material_name": {
                "source": material_name_source,
                "value": material_name,
            },
            "drama_name": {
                "source": drama_name_source,
                "value": drama_name,
            },
            "material_language": {
                "source": material_language_source,
                "value": routing_language,
            },
            "material_tag": {
                "source": material_tag_source,
                "value": material_tag,
            },
            "link_id": {
                "source": "legacy_short_link_namespace_plus_queue_id_surrogate",
                "value": str(legacy_link_id),
            },
        },
    }
    return route, evidence


def build_plan(
    conn: sqlite3.Connection,
    *,
    queue_ids: Sequence[Any] = (),
    reconstruct_route_from_ledger_queue_ids: Sequence[Any] = (),
) -> List[Dict[str, Any]]:
    """Build a deterministic plan without mutating SQLite."""

    _validate_schema(conn)
    normalized_ids = _queue_ids(queue_ids)
    reconstruction_ids = _queue_ids(reconstruct_route_from_ledger_queue_ids)
    if reconstruction_ids and not normalized_ids:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_scope_invalid",
            "ledger reconstruction requires explicit queue IDs",
        )
    clauses = ["q.status='published'", "trim(q.publish_id)<>''", "trim(q.code)='' "]
    params: List[Any] = []
    if normalized_ids:
        clauses.append("q.id IN (%s)" % ",".join("?" for _item in normalized_ids))
        params.extend(normalized_ids)
    rows = conn.execute(
        """
        SELECT q.id,q.scheduled_at_utc,q.account_id,q.account_username,
               q.account_display_name,q.creator_nickname_snapshot,
               q.creator_username_snapshot,q.content_id,q.material_id,
               q.material_name,q.drama_name,q.material_language,q.material_tag,
               q.short_link_id,q.short_url,q.long_url,q.code,q.status,q.publish_id,
               q.created_at,q.updated_at,
               (SELECT e.created_at FROM tt_post_event e
                WHERE e.queue_id=q.id AND e.to_status='published'
                ORDER BY e.id DESC LIMIT 1) AS route_published_at
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
    blank_long_url_ids = [
        int(row["id"]) for row in rows if not str(row["long_url"] or "").strip()
    ]
    if blank_long_url_ids and not reconstruction_ids:
        raise TTCodeBackfillError(
            "tt_code_backfill_long_url_missing",
            "queue %s needs explicit ledger reconstruction" % blank_long_url_ids[0],
        )
    if reconstruction_ids != blank_long_url_ids:
        raise TTCodeBackfillError(
            "tt_code_backfill_reconstruction_scope_mismatch",
            "ledger reconstruction IDs must exactly match selected blank routes",
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
        if str(row["long_url"] or ""):
            route = _route_from_queue(row)
            route_evidence: Dict[str, Any] = {"source": "frozen_long_url"}
        else:
            route, route_evidence = _route_from_ledger(conn, row)
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
                "route_source": str(route_evidence["source"]),
                "route_evidence": route_evidence,
                "route": route,
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
        "route_source",
        "route_evidence",
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
    reconstruct_route_from_ledger_queue_ids: Sequence[Any] = (),
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
            plan = build_plan(
                conn,
                queue_ids=normalized_ids,
                reconstruct_route_from_ledger_queue_ids=(
                    reconstruct_route_from_ledger_queue_ids
                ),
            )
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
                code = str(route.get("code") or "")
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


def inspect_database(
    db_path: Any,
    *,
    queue_ids: Sequence[Any] = (),
    reconstruct_route_from_ledger_queue_ids: Sequence[Any] = (),
) -> Dict[str, Any]:
    path = _database_path(db_path)
    with _connection(path, read_only=True) as conn:
        conn.execute("BEGIN")
        try:
            plan = build_plan(
                conn,
                queue_ids=queue_ids,
                reconstruct_route_from_ledger_queue_ids=(
                    reconstruct_route_from_ledger_queue_ids
                ),
            )
        finally:
            conn.rollback()
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
    parser.add_argument(
        "--reconstruct-route-from-ledger-queue-id",
        action="append",
        type=int,
        default=[],
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    try:
        if not args.apply:
            result = inspect_database(
                args.db_path,
                queue_ids=args.queue_id,
                reconstruct_route_from_ledger_queue_ids=(
                    args.reconstruct_route_from_ledger_queue_id
                ),
            )
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
                reconstruct_route_from_ledger_queue_ids=(
                    args.reconstruct_route_from_ledger_queue_id
                ),
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
