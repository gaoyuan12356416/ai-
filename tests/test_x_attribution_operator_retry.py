"""Attribution admits only the audited Run 446 retry and canonical publication."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from features.x_auto_posts import operator_retry as retry
from features.x_posts import attribution
from tests.test_x_attribution import Fixture, STAMP


class OperatorRetryAttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)
        with closing(sqlite3.connect(self.fx.x)) as db:
            db.executescript("""
                ALTER TABLE x_post_queue ADD COLUMN account_id INTEGER;
                ALTER TABLE x_post_publish_log ADD COLUMN account_id INTEGER;
                ALTER TABLE x_post_manual_run ADD COLUMN template_version INTEGER;
                ALTER TABLE x_post_manual_run ADD COLUMN account_ids_json TEXT;
                ALTER TABLE x_post_manual_run ADD COLUMN material_ids_json TEXT;
                UPDATE x_post_queue SET id=5908,manual_run_id=1826,account_id=46 WHERE id=1;
                UPDATE x_post_publish_log SET id=5853,queue_id=5908,account_id=46 WHERE id=1;
                UPDATE x_post_manual_run SET id=1826,template_ref='x-auto-template-4',
                    external_task_key='x-auto-task-2712-retry-1',template_version=6,
                    account_ids_json='[46]',material_ids_json='["901"]' WHERE id=1;
            """)
        task = dict(id=2712, run_id=446, template_id=4, template_version=6,
                    account_id="46", language="en", account_snapshot_version=1,
                    account_snapshot={"id": "46", "username": "nedialko123"},
                    scheduled_at_utc=STAMP, execution_run_id=1826,
                    execution_queue_id=5908, execution_log_id=5853,
                    material_id="901", content_id="101")
        with closing(sqlite3.connect(self.fx.auto)) as db:
            db.executescript("""
                DROP TABLE x_auto_task;
                CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,
                    template_id INTEGER,template_version INTEGER,account_id TEXT,
                    language TEXT,account_snapshot_version INTEGER,account_snapshot_json TEXT,
                    scheduled_at_utc TEXT,execution_run_id INTEGER,execution_queue_id INTEGER,
                    execution_log_id INTEGER,material_id TEXT,content_id TEXT);
                CREATE TABLE x_auto_run(id INTEGER PRIMARY KEY, config_sha256 TEXT);
                UPDATE x_auto_template SET id=4 WHERE id=7;
            """)
            db.execute("INSERT INTO x_auto_run VALUES(446,?)", ("c" * 64,))
            db.execute("INSERT INTO x_auto_task VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (2712, 446, 4, 6, "46", "en", 1, json.dumps(task["account_snapshot"]),
                        STAMP, 1826, 5908, 5853, "901", "101"))
            db.execute(retry.SCHEMA)
            db.execute("INSERT INTO x_auto_operator_retry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (2712, 1, 446, "46", 4, 6, 1657, retry.EXTERNAL_KEY,
                        retry.fingerprint(retry.frozen_identity(SimpleNamespace(**task))),
                        "c" * 64, "a" * 64, "b" * 64, "d" * 64, "operator", "approved retry", STAMP))
            db.commit()

    def resolve(self):
        return self.fx.resolver.resolve(5908, 5853)

    def assert_rejected(self):
        before = (self.fx.x.read_bytes(), self.fx.auto.read_bytes())
        with self.assertRaises(attribution.AttributionError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "x_attribution_template_task_conflict")
        self.assertEqual(before, (self.fx.x.read_bytes(), self.fx.auto.read_bytes()))

    def test_audited_retry_keeps_original_creator_and_resolution_is_read_only(self):
        before = (self.fx.x.read_bytes(), self.fx.auto.read_bytes())
        context = self.resolve()
        self.assertEqual((context["owner"], context["tenant"], context["sub_user_id"]),
                         ("creator", "tenant", "42"))
        self.assertEqual((context["template_id"], context["af_c_id"]), (4, "5908"))
        query = parse_qs(urlsplit(attribution.build_url(context, self.fx.log(5853))).query)
        self.assertEqual(query["af_channel"], ["42"])
        self.assertEqual(query["af_c_id"], ["5908"])
        self.assertEqual(query["af_ad_id"], ["901"])
        self.assertEqual(before, (self.fx.x.read_bytes(), self.fx.auto.read_bytes()))

    def test_retry_without_audit_is_rejected(self):
        self.fx.execute(self.fx.auto, "DROP TABLE x_auto_operator_retry")
        self.assert_rejected()

    def test_arbitrary_retry_key_is_not_authorized_by_matching_task_number(self):
        self.fx.execute(self.fx.x, "UPDATE x_post_manual_run SET external_task_key='x-auto-task-2712-retry-2' WHERE id=1826")
        self.assert_rejected()

    def test_mutated_audit_or_frozen_identity_is_rejected(self):
        cases = [
            "UPDATE x_auto_operator_retry SET prior_run_id=1658",
            "UPDATE x_auto_operator_retry SET identity_sha256='bad'",
            "UPDATE x_auto_run SET config_sha256='changed'",
            "UPDATE x_auto_task SET account_snapshot_json='{}'",
        ]
        for change in cases:
            with self.subTest(change=change):
                original = self.fx.auto.read_bytes()
                self.fx.execute(self.fx.auto, change)
                self.assert_rejected()
                self.fx.auto.write_bytes(original)

    def test_audit_does_not_override_canonical_execution_identity(self):
        cases = [
            (self.fx.auto, "UPDATE x_auto_task SET execution_run_id=1827"),
            (self.fx.auto, "UPDATE x_auto_task SET execution_queue_id=5909"),
            (self.fx.auto, "UPDATE x_auto_task SET execution_log_id=5854"),
            (self.fx.auto, "UPDATE x_auto_task SET material_id='902'"),
            (self.fx.auto, "UPDATE x_auto_task SET content_id='102'"),
            (self.fx.x, "UPDATE x_post_manual_run SET template_version=7 WHERE id=1826"),
            (self.fx.x, "UPDATE x_post_manual_run SET account_ids_json='[47]' WHERE id=1826"),
            (self.fx.x, "UPDATE x_post_manual_run SET material_ids_json='[\"902\"]' WHERE id=1826"),
            (self.fx.x, "UPDATE x_post_queue SET account_id=47 WHERE id=5908"),
            (self.fx.x, "UPDATE x_post_publish_log SET account_id=47 WHERE id=5853"),
        ]
        for path, change in cases:
            with self.subTest(change=change):
                original = path.read_bytes()
                self.fx.execute(path, change)
                self.assert_rejected()
                path.write_bytes(original)

    def test_already_frozen_context_is_reused_after_template_changes(self):
        context = self.resolve()
        log = self.fx.log(5853)
        attribution.freeze(self.fx.x, log, context)
        self.fx.execute(self.fx.auto, "DELETE FROM x_auto_operator_retry")
        self.fx.execute(self.fx.auto, "UPDATE x_auto_template SET created_by_user_id='editor'")
        self.assertEqual(self.resolve(), context)
        self.assertEqual(attribution.freeze(self.fx.x, log, context), context)


if __name__ == "__main__":
    unittest.main()
