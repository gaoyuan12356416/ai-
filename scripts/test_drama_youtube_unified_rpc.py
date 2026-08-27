#!/usr/bin/env python3
"""Offline tests for the dedicated ads_ai ledger and least-privilege RPC."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from features.drama_synthesis.core import CANARY_APP_ID, CANARY_CHANNEL_LOCAL_ID, CANARY_OPERATION_ID, DramaSynthesisError
from features.drama_synthesis.unified_youtube import WRITER_HEALTH_CONTRACT, TABLE_BY_KIND, validate_writer_health, validate_entity_payload
from features.drama_synthesis.unified_youtube_rpc import (
    LedgerRPCError, REQUIRED_COLUMN_DEFINITIONS, REQUIRED_COLUMNS, REQUIRED_INDEXES_BY_TABLE,
    RUNTIME_TABLE_PRIVILEGES, SCHEMA, TABLE_OWNERSHIP_COMMENT, WRITER_USER,
    UnifiedYouTubeLedger, _comment_record, _publish_log_record, _video_record,
    load_database_credential_file, inspect_owned_tables,
)
from scripts.test_drama_synthesis_upgrade import unified_comment_payload, unified_video_payload
from scripts.drama_youtube_unified_writer_rpc import ControlledWriterHandler, HEALTH_PATH, RPC_PATH


def exact_show_grants(user=WRITER_USER, privileges=RUNTIME_TABLE_PRIVILEGES, account_quote="'"):
    account = "%s%s%s@%s43.166.187.96%s" % (account_quote, user, account_quote, account_quote, account_quote)
    rows = [{"grant": "GRANT USAGE ON *.* TO " + account}]
    for table in sorted(REQUIRED_COLUMNS):
        rows.append({"grant": "GRANT %s ON ads_ai.%s TO %s" % (", ".join(sorted(privileges)), table, account)})
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
        c = self.connection
        c.sql.append(sql)
        if sql.startswith("SELECT DATABASE()"):
            self.result = [{"database_name": c.schema, "read_only": c.read_only, "account_name": c.account}]
        elif sql.startswith("SELECT VERSION()"):
            self.result = [{"version": "5.7.44"}]
        elif sql.startswith("SHOW GRANTS"):
            self.result = copy.deepcopy(c.show_grants if c.show_grants is not None else exact_show_grants(account_quote=c.show_grant_account_quote))
        elif "information_schema.USER_PRIVILEGES" in sql:
            self.result = list(c.global_privileges)
        elif "information_schema.SCHEMA_PRIVILEGES" in sql:
            self.result = list(c.schema_privileges)
        elif "information_schema.TABLE_PRIVILEGES" in sql:
            self.result = [
                {"TABLE_SCHEMA": SCHEMA, "TABLE_NAME": table, "PRIVILEGE_TYPE": privilege, "IS_GRANTABLE": "NO"}
                for table in REQUIRED_COLUMNS for privilege in RUNTIME_TABLE_PRIVILEGES
            ] + list(c.table_privileges_extra)
        elif "information_schema.COLUMN_PRIVILEGES" in sql:
            self.result = list(c.column_privileges)
        elif "information_schema.TABLES" in sql:
            self.result = [
                {"TABLE_NAME": table, "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB",
                 "TABLE_COLLATION": "utf8mb4_bin", "TABLE_COMMENT": TABLE_OWNERSHIP_COMMENT}
                for table in sorted(c.existing)
            ]
            if self.result:
                self.result[0].update(c.table_override)
        elif "information_schema.COLUMNS" in sql:
            self.result = [
                {"TABLE_NAME": table, "COLUMN_NAME": column, "COLUMN_TYPE": definition[0],
                 "IS_NULLABLE": definition[1], "COLUMN_DEFAULT": definition[2],
                 "CHARACTER_SET_NAME": definition[3], "COLLATION_NAME": definition[4], "EXTRA": definition[5]}
                for table in sorted(c.existing)
                for column, definition in REQUIRED_COLUMN_DEFINITIONS[table].items()
            ]
            if c.column_override and self.result:
                self.result[0].update(c.column_override)
            if c.schema_drift and self.result:
                self.result[0]["COLUMN_TYPE"] = "varchar(1)"
            if c.extra_column and self.result:
                self.result.append(dict(self.result[0], COLUMN_NAME="unexpected"))
        elif "information_schema.STATISTICS" in sql:
            self.result = [
                {"TABLE_NAME": table, "INDEX_NAME": index, "NON_UNIQUE": non_unique,
                 "SEQ_IN_INDEX": position, "COLUMN_NAME": column, "SUB_PART": None,
                 "INDEX_TYPE": "BTREE", "COLLATION": "A"}
                for table in sorted(c.existing)
                for index, (non_unique, columns) in REQUIRED_INDEXES_BY_TABLE[table].items()
                for position, column in enumerate(columns, 1)
            ]
            if c.index_override and self.result:
                self.result[0].update(c.index_override)
            if c.missing_index and self.result:
                self.result.pop()
        elif "information_schema.KEY_COLUMN_USAGE" in sql:
            self.result = list(c.foreign_keys)
        elif "information_schema.TRIGGERS" in sql:
            self.result = list(c.triggers)
        elif sql.startswith("CREATE TABLE ads_ai."):
            table = re.match(r"CREATE TABLE ads_ai\.(\w+) \(", sql).group(1)
            if table == c.fail_create:
                raise RuntimeError("simulated CREATE failure")
            if table in c.existing:
                raise pymysql.err.OperationalError(1050, "table already exists")
            c.existing.add(table)
            c.ddl.append(sql)
            self.result = []
        elif sql.startswith("SELECT id,"):
            table = re.search(r"FROM ads_ai\.(\w+)", sql).group(1)
            names = re.findall(r"(\w+)=%s", sql)
            self.result = [
                dict(row) for row in c.rows[table]
                if any(row.get(name) == value for name, value in zip(names, params))
            ][:3]
        elif sql.startswith("INSERT INTO ads_ai."):
            match = re.match(r"INSERT INTO ads_ai\.(\w+) \(([^)]+)\)", sql)
            table, columns = match.group(1), match.group(2).split(",")
            record = dict(zip(columns, params))
            record["id"] = len(c.rows[table]) + 1
            if c.race_record is not None:
                c.rows[table].append(dict(c.race_record, id=record["id"]))
                c.race_record = None
                raise pymysql.err.IntegrityError(1062, "duplicate fixture")
            for row in c.rows[table]:
                for _index, (_non_unique, key_columns) in REQUIRED_INDEXES_BY_TABLE[table].items():
                    if all(row.get(key) == record.get(key) for key in key_columns):
                        raise pymysql.err.IntegrityError(1062, "duplicate fixture")
            c.rows[table].append(record)
            c.inserts += 1
            self.result = []
        elif sql.startswith("SELECT payload_json,payload_sha256 FROM ads_ai."):
            table = sql.rsplit(".", 1)[1]
            self.result = [{"payload_json": row["payload_json"], "payload_sha256": row["payload_sha256"]} for row in c.rows[table]]
        else:
            raise AssertionError("unexpected SQL: " + sql)
        return len(self.result)

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None


class FakeConnection:
    def __init__(self, *, existing=True):
        self.rows = {table: [] for table in TABLE_BY_KIND.values()}
        self.existing = set(self.rows) if existing else set()
        self.schema = SCHEMA
        self.account = WRITER_USER + "@43.166.187.96"
        self.read_only = 0
        self.show_grants = None
        self.show_grant_account_quote = "'"
        self.global_privileges = []
        self.schema_privileges = []
        self.table_privileges_extra = []
        self.column_privileges = []
        self.table_override = {}
        self.column_override = {}
        self.index_override = {}
        self.extra_column = False
        self.missing_index = False
        self.schema_drift = False
        self.foreign_keys = []
        self.triggers = []
        self.fail_create = ""
        self.race_record = None
        self.sql = []
        self.ddl = []
        self.inserts = 0
        self.commits = 0
        self.rollbacks = 0

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


class HTTPFakeLedger:
    def __init__(self):
        self.calls = []

    def health(self):
        return UnifiedYouTubeLedger(lambda: FakeConnection()).health()

    def execute(self, action, table, external_id, payload):
        self.calls.append((action, table, external_id, payload))
        return {"found": False}


class UnifiedRPCRepositoryTests(unittest.TestCase):
    def test_health_v2_ads_ai_only(self):
        health = UnifiedYouTubeLedger(lambda: FakeConnection()).health()
        self.assertEqual(health["schema"], "ads_ai")
        self.assertEqual(health["contract"], "drama-youtube-writer-preflight-v2")
        self.assertEqual(validate_writer_health(health), health)
        for changed in (
            {"contract": "drama-youtube-writer-preflight-v1"}, {"schema": "kunlunads_dev"},
            {"writer_identity": "ads_aius@43.166.187.96"}, {"unexpected": True},
        ):
            with self.subTest(changed=changed), self.assertRaises(DramaSynthesisError):
                validate_writer_health(dict(health, **changed))

    def test_full_payload_and_canary_roundtrip_without_legacy_projection(self):
        video = dict(unified_video_payload(), source_url="https://example.test/" + "a" * 3500,
                     description_rendered="é" * 2000 + "🙂" * 200 + "a" * 200,
                     content_id="剧集😀" * 40)
        for build in (_video_record, _publish_log_record):
            record = build(video)
            self.assertEqual(json.loads(record["payload_json"]), video)
            self.assertEqual(record["description_rendered"], video["description_rendered"])
            self.assertEqual(record["source_url"], video["source_url"])
            self.assertEqual(record["content_id"], video["content_id"])
            self.assertEqual(record["publish_id"], 1)
            self.assertNotIn("queue_id", record)
            self.assertNotIn("created_queue", record)
            self.assertEqual(record["payload_sha256"], hashlib.sha256(record["payload_json"].encode()).hexdigest())
        comment = unified_comment_payload()
        self.assertEqual(json.loads(_comment_record(comment)["payload_json"]), comment)
        canary = dict(video, privacy_status="unlisted", canary_operation_id=CANARY_OPERATION_ID,
                      app_id=int(CANARY_APP_ID), channel_local_id=int(CANARY_CHANNEL_LOCAL_ID))
        c = FakeConnection()
        UnifiedYouTubeLedger(lambda: c).execute("insert", "ads_youtube_videos", "video_1", canary)
        self.assertEqual(c.rows["ads_youtube_videos"][0]["canary_operation_id"], CANARY_OPERATION_ID)

    def test_runtime_credential_loader_cannot_be_used_for_bootstrap(self):
        valid = {"host": "101.32.56.53", "port": 63353, "user": WRITER_USER, "password": "x" * 32, "database": SCHEMA}
        for change in ({}, {"user": "ads_aius"}, {"database": "kunlunads_dev"}, {"port": 63350},
                       {"port": "63353"}, {"host": "127.0.0.1"}, {"unexpected": True}, {"password": "short"}):
            value = dict(valid, **change)
            with mock.patch("features.drama_synthesis.unified_youtube_rpc.read_secure_owned_file", return_value=json.dumps(value).encode()):
                if change:
                    with self.subTest(change=change), self.assertRaises(RuntimeError):
                        load_database_credential_file("/fixture")
                else:
                    self.assertEqual(load_database_credential_file("/fixture"), valid)
                    with self.assertRaises(RuntimeError):
                        load_database_credential_file("/fixture", expected_user="ads_aius")

    def test_insert_update_reuse_and_missing_update(self):
        connection = FakeConnection()
        ledger = UnifiedYouTubeLedger(lambda: connection)
        payload = unified_video_payload()
        self.assertEqual(ledger.execute("select", "ads_youtube_videos", "video_1", {}), {"found": False})
        self.assertEqual(ledger.execute("insert", "ads_youtube_videos", "video_1", payload), {"idempotent_success": True, "reused": False})
        self.assertEqual(ledger.execute("update", "ads_youtube_videos", "video_1", payload), {"idempotent_success": True, "reused": True})
        with self.assertRaises(LedgerRPCError) as error:
            ledger.execute("update", "ads_youtube_videos", "video_2", unified_video_payload(publish_id=2, video_id="video_2"))
        self.assertEqual(error.exception.code, "youtube_sync_identity_missing")
        self.assertEqual(connection.inserts, 1)

    def test_complete_immutable_payload_mismatch_is_rejected(self):
        for field, changed in (
            ("title", "new title"), ("description_rendered", "required "),
            ("source_url", "https://example.test/different.mp4"), ("content_id", "new content"),
            ("job_id", "b" * 32), ("channel_local_id", 2), ("operator_user_id", "cf1edggd"),
            ("published_at_utc", "2026-08-26T00:00:01Z"),
        ):
            c = FakeConnection()
            ledger = UnifiedYouTubeLedger(lambda: c)
            payload = unified_video_payload()
            ledger.execute("insert", "ads_youtube_videos", "video_1", payload)
            with self.subTest(field=field), self.assertRaises(LedgerRPCError) as error:
                ledger.execute("insert", "ads_youtube_videos", "video_1", dict(payload, **{field: changed}))
            self.assertEqual(error.exception.code, "youtube_sync_identity_conflict")
            self.assertEqual(c.inserts, 1)

    def test_publish_and_external_ids_are_both_unique(self):
        for kind, payload, alternate in (
            ("video", unified_video_payload(), unified_video_payload(video_id="video_2")),
            ("comment", unified_comment_payload(), unified_comment_payload(comment_id="comment_2")),
            ("publish_log", unified_video_payload(), unified_video_payload(publish_id=2)),
        ):
            c = FakeConnection()
            ledger = UnifiedYouTubeLedger(lambda: c)
            key = {"video": "video_id", "comment": "comment_id", "publish_log": "publish_id"}[kind]
            ledger.execute("insert", TABLE_BY_KIND[kind], str(payload[key]), payload)
            with self.subTest(kind=kind), self.assertRaises(LedgerRPCError):
                ledger.execute("insert", TABLE_BY_KIND[kind], str(alternate[key]), alternate)
            self.assertEqual(c.inserts, 1)

    def test_feishu_operator_id_is_lossless_text_for_all_three_tables(self):
        for operator in ("892fd2e8", "c31ggb2g", "cf1edggd", "Mixed_CASE-01", "", "操作者😀", "a" * 128, " ID "):
            for kind, payload, key in (("video", unified_video_payload(), "video_id"),
                                       ("publish_log", unified_video_payload(), "publish_id"),
                                       ("comment", unified_comment_payload(), "comment_id")):
                payload["operator_user_id"] = operator
                c = FakeConnection()
                ledger = UnifiedYouTubeLedger(lambda: c)
                with self.subTest(operator=operator, kind=kind):
                    ledger.execute("insert", TABLE_BY_KIND[kind], str(payload[key]), payload)
                    row = c.rows[TABLE_BY_KIND[kind]][0]
                    self.assertIs(type(row["operator_user_id"]), str)
                    self.assertEqual(row["operator_user_id"], operator)
                    self.assertEqual(json.loads(row["payload_json"])["operator_user_id"], operator)

    def test_operator_id_rejects_numeric_oversize_and_unicode_controls(self):
        for operator in (None, 0, 803, True, "a" * 129, "bad\x00id", "bad\x1fid", "bad\x7fid", "bad\x85id", "bad\u202eid", "bad\ud800id"):
            for kind, payload, key in (("video", unified_video_payload(), "video_id"),
                                       ("publish_log", unified_video_payload(), "publish_id"),
                                       ("comment", unified_comment_payload(), "comment_id")):
                payload["operator_user_id"] = operator
                with self.subTest(operator=repr(operator), kind=kind), self.assertRaises(DramaSynthesisError):
                    validate_entity_payload(kind, str(payload[key]), payload)

    def test_canary_payload_requires_nonempty_safe_operator_at_ingress(self):
        for operator in ("", "操作者", "_leading", "has space", "a.b", "-leading"):
            for kind, payload, key in (("video", unified_video_payload(), "video_id"),
                                       ("publish_log", unified_video_payload(), "publish_id"),
                                       ("comment", unified_comment_payload(), "comment_id")):
                payload.update(operator_user_id=operator, canary_operation_id=CANARY_OPERATION_ID,
                               channel_local_id=int(CANARY_CHANNEL_LOCAL_ID))
                if kind != "comment":
                    payload.update(app_id=int(CANARY_APP_ID), privacy_status="unlisted")
                with self.subTest(operator=operator, kind=kind), self.assertRaises(DramaSynthesisError):
                    validate_entity_payload(kind, str(payload[key]), payload)

    def test_same_publish_identity_cannot_change_feishu_operator(self):
        for kind, payload, key in (("video", unified_video_payload(), "video_id"),
                                   ("publish_log", unified_video_payload(), "publish_id"),
                                   ("comment", unified_comment_payload(), "comment_id")):
            payload["operator_user_id"] = "892fd2e8"
            c = FakeConnection()
            ledger = UnifiedYouTubeLedger(lambda: c)
            ledger.execute("insert", TABLE_BY_KIND[kind], str(payload[key]), payload)
            with self.subTest(kind=kind), self.assertRaises(LedgerRPCError) as error:
                ledger.execute("insert", TABLE_BY_KIND[kind], str(payload[key]), dict(payload, operator_user_id="cf1edggd"))
            self.assertEqual(error.exception.code, "youtube_sync_identity_conflict")
            self.assertEqual(c.inserts, 1)

    def test_out_of_order_log_comment_video_allowed(self):
        c = FakeConnection()
        ledger = UnifiedYouTubeLedger(lambda: c)
        ledger.execute("insert", "ads_youtube_publish_log", "1", unified_video_payload())
        ledger.execute("insert", "ads_youtube_comments", "comment_1", unified_comment_payload())
        ledger.execute("insert", "ads_youtube_videos", "video_1", unified_video_payload())
        self.assertEqual(c.inserts, 3)
        self.assertFalse(any(re.match(r"(?:UPDATE|DELETE|ALTER|DROP|REPLACE|CREATE)", sql) for sql in c.sql))
        self.assertFalse(any("kunlunads_dev" in sql for sql in c.sql))

    def test_unique_key_race_reuses_only_exact_payload(self):
        for conflict in (False, True):
            c = FakeConnection()
            record = _video_record(unified_video_payload())
            if conflict:
                record["title"] = "race conflict"
            c.race_record = record
            ledger = UnifiedYouTubeLedger(lambda: c)
            if conflict:
                with self.assertRaises(LedgerRPCError):
                    ledger.execute("insert", "ads_youtube_videos", "video_1", unified_video_payload())
            else:
                self.assertEqual(ledger.execute("insert", "ads_youtube_videos", "video_1", unified_video_payload()),
                                 {"idempotent_success": True, "reused": True})
            self.assertGreater(c.rollbacks, 0)

    def test_health_rejects_identity_schema_indexes_and_indirect_writes(self):
        changes = [
            ("schema", "kunlunads_dev"), ("account", "ads_aius@43.166.187.96"), ("read_only", 1),
            ("read_only", None), ("existing", set()), ("extra_column", True), ("missing_index", True),
            ("table_override", {"TABLE_TYPE": "VIEW"}), ("table_override", {"ENGINE": "MyISAM"}),
            ("table_override", {"TABLE_COMMENT": "unowned"}),
            ("column_override", {"EXTRA": "STORED GENERATED"}),
            ("index_override", {"SUB_PART": 5}), ("index_override", {"NON_UNIQUE": 1}),
            ("foreign_keys", [{"TABLE_NAME": "ads_youtube_videos", "REFERENCED_TABLE_SCHEMA": "kunlunads_dev"}]),
        ]
        for attribute, value in changes:
            c = FakeConnection()
            setattr(c, attribute, value)
            with self.subTest(attribute=attribute, value=value), self.assertRaises(LedgerRPCError):
                UnifiedYouTubeLedger(lambda: c).health()

    def test_health_rejects_every_wider_grant_shape(self):
        extras = [
            "GRANT ALL PRIVILEGES ON ads_ai.* TO 'drama_youtube_writer'@'43.166.187.96'",
            "GRANT EXECUTE ON PROCEDURE ads_ai.p TO 'drama_youtube_writer'@'43.166.187.96'",
            "GRANT PROXY ON ''@'' TO 'drama_youtube_writer'@'43.166.187.96'",
            "GRANT SELECT ON kunlunads_dev.ads_youtube_videos TO 'drama_youtube_writer'@'43.166.187.96'",
        ]
        for grant in extras:
            c = FakeConnection()
            c.show_grants = exact_show_grants() + [{"grant": grant}]
            with self.subTest(grant=grant), self.assertRaises(LedgerRPCError):
                UnifiedYouTubeLedger(lambda: c).health()
        for attribute, value in (
            ("global_privileges", [{"PRIVILEGE_TYPE": "PROCESS", "IS_GRANTABLE": "NO"}]),
            ("schema_privileges", [{"TABLE_SCHEMA": SCHEMA, "PRIVILEGE_TYPE": "SELECT"}]),
            ("column_privileges", [{"COLUMN_NAME": "video_id", "PRIVILEGE_TYPE": "SELECT"}]),
            ("table_privileges_extra", [{"TABLE_SCHEMA": SCHEMA, "TABLE_NAME": "ads_youtube_videos", "PRIVILEGE_TYPE": "DELETE", "IS_GRANTABLE": "NO"}]),
        ):
            c = FakeConnection()
            setattr(c, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(LedgerRPCError):
                UnifiedYouTubeLedger(lambda: c).health()
        c = FakeConnection()
        c.show_grants = exact_show_grants()
        c.show_grants[-1]["grant"] += " WITH GRANT OPTION"
        with self.assertRaises(LedgerRPCError):
            UnifiedYouTubeLedger(lambda: c).health()

    def test_runtime_does_not_claim_hidden_trigger_absence(self):
        c = FakeConnection()
        UnifiedYouTubeLedger(lambda: c).health()
        self.assertFalse(any("information_schema.TRIGGERS" in sql for sql in c.sql))
        c.triggers = [{"TRIGGER_NAME": "forbidden", "EVENT_OBJECT_TABLE": "ads_youtube_videos"}]
        with self.assertRaises(LedgerRPCError):
            inspect_owned_tables(c.cursor(), inspect_triggers=True)

    def test_operation_preflight_rejects_drift_before_insert(self):
        c = FakeConnection()
        c.table_override = {"TABLE_COMMENT": "unowned"}
        with self.assertRaises(LedgerRPCError):
            UnifiedYouTubeLedger(lambda: c).execute("insert", "ads_youtube_videos", "video_1", unified_video_payload())
        self.assertEqual(c.inserts, 0)

    def test_exact_quoted_grant_accounts_remain_supported(self):
        for quote in ("'", chr(96)):
            c = FakeConnection()
            c.show_grant_account_quote = quote
            self.assertTrue(UnifiedYouTubeLedger(lambda: c).health()["ok"])

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
            self.assertEqual(validate_writer_health(json.loads(response.read()))["contract"], WRITER_HEALTH_CONTRACT)
            body = json.dumps({"action": "select", "table": "ads_youtube_videos", "external_id": "video_1", "payload": {}})
            client.request("POST", RPC_PATH, body=body, headers={"Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            client.request("POST", RPC_PATH, body=body, headers={"Content-Type": "application/json", "Authorization": "Bearer " + "t" * 32})
            response = client.getresponse()
            self.assertEqual((response.status, json.loads(response.read())), (200, {"found": False}))
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
