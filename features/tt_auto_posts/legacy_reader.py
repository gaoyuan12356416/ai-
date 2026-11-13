"""Strict read-only access to the existing TT Post publishing-pool ledger.

The automatic-template service must not instantiate ``TTPostStore`` because
that constructor runs additive migrations.  This adapter opens the exact
legacy SQLite file with ``mode=ro`` and additionally enables
``PRAGMA query_only`` on every connection.
"""

from __future__ import annotations

import contextlib
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


__all__ = [
    "LEGACY_MATERIAL_TABLES",
    "LegacyAccountSetting",
    "LegacyTTPostReader",
    "LegacyTTPostReaderError",
]
