#!/usr/bin/env python3
"""Offline tests for the fixed legacy YouTube-ledger writer."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from features.drama_synthesis.unified_youtube_rpc import (  # noqa: E402
    LedgerRPCError,
    MIGRATOR_TABLE_PRIVILEGES,
    MIGRATOR_USER,
    REQUIRED_COLUMN_DEFINITIONS,
    REQUIRED_COLUMNS,
    REQUIRED_UNIQUE_INDEX_BY_TABLE,
    RUNTIME_TABLE_PRIVILEGES,
    WRITER_USER,
    UnifiedYouTubeLedger,
    _comment_record,
    _publish_log_record,
    _video_record,
    load_database_credential_file,
)
from scripts.test_drama_synthesis_upgrade import (  # noqa: E402
    unified_comment_payload,
    unified_video_payload,
)
from scripts import migrate_drama_youtube_unified_schema as migration  # noqa: E402
from scripts.drama_youtube_unified_writer_rpc import (  # noqa: E402
    ControlledWriterHandler,
    HEALTH_PATH,
    RPC_PATH,
)


def exact_show_grants(user, privileges, account_quote="`"):
    account = "%s%s%s@%s43.166.187.96%s" % (
        account_quote, user, account_quote, account_quote, account_quote,
    )
    rows = [{"grant": "GRANT USAGE ON *.* TO " + account}]
    for table in sorted(REQUIRED_COLUMNS):
        rows.append({
            "grant": "GRANT %s ON `kunlunads_dev`.`%s` TO %s"
            % (", ".join(sorted(privileges)), table, account)
        })
    return rows


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("SELECT DATABASE()"):
            self.result = [{
                "database_name": "kunlunads_dev",
                "read_only": 0,
                "account_name": "drama_youtube_writer@43.166.187.96",
            }]
            return 1
        if "information_schema.COLUMNS" in sql:
            self.result = [
                {
                    "TABLE_NAME": table,
                    "COLUMN_NAME": column,
                    "COLUMN_TYPE": definition[0],
                    "IS_NULLABLE": definition[1],
                    "COLUMN_DEFAULT": definition[2],
                    "CHARACTER_SET_NAME": definition[3],
                    "COLLATION_NAME": definition[4],
                    "EXTRA": definition[5],
                }
                for table, definitions in REQUIRED_COLUMN_DEFINITIONS.items()
                for column, definition in definitions.items()
            ]
            if self.connection.schema_drift:
                next(
                    row for row in self.result
                    if row["TABLE_NAME"] == "ads_youtube_videos" and row["COLUMN_NAME"] == "queue_id"
                )["COLUMN_TYPE"] = "int(10) unsigned"
            return len(self.result)
        if sql.startswith("SHOW GRANTS"):
            self.result = exact_show_grants(
                WRITER_USER,
                RUNTIME_TABLE_PRIVILEGES,
                self.connection.show_grant_account_quote,
            )
            if self.connection.proxy_grant_extra:
                self.result.append({
                    "grant": "GRANT PROXY ON ''@'' TO 'drama_youtube_writer'@'43.166.187.96'"
                })
            if self.connection.routine_grant_extra:
                self.result.append({
                    "grant": "GRANT EXECUTE ON PROCEDURE `kunlunads_dev`.`unexpected_routine` "
                    "TO `drama_youtube_writer`@`43.166.187.96`"
                })
            return len(self.result)
        if "information_schema.USER_PRIVILEGES" in sql:
            self.result = []
            if self.connection.global_grant_extra:
                self.result.append({"PRIVILEGE_TYPE": "PROCESS", "IS_GRANTABLE": "NO"})
            return len(self.result)
        if "information_schema.SCHEMA_PRIVILEGES" in sql:
            self.result = []
            return 0
        if "information_schema.TABLE_PRIVILEGES" in sql:
            self.result = [
                {
                    "TABLE_SCHEMA": "kunlunads_dev",
                    "TABLE_NAME": table,
                    "PRIVILEGE_TYPE": privilege,
                    "IS_GRANTABLE": "NO",
                }
                for table in REQUIRED_COLUMNS
                for privilege in RUNTIME_TABLE_PRIVILEGES
            ]
            if self.connection.grant_extra:
                self.result.append({
                    "TABLE_SCHEMA": "kunlunads_dev",
                    "TABLE_NAME": "ads_youtube_videos",
                    "PRIVILEGE_TYPE": "DELETE",
                    "IS_GRANTABLE": "NO",
                })
            return len(self.result)
        if "information_schema.COLUMN_PRIVILEGES" in sql:
            self.result = []
            if self.connection.column_grant_extra:
                self.result.append({
                    "TABLE_SCHEMA": "kunlunads_dev",
                    "TABLE_NAME": "ads_youtube_videos",
                    "COLUMN_NAME": "video_id",
                    "PRIVILEGE_TYPE": "SELECT",
                    "IS_GRANTABLE": "NO",
                })
            return len(self.result)
        if "information_schema.STATISTICS" in sql:
            self.result = [
                {
                    "TABLE_NAME": table,
                    "INDEX_NAME": index_name,
                    "NON_UNIQUE": 0,
                    "SEQ_IN_INDEX": 1,
                    "COLUMN_NAME": column_name,
                }
                for table, (index_name, column_name) in REQUIRED_UNIQUE_INDEX_BY_TABLE.items()
            ]
            return len(self.result)
        if sql.startswith("SELECT `id`"):
            table = re.search(r"FROM `kunlunads_dev`.`([^`]+)`", sql).group(1)
            external_column = re.search(r"WHERE `([^`]+)`=%s", sql).group(1)
            external_id = params[0]
            self.result = [dict(row) for row in self.connection.rows[table] if row.get(external_column) == external_id][:2]
            return len(self.result)
        if sql.startswith("INSERT INTO"):
            match = re.search(r"INSERT INTO `kunlunads_dev`.`([^`]+)` \(([^)]+)\)", sql)
            table = match.group(1)
            columns = [item.strip("`") for item in match.group(2).split(",")]
            record = dict(zip(columns, params))
            record["id"] = len(self.connection.rows[table]) + 1
            self.connection.rows[table].append(record)
            self.result = []
            return 1
        raise AssertionError("unexpected SQL: " + sql)

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None


class FakeConnection:
    def __init__(self):
        self.rows = {
            "ads_youtube_videos": [],
            "ads_youtube_comments": [],
            "ads_youtube_publish_log": [],
        }
        self.commits = 0
        self.rollbacks = 0
        self.grant_extra = False
        self.global_grant_extra = False
        self.column_grant_extra = False
        self.routine_grant_extra = False
        self.proxy_grant_extra = False
        self.show_grant_account_quote = "`"
        self.schema_drift = False

    def cursor(self):
        return FakeCursor(self)

    def begin(self):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None


class MigrationCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("SELECT DATABASE()"):
            self.result = [{
                "database_name": "kunlunads_dev",
                "account_name": "drama_youtube_migrator@43.166.187.96",
                "server_read_only": 0,
            }]
        elif sql.startswith("SHOW GRANTS"):
            self.result = exact_show_grants(MIGRATOR_USER, MIGRATOR_TABLE_PRIVILEGES)
        elif "information_schema.USER_PRIVILEGES" in sql:
            self.result = []
        elif "information_schema.SCHEMA_PRIVILEGES" in sql:
            self.result = []
        elif "information_schema.TABLE_PRIVILEGES" in sql:
            self.result = [
                {
                    "TABLE_SCHEMA": "kunlunads_dev",
                    "TABLE_NAME": table,
                    "PRIVILEGE_TYPE": privilege,
                    "IS_GRANTABLE": "NO",
                }
                for table in migration.MIGRATIONS
                for privilege in MIGRATOR_TABLE_PRIVILEGES
            ]
        elif "information_schema.COLUMN_PRIVILEGES" in sql:
            self.result = []
        elif "information_schema.COLUMNS" in sql:
            table = params[1]
            spec = migration.MIGRATIONS[table]
            self.result = []
            for column, definition in REQUIRED_COLUMN_DEFINITIONS[table].items():
                if column == spec["column"] and not self.connection.state[table]["column"]:
                    continue
                self.result.append({
                    "TABLE_NAME": table,
                    "COLUMN_NAME": column,
                    "COLUMN_TYPE": definition[0],
                    "IS_NULLABLE": definition[1],
                    "COLUMN_DEFAULT": definition[2],
                    "CHARACTER_SET_NAME": definition[3],
                    "COLLATION_NAME": definition[4],
                    "EXTRA": definition[5],
                })
        elif "information_schema.STATISTICS" in sql:
            table = params[1]
            spec = migration.MIGRATIONS[table]
            self.result = []
            if self.connection.state[table]["index"]:
                self.result.append({
                    "INDEX_NAME": spec["index"],
                    "NON_UNIQUE": 0,
                    "SEQ_IN_INDEX": 1,
                    "COLUMN_NAME": spec["column"],
                })
        elif sql.startswith("SELECT COUNT(*) AS duplicate_groups"):
            self.result = [{"duplicate_groups": 0}]
        elif sql.startswith("ALTER TABLE"):
            table = re.search(r"ALTER TABLE `kunlunads_dev`.`([^`]+)`", sql).group(1)
            if "ADD COLUMN" in sql:
                self.connection.state[table]["column"] = True
            self.connection.state[table]["index"] = True
            self.connection.ddl.append(sql)
            self.result = []
        else:
            raise AssertionError("unexpected migration SQL: " + sql)
        return len(self.result)

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None


class MigrationConnection:
    def __init__(self):
        self.state = {table: {"column": False, "index": False} for table in migration.MIGRATIONS}
        self.ddl = []

    def cursor(self):
        return MigrationCursor(self)

    def close(self):
        return None


class HTTPFakeLedger:
    def __init__(self):
        self.calls = []

    def health(self):
        return {"ok": True, "schema": "kunlunads_dev", "grant_fingerprint": "f" * 64}

    def execute(self, action, table, external_id, payload):
        self.calls.append((action, table, external_id, payload))
        return {"found": False}


class UnifiedRPCRepositoryTests(unittest.TestCase):
    def test_legacy_record_projection_is_bounded_and_safe(self):
        video_payload = unified_video_payload()
        video = _video_record(video_payload)
        self.assertEqual((video["video_id"], video["app_id"], video["channel_id"]), ("video_1", 1479, 1))
        self.assertEqual(video["template_make_id"], 0)
        self.assertEqual(video["queue_id"], -1)
        self.assertLessEqual(len(video["video_description"]), 3000)
        comment = _comment_record(unified_comment_payload())
        self.assertEqual((comment["video_id"], comment["comment"]), ("video_1", "hello"))
        publish_log = _publish_log_record(video_payload)
        safe_log = json.loads(publish_log["log"])
        self.assertEqual((publish_log["type_id"], publish_log["status"]), (3, 1))
        self.assertEqual(publish_log["created_queue"], video["queue_id"])
        self.assertNotIn(video_payload["description_rendered"], publish_log["log"])
        self.assertNotIn(video_payload["source_url"], publish_log["log"])
        self.assertEqual(safe_log["video_id"], "video_1")

    def test_credential_file_is_exact_and_not_environment_derived(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "writer-db.json"
            value = {
                "host": "101.32.56.53",
                "port": 63353,
                "user": "drama_youtube_writer",
                "password": "x" * 32,
                "database": "kunlunads_dev",
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            self.assertEqual(load_database_credential_file(str(path), expected_user=WRITER_USER), value)
            with self.assertRaises(RuntimeError):
                load_database_credential_file(str(path), expected_user=MIGRATOR_USER)
            if os.name != "nt":
                path.chmod(0o640)
                with self.assertRaises(RuntimeError):
                    load_database_credential_file(str(path), expected_user=WRITER_USER)
                path.chmod(0o600)
            path.write_text(json.dumps(dict(value, unexpected="reject")), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_database_credential_file(str(path))

    def test_insert_reuse_conflict_and_missing_update(self):
        connection = FakeConnection()
        ledger = UnifiedYouTubeLedger(lambda: connection)
        payload = unified_video_payload()
        self.assertEqual(
            ledger.execute("select", "ads_youtube_videos", "video_1", {}),
            {"found": False},
        )
        self.assertEqual(
            ledger.execute("insert", "ads_youtube_videos", "video_1", payload),
            {"idempotent_success": True, "reused": False},
        )
        self.assertEqual(
            ledger.execute("insert", "ads_youtube_videos", "video_1", payload),
            {"idempotent_success": True, "reused": True},
        )
        with self.assertRaises(LedgerRPCError) as conflict:
            ledger.execute(
                "insert",
                "ads_youtube_videos",
                "video_1",
                dict(payload, title="different"),
            )
        self.assertEqual(conflict.exception.code, "youtube_sync_identity_conflict")
        with self.assertRaises(LedgerRPCError) as missing:
            ledger.execute(
                "update",
                "ads_youtube_videos",
                "video_2",
                unified_video_payload(video_id="video_2"),
            )
        self.assertEqual(missing.exception.code, "youtube_sync_identity_missing")

    def test_health_requires_exact_schema_indexes_and_writer_identity(self):
        ledger = UnifiedYouTubeLedger(lambda: FakeConnection())
        health = ledger.health()
        self.assertEqual((health["ok"], health["schema"]), (True, "kunlunads_dev"))
        self.assertRegex(health["grant_fingerprint"], r"^[0-9a-f]{64}$")

        live_quote_shape = FakeConnection()
        live_quote_shape.show_grant_account_quote = "'"
        self.assertTrue(UnifiedYouTubeLedger(lambda: live_quote_shape).health()["ok"])

        grant_drift = FakeConnection()
        grant_drift.grant_extra = True
        with self.assertRaises(LedgerRPCError) as grant_error:
            UnifiedYouTubeLedger(lambda: grant_drift).health()
        self.assertEqual(grant_error.exception.code, "youtube_sync_grant_mismatch")

        for attribute in (
            "global_grant_extra", "column_grant_extra", "routine_grant_extra",
            "proxy_grant_extra",
        ):
            grant_drift = FakeConnection()
            setattr(grant_drift, attribute, True)
            with self.subTest(attribute=attribute), self.assertRaises(LedgerRPCError) as grant_error:
                UnifiedYouTubeLedger(lambda: grant_drift).health()
            self.assertEqual(grant_error.exception.code, "youtube_sync_grant_mismatch")

        schema_drift = FakeConnection()
        schema_drift.schema_drift = True
        with self.assertRaises(LedgerRPCError) as schema_error:
            UnifiedYouTubeLedger(lambda: schema_drift).health()
        self.assertEqual(schema_error.exception.code, "youtube_sync_schema_mismatch")

    def test_backup_evidence_is_exact_fresh_and_rehearsed(self):
        example = json.loads(
            (ROOT / "deploy/drama-youtube-backup-evidence.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(example), migration.BACKUP_EVIDENCE_KEYS)
        self.assertEqual(example["migration_contract_sha256"], migration.MIGRATION_CONTRACT_SHA256)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backup-evidence.json"
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            candidate_git_sha = "a" * 40
            evidence = {
                "cluster_id": migration.CLUSTER_ID,
                "schema": migration.SCHEMA,
                "backup_id": "backup-20260826",
                "backup_status": "SUCCESS",
                "backup_completed_at_utc": now,
                "verified_at_utc": now,
                "verification_source": "tencent_cynosdb_api",
                "rehearsal_status": "PASS",
                "rehearsal_at_utc": now,
                "restore_instance_id": "cynosdbmysql-restored1",
                "migration_contract_sha256": migration.MIGRATION_CONTRACT_SHA256,
                "candidate_git_sha": candidate_git_sha,
                "rehearsal_result_sha256": "b" * 64,
            }
            path.write_text(json.dumps(evidence), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            loaded = migration.load_backup_evidence_file(
                str(path), candidate_git_sha=candidate_git_sha
            )
            self.assertRegex(loaded["evidence_sha256"], r"^[0-9a-f]{64}$")
            path.write_text(json.dumps(dict(evidence, rehearsal_status="SKIPPED")), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                migration.load_backup_evidence_file(str(path))
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                migration.load_backup_evidence_file(str(path), candidate_git_sha="c" * 40)
            path.write_text(
                json.dumps(dict(evidence, restore_instance_id=migration.CLUSTER_ID)),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                migration.load_backup_evidence_file(str(path))

    def test_additive_migration_dry_run_apply_and_idempotent_recheck(self):
        connection = MigrationConnection()
        config = {
            "host": migration.HOST,
            "port": migration.PORT,
            "user": MIGRATOR_USER,
            "password": "x" * 32,
            "database": migration.SCHEMA,
        }
        evidence = {"evidence_sha256": "e" * 64}
        candidate_git_sha = "a" * 40
        with mock.patch.object(migration, "load_database_credential_file", return_value=config), mock.patch.object(migration, "load_backup_evidence_file", return_value=evidence), mock.patch.object(migration, "_connect", return_value=connection):
            dry = migration.migrate("ignored", apply=False, cluster_id=migration.CLUSTER_ID)
            self.assertEqual((dry["complete"], len(dry["plan"]), connection.ddl), (False, 3, []))
            applied = migration.migrate(
                "ignored",
                apply=True,
                cluster_id=migration.CLUSTER_ID,
                backup_evidence_file="backup-evidence.json",
                candidate_git_sha=candidate_git_sha,
            )
            self.assertTrue(applied["complete"])
            self.assertEqual(len(applied["applied"]), 3)
            self.assertEqual(applied["candidate_git_sha"], candidate_git_sha)
            self.assertTrue(all("ALGORITHM=INPLACE, LOCK=NONE" in sql for sql in connection.ddl))
            second = migration.migrate("ignored", apply=False, cluster_id=migration.CLUSTER_ID)
            self.assertEqual((second["complete"], second["plan"]), (True, []))

    def test_loopback_handler_health_auth_and_exact_envelope(self):
        server = HTTPServer(("127.0.0.1", 0), ControlledWriterHandler)
        server.rpc_token = "t" * 32
        server.ledger = HTTPFakeLedger()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            client.request("GET", HEALTH_PATH)
            response = client.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            client.request("GET", HEALTH_PATH, headers={"Authorization": "Bearer " + "t" * 32})
            response = client.getresponse()
            self.assertEqual((response.status, json.loads(response.read())["ok"]), (200, True))
            body = json.dumps({
                "action": "select",
                "table": "ads_youtube_videos",
                "external_id": "video_1",
                "payload": {},
            })
            client.request("POST", RPC_PATH, body=body, headers={"Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            client.request(
                "POST",
                RPC_PATH,
                body=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + "t" * 32},
            )
            response = client.getresponse()
            self.assertEqual((response.status, json.loads(response.read())), (200, {"found": False}))
            self.assertEqual(server.ledger.calls, [("select", "ads_youtube_videos", "video_1", {})])
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
