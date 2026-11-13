"""Strict read-only access to the existing TT Post publishing-pool ledger.

The automatic-template service must not instantiate ``TTPostStore`` because
that constructor runs additive migrations.  This adapter opens the exact
legacy SQLite file with ``mode=ro`` and additionally enables
``PRAGMA query_only`` on every connection.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Set, Tuple


LEGACY_MATERIAL_TABLES: Tuple[str, ...] = (
    "tt_post_material_pool",
    "tt_post_material_intake",
    "tt_post_recurring_pool",
    "tt_post_queue",
    "tt_post_direct_test",
)
LEGACY_ACCOUNT_SETTINGS_TABLE = "tt_post_account_setting"
MAX_BATCH_MATERIAL_IDS = 10_000
MAX_BATCH_CODE_ROUTE_IDS = 10_200
_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")
PUBLISH_LOG_STATUS_GROUPS: Tuple[str, ...] = (
    "scheduled",
    "processing",
    "published",
    "needs_review",
    "failed",
    "canceled",
    "no_candidate",
    "hold",
    "other",
)


class LegacyTTPostReaderError(RuntimeError):
    """Stable fail-closed legacy-ledger read error."""

    def __init__(self, code: str, message: str, status: int = 503):
        self.code = str(code or "tt_auto_legacy_read_failed")[:96]
        self.status = int(status)
        super().__init__(str(message or "legacy TT Post ledger is unavailable")[:500])


@dataclass(frozen=True)
class LegacyAccountSetting:
    account_id: str
    drama_language: str
    privacy_level: str
    allow_comment: bool
    allow_duet: bool
    allow_stitch: bool
    brand_content_toggle: bool
    brand_organic_toggle: bool
    is_aigc: bool
    version: int
    updated_at: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "drama_language": self.drama_language,
            "privacy_level": self.privacy_level,
            "allow_comment": self.allow_comment,
            "allow_duet": self.allow_duet,
            "allow_stitch": self.allow_stitch,
            "brand_content_toggle": self.brand_content_toggle,
            "brand_organic_toggle": self.brand_organic_toggle,
            "is_aigc": self.is_aigc,
            "version": self.version,
            "updated_at": self.updated_at,
        }


def _identity(value: Any, label: str, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or "\x00" in text:
        raise LegacyTTPostReaderError(
            "tt_auto_legacy_identity_invalid",
            "%s is invalid" % label,
            400,
        )
    return text


class LegacyTTPostReader:
    """Open-and-close read-only adapter for account settings and seen media."""

    def __init__(self, db_path: Any):
        path = Path(str(db_path)).expanduser()
        if not path.is_absolute():
            raise LegacyTTPostReaderError(
                "tt_auto_legacy_db_path_invalid",
                "legacy TT Post database path must be absolute",
                500,
            )
        self.db_path = path

    def _uri(self) -> str:
        if not self.db_path.is_file():
            raise LegacyTTPostReaderError(
                "tt_auto_legacy_db_missing",
                "legacy TT Post database does not exist",
                503,
            )
        return self.db_path.resolve().as_uri() + "?mode=ro"

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that is protected by both SQLite read-only gates."""

        try:
            conn = sqlite3.connect(
                self._uri(),
                uri=True,
                timeout=5,
                isolation_level=None,
            )
        except (OSError, sqlite3.Error):
            raise LegacyTTPostReaderError(
                "tt_auto_legacy_db_unavailable",
                "legacy TT Post database is unavailable",
                503,
            ) from None
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute("PRAGMA query_only").fetchone()
            if row is None or int(row[0]) != 1:
                raise LegacyTTPostReaderError(
                    "tt_auto_legacy_query_only_failed",
                    "legacy TT Post database did not enter query-only mode",
                    503,
                )
            yield conn
        except LegacyTTPostReaderError:
            raise
        except sqlite3.Error:
            raise LegacyTTPostReaderError(
                "tt_auto_legacy_read_failed",
                "legacy TT Post database query failed",
                503,
            ) from None
        finally:
            conn.close()

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
        return {str(row["name"]) for row in conn.execute("PRAGMA table_info(%s)" % table)}

    def validate_schema(self) -> None:
        """Fail closed unless all five history tables and settings are present."""

        required_columns = {
            **{table: {"material_id"} for table in LEGACY_MATERIAL_TABLES},
            LEGACY_ACCOUNT_SETTINGS_TABLE: {
                "account_id",
                "drama_language",
                "privacy_level",
                "allow_comment",
                "allow_duet",
                "allow_stitch",
                "brand_content_toggle",
                "brand_organic_toggle",
                "is_aigc",
                "version",
                "updated_at",
            },
        }
        with self.connection() as conn:
            names = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing_tables = sorted(set(required_columns) - names)
            missing_columns = {
                table: sorted(columns - self._table_columns(conn, table))
                for table, columns in required_columns.items()
                if table in names and columns - self._table_columns(conn, table)
            }
        if missing_tables or missing_columns:
            raise LegacyTTPostReaderError(
                "tt_auto_legacy_schema_invalid",
                "legacy TT Post database schema is incomplete",
                503,
            )

    def get_account_setting(self, account_id: Any) -> LegacyAccountSetting:
        normalized = _identity(account_id, "account id", 64)
        with self.connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT
                        account_id,drama_language,privacy_level,
                        allow_comment,allow_duet,allow_stitch,
                        brand_content_toggle,brand_organic_toggle,is_aigc,
                        version,updated_at
                    FROM tt_post_account_setting
                    WHERE account_id=?
                    LIMIT 2
                    """,
                    (normalized,),
                ).fetchall()
            except sqlite3.Error:
                raise LegacyTTPostReaderError(
                    "tt_auto_legacy_schema_invalid",
                    "legacy account setting schema is unavailable",
                    503,
                ) from None
        if not rows:
            raise LegacyTTPostReaderError(
                "tt_auto_account_setting_not_found",
                "TikTok account publishing settings are not configured",
                409,
            )
        if len(rows) != 1 or str(rows[0]["account_id"]) != normalized:
            raise LegacyTTPostReaderError(
                "tt_auto_account_setting_ambiguous",
                "TikTok account publishing settings are ambiguous",
                503,
            )
        row = rows[0]
        try:
            version = int(row["version"])
            booleans = {
                key: int(row[key])
                for key in (
                    "allow_comment",
                    "allow_duet",
                    "allow_stitch",
                    "brand_content_toggle",
                    "brand_organic_toggle",
                    "is_aigc",
                )
            }
        except (TypeError, ValueError, OverflowError):
            raise LegacyTTPostReaderError(
                "tt_auto_account_setting_invalid",
                "TikTok account publishing settings are invalid",
                503,
            ) from None
        if version <= 0 or any(value not in (0, 1) for value in booleans.values()):
            raise LegacyTTPostReaderError(
                "tt_auto_account_setting_invalid",
                "TikTok account publishing settings are invalid",
                503,
            )
        drama_language = str(row["drama_language"] or "").strip().lower()
        privacy_level = str(row["privacy_level"] or "").strip()
        if not drama_language or not privacy_level:
            raise LegacyTTPostReaderError(
                "tt_auto_account_setting_invalid",
                "TikTok account publishing settings are invalid",
                503,
            )
        return LegacyAccountSetting(
            account_id=normalized,
            drama_language=drama_language,
            privacy_level=privacy_level,
            allow_comment=bool(booleans["allow_comment"]),
            allow_duet=bool(booleans["allow_duet"]),
            allow_stitch=bool(booleans["allow_stitch"]),
            brand_content_toggle=bool(booleans["brand_content_toggle"]),
            brand_organic_toggle=bool(booleans["brand_organic_toggle"]),
            is_aigc=bool(booleans["is_aigc"]),
            version=version,
            updated_at=str(row["updated_at"] or ""),
        )

    def material_occurrences(self, material_id: Any) -> Tuple[str, ...]:
        normalized = _identity(material_id, "material id")
        matches: List[str] = []
        with self.connection() as conn:
            for table in LEGACY_MATERIAL_TABLES:
                try:
                    row = conn.execute(
                        "SELECT 1 FROM %s WHERE material_id=? LIMIT 1" % table,
                        (normalized,),
                    ).fetchone()
                except sqlite3.Error:
                    raise LegacyTTPostReaderError(
                        "tt_auto_legacy_schema_invalid",
                        "legacy material history schema is unavailable",
                        503,
                    ) from None
                if row is not None:
                    matches.append(table)
        return tuple(matches)

    def material_seen(self, material_id: Any) -> bool:
        return bool(self.material_occurrences(material_id))

    def seen_material_ids(self, material_ids: Sequence[Any]) -> Set[str]:
        """Return IDs present in any of the five old publishing tables."""

        if isinstance(material_ids, (str, bytes)) or not isinstance(material_ids, Sequence):
            raise LegacyTTPostReaderError(
                "tt_auto_material_ids_invalid",
                "material ids must be a list",
                400,
            )
        normalized = list(dict.fromkeys(_identity(value, "material id") for value in material_ids))
        if len(normalized) > MAX_BATCH_MATERIAL_IDS:
            raise LegacyTTPostReaderError(
                "tt_auto_material_ids_too_many",
                "too many material ids",
                400,
            )
        if not normalized:
            return set()
        seen: Set[str] = set()
        with self.connection() as conn:
            for table in LEGACY_MATERIAL_TABLES:
                for offset in range(0, len(normalized), 500):
                    batch = normalized[offset : offset + 500]
                    placeholders = ",".join("?" for _ in batch)
                    try:
                        rows = conn.execute(
                            "SELECT material_id FROM %s WHERE material_id IN (%s)"
                            % (table, placeholders),
                            tuple(batch),
                        ).fetchall()
                    except sqlite3.Error:
                        raise LegacyTTPostReaderError(
                            "tt_auto_legacy_schema_invalid",
                            "legacy material history schema is unavailable",
                            503,
                        ) from None
                    seen.update(str(row["material_id"]) for row in rows)
        return seen

    def list_publish_logs(
        self,
        *,
        trigger_type: str = "",
        account_id: str = "",
        material_id: str = "",
        content_id: str = "",
        status_group: str = "",
        from_utc: str = "",
        to_utc: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return a bounded, public-safe task view from the legacy ledger.

        This deliberately repeats the minimum read projection instead of
        constructing ``TTPostStore``.  The latter performs additive migrations
        and would violate the automatic publisher's read-only boundary.
        """

        if trigger_type and trigger_type not in {"scheduled", "direct_test"}:
            raise LegacyTTPostReaderError(
                "tt_auto_publish_log_trigger_invalid",
                "publish log trigger type is invalid",
                400,
            )
        if status_group and status_group not in PUBLISH_LOG_STATUS_GROUPS:
            raise LegacyTTPostReaderError(
                "tt_auto_publish_log_status_invalid",
                "publish log status is invalid",
                400,
            )
        try:
            bounded_limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 0
        if not 1 <= bounded_limit <= 10_200:
            raise LegacyTTPostReaderError(
                "tt_auto_publish_log_limit_invalid",
                "publish log limit is invalid",
                400,
            )

        normalized_account = _identity(account_id, "account id", 64) if account_id else ""
        normalized_material = _identity(material_id, "material id") if material_id else ""
        normalized_content = _identity(content_id, "content id") if content_id else ""
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("trigger_type", trigger_type),
            ("account_id", normalized_account),
            ("material_id", normalized_material),
            ("content_id", normalized_content),
            ("status_group", status_group),
        ):
            if value:
                clauses.append("%s=?" % column)
                params.append(value)
        if from_utc:
            clauses.append("task_at_utc>=?")
            params.append(str(from_utc))
        if to_utc:
            clauses.append("task_at_utc<?")
            params.append(str(to_utc))
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        cte = """
            WITH publish_logs AS (
                SELECT
                    'scheduled' AS trigger_type,
                    'automatic' AS source_task_type,
                    id AS task_id,
                    scheduled_at_utc AS task_at_utc,
                    scheduled_at_utc,
                    account_id,account_username,account_display_name,
                    creator_nickname_snapshot,creator_username_snapshot,
                    content_id,material_id,material_name,drama_name,
                    material_language,caption,code,privacy_level,
                    allow_comment,allow_duet,allow_stitch,
                    brand_content_toggle,brand_organic_toggle,is_aigc,
                    status,publish_id,publish_url,error_code,error_message,
                    unknown_outcome,created_at,updated_at,
                    '' AS prepared_at_utc,'' AS publish_started_at_utc,
                    '' AS published_at_utc,'' AS failed_at_utc,
                    '' AS canceled_at_utc
                FROM tt_post_queue
                UNION ALL
                SELECT
                    'direct_test' AS trigger_type,
                    'direct_test' AS source_task_type,
                    id AS task_id,
                    created_at AS task_at_utc,
                    created_at AS scheduled_at_utc,
                    account_id,account_username,account_display_name,
                    creator_nickname_snapshot,creator_username_snapshot,
                    content_id,material_id,material_name,drama_name,
                    material_language,caption,'' AS code,privacy_level,
                    allow_comment,allow_duet,allow_stitch,
                    brand_content_toggle,brand_organic_toggle,is_aigc,
                    status,publish_id,publish_url,error_code,error_message,
                    unknown_outcome,created_at,updated_at,
                    prepared_at_utc,publish_started_at_utc,published_at_utc,
                    failed_at_utc,canceled_at_utc
                FROM tt_post_direct_test
            ), classified AS (
                SELECT *,
                    CASE
                        WHEN unknown_outcome=1 OR status='unknown'
                            THEN 'needs_review'
                        WHEN status='published' THEN 'published'
                        WHEN trigger_type='scheduled' AND status='scheduled'
                            THEN 'scheduled'
                        WHEN (
                            trigger_type='scheduled'
                            AND status IN ('claimed','publishing','reconciling')
                        ) OR (
                            trigger_type='direct_test'
                            AND status IN (
                                'queued','preparing','ready',
                                'publishing','reconciling'
                            )
                        ) THEN 'processing'
                        WHEN status IN ('failed','missed') THEN 'failed'
                        WHEN status='canceled' THEN 'canceled'
                        WHEN status='blocked_compliance' THEN 'hold'
                        ELSE 'other'
                    END AS status_group
                FROM publish_logs
            )
        """
        with self.connection() as conn:
            conn.execute("BEGIN")
            summary_row = conn.execute(
                cte
                + """
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(status_group='scheduled'),0) AS scheduled,
                           COALESCE(SUM(status_group='processing'),0) AS processing,
                           COALESCE(SUM(status_group='published'),0) AS published,
                           COALESCE(SUM(status_group='needs_review'),0) AS needs_review,
                           COALESCE(SUM(status_group='failed'),0) AS failed,
                           COALESCE(SUM(status_group='canceled'),0) AS canceled,
                           0 AS no_candidate,
                           COALESCE(SUM(status_group='hold'),0) AS hold
                    FROM classified
                """
                + where_sql,
                tuple(params),
            ).fetchone()
            rows = conn.execute(
                cte
                + "SELECT * FROM classified"
                + where_sql
                + " ORDER BY task_at_utc DESC,trigger_type,task_id DESC LIMIT ?",
                tuple([*params, bounded_limit]),
            ).fetchall()

        items: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in (
                "allow_comment",
                "allow_duet",
                "allow_stitch",
                "brand_content_toggle",
                "brand_organic_toggle",
                "is_aigc",
                "unknown_outcome",
            ):
                item[key] = bool(item.get(key))
            task_id = int(item.pop("task_id"))
            source_task_type = str(item.get("source_task_type") or "")
            item.update(
                {
                    "publish_source": "material_pool",
                    "task_id": task_id,
                    "task_key": "material_pool:%s:%s"
                    % (source_task_type, task_id),
                    "source_account_id": str(item.get("account_id") or ""),
                    "template_id": None,
                    "template_version": None,
                    "template_name": "",
                    "run_id": None,
                    "series_code": "",
                }
            )
            items.append(item)
        summary = dict(summary_row or {})
        return {
            "items": items,
            "total": int(summary.get("total") or 0),
            "summary": {
                key: int(summary.get(key) or 0)
                for key in (
                    "total",
                    "scheduled",
                    "processing",
                    "published",
                    "needs_review",
                    "failed",
                    "canceled",
                    "no_candidate",
                    "hold",
                )
            },
        }

    def code_routes_for_queue_ids(
        self,
        queue_ids: Iterable[Any],
    ) -> Dict[int, str]:
        """Read valid four-character codes from the shared route ledger.

        Automatic-post tasks use a synthetic high queue ID in the same route
        ledger as scheduled material-pool posts.  This method reads only that
        narrow identity projection and never constructs the mutating legacy
        store.
        """

        normalized: List[int] = []
        seen: Set[int] = set()
        for value in queue_ids:
            if isinstance(value, bool):
                raise LegacyTTPostReaderError(
                    "tt_auto_code_route_identity_invalid",
                    "code route queue identity is invalid",
                    400,
                )
            try:
                queue_id = int(value)
            except (TypeError, ValueError, OverflowError):
                queue_id = 0
            if queue_id <= 0:
                raise LegacyTTPostReaderError(
                    "tt_auto_code_route_identity_invalid",
                    "code route queue identity is invalid",
                    400,
                )
            if queue_id not in seen:
                seen.add(queue_id)
                normalized.append(queue_id)
        if len(normalized) > MAX_BATCH_CODE_ROUTE_IDS:
            raise LegacyTTPostReaderError(
                "tt_auto_code_route_batch_too_large",
                "too many code route identities were requested",
                400,
            )
        if not normalized:
            return {}

        result: Dict[int, str] = {}
        with self.connection() as conn:
            names = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "tt_post_code_route" not in names:
                raise LegacyTTPostReaderError(
                    "tt_auto_code_route_schema_invalid",
                    "shared TT code route ledger is unavailable",
                    503,
                )
            columns = self._table_columns(conn, "tt_post_code_route")
            if not {"queue_id", "code"}.issubset(columns):
                raise LegacyTTPostReaderError(
                    "tt_auto_code_route_schema_invalid",
                    "shared TT code route schema is invalid",
                    503,
                )
            conn.execute("BEGIN")
            for offset in range(0, len(normalized), 500):
                batch = normalized[offset : offset + 500]
                placeholders = ",".join("?" for _item in batch)
                rows = conn.execute(
                    "SELECT queue_id,code FROM tt_post_code_route "
                    "WHERE queue_id IN (%s)" % placeholders,
                    tuple(batch),
                ).fetchall()
                for row in rows:
                    queue_id = int(row["queue_id"])
                    code = str(row["code"] or "")
                    if queue_id not in seen or not _CODE_RE.fullmatch(code):
                        raise LegacyTTPostReaderError(
                            "tt_auto_code_route_row_invalid",
                            "shared TT code route row is invalid",
                            503,
                        )
                    if queue_id in result and result[queue_id] != code:
                        raise LegacyTTPostReaderError(
                            "tt_auto_code_route_row_invalid",
                            "shared TT code route identity is not unique",
                            503,
                        )
                    result[queue_id] = code
        return result


__all__ = [
    "LEGACY_MATERIAL_TABLES",
    "MAX_BATCH_CODE_ROUTE_IDS",
    "PUBLISH_LOG_STATUS_GROUPS",
    "LegacyAccountSetting",
    "LegacyTTPostReader",
    "LegacyTTPostReaderError",
]
