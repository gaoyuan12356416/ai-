"""Scoped zero-queue recovery preserves prior publication facts and identity."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from features.x_auto_posts.operator_retry import EXTERNAL_KEY, OperatorRetryError, execution_key
from scripts.retry_x_auto_zero_queue import RecoveryRejected, apply_plan, build_plan


class OperatorRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auto = self.root / "auto.sqlite3"
        self.native = self.root / "native.sqlite3"
        self.task = {
            "id": 2712, "run_id": 446, "account_id": "46", "template_id": 4, "template_version": 6,
            "status": "failed", "error_code": "x_post_account_needs_review", "error_message": "old hold",
            "execution_run_id": 1657, "execution_queue_id": None, "execution_log_id": None,
            "unknown_outcome": 0, "publish_id": "", "publish_url": "", "claim_token": "", "claim_worker": "",
            "claim_phase": "", "lease_expires_at_utc": "", "preparation_attempt_count": 0, "publish_attempt_count": 0,
            "account_snapshot_json": '{"id":46,"username":"nedialko123","drama_language":"de"}',
            "account_snapshot_version": 1, "language": "de", "scheduled_at_utc": "2026-11-03T12:59:00+00:00",
            "content_id": "", "series_code": "", "material_id": "", "selection_json": "{}", "body_sha256": "",
            "body_utf16_units": 0, "selected_duration_sec": 0, "body_template": "", "next_attempt_at_utc": "",
            "selected_at_utc": "2026-11-03T13:00:51+00:00", "reserved_at_utc": "", "bridged_at_utc": "",
            "published_at_utc": "", "finished_at_utc": "2026-11-03T13:01:12+00:00", "updated_at": "before",
        }
        with closing(sqlite3.connect(self.auto)) as c, c:
            columns = ",".join(key + (" INTEGER" if isinstance(value, int) or value is None else " TEXT")
                               + (" PRIMARY KEY" if key == "id" else "") for key, value in self.task.items())
            c.execute("CREATE TABLE x_auto_task(" + columns + ")")
            c.execute("INSERT INTO x_auto_task VALUES(" + ",".join("?" for _ in self.task) + ")", tuple(self.task.values()))
            other = {**self.task, "id": 2731, "account_id": "49", "execution_run_id": None,
                     "error_code": "x_account_publish_not_approved"}
            c.execute("INSERT INTO x_auto_task VALUES(" + ",".join("?" for _ in other) + ")", tuple(other.values()))
            c.executescript("""
                CREATE TABLE x_auto_run(id INTEGER PRIMARY KEY,template_id INTEGER,template_version INTEGER,
                    status TEXT,config_sha256 TEXT,finished_at_utc TEXT,updated_at TEXT);
                CREATE TABLE x_auto_material_ledger(material_id TEXT,task_id INTEGER);
                CREATE TABLE x_auto_event(id INTEGER PRIMARY KEY,run_id INTEGER,task_id INTEGER,event_type TEXT,
                    from_status TEXT,to_status TEXT,message TEXT,details_json TEXT,created_at TEXT);
            """)
            c.execute("INSERT INTO x_auto_run VALUES(446,4,6,'running',?,'','before')", ("a" * 64,))
        with closing(sqlite3.connect(self.native)) as c, c:
            c.executescript("""
                CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,status TEXT,error_code TEXT,trigger_source TEXT,
                    external_task_key TEXT,template_ref TEXT,template_version INTEGER,account_ids_json TEXT,
                    expected_count INTEGER,queued_count INTEGER,published_count INTEGER,failed_count INTEGER,unknown_count INTEGER);
                INSERT INTO x_post_manual_run VALUES(1657,'failed_preflight','x_post_account_needs_review','auto_template',
                    'x-auto-task-2712','x-auto-template-4',6,'[46]',1,0,0,0,0);
                CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,manual_run_id INTEGER,account_id INTEGER,status TEXT);
                CREATE TABLE x_post_manual_material_reservation(manual_run_id INTEGER,state TEXT);
                INSERT INTO x_post_manual_material_reservation VALUES(1657,'released');
                CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,username TEXT,status TEXT,publish_approved INTEGER,
                    drama_language TEXT,deleted_at TEXT);
                INSERT INTO x_authorized_account VALUES(46,'nedialko123','active',1,'de','');
                CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,account_id INTEGER,unknown_outcome INTEGER,status TEXT,
                    queue_id INTEGER,attempt_count INTEGER,x_post_id TEXT,published_at TEXT);
                INSERT INTO x_post_publish_log VALUES(5327,46,0,'failed',5393,1,'','');
                CREATE TABLE x_post_repost_ledger(target_account_id INTEGER,relay_account_id INTEGER,unknown_outcome INTEGER,status TEXT);
            """)

    def read(self, path, sql):
        with closing(sqlite3.connect(path)) as c:
            c.row_factory = sqlite3.Row
            return [dict(row) for row in c.execute(sql)]

    def mutate(self, path, sql):
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(sql)

    def plan(self):
        return build_plan(self.auto, self.native, 2712)

    def apply(self, plan):
        return apply_plan(plan, self.auto, self.native, self.root / "backup", "operator", "user authorized HeartFlare retry")

    def task_object(self):
        row = self.read(self.auto, "SELECT * FROM x_auto_task WHERE id=2712")[0]
        return SimpleNamespace(**{**row, "account_snapshot": json.loads(row["account_snapshot_json"])})

    def test_absent_audit_table_keeps_default_without_creating_storage(self):
        store = SimpleNamespace(db_path=str(self.auto))
        self.assertEqual(execution_key(store, self.task_object()), "x-auto-task-2712")
        self.assertEqual(self.read(self.auto, "SELECT name FROM sqlite_master WHERE name='x_auto_operator_retry'"), [])

    def test_apply_rearms_only_original_task_and_preserves_prior_run(self):
        prior = self.read(self.native, "SELECT * FROM x_post_manual_run")
        other = self.read(self.auto, "SELECT * FROM x_auto_task WHERE id=2731")
        plan = self.plan()
        result = self.apply(plan)
        self.assertEqual(result["publish_requests"], 0)
        self.assertEqual(self.read(self.native, "SELECT * FROM x_post_manual_run"), prior)
        self.assertEqual(self.read(self.auto, "SELECT * FROM x_auto_task WHERE id=2731"), other)
        task = self.task_object()
        self.assertEqual(task.status, "pending")
        self.assertIsNone(task.execution_run_id)
        self.assertEqual(task.account_snapshot_json, self.task["account_snapshot_json"])
        self.assertEqual(task.scheduled_at_utc, self.task["scheduled_at_utc"])
        self.assertEqual(execution_key(SimpleNamespace(db_path=str(self.auto)), task), EXTERNAL_KEY)
        self.assertTrue((self.root / "backup" / "auto.before.sqlite3").is_file())
        self.assertTrue((self.root / "backup" / "native.before.sqlite3").is_file())
        self.assertEqual(len(self.read(self.auto, "SELECT * FROM x_auto_event")), 1)
        self.assertTrue(self.apply(plan)["already_applied"])
        self.assertEqual(len(self.read(self.auto, "SELECT * FROM x_auto_event")), 1)

    def test_unknown_result_and_real_queue_each_block_plan(self):
        self.mutate(self.native, "UPDATE x_post_publish_log SET unknown_outcome=1 WHERE id=5327")
        with self.assertRaisesRegex(RecoveryRejected, "not_reconciled"):
            self.plan()
        self.mutate(self.native, "UPDATE x_post_publish_log SET unknown_outcome=0 WHERE id=5327")
        self.mutate(self.native, "INSERT INTO x_post_queue VALUES(999,1657,46,'failed')")
        with self.assertRaisesRegex(RecoveryRejected, "queue_exists"):
            self.plan()

    def test_existing_healthy_publisher_is_preserved_while_task_returns_pending(self):
        self.mutate(self.native, "INSERT INTO x_post_queue VALUES(5362,NULL,46,'publishing')")
        self.mutate(self.native, "INSERT INTO x_post_publish_log VALUES(5850,46,0,'media_uploading',5362,1,'','')")
        before_queue = self.read(self.native, "SELECT * FROM x_post_queue")
        before_log = self.read(self.native, "SELECT * FROM x_post_publish_log")
        plan = self.plan()
        busy = plan["state"]["existing_busy_queues"]
        self.assertEqual([(r["queue_id"], r["log_id"], r["log_status"]) for r in busy],
                         [(5362, 5850, "media_uploading")])
        result = self.apply(plan)
        self.assertEqual(result["publish_requests"], 0)
        self.assertEqual(self.task_object().status, "pending")
        self.assertEqual(self.read(self.native, "SELECT * FROM x_post_queue"), before_queue)
        self.assertEqual(self.read(self.native, "SELECT * FROM x_post_publish_log"), before_log)

    def test_busy_publisher_unknown_or_post_creation_still_rejects_admission(self):
        self.mutate(self.native, "INSERT INTO x_post_queue VALUES(5362,NULL,46,'publishing')")
        self.mutate(self.native, "INSERT INTO x_post_publish_log VALUES(5850,46,1,'media_uploading',5362,1,'','')")
        with self.assertRaisesRegex(RecoveryRejected, "not_reconciled"):
            self.plan()
        self.mutate(self.native, "UPDATE x_post_publish_log SET unknown_outcome=0,status='post_creating' WHERE id=5850")
        with self.assertRaisesRegex(RecoveryRejected, "not_reconciled"):
            self.plan()

    def test_scope_reservation_and_live_drift_rejected(self):
        with self.assertRaisesRegex(RecoveryRejected, "only_task_2712"):
            build_plan(self.auto, self.native, 2731)
        self.mutate(self.auto, "INSERT INTO x_auto_material_ledger VALUES('material',2712)")
        with self.assertRaisesRegex(RecoveryRejected, "reservation_exists"):
            self.plan()
        self.mutate(self.auto, "DELETE FROM x_auto_material_ledger")
        plan = self.plan()
        self.mutate(self.auto, "UPDATE x_auto_task SET updated_at='changed' WHERE id=2712")
        with self.assertRaisesRegex(RecoveryRejected, "live_state_changed"):
            self.apply(plan)
        self.assertEqual(self.task_object().status, "failed")
        self.assertEqual(self.read(self.auto, "SELECT name FROM sqlite_master WHERE name='x_auto_operator_retry'"), [])

    def test_helper_rejects_identity_drift_and_does_not_bind_new_material(self):
        self.apply(self.plan())
        self.mutate(self.auto, "UPDATE x_auto_task SET material_id='new-material',execution_run_id=2000 WHERE id=2712")
        store = SimpleNamespace(db_path=str(self.auto))
        self.assertEqual(execution_key(store, self.task_object()), EXTERNAL_KEY)
        self.mutate(self.auto, "UPDATE x_auto_task SET language='es' WHERE id=2712")
        with self.assertRaises(OperatorRetryError):
            execution_key(store, self.task_object())

    def test_invalid_receipt_cannot_redirect_task_to_another_execution_key(self):
        self.apply(self.plan())
        self.mutate(self.auto, "UPDATE x_auto_operator_retry SET external_task_key='x-auto-task-2712-retry-2'")
        with self.assertRaises(OperatorRetryError):
            execution_key(SimpleNamespace(db_path=str(self.auto)), self.task_object())


if __name__ == "__main__":
    unittest.main()
