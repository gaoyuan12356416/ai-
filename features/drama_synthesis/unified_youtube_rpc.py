"""Dedicated ads_ai YouTube ledger: full immutable facts, no legacy writes.

The caller cannot supply SQL. The approved existing database account is shared;
the write boundary is an application table allowlist, not database least privilege.
Schema/bootstrap is separate; update means exact compare-and-reuse only.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, Mapping

from .unified_youtube import (
    ALLOWED_ACTIONS, TABLE_BY_KIND, TABLE_TO_KIND, WRITER_HEALTH_CONTRACT,
    WRITER_CREDENTIAL_MODE, WRITER_WRITE_BOUNDARY,
    read_secure_owned_file, validate_controlled_operation,
    validate_entity_payload, validate_external_id,
)

SCHEMA = "ads_ai"
WRITER_USER = "ads_aius"
ACCOUNT_HOST = "43.166.187.96"
RUNTIME_TABLE_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE"})
TABLE_OWNERSHIP_COMMENT = "drama-synthesis:youtube-ledger:ads_ai:v2"
EXTERNAL_COLUMN_BY_KIND = {"video": "video_id", "comment": "comment_id", "publish_log": "publish_id"}


def _column_definition(column_type: str, *, nullable: str = "NO", default: Any = None,
                       charset: str | None = None, collation: str | None = None,
                       extra: str = "") -> tuple[Any, ...]:
    return (column_type, nullable, default, charset, collation, extra)


def _ascii(column_type: str, *, default: Any = None) -> tuple[Any, ...]:
    return _column_definition(column_type, default=default, charset="ascii", collation="ascii_bin")


def _text(column_type: str) -> tuple[Any, ...]:
    return _column_definition(column_type, charset="utf8mb4", collation="utf8mb4_bin")


_BASE_COLUMNS = {
    "id": _column_definition("bigint unsigned", extra="auto_increment"),
    "publish_id": _column_definition("bigint unsigned"),
    "video_id": _ascii("varchar(32)"),
}
_VIDEO_COLUMNS = {
    **_BASE_COLUMNS,
    "app_id": _column_definition("int unsigned"),
    "channel_local_id": _column_definition("int unsigned"),
    "operator_user_id": _text("varchar(128)"),
    "job_id": _ascii("char(32)"),
    "content_id": _text("varchar(256)"),
    "source_kind": _ascii("varchar(32)"),
    "source_url": _text("text"),
    "title": _text("varchar(100)"),
    "description_rendered": _text("text"),
    "privacy_status": _ascii("varchar(16)"),
    "published_at_utc": _ascii("varchar(32)"),
}
_AUDIT_COLUMNS = {
    "canary_operation_id": _ascii("varchar(128)", default=""),
    "payload_json": _text("longtext"),
    "payload_sha256": _ascii("char(64)"),
    "created_at": _column_definition("timestamp", default="CURRENT_TIMESTAMP"),
}
REQUIRED_COLUMN_DEFINITIONS = {
    "ads_youtube_videos": {**_VIDEO_COLUMNS, **_AUDIT_COLUMNS},
    "ads_youtube_comments": {
        **_BASE_COLUMNS,
        "comment_id": _ascii("varchar(255)"),
        "channel_local_id": _column_definition("int unsigned"),
        "operator_user_id": _text("varchar(128)"),
        "comment_text": _text("text"),
        "published_at_utc": _ascii("varchar(32)"),
        **_AUDIT_COLUMNS,
    },
    "ads_youtube_publish_log": {**_VIDEO_COLUMNS, **_AUDIT_COLUMNS},
}
REQUIRED_COLUMNS = {table: frozenset(columns) for table, columns in REQUIRED_COLUMN_DEFINITIONS.items()}
REQUIRED_INDEXES_BY_TABLE = {
    "ads_youtube_videos": {
        "PRIMARY": (0, ("id",)), "ux_ds_video_external": (0, ("video_id",)),
        "ux_ds_video_publish": (0, ("publish_id",)),
    },
    "ads_youtube_comments": {
        "PRIMARY": (0, ("id",)), "ux_ds_comment_external": (0, ("comment_id",)),
        "ux_ds_comment_publish": (0, ("publish_id",)), "ux_ds_comment_video": (0, ("video_id",)),
    },
    "ads_youtube_publish_log": {
        "PRIMARY": (0, ("id",)), "ux_ds_log_publish": (0, ("publish_id",)),
        "ux_ds_log_video": (0, ("video_id",)),
    },
}
RECORD_COLUMNS_BY_KIND = {
    kind: tuple(column for column in REQUIRED_COLUMN_DEFINITIONS[table] if column not in {"id", "created_at"})
    for kind, table in TABLE_BY_KIND.items()
}


class LedgerRPCError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code = str(code)
        self.status = int(status)
        super().__init__(self.code)


def _column_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    column_type = re.sub(r"\b(bigint|int)\(\d+\)", r"\1", str(row.get("COLUMN_TYPE") or "").lower())
    default = row.get("COLUMN_DEFAULT")
    if isinstance(default, str) and default.upper() in {"CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"}:
        default = "CURRENT_TIMESTAMP"
    extra = str(row.get("EXTRA") or "").lower()
    # MySQL 8 adds this marker to the same CURRENT_TIMESTAMP default.
    extra = re.sub(r"\bdefault_generated\b", "", extra).strip()
    return (
        column_type, str(row.get("IS_NULLABLE") or ""),
        None if default is None else str(default),
        row.get("CHARACTER_SET_NAME"), row.get("COLLATION_NAME"), extra,
    )


def validate_required_schema_rows(rows: Any, *, tables: Any = None) -> None:
    expected_tables = set(REQUIRED_COLUMNS if tables is None else tables)
    if not expected_tables <= set(REQUIRED_COLUMNS):
        raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
    seen = {table: {} for table in expected_tables}
    for row in rows:
        if not isinstance(row, Mapping):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        table, column = str(row.get("TABLE_NAME") or ""), str(row.get("COLUMN_NAME") or "")
        if table not in seen or not column or column in seen[table]:
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        seen[table][column] = row
    for table in expected_tables:
        definitions = REQUIRED_COLUMN_DEFINITIONS[table]
        if set(seen[table]) != set(definitions):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        for column, expected in definitions.items():
            if _column_signature(seen[table][column]) != expected:
                raise LedgerRPCError("youtube_sync_schema_mismatch", 503)


def validate_required_index_rows(rows: Any, *, tables: Any = None) -> None:
    expected_tables = set(REQUIRED_COLUMNS if tables is None else tables)
    if not expected_tables <= set(REQUIRED_COLUMNS):
        raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
    seen = {table: {} for table in expected_tables}
    for row in rows:
        if not isinstance(row, Mapping):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        table, index = str(row.get("TABLE_NAME") or ""), str(row.get("INDEX_NAME") or "")
        if (table not in seen or not index or row.get("SUB_PART") is not None
                or str(row.get("INDEX_TYPE") or "").upper() != "BTREE"
                or str(row.get("COLLATION") or "").upper() != "A"):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        seen[table].setdefault(index, []).append(row)
    for table in expected_tables:
        definitions = REQUIRED_INDEXES_BY_TABLE[table]
        if set(seen[table]) != set(definitions):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        for index, (non_unique, columns) in definitions.items():
            actual = sorted(seen[table][index], key=lambda row: int(row.get("SEQ_IN_INDEX") or 0))
            if len(actual) != len(columns):
                raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
            for position, (row, column) in enumerate(zip(actual, columns), 1):
                if (type(row.get("NON_UNIQUE")) is not int or row["NON_UNIQUE"] != non_unique
                        or int(row.get("SEQ_IN_INDEX") or 0) != position or row.get("COLUMN_NAME") != column):
                    raise LedgerRPCError("youtube_sync_schema_mismatch", 503)


def inspect_owned_tables(cursor: Any, *, allow_missing: bool = False,
                         inspect_triggers: bool = False) -> Dict[str, str]:
    """Check every object before any CREATE, without inspecting business rows.

    The caller must prove TRIGGER visibility before requesting an absence check.
    The shared runtime account and bootstrap both have that capability checked.
    """
    params = (SCHEMA,) + tuple(TABLE_BY_KIND.values())
    suffix = " WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (%s,%s,%s)"
    cursor.execute("SELECT TABLE_NAME,TABLE_TYPE,ENGINE,TABLE_COLLATION,TABLE_COMMENT "
                   "FROM information_schema.TABLES" + suffix, params)
    state = {table: "missing" for table in TABLE_BY_KIND.values()}
    for row in cursor.fetchall():
        if not isinstance(row, Mapping):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        table = str(row.get("TABLE_NAME") or "")
        if (table not in state or state[table] != "missing"
                or row.get("TABLE_TYPE") != "BASE TABLE" or row.get("ENGINE") != "InnoDB"
                or row.get("TABLE_COLLATION") != "utf8mb4_bin"
                or row.get("TABLE_COMMENT") != TABLE_OWNERSHIP_COMMENT):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
        state[table] = "compatible"
    existing = {table for table, status in state.items() if status == "compatible"}
    if not allow_missing and len(existing) != len(state):
        raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
    cursor.execute("SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,"
                   "CHARACTER_SET_NAME,COLLATION_NAME,EXTRA FROM information_schema.COLUMNS" + suffix, params)
    validate_required_schema_rows(cursor.fetchall(), tables=existing)
    cursor.execute("SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART,INDEX_TYPE,COLLATION "
                   "FROM information_schema.STATISTICS" + suffix, params)
    validate_required_index_rows(cursor.fetchall(), tables=existing)
    cursor.execute("SELECT TABLE_NAME,CONSTRAINT_NAME,REFERENCED_TABLE_SCHEMA,REFERENCED_TABLE_NAME "
                   "FROM information_schema.KEY_COLUMN_USAGE" + suffix
                   + " AND REFERENCED_TABLE_NAME IS NOT NULL", params)
    if list(cursor.fetchall()):
        raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
    if inspect_triggers:
        cursor.execute("SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS "
                       "WHERE TRIGGER_SCHEMA=%s AND EVENT_OBJECT_TABLE IN (%s,%s,%s)", params)
        if list(cursor.fetchall()):
            raise LedgerRPCError("youtube_sync_schema_mismatch", 503)
    return state


def validate_shared_account_capabilities(cursor: Any, current_user: str) -> str:
    """Prove required capabilities only, never claim database least privilege.

    All metadata queries are scoped to the fixed grantee and required
    capabilities. Other schemas, routine grants and account secrets are not
    queried. TRIGGER is required so an empty TRIGGERS result is meaningful.
    """
    if current_user != "%s@%s" % (WRITER_USER, ACCOUNT_HOST):
        raise LedgerRPCError("youtube_sync_database_identity_invalid", 503)
    grantee = "'%s'@'%s'" % (WRITER_USER, ACCOUNT_HOST)
    privileges = tuple(sorted(RUNTIME_TABLE_PRIVILEGES | {"TRIGGER"}))
    tables = tuple(TABLE_BY_KIND.values())
    # mysql.db can retain the literal underscore escape used by MySQL 5.7.
    # Do not interpret arbitrary SQL LIKE patterns as an approved schema.
    schema_names = (SCHEMA, r"ads\_ai")
    queries = (
        ("global", "SELECT PRIVILEGE_TYPE,IS_GRANTABLE FROM information_schema.USER_PRIVILEGES "
         "WHERE GRANTEE=%s AND PRIVILEGE_TYPE IN (%s,%s,%s,%s)", (grantee,) + privileges, 4),
        ("schema", "SELECT TABLE_SCHEMA,PRIVILEGE_TYPE,IS_GRANTABLE FROM information_schema.SCHEMA_PRIVILEGES "
         "WHERE GRANTEE=%s AND TABLE_SCHEMA IN (%s,%s) AND PRIVILEGE_TYPE IN (%s,%s,%s,%s)",
         (grantee,) + schema_names + privileges, 8),
        ("table", "SELECT TABLE_SCHEMA,TABLE_NAME,PRIVILEGE_TYPE,IS_GRANTABLE "
         "FROM information_schema.TABLE_PRIVILEGES WHERE GRANTEE=%s AND TABLE_SCHEMA=%s "
         "AND TABLE_NAME IN (%s,%s,%s) AND PRIVILEGE_TYPE IN (%s,%s,%s,%s)",
         (grantee, SCHEMA) + tables + privileges, 12),
    )
    effective = {table: set() for table in tables}
    observed = set()
    for scope, sql, params, maximum in queries:
        cursor.execute(sql, params)
        rows = list(cursor.fetchall())
        if len(rows) > maximum:
            raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
        for row in rows:
            if not isinstance(row, Mapping):
                raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
            privilege = row.get("PRIVILEGE_TYPE")
            grantable = row.get("IS_GRANTABLE")
            schema = row.get("TABLE_SCHEMA", "")
            table = row.get("TABLE_NAME", "")
            if (privilege not in privileges or grantable not in {"YES", "NO"}
                    or scope == "schema" and schema not in schema_names
                    or scope == "table" and (schema != SCHEMA or table not in effective)):
                raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
            item = (scope, schema, table, privilege, grantable)
            if item in observed:
                raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
            observed.add(item)
            for target in (tables if scope != "table" else (table,)):
                effective[target].add(privilege)
    if any(not RUNTIME_TABLE_PRIVILEGES <= values for values in effective.values()):
        raise LedgerRPCError("youtube_sync_grant_mismatch", 503)
    if any("TRIGGER" not in values for values in effective.values()):
        raise LedgerRPCError("youtube_sync_trigger_visibility_invalid", 503)
    canonical = {
        "account": current_user, "schema": SCHEMA, "credential_mode": WRITER_CREDENTIAL_MODE,
        "write_boundary": WRITER_WRITE_BOUNDARY, "required_capabilities": sorted(observed),
    }
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_database_credential_file(path_text: str, *, expected_user: str = WRITER_USER) -> Mapping[str, Any]:
    """The approved existing account only; target and driver keys stay fixed."""
    if expected_user != WRITER_USER:
        raise RuntimeError("writer database identity is invalid")
    try:
        raw = read_secure_owned_file(path_text, max_bytes=8192)
        value = json.loads(raw.decode("utf-8"))
    except (RuntimeError, UnicodeDecodeError, ValueError):
        value = None
    if not isinstance(value, Mapping) or set(value) != {"host", "port", "user", "password", "database"}:
        raise RuntimeError("writer database credential file is invalid")
    if (value.get("host") != "101.32.56.53" or type(value.get("port")) is not int
            or value["port"] != 63353 or value.get("database") != SCHEMA or value.get("user") != WRITER_USER):
        raise RuntimeError("writer database credential target is invalid")
    password = value.get("password")
    if type(password) is not str or not 1 <= len(password) <= 1024 or "\x00" in password:
        raise RuntimeError("writer database credential file is invalid")
    return dict(value)


def _record(kind: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    record = dict(payload)
    record["canary_operation_id"] = str(payload.get("canary_operation_id") or "")
    record["payload_json"] = serialized
    record["payload_sha256"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    columns = RECORD_COLUMNS_BY_KIND[kind]
    if set(record) != set(columns):
        raise LedgerRPCError("youtube_sync_contract_invalid")
    return {column: record[column] for column in columns}


def _video_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return _record("video", payload)


def _comment_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return _record("comment", payload)


def _publish_log_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return _record("publish_log", payload)


RECORD_BUILDER_BY_KIND = {"video": _video_record, "comment": _comment_record, "publish_log": _publish_log_record}


class UnifiedYouTubeLedger:
    """Immutable idempotent facts, confined to three owned ads_ai tables."""

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

    def _preflight(self, cursor: Any) -> Mapping[str, Any]:
        cursor.execute("SELECT DATABASE() AS database_name,@@read_only AS read_only,CURRENT_USER() AS account_name")
        identity = cursor.fetchone()
        if (not isinstance(identity, Mapping) or identity.get("database_name") != self.schema
                or type(identity.get("read_only")) is not int or identity["read_only"] != 0
                or identity.get("account_name") != "%s@%s" % (WRITER_USER, ACCOUNT_HOST)):
            raise LedgerRPCError("youtube_sync_database_identity_invalid", 503)
        grant_fingerprint = validate_shared_account_capabilities(cursor, str(identity["account_name"]))
        inspect_owned_tables(cursor, inspect_triggers=True)
        return {
            "ok": True, "contract": WRITER_HEALTH_CONTRACT, "schema": self.schema,
            "writer_identity": str(identity["account_name"]), "writable": True,
            "schema_verified": True, "indexes_verified": True, "grant_fingerprint": grant_fingerprint,
            "credential_mode": WRITER_CREDENTIAL_MODE, "write_boundary": WRITER_WRITE_BOUNDARY,
            "db_least_privilege": False, "triggers_verified": True, "foreign_keys_verified": True,
        }

    def health(self) -> Mapping[str, Any]:
        connection = self.connect_factory()
        try:
            with connection.cursor() as cursor:
                return self._preflight(cursor)
        finally:
            self._close(connection)

    @staticmethod
    def _row_matches(row: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
        for key, expected in record.items():
            actual = row.get(key)
            if type(expected) is int:
                if type(actual) is not int or actual != expected:
                    return False
            elif type(actual) is not str or actual.encode("utf-8") != str(expected).encode("utf-8"):
                return False
        return True

    def _find(self, cursor: Any, kind: str, external_id: str, *, lock: bool,
              record: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]:
        table = TABLE_BY_KIND[kind]
        external_column = EXTERNAL_COLUMN_BY_KIND[kind]
        columns = ",".join(("id",) + RECORD_COLUMNS_BY_KIND[kind])
        keys = {external_column: int(external_id) if kind == "publish_log" else external_id}
        if record is not None:
            keys["publish_id"] = record["publish_id"]
            keys["video_id"] = record["video_id"]
        predicate = " OR ".join("%s=%%s" % key for key in keys)
        sql = "SELECT %s FROM %s.%s WHERE %s LIMIT 3%s" % (
            columns, self.schema, table, predicate, " FOR UPDATE" if lock else "",
        )
        cursor.execute(sql, tuple(keys.values()))
        rows = list(cursor.fetchall())
        if len(rows) > 1:
            raise LedgerRPCError("youtube_sync_identity_conflict")
        return rows

    def execute(self, action: str, table: str, external_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_controlled_operation(action, table)
        kind = TABLE_TO_KIND[table]
        validate_external_id(kind, external_id)
        if action == "select":
            if not isinstance(payload, Mapping) or payload:
                raise LedgerRPCError("youtube_sync_contract_invalid")
            record = None
        else:
            record = RECORD_BUILDER_BY_KIND[kind](validate_entity_payload(kind, external_id, payload))
        connection = self.connect_factory()
        try:
            with connection.cursor() as cursor:
                self._preflight(cursor)
                if action == "select":
                    return {"found": bool(self._find(cursor, kind, external_id, lock=False))}
                connection.begin()
                rows = self._find(cursor, kind, external_id, lock=True, record=record)
                if rows:
                    if not self._row_matches(rows[0], record):
                        raise LedgerRPCError("youtube_sync_identity_conflict")
                    connection.commit()
                    return {"idempotent_success": True, "reused": True}
                if action == "update":
                    raise LedgerRPCError("youtube_sync_identity_missing")
                columns = tuple(record)
                sql = "INSERT INTO %s.%s (%s) VALUES (%s)" % (
                    self.schema, table, ",".join(columns), ",".join(["%s"] * len(columns)),
                )
                try:
                    cursor.execute(sql, tuple(record[column] for column in columns))
                except Exception as exc:
                    # Handle a unique-key race without rewriting an existing fact.
                    if not exc.args or exc.args[0] != 1062:
                        raise
                    connection.rollback()
                    connection.begin()
                    rows = self._find(cursor, kind, external_id, lock=True, record=record)
                    if len(rows) != 1 or not self._row_matches(rows[0], record):
                        raise LedgerRPCError("youtube_sync_identity_conflict") from None
                    connection.commit()
                    return {"idempotent_success": True, "reused": True}
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
    "REQUIRED_COLUMN_DEFINITIONS", "REQUIRED_COLUMNS", "REQUIRED_INDEXES_BY_TABLE",
    "RECORD_COLUMNS_BY_KIND", "RUNTIME_TABLE_PRIVILEGES", "SCHEMA", "TABLE_OWNERSHIP_COMMENT",
    "UnifiedYouTubeLedger", "WRITER_USER", "_comment_record", "_publish_log_record", "_video_record",
    "inspect_owned_tables", "load_database_credential_file", "validate_shared_account_capabilities",
    "validate_required_schema_rows", "validate_required_index_rows",
]
