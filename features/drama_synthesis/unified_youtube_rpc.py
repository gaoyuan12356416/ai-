"""Primary-side repository for the controlled unified YouTube RPC.

The caller never supplies SQL or column names.  Every statement is selected
from a fixed table/entity map, and rows are immutable once an external ID has
been recorded.  The three ``drama_external_*`` columns are additive nullable
columns on the legacy unified tables and provide database-enforced idempotency
without reinterpreting historical rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping

from .unified_youtube import (
    ALLOWED_ACTIONS,
    TABLE_BY_KIND,
    TABLE_TO_KIND,
    validate_controlled_operation,
    validate_entity_payload,
    validate_external_id,
    read_secure_owned_file,
)


SCHEMA = "kunlunads_dev"
WRITER_USER = "drama_youtube_writer"
MIGRATOR_USER = "drama_youtube_migrator"
ACCOUNT_HOST = "43.166.187.96"
RUNTIME_TABLE_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE"})
MIGRATOR_TABLE_PRIVILEGES = frozenset({"SELECT", "INSERT", "CREATE", "ALTER"})
EXTERNAL_COLUMN_BY_KIND = {
    "video": "drama_external_video_id",
    "comment": "drama_external_comment_id",
    "publish_log": "drama_external_publish_id",
}


def _column_definition(
    column_type: str,
    *,
    nullable: str = "NO",
    default: Any = "0",
    charset: str | None = None,
    collation: str | None = None,
    extra: str = "",
) -> tuple[Any, ...]:
    return (column_type, nullable, default, charset, collation, extra)


REQUIRED_COLUMN_DEFINITIONS = {
    "ads_youtube_videos": {
        "id": _column_definition("int(10) unsigned", default=None, extra="auto_increment"),
        "created_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP"),
        "updated_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP", extra="on update current_timestamp"),
        "app_id": _column_definition("int(11)"),
        "template_id": _column_definition("int(11)"),
        "source_id": _column_definition("bigint(20)"),
        "original_source_id": _column_definition("varchar(50)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "title": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "body": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "channel_id": _column_definition("int(11)"),
        "template_make_id": _column_definition("int(11)", default=None),
        "video_id": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "video_title": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "video_description": _column_definition("varchar(3000)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "published_at": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "thumbs": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "filenames": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "countries": _column_definition("varchar(40)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "languages": _column_definition("varchar(40)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "privacy_status": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "is_made_for_kids": _column_definition("tinyint(4)"),
        "is_allow_like": _column_definition("tinyint(4)"),
        "user_id": _column_definition("int(11)"),
        "view_count": _column_definition("int(11)"),
        "like_count": _column_definition("int(11)"),
        "dislike_count": _column_definition("int(11)"),
        "favorite_count": _column_definition("int(11)"),
        "copyright": _column_definition("int(11)"),
        "queue_id": _column_definition("int(11)"),
        "drama_external_video_id": _column_definition("varchar(32)", nullable="YES", default=None, charset="ascii", collation="ascii_bin"),
    },
    "ads_youtube_comments": {
        "id": _column_definition("int(10) unsigned", default=None, extra="auto_increment"),
        "created_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP"),
        "updated_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP", extra="on update current_timestamp"),
        "user_id": _column_definition("int(11)"),
        "channel_id": _column_definition("int(11)"),
        "video_id": _column_definition("varchar(255)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "comment": _column_definition("varchar(1000)", default="", charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        "drama_external_comment_id": _column_definition("varchar(255)", nullable="YES", default=None, charset="ascii", collation="ascii_bin"),
    },
    "ads_youtube_publish_log": {
        "id": _column_definition("int(11) unsigned", default=None, extra="auto_increment"),
        "created_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP"),
        "updated_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP", extra="on update current_timestamp"),
        "type_id": _column_definition("tinyint(4)"),
        "user_id": _column_definition("int(11)"),
        "source_id": _column_definition("bigint(20)"),
        "log": _column_definition("varchar(3000)", default="", charset="utf8mb4", collation="utf8mb4_general_ci"),
        "created_queue": _column_definition("int(11)"),
        "status": _column_definition("tinyint(4)"),
        "drama_external_publish_id": _column_definition("varchar(19)", nullable="YES", default=None, charset="ascii", collation="ascii_bin"),
    },
}
REQUIRED_COLUMNS = {
    table: frozenset(definitions) for table, definitions in REQUIRED_COLUMN_DEFINITIONS.items()
}
REQUIRED_UNIQUE_INDEX_BY_TABLE = {
    "ads_youtube_videos": (
        "ux_ads_youtube_videos_drama_external_video_id",
        "drama_external_video_id",
    ),
    "ads_youtube_comments": (
        "ux_ads_youtube_comments_drama_external_comment_id",
        "drama_external_comment_id",
    ),
    "ads_youtube_publish_log": (
        "ux_ads_youtube_publish_log_drama_external_publish_id",
        "drama_external_publish_id",
    ),
}


class LedgerRPCError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code = str(code)
        self.status = int(status)
        super().__init__(self.code)


def _column_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    default = row.get("COLUMN_DEFAULT")
    return (
        str(row.get("COLUMN_TYPE") or "").lower(),
        str(row.get("IS_NULLABLE") or ""),
        None if default is None else str(default),
        str(row.get("CHARACTER_SET_NAME")) if row.get("CHARACTER_SET_NAME") is not None else None,
        str(row.get("COLLATION_NAME")) if row.get("COLLATION_NAME") is not None else None,
        str(row.get("EXTRA") or "").lower(),
    )


def validate_required_schema_rows(rows: Any, *, require_external: bool) -> None:
    seen: Dict[str, Dict[str, Mapping[str, Any]]] = {
        table: {} for table in REQUIRED_COLUMN_DEFINITIONS
    }
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        table = str(row.get("TABLE_NAME") or "")
        column = str(row.get("COLUMN_NAME") or "")
        if table in seen and column:
            if column in seen[table]:
                raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
            seen[table][column] = row
    external_columns = set(EXTERNAL_COLUMN_BY_KIND.values())
    for table, definitions in REQUIRED_COLUMN_DEFINITIONS.items():
        if set(seen[table]) - set(definitions):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        for column, expected in definitions.items():
            row = seen[table].get(column)
            if row is None and column in external_columns and not require_external:
                continue
            if row is None or _column_signature(row) != expected:
                raise LedgerRPCError("youtube_sync_schema_mismatch", 503)


def validate_exact_account_grants(
    cursor: Any,
    current_user: str,
    *,
    expected_user: str,
    expected_table_privileges: frozenset[str],
) -> str:
    if current_user != "%s@%s" % (expected_user, ACCOUNT_HOST):
        raise LedgerRPCError("youtube_sync_database_identity_invalid", 503)
    grantee = "'%s'@'%s'" % (expected_user, ACCOUNT_HOST)
    cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
    grant_rows = list(cursor.fetchall())
    grants = []
    for row in grant_rows:
        if isinstance(row, Mapping) and row:
            grants.append(str(next(iter(row.values()))))
    if not grants:
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    # MySQL 5.7 SHOW GRANTS normally renders account names with identifier
    # quotes (backticks); compatible forks can use SQL string quotes instead.
    # Accept only those quote wrappers around the exact frozen account.
    account_pattern = r"[`']?%s[`']?@[`']?%s[`']?" % (
        re.escape(expected_user),
        re.escape(ACCOUNT_HOST),
    )
    usage_pattern = re.compile(
        r"^GRANT\s+USAGE\s+ON\s+\*\.\*\s+TO\s+%s"
        r"$"
        % account_pattern,
        re.IGNORECASE,
    )
    table_pattern = re.compile(
        r"^GRANT\s+(?P<privileges>[A-Z][A-Z ,]*)\s+ON\s+"
        r"[`']?%s[`']?\.[`']?(?P<table>[A-Z0-9_]+)[`']?\s+TO\s+%s$"
        % (re.escape(SCHEMA), account_pattern),
        re.IGNORECASE,
    )
    usage_count = 0
    shown_table_privileges: Dict[str, frozenset[str]] = {}
    expected_tables = set(TABLE_BY_KIND.values())
    for raw_grant in grants:
        normalized = re.sub(r"\s+", " ", raw_grant.strip())
        upper = normalized.upper()
        if "WITH GRANT OPTION" in upper or upper.startswith("GRANT PROXY"):
            raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
        if usage_pattern.fullmatch(normalized):
            usage_count += 1
            continue
        match = table_pattern.fullmatch(normalized)
        if not match:
            raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
        table = str(match.group("table") or "")
        privileges = [item.strip().upper() for item in match.group("privileges").split(",")]
        privilege_set = frozenset(privileges)
        if (
            table not in expected_tables
            or table in shown_table_privileges
            or len(privileges) != len(privilege_set)
            or privilege_set != expected_table_privileges
        ):
            raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
        shown_table_privileges[table] = privilege_set
    if usage_count != 1 or set(shown_table_privileges) != expected_tables:
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    cursor.execute(
        "SELECT PRIVILEGE_TYPE,IS_GRANTABLE FROM information_schema.USER_PRIVILEGES WHERE GRANTEE=%s",
        (grantee,),
    )
    user_privileges = set()
    for row in cursor.fetchall():
        if not isinstance(row, Mapping):
            raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
        user_privileges.add(
            (
                str(row.get("PRIVILEGE_TYPE") or "").upper(),
                str(row.get("IS_GRANTABLE") or "").upper(),
            )
        )
    # USAGE means no global privileges.  MySQL 5.7 commonly omits that
    # pseudo-privilege from USER_PRIVILEGES, while compatible servers may
    # expose it explicitly; both shapes represent the same empty privilege
    # set and every real global privilege remains forbidden.
    if user_privileges not in (set(), {("USAGE", "NO")}):
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    cursor.execute(
        "SELECT TABLE_SCHEMA,PRIVILEGE_TYPE,IS_GRANTABLE FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE=%s",
        (grantee,),
    )
    if list(cursor.fetchall()):
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    cursor.execute(
        "SELECT TABLE_SCHEMA,TABLE_NAME,PRIVILEGE_TYPE,IS_GRANTABLE "
        "FROM information_schema.TABLE_PRIVILEGES WHERE GRANTEE=%s",
        (grantee,),
    )
    actual = set()
    for row in cursor.fetchall():
        if not isinstance(row, Mapping) or str(row.get("IS_GRANTABLE") or "") != "NO":
            raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
        actual.add(
            (
                str(row.get("TABLE_SCHEMA") or ""),
                str(row.get("TABLE_NAME") or ""),
                str(row.get("PRIVILEGE_TYPE") or "").upper(),
            )
        )
    expected = {
        (SCHEMA, table, privilege)
        for table in TABLE_BY_KIND.values()
        for privilege in expected_table_privileges
    }
    if actual != expected:
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    cursor.execute(
        "SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,PRIVILEGE_TYPE,IS_GRANTABLE "
        "FROM information_schema.COLUMN_PRIVILEGES WHERE GRANTEE=%s",
        (grantee,),
    )
    if list(cursor.fetchall()):
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    # MySQL 5.7 (including the live CynosDB 5.7.18 build) has no
    # information_schema.ROUTINE_PRIVILEGES table. Routine grants are still
    # fail-closed because SHOW GRANTS above accepts only USAGE plus the three
    # exact table-grant statements; EXECUTE/ALTER ROUTINE/PROCEDURE/FUNCTION
    # statements cannot match that allowlist.
    canonical = {
        "account": current_user,
        "global": ["USAGE"],
        "tables": {
            table: sorted(expected_table_privileges)
            for table in sorted(expected_tables)
        },
    }
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_database_credential_file(path_text: str, *, expected_user: str = "") -> Mapping[str, Any]:
    try:
        raw = read_secure_owned_file(path_text, max_bytes=8192)
        value = json.loads(raw.decode("utf-8"))
    except (RuntimeError, UnicodeDecodeError, ValueError):
        value = None
    required = {"host", "port", "user", "password", "database"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise RuntimeError("writer database credential file is invalid")
    normalized = {
        "host": str(value.get("host") or "").strip(),
        "port": value.get("port"),
        "user": str(value.get("user") or "").strip(),
        "password": str(value.get("password") or ""),
        "database": str(value.get("database") or "").strip(),
    }
    if type(normalized["port"]) is not int:
        raise RuntimeError("writer database credential file is invalid")
    if (
        normalized["host"] != "101.32.56.53"
        or normalized["port"] != 63353
        or normalized["database"] != SCHEMA
        or (expected_user and normalized["user"] != expected_user)
    ):
        raise RuntimeError("writer database credential target is invalid")
    password = str(normalized["password"])
    if not 32 <= len(password) <= 1024 or any(ord(char) < 32 for char in password):
        raise RuntimeError("writer database credential file is invalid")
    return normalized


def _legacy_source_id(content_id: str) -> int:
    text = str(content_id or "")
    if text.isdigit():
        value = int(text)
        if 0 < value <= 9_223_372_036_854_775_807:
            return value
    return 0


def _synthetic_queue_id(publish_id: Any) -> int:
    value = int(publish_id)
    if not 1 <= value <= 2_147_483_647:
        raise LedgerRPCError("youtube_sync_contract_invalid")
    # Legacy queue IDs are positive (production currently starts above 20M).
    # A negative local publish ID keeps video/log joins deterministic without
    # pretending that a drama-synthesis task is an ads_created_queue row.
    return -value


def _mysql_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(str(value)[:-1] + "+00:00").astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _video_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    content_id = str(payload["content_id"])
    description = str(payload["description_rendered"])
    source_url = str(payload["source_url"])
    return {
        "app_id": int(payload["app_id"]),
        "template_id": 0,
        "source_id": _legacy_source_id(content_id),
        "original_source_id": content_id if len(content_id) <= 50 else str(payload["job_id"]),
        "title": "AI剧集合成",
        "body": str(payload["source_kind"]),
        "channel_id": int(payload["channel_local_id"]),
        "template_make_id": 0,
        "video_id": str(payload["video_id"]),
        "video_title": str(payload["title"]),
        "video_description": description[:3000],
        "published_at": _mysql_timestamp(str(payload["published_at_utc"])),
        "filenames": source_url[:255],
        "privacy_status": "public",
        "is_made_for_kids": 0,
        "is_allow_like": 1,
        "user_id": int(payload["operator_user_id"]),
        "queue_id": _synthetic_queue_id(payload["publish_id"]),
        "drama_external_video_id": str(payload["video_id"]),
    }


def _comment_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "user_id": int(payload["operator_user_id"]),
        "channel_id": int(payload["channel_local_id"]),
        "video_id": str(payload["video_id"]),
        "comment": str(payload["comment_text"]),
        "drama_external_comment_id": str(payload["comment_id"]),
    }


def _publish_log_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    description = str(payload["description_rendered"])
    source_url = str(payload["source_url"])
    safe_log = {
        "source": "drama_synthesis",
        "publish_id": int(payload["publish_id"]),
        "video_id": str(payload["video_id"]),
        "job_id": str(payload["job_id"]),
        "published_at_utc": str(payload["published_at_utc"]),
        "description_bytes": len(description.encode("utf-8")),
        "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "description_legacy_truncated": len(description) > 3000,
        "source_url_sha256": hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
        "source_url_legacy_truncated": len(source_url) > 255,
    }
    return {
        "type_id": 3,
        "user_id": int(payload["operator_user_id"]),
        "source_id": _legacy_source_id(str(payload["content_id"])),
        "log": json.dumps(safe_log, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "created_queue": _synthetic_queue_id(payload["publish_id"]),
        "status": 1,
        "drama_external_publish_id": str(payload["publish_id"]),
    }


RECORD_BUILDER_BY_KIND = {
    "video": _video_record,
    "comment": _comment_record,
    "publish_log": _publish_log_record,
}
RECORD_COLUMNS_BY_KIND = {
    "video": (
        "app_id", "template_id", "source_id", "original_source_id", "title", "body",
        "channel_id", "template_make_id", "video_id", "video_title", "video_description",
        "published_at", "filenames", "privacy_status", "is_made_for_kids", "is_allow_like",
        "user_id", "queue_id", "drama_external_video_id",
    ),
    "comment": (
        "user_id", "channel_id", "video_id", "comment", "drama_external_comment_id",
    ),
    "publish_log": (
        "type_id", "user_id", "source_id", "log", "created_queue", "status",
        "drama_external_publish_id",
    ),
}


class UnifiedYouTubeLedger:
    """Execute the fixed select/insert/update envelope against one primary DB."""

    def __init__(self, connect_factory: Callable[[], Any], *, schema: str = SCHEMA):
        if not callable(connect_factory) or schema != SCHEMA:
            raise ValueError("invalid unified YouTube ledger configuration")
        self.connect_factory = connect_factory
        self.schema = schema

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass

    def health(self) -> Mapping[str, Any]:
        connection = self.connect_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT DATABASE() AS database_name,@@read_only AS read_only,CURRENT_USER() AS account_name")
                identity = cursor.fetchone()
                if not isinstance(identity, Mapping):
                    raise LedgerRPCError("youtube_sync_database_identity_invalid", 503)
                current_user = str(identity.get("account_name") or "")
                if (
                    identity.get("database_name") != self.schema
                    or int(identity.get("read_only") or 0) != 0
                    or current_user != "%s@%s" % (WRITER_USER, ACCOUNT_HOST)
                ):
                    raise LedgerRPCError("youtube_sync_database_identity_invalid", 503)
                grant_fingerprint = validate_exact_account_grants(
                    cursor,
                    current_user,
                    expected_user=WRITER_USER,
                    expected_table_privileges=RUNTIME_TABLE_PRIVILEGES,
                )
                cursor.execute(
                    "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,"
                    "CHARACTER_SET_NAME,COLLATION_NAME,EXTRA FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (%s,%s,%s)",
                    (self.schema,) + tuple(TABLE_BY_KIND.values()),
                )
                validate_required_schema_rows(cursor.fetchall(), require_external=True)
                cursor.execute(
                    "SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME "
                    "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_NAME IN (%s,%s,%s)",
                    (self.schema,) + tuple(TABLE_BY_KIND.values()),
                )
                indexes: Dict[tuple[str, str], list[Mapping[str, Any]]] = {}
                for row in cursor.fetchall():
                    if not isinstance(row, Mapping):
                        continue
                    key = (str(row.get("TABLE_NAME") or ""), str(row.get("INDEX_NAME") or ""))
                    indexes.setdefault(key, []).append(row)
                for table, (index_name, column_name) in REQUIRED_UNIQUE_INDEX_BY_TABLE.items():
                    rows = indexes.get((table, index_name), [])
                    if not (
                        len(rows) == 1
                        and int(rows[0].get("NON_UNIQUE") or 0) == 0
                        and int(rows[0].get("SEQ_IN_INDEX") or 0) == 1
                        and str(rows[0].get("COLUMN_NAME") or "") == column_name
                    ):
                        raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
                return {
                    "ok": True,
                    "schema": self.schema,
                    "grant_fingerprint": grant_fingerprint,
                }
        finally:
            self._close(connection)

    @staticmethod
    def _row_matches(row: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
        for key, expected in record.items():
            actual = row.get(key)
            if isinstance(expected, int):
                try:
                    if int(actual) != expected:
                        return False
                except (TypeError, ValueError):
                    return False
            elif str(actual or "") != str(expected):
                return False
        return True

    def _find(self, cursor: Any, kind: str, external_id: str, *, lock: bool) -> list[Mapping[str, Any]]:
        table = TABLE_BY_KIND[kind]
        external_column = EXTERNAL_COLUMN_BY_KIND[kind]
        record_columns = list(RECORD_COLUMNS_BY_KIND[kind])
        columns = ",".join("`%s`" % column for column in (["id"] + record_columns))
        sql = "SELECT %s FROM `%s`.`%s` WHERE `%s`=%%s LIMIT 2%s" % (
            columns, self.schema, table, external_column, " FOR UPDATE" if lock else "",
        )
        cursor.execute(sql, (external_id,))
        rows = list(cursor.fetchall())
        if len(rows) > 1:
            raise LedgerRPCError("youtube_sync_identity_not_unique", 503)
        return rows

    def execute(self, action: str, table: str, external_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_controlled_operation(action, table)
        kind = TABLE_TO_KIND[table]
        validate_external_id(kind, external_id)
        if action == "select":
            if not isinstance(payload, Mapping) or payload:
                raise LedgerRPCError("youtube_sync_contract_invalid")
            connection = self.connect_factory()
            try:
                with connection.cursor() as cursor:
                    return {"found": bool(self._find(cursor, kind, external_id, lock=False))}
            finally:
                self._close(connection)

        safe_payload = validate_entity_payload(kind, external_id, payload)
        record = RECORD_BUILDER_BY_KIND[kind](safe_payload)
        connection = self.connect_factory()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                rows = self._find(cursor, kind, external_id, lock=True)
                if rows:
                    if not self._row_matches(rows[0], record):
                        raise LedgerRPCError("youtube_sync_identity_conflict")
                    connection.commit()
                    return {"idempotent_success": True, "reused": True}
                if action == "update":
                    raise LedgerRPCError("youtube_sync_identity_missing")
                columns = list(record.keys())
                sql = "INSERT INTO `%s`.`%s` (%s) VALUES (%s)" % (
                    self.schema,
                    table,
                    ",".join("`%s`" % column for column in columns),
                    ",".join(["%s"] * len(columns)),
                )
                cursor.execute(sql, tuple(record[column] for column in columns))
            connection.commit()
            return {"idempotent_success": True, "reused": False}
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            self._close(connection)


__all__ = [
    "ACCOUNT_HOST", "ALLOWED_ACTIONS", "EXTERNAL_COLUMN_BY_KIND", "LedgerRPCError",
    "MIGRATOR_TABLE_PRIVILEGES", "MIGRATOR_USER", "REQUIRED_COLUMN_DEFINITIONS",
    "REQUIRED_COLUMNS", "REQUIRED_UNIQUE_INDEX_BY_TABLE", "RUNTIME_TABLE_PRIVILEGES",
    "SCHEMA", "UnifiedYouTubeLedger", "WRITER_USER", "_comment_record", "_publish_log_record",
    "_video_record", "load_database_credential_file", "validate_exact_account_grants",
    "validate_required_schema_rows",
]
