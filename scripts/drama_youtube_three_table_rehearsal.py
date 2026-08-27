#!/usr/bin/env python3
"""Bounded three-table backup and loopback-only MySQL 5.7 rehearsal.

This is not a CynosDB cluster backup/restore attestation. Export uses one
READ ONLY consistent snapshot on the fixed reader. Restoration and DDL can
only reach an inspected local Docker container, never the production writer.
Provision the empty schema/container separately; this tool never drops data,
creates accounts, starts Docker, or reads an application environment file.

All artifacts contain private legacy data and must remain outside Git. Only
hashes, counts and safe status fields are printed. A failed run leaves its
partial files/database for inspection, with no successful evidence file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pymysql


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import migrate_drama_youtube_unified_schema as migration  # noqa: E402
from features.drama_synthesis.unified_youtube import read_secure_owned_file  # noqa: E402


SOURCE_PORT = 63350
REHEARSAL_PORT = 23357
REHEARSAL_USER = "drama_rehearsal"
MAX_SNAPSHOT_BYTES = 2 * 1024**3
MIN_FREE_BYTES = 4 * 1024**3
MANIFEST_NAME = "snapshot-manifest.json"
REPORT_NAME = "rehearsal-result.json"
EVIDENCE_NAME = "backup-evidence.json"
CONTEXT_RE = re.compile(r"[0-9a-f]{16}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
TABLES = tuple(migration.MIGRATIONS)
LEGACY_COLUMNS = {
    table: tuple(column for column in definitions if column != migration.MIGRATIONS[table]["column"])
    for table, definitions in migration.REQUIRED_COLUMN_DEFINITIONS.items()
}
CANDIDATE_FILES = (
    "scripts/migrate_drama_youtube_unified_schema.py",
    "scripts/drama_youtube_three_table_rehearsal.py",
    "features/drama_synthesis/unified_youtube_rpc.py",
    "features/drama_synthesis/unified_youtube.py",
    "features/drama_synthesis/core.py",
)
COLUMN_FIELDS = (
    "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION", "COLUMN_TYPE", "IS_NULLABLE",
    "COLUMN_DEFAULT", "CHARACTER_SET_NAME", "COLLATION_NAME", "EXTRA", "COLUMN_COMMENT",
)
INDEX_FIELDS = (
    "TABLE_NAME", "INDEX_NAME", "NON_UNIQUE", "SEQ_IN_INDEX", "COLUMN_NAME",
    "COLLATION", "SUB_PART", "PACKED", "NULLABLE", "INDEX_TYPE", "COMMENT", "INDEX_COMMENT",
)
EVIDENCE_KEYS = {
    "verification_source", "cluster_id", "schema", "backup_id", "backup_status",
    "backup_completed_at_utc", "verified_at_utc", "rehearsal_status", "rehearsal_at_utc",
    "migration_contract_sha256", "source_contract_sha256", "candidate_git_sha",
    "candidate_code_sha256", "snapshot_manifest_sha256", "inventory_sha256",
    "rehearsal_result_sha256", "rehearsal_context", "rehearsal_port",
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


SOURCE_CONTRACT_SHA256 = _sha({
    table: {column: list(migration.REQUIRED_COLUMN_DEFINITIONS[table][column]) for column in columns}
    for table, columns in LEGACY_COLUMNS.items()
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _context(context: str, port: int = REHEARSAL_PORT) -> tuple[str, str, Path]:
    if not isinstance(context, str) or not CONTEXT_RE.fullmatch(context) or type(port) is not int or port != REHEARSAL_PORT:
        raise RuntimeError("rehearsal context or fixed loopback port is invalid")
    name = "drama-youtube-rehearsal-" + context
    return "drama_youtube_rehearsal_" + context, name, Path("/mnt/data-disk") / name / "mysql"


def _private_dir(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute() or not path.is_dir() or path.resolve() != path:
        raise RuntimeError("snapshot directory must be an absolute non-symlink directory")
    if path == ROOT or ROOT in path.parents:
        raise RuntimeError("private snapshot artifacts must stay outside the repository")
    metadata = path.stat()
    if os.name != "nt" and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise RuntimeError("snapshot directory owner or mode is unsafe")
    return path


@contextmanager
def _private_file(path: Path, *, create: bool = False):
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("private artifact path is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except OSError:
        raise RuntimeError("private artifact is unavailable or already exists") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SNAPSHOT_BYTES:
            raise RuntimeError("private artifact is invalid or oversized")
        if os.name != "nt" and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise RuntimeError("private artifact owner or mode is unsafe")
        with os.fdopen(descriptor, "wb" if create else "rb") as handle:
            descriptor = -1
            yield handle
            if create:
                handle.flush()
                os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json(path: Path, value: Any) -> str:
    raw = _json_bytes(value)
    with _private_file(path, create=True) as handle:
        handle.write(raw)
    if os.name != "nt":
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        raw = read_secure_owned_file(str(path), max_bytes=512 * 1024)
        value = json.loads(raw)
    except (RuntimeError, ValueError, UnicodeDecodeError):
        raise RuntimeError("private evidence artifact is invalid") from None
    if not isinstance(value, Mapping):
        raise RuntimeError("private evidence artifact is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _candidate_files(candidate_git_sha: str, *, require_git: bool) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_git_sha or ""):
        raise RuntimeError("an exact candidate git sha is required")
    if require_git:
        try:
            actual = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            ).stdout.decode("ascii").strip()
            changed = subprocess.run(
                ["git", "-C", str(ROOT), "status", "--porcelain", "--", *CANDIDATE_FILES], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise RuntimeError("candidate checkout cannot be verified") from None
        if actual != candidate_git_sha or changed.strip():
            raise RuntimeError("candidate checkout does not match the clean reviewed git sha")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CANDIDATE_FILES}


def _credential(path_text: str, *, source: bool, context: str = "", port: int = REHEARSAL_PORT) -> Mapping[str, Any]:
    value, _ = _read_json(Path(path_text))
    if set(value) != {"host", "port", "user", "password", "database"}:
        raise RuntimeError("scoped database credential file is invalid")
    expected_schema = migration.SCHEMA if source else _context(context, port)[0]
    if (
        value.get("host") != (migration.HOST if source else "127.0.0.1")
        or type(value.get("port")) is not int
        or value.get("port") != (SOURCE_PORT if source else REHEARSAL_PORT)
        or value.get("database") != expected_schema
        or not isinstance(value.get("user"), str)
        or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", value["user"])
        or (not source and value["user"] != REHEARSAL_USER)
        or not isinstance(value.get("password"), str)
        or not (1 if source else 32) <= len(value["password"]) <= 1024
        or any(ord(char) < 32 for char in value["password"])
    ):
        raise RuntimeError("scoped database target or credential is invalid")
    return value


def _connect(config: Mapping[str, Any], *, source: bool, context: str = "", port: int = REHEARSAL_PORT):
    # Repeat the endpoint check immediately adjacent to the only connection call.
    expected = (migration.HOST, SOURCE_PORT, migration.SCHEMA) if source else ("127.0.0.1", REHEARSAL_PORT, _context(context, port)[0])
    if (
        set(config) != {"host", "port", "database", "user", "password"}
        or (config.get("host"), config.get("port"), config.get("database")) != expected
        or (not source and config.get("user") != REHEARSAL_USER)
    ):
        raise RuntimeError("snapshot/rehearsal database target is invalid")
    return pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"],
        password=config["password"], database=config["database"],
        charset="utf8mb4", autocommit=False, connect_timeout=5,
        read_timeout=180, write_timeout=180, local_infile=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _headroom(path: Path, estimated_bytes: int) -> None:
    if estimated_bytes < 0 or estimated_bytes > MAX_SNAPSHOT_BYTES or shutil.disk_usage(path).free < max(MIN_FREE_BYTES, estimated_bytes * 6):
        raise RuntimeError("snapshot/rehearsal data disk headroom is insufficient")


def _require_data_disk(directory: Path) -> None:
    if (
        os.name != "posix" or os.geteuid() != 0
        or Path("/mnt/data-disk") not in directory.parents
        or not os.path.ismount("/mnt/data-disk")
    ):
        raise RuntimeError("snapshot/rehearsal execution requires root and a real /mnt/data-disk mount")


def _safe_create_sql(sql: Any, table: str, schema: str) -> str:
    prefix = "CREATE TABLE `%s` (" % table
    if (
        table not in TABLES or not isinstance(sql, str) or not sql.startswith(prefix)
        or not re.search(r"\) ENGINE=InnoDB(?: |$)", sql)
        or any(token in sql for token in (";", "/*", "--", "\n#"))
        or re.search(r"\b(?:FOREIGN\s+KEY|REFERENCES|DATA\s+DIRECTORY|INDEX\s+DIRECTORY|CONNECTION|UNION|PARTITION|AS\s+SELECT|LIKE)\b", sql, re.I)
        or not re.fullmatch(r"drama_youtube_rehearsal_[0-9a-f]{16}", schema)
    ):
        raise RuntimeError("snapshot CREATE TABLE is outside the isolated restore contract")
    return sql.replace("CREATE TABLE `%s`" % table, "CREATE TABLE `%s`.`%s`" % (schema, table), 1)


def _schema_metadata(cursor: Any, schema: str, table: str) -> Mapping[str, Any]:
    cursor.execute(
        "SELECT " + ",".join(COLUMN_FIELDS) + " FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (schema, table),
    )
    columns = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT " + ",".join(INDEX_FIELDS) + " FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX", (schema, table),
    )
    indexes = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT ENGINE,TABLE_COLLATION,CREATE_OPTIONS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (schema, table),
    )
    options = cursor.fetchone()
    if not isinstance(options, Mapping) or options.get("ENGINE") != "InnoDB":
        raise RuntimeError("all snapshot tables must exist and use InnoDB")
    return {"columns": columns, "indexes": indexes, "table_options": dict(options)}


def _legacy_metadata(metadata: Mapping[str, Any], table: str) -> Mapping[str, Any]:
    spec = migration.MIGRATIONS[table]
    return {
        "columns": [row for row in metadata["columns"] if row["COLUMN_NAME"] != spec["column"]],
        "indexes": [row for row in metadata["indexes"] if row["INDEX_NAME"] != spec["index"]],
        "table_options": metadata["table_options"],
    }


def _validate_source_schemas(schemas: Mapping[str, Mapping[str, Any]]) -> None:
    if set(schemas) != set(TABLES):
        raise RuntimeError("snapshot table inventory is invalid")
    rows = []
    for table, value in schemas.items():
        if set(value) != {"create_sql", "columns", "indexes", "table_options"}:
            raise RuntimeError("snapshot schema artifact is invalid")
        columns = value["columns"]
        if (
            not isinstance(columns, list)
            or len(columns) != len(LEGACY_COLUMNS[table])
            or {row.get("COLUMN_NAME") for row in columns} != set(LEGACY_COLUMNS[table])
            or any(set(row) != set(COLUMN_FIELDS) or row["TABLE_NAME"] != table for row in columns)
            or [row["ORDINAL_POSITION"] for row in columns] != list(range(1, len(columns) + 1))
            or value["table_options"].get("ENGINE") != "InnoDB"
        ):
            raise RuntimeError("snapshot does not contain the exact legacy column contract")
        indexes = value["indexes"]
        primary = [row for row in indexes if row.get("INDEX_NAME") == "PRIMARY"]
        if (
            len(primary) != 1 or primary[0].get("COLUMN_NAME") != "id"
            or primary[0].get("NON_UNIQUE") != 0 or primary[0].get("SEQ_IN_INDEX") != 1
            or any(set(row) != set(INDEX_FIELDS) or row["TABLE_NAME"] != table or row["COLUMN_NAME"] not in LEGACY_COLUMNS[table] for row in indexes)
        ):
            raise RuntimeError("snapshot legacy index contract is invalid")
        rows.extend(columns)
        _safe_create_sql(value["create_sql"], table, "drama_youtube_rehearsal_" + "0" * 16)
    migration.validate_required_schema_rows(rows, require_external=False)


def _row_bytes(row: Any) -> bytes:
    normalized = [value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value for value in row]
    if any(type(value) not in (int, str, type(None)) for value in normalized):
        raise RuntimeError("snapshot row has an unsupported legacy value type")
    return _json_bytes(normalized) + b"\n"


def _scan_table(connection: Any, schema: str, table: str, *, output: Any = None, remaining_bytes: int = MAX_SNAPSHOT_BYTES) -> Mapping[str, Any]:
    digest = hashlib.sha256()
    count = 0
    size = 0
    previous_id = -1
    with connection.cursor(pymysql.cursors.SSCursor) as cursor:
        cursor.execute(
            "SELECT %s FROM `%s`.`%s` ORDER BY `id`" % (
                ",".join("`%s`" % column for column in LEGACY_COLUMNS[table]), schema, table,
            )
        )
        while True:
            batch = cursor.fetchmany(200)
            if not batch:
                break
            for row in batch:
                if len(row) != len(LEGACY_COLUMNS[table]) or type(row[0]) is not int or row[0] <= previous_id:
                    raise RuntimeError("snapshot primary-key ordering is invalid")
                previous_id = row[0]
                raw = _row_bytes(row)
                size += len(raw)
                if size > remaining_bytes:
                    raise RuntimeError("three-table snapshot exceeds the fixed size limit")
                digest.update(raw)
                if output is not None:
                    output.write(raw)
                count += 1
    return {"row_count": count, "rows_bytes": size, "rows_sha256": digest.hexdigest()}


def export_snapshot(credential_file: str, *, snapshot_dir: str, candidate_git_sha: str, context: str) -> Mapping[str, Any]:
    _context(context)
    directory = _private_dir(snapshot_dir)
    _require_data_disk(directory)
    if any(directory.iterdir()):
        raise RuntimeError("snapshot export requires a new empty private directory")
    candidate_files = _candidate_files(candidate_git_sha, require_git=True)
    config = _credential(credential_file, source=True)
    _headroom(directory, 0)
    connection = _connect(config, source=True)
    inventory = {}
    started = _now()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATABASE() AS schema_name,CURRENT_USER() AS account_name,@@read_only AS read_only,"
                "@@server_uuid AS server_uuid,@@version AS version,@@sql_mode AS sql_mode"
            )
            identity = cursor.fetchone()
            if (
                identity.get("schema_name") != migration.SCHEMA or int(identity.get("read_only", -1)) != 1
                or not str(identity.get("account_name", "")).startswith(str(config["user"]) + "@")
                or not UUID_RE.fullmatch(str(identity.get("server_uuid", "")))
                or not str(identity.get("version", "")).startswith("5.7.")
                or not re.fullmatch(r"[A-Z0-9_,]*", str(identity.get("sql_mode", "")))
            ):
                raise RuntimeError("source reader identity, version or read-only guard failed")
            cursor.execute("SET SESSION time_zone = '+00:00'")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            # Acquire all three metadata locks before recording schema or data.
            for table in TABLES:
                cursor.execute("SELECT `id` FROM `%s`.`%s` LIMIT 0" % (migration.SCHEMA, table))
                cursor.fetchall()
            schemas = {}
            for table in TABLES:
                metadata = _schema_metadata(cursor, migration.SCHEMA, table)
                cursor.execute("SHOW CREATE TABLE `%s`.`%s`" % (migration.SCHEMA, table))
                create = cursor.fetchone()
                schemas[table] = dict(metadata, create_sql=create.get("Create Table"))
            _validate_source_schemas(schemas)
        used = 0
        for table in TABLES:
            schema = schemas[table]
            schema_name = table + ".schema.json"
            rows_name = table + ".rows.ndjson"
            schema_sha = _write_json(directory / schema_name, schema)
            with _private_file(directory / rows_name, create=True) as output:
                summary = _scan_table(connection, migration.SCHEMA, table, output=output, remaining_bytes=MAX_SNAPSHOT_BYTES - used)
            used += summary["rows_bytes"]
            inventory[table] = dict(
                summary, schema_file=schema_name, schema_sha256=schema_sha, rows_file=rows_name,
                legacy_schema_sha256=_sha(_legacy_metadata(schema, table)), indexes_sha256=_sha(schema["indexes"]),
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS row_count FROM `%s`.`%s`" % (migration.SCHEMA, table))
                if cursor.fetchone()["row_count"] != summary["row_count"] or _schema_metadata(cursor, migration.SCHEMA, table) != _legacy_metadata(schema, table):
                    raise RuntimeError("source schema/data changed outside the consistent snapshot contract")
        connection.rollback()
    finally:
        connection.close()
    # This manifest is written only after the read-only connection is closed.
    manifest = {
        "format_version": 1, "kind": "three_table_read_only_snapshot", "cluster_id": migration.CLUSTER_ID,
        "schema": migration.SCHEMA, "context": context, "source_connection_closed": True,
        "source": {
            "host": migration.HOST, "port": SOURCE_PORT, "schema": migration.SCHEMA,
            "server_uuid": identity["server_uuid"], "version": identity["version"], "read_only": 1,
            "account_sha256": hashlib.sha256(identity["account_name"].encode("utf-8")).hexdigest(),
            "transaction": "REPEATABLE READ / WITH CONSISTENT SNAPSHOT / READ ONLY",
            "time_zone": "+00:00", "sql_mode": identity["sql_mode"],
        },
        "snapshot_started_at_utc": started, "snapshot_completed_at_utc": _now(),
        "migration_contract_sha256": migration.MIGRATION_CONTRACT_SHA256,
        "source_contract_sha256": SOURCE_CONTRACT_SHA256, "candidate_git_sha": candidate_git_sha,
        "candidate_files": candidate_files, "inventory": inventory, "inventory_sha256": _sha(inventory),
    }
    manifest_sha = _write_json(directory / MANIFEST_NAME, manifest)
    return {"ok": True, "mode": "snapshot_only_not_rehearsed", "snapshot_manifest_sha256": manifest_sha,
            "inventory_sha256": manifest["inventory_sha256"], "row_counts": {table: item["row_count"] for table, item in inventory.items()}}


def _read_rows(path: Path, table: str):
    previous_id = -1
    with _private_file(path) as handle:
        while True:
            raw = handle.readline(1024 * 1024 + 1)
            if not raw:
                break
            if len(raw) > 1024 * 1024:
                raise RuntimeError("snapshot row exceeds the legacy contract bound")
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError("snapshot row encoding is invalid") from None
            if (
                not isinstance(row, list) or len(row) != len(LEGACY_COLUMNS[table])
                or type(row[0]) is not int or row[0] <= previous_id or _row_bytes(row) != raw
            ):
                raise RuntimeError("snapshot row contract is invalid")
            previous_id = row[0]
            yield raw, row


def _load_snapshot(directory: Path, manifest_sha: str, candidate_git_sha: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    manifest, actual_sha = _read_json(directory / MANIFEST_NAME)
    if not SHA_RE.fullmatch(manifest_sha or "") or actual_sha != manifest_sha:
        raise RuntimeError("snapshot manifest fingerprint does not match")
    _context(str(manifest.get("context", "")))
    source = manifest.get("source", {})
    if (
        manifest.get("format_version") != 1 or manifest.get("kind") != "three_table_read_only_snapshot"
        or manifest.get("cluster_id") != migration.CLUSTER_ID or manifest.get("schema") != migration.SCHEMA
        or manifest.get("source_connection_closed") is not True
        or manifest.get("migration_contract_sha256") != migration.MIGRATION_CONTRACT_SHA256
        or manifest.get("source_contract_sha256") != SOURCE_CONTRACT_SHA256
        or manifest.get("candidate_git_sha") != candidate_git_sha
        or manifest.get("candidate_files") != _candidate_files(candidate_git_sha, require_git=False)
        or source.get("host") != migration.HOST or source.get("port") != SOURCE_PORT
        or source.get("schema") != migration.SCHEMA or source.get("read_only") != 1
        or source.get("transaction") != "REPEATABLE READ / WITH CONSISTENT SNAPSHOT / READ ONLY"
        or source.get("time_zone") != "+00:00" or not re.fullmatch(r"[A-Z0-9_,]*", str(source.get("sql_mode", "")))
        or not UUID_RE.fullmatch(str(source.get("server_uuid", "")))
        or not str(source.get("version", "")).startswith("5.7.")
        or not SHA_RE.fullmatch(str(source.get("account_sha256", "")))
    ):
        raise RuntimeError("snapshot source/candidate contract is invalid")
    started = migration._utc_timestamp(manifest.get("snapshot_started_at_utc"))
    completed = migration._utc_timestamp(manifest.get("snapshot_completed_at_utc"))
    now = datetime.now(timezone.utc)
    if not started <= completed <= now + timedelta(minutes=5) or now - started > timedelta(hours=48):
        raise RuntimeError("snapshot timestamps are stale or inconsistent")
    inventory = manifest.get("inventory", {})
    if set(inventory) != set(TABLES) or manifest.get("inventory_sha256") != _sha(inventory):
        raise RuntimeError("snapshot inventory fingerprint is invalid")
    schemas = {}
    total_bytes = 0
    for table, item in inventory.items():
        if (
            set(item) != {"schema_file", "schema_sha256", "rows_file", "rows_sha256", "rows_bytes", "row_count", "legacy_schema_sha256", "indexes_sha256"}
            or item.get("schema_file") != table + ".schema.json" or item.get("rows_file") != table + ".rows.ndjson"
            or type(item.get("rows_bytes")) is not int or not 0 <= item["rows_bytes"] <= MAX_SNAPSHOT_BYTES
            or type(item.get("row_count")) is not int or item["row_count"] < 0
        ):
            raise RuntimeError("snapshot table file inventory is invalid")
        schema, schema_sha = _read_json(directory / item["schema_file"])
        if schema_sha != item.get("schema_sha256") or _sha(_legacy_metadata(schema, table)) != item.get("legacy_schema_sha256") or _sha(schema["indexes"]) != item.get("indexes_sha256"):
            raise RuntimeError("snapshot schema file fingerprint is invalid")
        schemas[table] = schema
        digest = hashlib.sha256()
        count = size = 0
        for raw, _row in _read_rows(directory / item["rows_file"], table):
            digest.update(raw)
            count += 1
            size += len(raw)
            if total_bytes + size > MAX_SNAPSHOT_BYTES:
                raise RuntimeError("snapshot exceeds its total size limit")
        if (digest.hexdigest(), count, size) != (item["rows_sha256"], item["row_count"], item["rows_bytes"]):
            raise RuntimeError("snapshot data hash, row count or size does not match")
        total_bytes += size
    _validate_source_schemas(schemas)
    return manifest, schemas


def _inspect_container(context: str, port: int) -> Mapping[str, Any]:
    _schema, name, _datadir = _context(context, port)
    # Deliberately do not request Config.Env: it can contain database passwords.
    template = '{"container_id":{{json .Id}},"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"hostname":{{json .Config.Hostname}},"running":{{json .State.Running}},"network_mode":{{json .HostConfig.NetworkMode}},"publish_all_ports":{{json .HostConfig.PublishAllPorts}},"port_bindings":{{json .HostConfig.PortBindings}},"actual_ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}'
    try:
        result = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format", template, name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
        )
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError):
        raise RuntimeError("local rehearsal container inspection failed") from None
    target = _validate_container(value, context, port)
    datadir = Path(target["datadir"])
    if not datadir.is_dir() or datadir.resolve() != datadir:
        raise RuntimeError("rehearsal datadir must be an existing non-symlink bind directory")
    return target


def _validate_container(value: Mapping[str, Any], context: str, port: int) -> Mapping[str, Any]:
    schema, name, datadir = _context(context, port)
    bindings = {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
    mounts = value.get("mounts", [])
    if (
        not SHA_RE.fullmatch(str(value.get("container_id", "")))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("image_id", "")))
        or not re.fullmatch(r"(?:docker.io/library/)?mysql(?::5\.7(?:\.\d+)?)?@sha256:[0-9a-f]{64}", str(value.get("image_reference", "")))
        or value.get("running") is not True or value.get("network_mode") != "bridge"
        or value.get("publish_all_ports") is not False
        or {key: item for key, item in value.get("port_bindings", {}).items() if item} != bindings
        or {key: item for key, item in value.get("actual_ports", {}).items() if item} != bindings
        or len(mounts) != 1 or mounts[0].get("Type") != "bind"
        or mounts[0].get("Source") != str(datadir) or mounts[0].get("Destination") != "/var/lib/mysql"
        or mounts[0].get("RW") is not True
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(value.get("hostname", "")))
    ):
        raise RuntimeError("rehearsal container is not isolated, pinned and data-disk backed")
    return {"host": "127.0.0.1", "port": port, "schema": schema, "container_name": name,
            "container_id": value["container_id"], "image_id": value["image_id"],
            "image_reference": value["image_reference"], "hostname": value["hostname"],
            "datadir": str(datadir), "inspection": dict(value)}


def _projection(connection: Any, schema: str, *, require_external_null: bool) -> Mapping[str, Any]:
    result = {}
    for table in TABLES:
        summary = _scan_table(connection, schema, table)
        with connection.cursor() as cursor:
            metadata = _legacy_metadata(_schema_metadata(cursor, schema, table), table)
            if require_external_null:
                cursor.execute("SELECT COUNT(*) AS nonnull_count FROM `%s`.`%s` WHERE `%s` IS NOT NULL" % (schema, table, migration.MIGRATIONS[table]["column"]))
                if cursor.fetchone()["nonnull_count"] != 0:
                    raise RuntimeError("rehearsal unexpectedly changed legacy external-id NULL values")
        result[table] = {"row_count": summary["row_count"], "rows_sha256": summary["rows_sha256"],
                         "legacy_schema_sha256": _sha(metadata), "indexes_sha256": _sha(metadata["indexes"])}
    return result


def _expected_projection(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    return {table: {key: item[key] for key in ("row_count", "rows_sha256", "legacy_schema_sha256", "indexes_sha256")}
            for table, item in manifest["inventory"].items()}


def _expected_runs() -> list[Mapping[str, Any]]:
    plan = [{"table": table, "action": "add_column_and_unique_index"} for table in TABLES]
    return [
        {"mode": "dry-run", "plan": plan, "applied": [], "complete": False},
        {"mode": "apply", "plan": plan, "applied": plan, "complete": True},
        {"mode": "apply", "plan": [], "applied": [], "complete": True},
        {"mode": "dry-run", "plan": [], "applied": [], "complete": True},
    ]


def rehearse_loopback(credential_file: str, *, snapshot_dir: str, snapshot_manifest_sha256: str, candidate_git_sha: str, context: str, port: int) -> Mapping[str, Any]:
    schema, _name, _datadir = _context(context, port)
    directory = _private_dir(snapshot_dir)
    _require_data_disk(directory)
    if any((directory / filename).exists() for filename in (REPORT_NAME, EVIDENCE_NAME)):
        raise RuntimeError("rehearsal evidence must not be overwritten")
    _candidate_files(candidate_git_sha, require_git=True)
    manifest, schemas = _load_snapshot(directory, snapshot_manifest_sha256, candidate_git_sha)
    if manifest["context"] != context:
        raise RuntimeError("snapshot/rehearsal context mismatch")
    config = _credential(credential_file, source=False, context=context, port=port)
    target = _inspect_container(context, port)
    _headroom(directory, sum(item["rows_bytes"] for item in manifest["inventory"].values()))
    _headroom(Path(target["datadir"]), sum(item["rows_bytes"] for item in manifest["inventory"].values()))
    connection = _connect(config, source=False, context=context, port=port)
    started = _now()
    runs = []
    projections = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATABASE() AS schema_name,CURRENT_USER() AS account_name,@@read_only AS read_only,"
                "@@server_uuid AS server_uuid,@@version AS version,@@hostname AS hostname,@@port AS server_port"
            )
            identity = cursor.fetchone()
            if (
                identity.get("schema_name") != schema or int(identity.get("read_only", -1)) != 0
                or not str(identity.get("account_name", "")).startswith(REHEARSAL_USER + "@")
                or identity.get("hostname") != target["hostname"] or identity.get("server_port") != 3306
                or not UUID_RE.fullmatch(str(identity.get("server_uuid", "")))
                or identity.get("server_uuid") == manifest["source"]["server_uuid"]
                or not str(identity.get("version", "")).startswith("5.7.")
            ):
                raise RuntimeError("loopback rehearsal database identity is invalid")
            cursor.execute("SELECT COUNT(*) AS table_count FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (schema,))
            if cursor.fetchone()["table_count"] != 0:
                raise RuntimeError("restoration requires a completely empty isolated schema")
            cursor.execute("SET SESSION time_zone = '+00:00'")
            sql_modes = set(filter(None, manifest["source"]["sql_mode"].split(","))) | {"NO_AUTO_VALUE_ON_ZERO"}
            cursor.execute("SET SESSION sql_mode=%s", (",".join(sorted(sql_modes)),))
            for table in TABLES:
                cursor.execute(_safe_create_sql(schemas[table]["create_sql"], table, schema))
                insert_sql = "INSERT INTO `%s`.`%s` (%s) VALUES (%s)" % (
                    schema, table, ",".join("`%s`" % column for column in LEGACY_COLUMNS[table]),
                    ",".join(["%s"] * len(LEGACY_COLUMNS[table])),
                )
                batch = []
                for _raw, row in _read_rows(directory / manifest["inventory"][table]["rows_file"], table):
                    batch.append(row)
                    if len(batch) == 100:
                        cursor.executemany(insert_sql, batch)
                        batch = []
                if batch:
                    cursor.executemany(insert_sql, batch)
                connection.commit()
        projections["restored"] = _projection(connection, schema, require_external_null=False)
        if projections["restored"] != _expected_projection(manifest):
            raise RuntimeError("restored legacy rows/schema/indexes do not match the snapshot")
        for number, apply in enumerate((False, True, True, False)):
            with connection.cursor() as cursor:
                run = migration._run_migration(cursor, apply=apply, schema=schema)
            runs.append(dict(run, mode="apply" if apply else "dry-run"))
            if number in (1, 2):
                projections["after_first" if number == 1 else "after_second"] = _projection(connection, schema, require_external_null=True)
        if runs != _expected_runs() or any(value != _expected_projection(manifest) for value in projections.values()):
            raise RuntimeError("rehearsal migration, idempotency or legacy invariants failed")
        connection.rollback()
    finally:
        connection.close()
    target = dict(target, server_uuid=identity["server_uuid"], version=identity["version"], server_port=3306, read_only=0)
    report = {
        "format_version": 1, "verification_source": "table_snapshot_rehearsal", "status": "PASS",
        "started_at_utc": started, "completed_at_utc": _now(), "context": context, "target": target,
        "snapshot_manifest_sha256": snapshot_manifest_sha256, "inventory_sha256": manifest["inventory_sha256"],
        "migration_contract_sha256": migration.MIGRATION_CONTRACT_SHA256, "source_contract_sha256": SOURCE_CONTRACT_SHA256,
        "candidate_git_sha": candidate_git_sha, "candidate_code_sha256": _sha(manifest["candidate_files"]),
        "runs": runs, "runs_sha256": _sha(runs), "projections": projections,
        "projections_sha256": _sha(projections), "external_columns_all_null": True, "target_connection_closed": True,
    }
    report_sha = _write_json(directory / REPORT_NAME, report)
    evidence = {
        "verification_source": "table_snapshot_rehearsal", "cluster_id": migration.CLUSTER_ID, "schema": migration.SCHEMA,
        "backup_id": "table-snapshot-" + snapshot_manifest_sha256, "backup_status": "SUCCESS",
        "backup_completed_at_utc": manifest["snapshot_completed_at_utc"], "verified_at_utc": _now(),
        "rehearsal_status": "PASS", "rehearsal_at_utc": report["completed_at_utc"],
        "migration_contract_sha256": migration.MIGRATION_CONTRACT_SHA256, "source_contract_sha256": SOURCE_CONTRACT_SHA256,
        "candidate_git_sha": candidate_git_sha, "candidate_code_sha256": report["candidate_code_sha256"],
        "snapshot_manifest_sha256": snapshot_manifest_sha256, "inventory_sha256": manifest["inventory_sha256"],
        "rehearsal_result_sha256": report_sha, "rehearsal_context": context, "rehearsal_port": port,
    }
    validate_table_snapshot_evidence(str(directory / EVIDENCE_NAME), evidence, candidate_git_sha=candidate_git_sha)
    evidence_sha = _write_json(directory / EVIDENCE_NAME, evidence)
    return {"ok": True, "mode": "table_snapshot_rehearsal", "schema": schema,
            "snapshot_manifest_sha256": snapshot_manifest_sha256, "rehearsal_result_sha256": report_sha,
            "evidence_sha256": evidence_sha, "row_counts": {table: item["row_count"] for table, item in manifest["inventory"].items()}}


def _validate_table_snapshot_evidence(path_text: str, evidence: Mapping[str, Any], *, candidate_git_sha: str = "") -> None:
    if set(evidence) != EVIDENCE_KEYS:
        raise RuntimeError("table snapshot rehearsal evidence keys are invalid")
    candidate = candidate_git_sha or str(evidence.get("candidate_git_sha", ""))
    context = str(evidence.get("rehearsal_context", ""))
    _context(context, evidence.get("rehearsal_port"))
    directory = _private_dir(str(Path(path_text).parent))
    manifest, _schemas = _load_snapshot(directory, str(evidence.get("snapshot_manifest_sha256", "")), candidate)
    report, report_sha = _read_json(directory / REPORT_NAME)
    if (
        evidence.get("verification_source") != "table_snapshot_rehearsal"
        or evidence.get("cluster_id") != migration.CLUSTER_ID or evidence.get("schema") != migration.SCHEMA
        or evidence.get("backup_id") != "table-snapshot-" + evidence["snapshot_manifest_sha256"]
        or evidence.get("backup_status") != "SUCCESS" or evidence.get("rehearsal_status") != "PASS"
        or evidence.get("migration_contract_sha256") != migration.MIGRATION_CONTRACT_SHA256
        or evidence.get("source_contract_sha256") != SOURCE_CONTRACT_SHA256
        or evidence.get("candidate_git_sha") != candidate or evidence.get("candidate_code_sha256") != _sha(manifest["candidate_files"])
        or evidence.get("inventory_sha256") != manifest["inventory_sha256"]
        or evidence.get("backup_completed_at_utc") != manifest["snapshot_completed_at_utc"]
        or manifest.get("context") != context or evidence.get("rehearsal_result_sha256") != report_sha
        or report.get("format_version") != 1 or report.get("verification_source") != "table_snapshot_rehearsal"
        or report.get("status") != "PASS" or report.get("context") != context
        or report.get("target_connection_closed") is not True or report.get("external_columns_all_null") is not True
        or any(report.get(key) != evidence.get(key) for key in ("snapshot_manifest_sha256", "inventory_sha256", "migration_contract_sha256", "source_contract_sha256", "candidate_git_sha", "candidate_code_sha256"))
        or report.get("completed_at_utc") != evidence.get("rehearsal_at_utc")
        or report.get("runs") != _expected_runs() or report.get("runs_sha256") != _sha(report.get("runs"))
        or report.get("projections") != {stage: _expected_projection(manifest) for stage in ("restored", "after_first", "after_second")}
        or report.get("projections_sha256") != _sha(report.get("projections"))
    ):
        raise RuntimeError("table snapshot rehearsal evidence is incomplete or mismatched")
    target = report.get("target", {})
    inspected = _validate_container(target.get("inspection", {}), context, evidence["rehearsal_port"])
    if (
        any(target.get(key) != value for key, value in inspected.items())
        or not UUID_RE.fullmatch(str(target.get("server_uuid", "")))
        or target.get("server_uuid") == manifest["source"]["server_uuid"]
        or not str(target.get("version", "")).startswith("5.7.")
        or target.get("server_port") != 3306 or target.get("read_only") != 0
    ):
        raise RuntimeError("replay target isolation proof is invalid")
    completed = migration._utc_timestamp(evidence.get("backup_completed_at_utc"))
    started = migration._utc_timestamp(report.get("started_at_utc"))
    rehearsal = migration._utc_timestamp(evidence.get("rehearsal_at_utc"))
    verified = migration._utc_timestamp(evidence.get("verified_at_utc"))
    now = datetime.now(timezone.utc)
    if not (completed <= started <= rehearsal <= verified <= now + timedelta(minutes=5) and now - completed <= timedelta(hours=48) and now - verified <= timedelta(hours=4)):
        raise RuntimeError("table snapshot rehearsal evidence is stale or inconsistent")


def validate_table_snapshot_evidence(path_text: str, evidence: Mapping[str, Any], *, candidate_git_sha: str = "") -> None:
    try:
        _validate_table_snapshot_evidence(path_text, evidence, candidate_git_sha=candidate_git_sha)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeError("table snapshot rehearsal evidence structure is invalid") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-snapshot", action="store_true", required=True)
    parser.add_argument("--source-credential-file", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    parser.add_argument("--candidate-git-sha", required=True)
    parser.add_argument("--rehearsal-context", required=True)
    args = parser.parse_args()
    result = export_snapshot(args.source_credential_file, snapshot_dir=args.snapshot_dir,
                             candidate_git_sha=args.candidate_git_sha, context=args.rehearsal_context)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Driver exceptions can include row values; do not print exception text.
        result = {"ok": False, "error": "three_table_snapshot_failed_no_success_evidence"}
        if type(exc) is RuntimeError:
            result["reason"] = str(exc)
        elif exc.args and type(exc.args[0]) is int:
            result["database_error_code"] = exc.args[0]
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
