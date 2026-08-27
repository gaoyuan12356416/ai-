#!/usr/bin/env python3
"""Offline safety and artifact-integrity tests; never contact a real database."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from scripts import drama_youtube_three_table_rehearsal as rehearsal  # noqa: E402
from scripts import migrate_drama_youtube_unified_schema as migration  # noqa: E402


CONTEXT = "1234567890abcdef"
CANDIDATE = "a" * 40
SOURCE_UUID = "11111111-1111-1111-1111-111111111111"
TARGET_UUID = "22222222-2222-2222-2222-222222222222"
PRIVATE_ROW_MARKER = "private-row-marker"


def schema_fixture(table, *, external=False):
    columns = []
    for name, definition in migration.REQUIRED_COLUMN_DEFINITIONS[table].items():
        if not external and name == migration.MIGRATIONS[table]["column"]:
            continue
        columns.append({
            "TABLE_NAME": table, "COLUMN_NAME": name, "ORDINAL_POSITION": len(columns) + 1,
            "COLUMN_TYPE": definition[0], "IS_NULLABLE": definition[1], "COLUMN_DEFAULT": definition[2],
            "CHARACTER_SET_NAME": definition[3], "COLLATION_NAME": definition[4],
            "EXTRA": definition[5], "COLUMN_COMMENT": "",
        })
    indexes = [{
        "TABLE_NAME": table, "INDEX_NAME": "PRIMARY", "NON_UNIQUE": 0, "SEQ_IN_INDEX": 1,
        "COLUMN_NAME": "id", "COLLATION": "A", "SUB_PART": None, "PACKED": None,
        "NULLABLE": "", "INDEX_TYPE": "BTREE", "COMMENT": "", "INDEX_COMMENT": "",
    }]
    if external:
        spec = migration.MIGRATIONS[table]
        indexes.append(dict(indexes[0], INDEX_NAME=spec["index"], COLUMN_NAME=spec["column"], NULLABLE="YES"))
    options = {"ENGINE": "InnoDB", "TABLE_COLLATION": "utf8mb4_unicode_ci", "CREATE_OPTIONS": ""}
    definitions = []
    for column in columns:
        sql = "`%s` %s" % (column["COLUMN_NAME"], column["COLUMN_TYPE"])
        if column["CHARACTER_SET_NAME"]:
            sql += " CHARACTER SET %s COLLATE %s" % (column["CHARACTER_SET_NAME"], column["COLLATION_NAME"])
        sql += " NULL" if column["IS_NULLABLE"] == "YES" else " NOT NULL"
        default = column["COLUMN_DEFAULT"]
        if default is not None:
            sql += " DEFAULT " + (default if default == "CURRENT_TIMESTAMP" else "'%s'" % default)
        if column["EXTRA"]:
            sql += " " + column["EXTRA"]
        definitions.append(sql)
    definitions.append("PRIMARY KEY (`id`)")
    create_sql = "CREATE TABLE `%s` (\n%s\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci" % (table, ",\n".join(definitions))
    return {"columns": columns, "indexes": indexes, "table_options": options, "create_sql": create_sql}


def row_fixture(table, identifier):
    values = []
    for name in rehearsal.LEGACY_COLUMNS[table]:
        definition = migration.REQUIRED_COLUMN_DEFINITIONS[table][name]
        if name == "id":
            value = identifier
        elif definition[0] == "timestamp":
            value = datetime(2026, 8, 1, 12, 34, 56)
        elif "int" in definition[0]:
            value = 0
        else:
            value = PRIVATE_ROW_MARKER
        values.append(value)
    return values


def container_fixture():
    _schema, _name, datadir = rehearsal._context(CONTEXT)
    binding = {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23357"}]}
    return {
        "container_id": "c" * 64, "image_id": "sha256:" + "d" * 64,
        "image_reference": "mysql:5.7@sha256:" + "e" * 64, "hostname": "c" * 12,
        "running": True, "network_mode": "bridge", "publish_all_ports": False,
        "port_bindings": copy.deepcopy(binding), "actual_ports": copy.deepcopy(binding),
        "mounts": [{"Type": "bind", "Source": str(datadir), "Destination": "/var/lib/mysql", "RW": True}],
    }


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        db = self.connection
        db.sql.append(sql)
        if db.source and not sql.startswith(("SELECT ", "SHOW ", "SET SESSION ", "START TRANSACTION ")):
            raise AssertionError("source received a write")
        if sql.startswith("SELECT DATABASE()"):
            self.result = [dict(db.identity)]
        elif "information_schema.COLUMNS" in sql:
            self.result = copy.deepcopy(db.schemas[params[1]]["columns"])
        elif "information_schema.STATISTICS" in sql:
            self.result = copy.deepcopy(db.schemas[params[1]]["indexes"])
            if len(params) == 3:
                self.result = [row for row in self.result if row["INDEX_NAME"] == params[2]]
        elif sql.startswith("SELECT ENGINE"):
            self.result = [dict(db.schemas[params[1]]["table_options"])]
        elif sql.startswith("SHOW CREATE TABLE"):
            table = re.search(r"\.\`([^`]+)\`", sql).group(1)
            self.result = [{"Create Table": db.schemas[table]["create_sql"]}]
        elif " AS table_count " in sql:
            self.result = [{"table_count": len(db.created)}]
        elif " AS duplicate_groups " in sql:
            self.result = [{"duplicate_groups": 0}]
        elif " AS nonnull_count " in sql:
            self.result = [{"nonnull_count": db.nonnull_count}]
        elif " AS row_count " in sql:
            table = re.search(r"\.\`([^`]+)\`", sql).group(1)
            self.result = [{"row_count": len(db.rows[table])}]
        elif sql.startswith("SELECT ") and " ORDER BY `id`" in sql:
            table = re.search(r"\.\`([^`]+)\`", sql).group(1)
            self.result = copy.deepcopy(db.rows[table])
        elif sql.startswith(("SET SESSION ", "START TRANSACTION ")) or " LIMIT 0" in sql:
            self.result = []
        elif sql.startswith("CREATE TABLE"):
            table = re.search(r"\.\`([^`]+)\`", sql).group(1)
            if table in db.created:
                raise AssertionError("restore reused a table")
            db.created.add(table)
            self.result = []
        elif sql.startswith("ALTER TABLE"):
            table = re.search(r"\.\`([^`]+)\`", sql).group(1)
            db.schemas[table] = schema_fixture(table, external=True)
            db.ddl.append(sql)
            self.result = []
        else:
            raise AssertionError("unexpected test SQL: " + sql)
        return len(self.result)

    def executemany(self, sql, rows):
        if self.connection.source or not sql.startswith("INSERT INTO"):
            raise AssertionError("unexpected batch write")
        self.connection.sql.append(sql)
        table = re.search(r"\.\`([^`]+)\`", sql).group(1)
        self.connection.rows[table].extend(copy.deepcopy(rows))

    def fetchall(self):
        rows = self.result
        self.result = []
        return rows

    def fetchone(self):
        return self.result.pop(0) if self.result else None

    def fetchmany(self, amount):
        rows = self.result[:amount]
        self.result = self.result[amount:]
        return rows


class FakeConnection:
    def __init__(self, *, source):
        self.source = source
        self.schemas = {table: schema_fixture(table) for table in rehearsal.TABLES}
        self.rows = {table: [row_fixture(table, 1), row_fixture(table, 2)] if source else [] for table in rehearsal.TABLES}
        self.created = set(rehearsal.TABLES) if source else set()
        self.sql = []
        self.ddl = []
        self.closed = False
        self.nonnull_count = 0
        self.identity = {
            "schema_name": migration.SCHEMA if source else rehearsal._context(CONTEXT)[0],
            "account_name": "readonly_fixture@%" if source else rehearsal.REHEARSAL_USER + "@%",
            "read_only": 1 if source else 0, "server_uuid": SOURCE_UUID if source else TARGET_UUID,
            "version": "5.7.44", "sql_mode": "STRICT_TRANS_TABLES", "hostname": "c" * 12, "server_port": 3306,
        }

    def cursor(self, *_args):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def rollback(self):
        pass

    def commit(self):
        pass


class ThreeTableRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "snapshot"
        self.directory.mkdir(mode=0o700)
        self.source = FakeConnection(source=True)
        self.target = FakeConnection(source=False)
        self.source_credential = Path(self.temp.name) / "source.json"
        self.target_credential = Path(self.temp.name) / "target.json"
        rehearsal._write_json(self.source_credential, {
            "host": migration.HOST, "port": 63350, "user": "readonly_fixture",
            "password": "fake-source-only", "database": migration.SCHEMA,
        })
        rehearsal._write_json(self.target_credential, {
            "host": "127.0.0.1", "port": 23357, "user": rehearsal.REHEARSAL_USER,
            "password": "fake-local-only-" + "x" * 32, "database": rehearsal._context(CONTEXT)[0],
        })
        original_candidate_files = rehearsal._candidate_files
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.object(rehearsal, "_candidate_files", side_effect=lambda sha, require_git: original_candidate_files(sha, require_git=False)))
        stack.enter_context(mock.patch.object(rehearsal, "_headroom"))
        stack.enter_context(mock.patch.object(rehearsal, "_require_data_disk"))
        self.connect = stack.enter_context(mock.patch.object(rehearsal, "_connect", side_effect=lambda _config, source, **_kwargs: self.source if source else self.target))
        stack.enter_context(mock.patch.object(rehearsal, "_inspect_container", return_value=rehearsal._validate_container(container_fixture(), CONTEXT, 23357)))

    def export(self):
        return rehearsal.export_snapshot(str(self.source_credential), snapshot_dir=str(self.directory), candidate_git_sha=CANDIDATE, context=CONTEXT)

    def run_rehearsal(self, exported):
        self.assertTrue(self.source.closed)
        return rehearsal.rehearse_loopback(
            str(self.target_credential), snapshot_dir=str(self.directory),
            snapshot_manifest_sha256=exported["snapshot_manifest_sha256"],
            candidate_git_sha=CANDIDATE, context=CONTEXT, port=23357,
        )

    def validate(self):
        return migration.load_backup_evidence_file(str(self.directory / rehearsal.EVIDENCE_NAME), candidate_git_sha=CANDIDATE)

    def read(self, filename):
        return json.loads((self.directory / filename).read_bytes())

    def replace(self, filename, value):
        raw = rehearsal._json_bytes(value)
        (self.directory / filename).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_snapshot_replay_and_production_evidence_round_trip(self):
        write_json = rehearsal._write_json

        def write_after_source_closed(path, value):
            if path.name == rehearsal.MANIFEST_NAME:
                self.assertTrue(self.source.closed)
            return write_json(path, value)

        with mock.patch.object(rehearsal, "_write_json", side_effect=write_after_source_closed):
            exported = self.export()
        self.assertEqual(exported["mode"], "snapshot_only_not_rehearsed")
        self.assertEqual(len(list(self.directory.iterdir())), 7)
        self.assertEqual(self.source.sql.count("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"), 1)
        self.assertTrue(all(sql.startswith(("SELECT ", "SHOW ", "SET SESSION ", "START TRANSACTION ")) for sql in self.source.sql))
        result = self.run_rehearsal(exported)
        self.assertEqual(result["mode"], "table_snapshot_rehearsal")
        self.assertEqual(len(self.target.ddl), 3)
        self.assertTrue(all("ALGORITHM=INPLACE, LOCK=NONE" in sql for sql in self.target.ddl))
        self.assertTrue(all(migration.SCHEMA not in sql for sql in self.target.sql))
        self.assertTrue(self.target.closed)
        evidence = self.validate()
        self.assertEqual(evidence["verification_source"], "table_snapshot_rehearsal")
        self.assertNotIn(PRIVATE_ROW_MARKER, json.dumps(result))
        self.assertNotIn(PRIVATE_ROW_MARKER, json.dumps(evidence))
        self.assertNotIn(PRIVATE_ROW_MARKER, (self.directory / rehearsal.REPORT_NAME).read_text())
        with self.assertRaisesRegex(RuntimeError, "overwritten"):
            self.run_rehearsal(exported)

    def test_source_primary_endpoint_or_writable_identity_is_rejected(self):
        value = json.loads(self.source_credential.read_bytes())
        value["port"] = migration.PORT
        self.source_credential.write_bytes(rehearsal._json_bytes(value))
        with self.assertRaises(RuntimeError):
            self.export()
        self.connect.assert_not_called()
        value["port"] = 63350
        self.source_credential.write_bytes(rehearsal._json_bytes(value))
        self.source.identity["read_only"] = 0
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertTrue(self.source.closed)
        self.assertFalse((self.directory / rehearsal.MANIFEST_NAME).exists())

    def test_source_contract_drift_and_non_innodb_fail_before_backup(self):
        table = rehearsal.TABLES[0]
        self.source.schemas[table]["table_options"]["ENGINE"] = "MyISAM"
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertFalse((self.directory / rehearsal.MANIFEST_NAME).exists())
        self.source.schemas[table] = schema_fixture(table)
        self.source.schemas[table]["columns"][0]["COLUMN_TYPE"] = "bigint(20) unsigned"
        with self.assertRaises(RuntimeError):
            self.export()

    def test_data_size_bound_leaves_no_success_manifest(self):
        with mock.patch.object(rehearsal, "MAX_SNAPSHOT_BYTES", 16):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                self.export()
        self.assertTrue(self.source.closed)
        self.assertFalse((self.directory / rehearsal.MANIFEST_NAME).exists())

    def test_loopback_rejects_production_host_schema_port_and_user_before_connect(self):
        exported = self.export()
        baseline = json.loads(self.target_credential.read_bytes())
        for field, value in (("host", migration.HOST), ("host", "localhost"), ("host", "::1"), ("port", 63353), ("database", migration.SCHEMA), ("user", migration.MIGRATOR_USER)):
            with self.subTest(field=field, value=value):
                self.target_credential.write_bytes(rehearsal._json_bytes(dict(baseline, **{field: value})))
                self.connect.reset_mock()
                with self.assertRaises(RuntimeError):
                    self.run_rehearsal(exported)
                self.connect.assert_not_called()

    def test_restore_rejects_existing_tables_and_source_server_uuid(self):
        exported = self.export()
        self.target.created.add("unrelated_table")
        with self.assertRaisesRegex(RuntimeError, "empty isolated"):
            self.run_rehearsal(exported)
        self.assertFalse(any(sql.startswith("CREATE ") for sql in self.target.sql))
        self.target.created.clear()
        self.target.identity["server_uuid"] = SOURCE_UUID
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.run_rehearsal(exported)
        self.assertFalse((self.directory / rehearsal.EVIDENCE_NAME).exists())

    def test_restore_rejects_container_hostname_mismatch(self):
        exported = self.export()
        self.target.identity["hostname"] = "remote-host"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.run_rehearsal(exported)
        self.assertFalse(any(sql.startswith("CREATE ") for sql in self.target.sql))

    def test_legacy_data_and_index_changes_cannot_pass_rehearsal(self):
        exported = self.export()
        projection = rehearsal._projection

        def corrupt_projection(*args, **kwargs):
            result = projection(*args, **kwargs)
            result[rehearsal.TABLES[0]]["rows_sha256"] = "0" * 64
            return result

        with mock.patch.object(rehearsal, "_projection", side_effect=corrupt_projection):
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                self.run_rehearsal(exported)
        self.assertFalse((self.directory / rehearsal.EVIDENCE_NAME).exists())

    def test_nonnull_external_ids_block_success_evidence(self):
        exported = self.export()
        self.target.nonnull_count = 1
        with self.assertRaisesRegex(RuntimeError, "NULL"):
            self.run_rehearsal(exported)
        self.assertFalse((self.directory / rehearsal.EVIDENCE_NAME).exists())

    def test_changed_backup_data_and_schema_are_detected(self):
        self.run_rehearsal(self.export())
        self.validate()
        table = rehearsal.TABLES[0]
        rows_path = self.directory / (table + ".rows.ndjson")
        original = rows_path.read_bytes()
        rows_path.write_bytes(original.replace(PRIVATE_ROW_MARKER.encode(), b"altered-row-marker", 1))
        with self.assertRaisesRegex(RuntimeError, "hash"):
            self.validate()
        rows_path.write_bytes(original)
        schema = self.read(table + ".schema.json")
        schema["indexes"][0]["INDEX_TYPE"] = "HASH"
        self.replace(table + ".schema.json", schema)
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            self.validate()

    def test_rehashed_false_pass_and_idempotency_claims_are_rejected(self):
        self.run_rehearsal(self.export())
        report = self.read(rehearsal.REPORT_NAME)
        report["runs"][2]["applied"] = report["runs"][1]["applied"]
        report["runs_sha256"] = rehearsal._sha(report["runs"])
        evidence = self.read(rehearsal.EVIDENCE_NAME)
        evidence["rehearsal_result_sha256"] = self.replace(rehearsal.REPORT_NAME, report)
        self.replace(rehearsal.EVIDENCE_NAME, evidence)
        with self.assertRaisesRegex(RuntimeError, "incomplete or mismatched"):
            self.validate()

    def test_rehashed_changed_legacy_invariant_claim_is_rejected(self):
        self.run_rehearsal(self.export())
        report = self.read(rehearsal.REPORT_NAME)
        report["projections"]["after_second"][rehearsal.TABLES[0]]["row_count"] = 0
        report["projections_sha256"] = rehearsal._sha(report["projections"])
        evidence = self.read(rehearsal.EVIDENCE_NAME)
        evidence["rehearsal_result_sha256"] = self.replace(rehearsal.REPORT_NAME, report)
        self.replace(rehearsal.EVIDENCE_NAME, evidence)
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_fingerprint_context_and_freshness_gates(self):
        self.run_rehearsal(self.export())
        original = self.read(rehearsal.EVIDENCE_NAME)
        mutations = {
            "candidate_git_sha": "b" * 40, "candidate_code_sha256": "0" * 64,
            "migration_contract_sha256": "0" * 64, "source_contract_sha256": "0" * 64,
            "inventory_sha256": "0" * 64, "snapshot_manifest_sha256": "0" * 64,
            "rehearsal_result_sha256": "0" * 64, "rehearsal_context": "0" * 16,
            "rehearsal_port": 63353, "rehearsal_status": "SKIPPED",
            "verified_at_utc": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                self.replace(rehearsal.EVIDENCE_NAME, dict(original, **{key: value}))
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_pass_json_without_actual_backup_and_replay_is_rejected(self):
        evidence = {key: "PASS" for key in rehearsal.EVIDENCE_KEYS}
        evidence.update(verification_source="table_snapshot_rehearsal", rehearsal_context=CONTEXT, rehearsal_port=23357)
        self.replace(rehearsal.EVIDENCE_NAME, evidence)
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_export_does_not_overwrite_existing_artifacts(self):
        self.export()
        self.connect.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "new empty"):
            self.export()
        self.connect.assert_not_called()


class BoundaryGuardTests(unittest.TestCase):
    def test_production_connector_keeps_exact_fixed_target(self):
        config = {"host": migration.HOST, "port": migration.PORT, "database": migration.SCHEMA,
                  "user": migration.MIGRATOR_USER, "password": "fake"}
        with mock.patch.object(migration.pymysql, "connect") as connect:
            for field, value in (("host", "127.0.0.1"), ("port", 63350), ("port", 23357), ("database", rehearsal._context(CONTEXT)[0]), ("user", rehearsal.REHEARSAL_USER)):
                with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                    migration._connect(dict(config, **{field: value}))
            connect.assert_not_called()
            migration._connect(config)
            self.assertEqual(connect.call_args.kwargs["host"], migration.HOST)
            self.assertEqual(connect.call_args.kwargs["port"], 63353)
        self.assertEqual(migration.MIGRATOR_TABLE_PRIVILEGES, {"SELECT", "INSERT", "CREATE", "ALTER"})

    def test_snapshot_connectors_cannot_use_opposite_endpoint_or_socket_override(self):
        source = {"host": migration.HOST, "port": 63350, "database": migration.SCHEMA,
                  "user": "readonly_fixture", "password": "fake"}
        target = {"host": "127.0.0.1", "port": 23357, "database": rehearsal._context(CONTEXT)[0],
                  "user": rehearsal.REHEARSAL_USER, "password": "fake"}
        with mock.patch.object(rehearsal.pymysql, "connect") as connect:
            for config, is_source in ((target, True), (source, False), (dict(source, port=63353), True), (dict(target, unix_socket="/tmp/mysql.sock"), False), (dict(target, user="root"), False)):
                with self.subTest(source=is_source), self.assertRaises(RuntimeError):
                    rehearsal._connect(config, source=is_source, context=CONTEXT)
            connect.assert_not_called()
            rehearsal._connect(source, source=True)
            self.assertEqual(connect.call_args.kwargs["port"], 63350)
            rehearsal._connect(target, source=False, context=CONTEXT, port=23357)
            self.assertEqual(connect.call_args.kwargs["host"], "127.0.0.1")
            self.assertFalse(connect.call_args.kwargs["local_infile"])

    def test_container_must_be_loopback_pinned_bridge_and_bind_mounted(self):
        baseline = container_fixture()
        self.assertEqual(rehearsal._validate_container(baseline, CONTEXT, 23357)["host"], "127.0.0.1")
        mutations = [
            ("network_mode", "host"), ("image_reference", "mysql:5.7"), ("image_reference", "mysql:8.0@sha256:" + "e" * 64),
            ("running", False), ("publish_all_ports", True),
            ("port_bindings", {"3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": "23357"}]}),
            ("actual_ports", {"3306/tcp": [{"HostIp": "::", "HostPort": "23357"}]}),
            ("mounts", [{"Type": "volume", "Source": "/var/lib/docker/volumes/mysql", "Destination": "/var/lib/mysql", "RW": True}]),
            ("mounts", [{"Type": "bind", "Source": "/", "Destination": "/var/lib/mysql", "RW": True}]),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                rehearsal._validate_container(dict(baseline, **{field: value}), CONTEXT, 23357)
        for context, port in (("prod", 23357), (CONTEXT, 63353), (CONTEXT, 3306), (CONTEXT, True)):
            with self.assertRaises(RuntimeError):
                rehearsal._validate_container(baseline, context, port)

    def test_container_inspection_never_reads_environment_secrets(self):
        process = mock.Mock(stdout=json.dumps(container_fixture()).encode())
        with mock.patch.object(rehearsal.subprocess, "run", return_value=process) as run, mock.patch.object(Path, "is_dir", return_value=True), mock.patch.object(Path, "resolve", autospec=True, side_effect=lambda path: path):
            rehearsal._inspect_container(CONTEXT, 23357)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["docker", "inspect", "--type", "container"])
        self.assertNotIn(".Config.Env", " ".join(args))
        self.assertEqual(args[-1], "drama-youtube-rehearsal-" + CONTEXT)

    def test_restore_sql_is_one_exact_create_and_never_cross_schema(self):
        table = rehearsal.TABLES[0]
        sql = schema_fixture(table)["create_sql"]
        schema = rehearsal._context(CONTEXT)[0]
        self.assertTrue(rehearsal._safe_create_sql(sql, table, schema).startswith("CREATE TABLE `%s`.`%s`" % (schema, table)))
        for invalid in (sql + "; DROP TABLE other", sql.replace("ENGINE=InnoDB", "ENGINE=MyISAM"), sql + " DATA DIRECTORY='/tmp'", sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"), sql + " AS SELECT 1"):
            with self.assertRaises(RuntimeError):
                rehearsal._safe_create_sql(invalid, table, schema)
        with self.assertRaises(RuntimeError):
            rehearsal._safe_create_sql(sql, table, migration.SCHEMA)

    def test_secure_artifact_paths_permissions_and_size_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "artifact.json"
            rehearsal._write_json(path, {"safe": True})
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                rehearsal._write_json(path, {"safe": False})
            with mock.patch.object(rehearsal, "MAX_SNAPSHOT_BYTES", 1):
                with self.assertRaisesRegex(RuntimeError, "oversized"):
                    with rehearsal._private_file(path):
                        pass
            if os.name != "nt":
                path.chmod(0o644)
                with self.assertRaises(RuntimeError):
                    rehearsal._read_json(path)
                path.chmod(0o600)
                directory.chmod(0o755)
                with self.assertRaises(RuntimeError):
                    rehearsal._private_dir(str(directory))
                directory.chmod(0o700)
            with self.assertRaises(RuntimeError):
                rehearsal._private_dir("relative")
            with self.assertRaises(RuntimeError):
                rehearsal._private_dir(str(ROOT))

    def test_disk_headroom_and_runtime_datadisk_requirement(self):
        with mock.patch.object(rehearsal.shutil, "disk_usage", return_value=mock.Mock(free=1)):
            with self.assertRaises(RuntimeError):
                rehearsal._headroom(Path.cwd(), 0)
        with mock.patch.object(rehearsal.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)):
            rehearsal._headroom(Path.cwd(), 1000)
            with self.assertRaises(RuntimeError):
                rehearsal._headroom(Path.cwd(), rehearsal.MAX_SNAPSHOT_BYTES + 1)
        with self.assertRaises(RuntimeError):
            rehearsal._require_data_disk(Path.cwd())

    def test_candidate_requires_exact_clean_checkout_and_hashes_actual_files(self):
        clean = [mock.Mock(stdout=CANDIDATE.encode()), mock.Mock(stdout=b"")]
        with mock.patch.object(rehearsal.subprocess, "run", side_effect=clean) as run:
            fingerprints = rehearsal._candidate_files(CANDIDATE, require_git=True)
        self.assertEqual(set(fingerprints), set(rehearsal.CANDIDATE_FILES))
        for name in rehearsal.CANDIDATE_FILES:
            self.assertEqual(fingerprints[name], hashlib.sha256((ROOT / name).read_bytes()).hexdigest())
        self.assertTrue(all(call.args[0][0] == "git" for call in run.call_args_list))
        for responses in (
            [mock.Mock(stdout=("b" * 40).encode()), mock.Mock(stdout=b"")],
            [mock.Mock(stdout=CANDIDATE.encode()), mock.Mock(stdout=b" M candidate.py")],
        ):
            with mock.patch.object(rehearsal.subprocess, "run", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "clean reviewed"):
                    rehearsal._candidate_files(CANDIDATE, require_git=True)

    def test_cloud_evidence_accepts_real_six_digit_backup_id_without_other_relaxation(self):
        now = rehearsal._now()
        evidence = {
            "cluster_id": migration.CLUSTER_ID, "schema": migration.SCHEMA, "backup_id": "819738",
            "backup_status": "SUCCESS", "backup_completed_at_utc": now, "verified_at_utc": now,
            "verification_source": "tencent_cynosdb_api", "rehearsal_status": "PASS", "rehearsal_at_utc": now,
            "restore_instance_id": "cynosdbmysql-restored1", "migration_contract_sha256": migration.MIGRATION_CONTRACT_SHA256,
            "candidate_git_sha": CANDIDATE, "rehearsal_result_sha256": "b" * 64,
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cloud.json"
            rehearsal._write_json(path, evidence)
            self.assertEqual(migration.load_backup_evidence_file(str(path), candidate_git_sha=CANDIDATE)["backup_id"], "819738")
            for key, value in (("backup_id", "12345"), ("backup_id", "abcdef"), ("restore_instance_id", migration.CLUSTER_ID), ("migration_contract_sha256", "0" * 64), ("candidate_git_sha", "b" * 40)):
                path.write_bytes(rehearsal._json_bytes(dict(evidence, **{key: value})))
                with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                    migration.load_backup_evidence_file(str(path), candidate_git_sha=CANDIDATE)


if __name__ == "__main__":
    unittest.main()
