"""Offline exact-admission, transaction rollback, race and no-repost contracts."""
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from features.x_posts.service import XPostError, XPostStore
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.pool import install_schema
from features.x_video_publish import recovery_1389 as recovery
from features.pipeline_dispatch import x_manual_recovery
from scripts import recover_x_manual_1389 as helper
from scripts.recover_x_unstarted_manual import connect
from scripts.test_x_post_priority_manual_store import material_candidate


class ExactPersonalAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "native.sqlite3"
        self.personal = PersonalStore(self.path)
        self.store = XPostStore(self.path)
        with contextlib.closing(connect(self.path, False)) as c:
            for table, value in (("x_post_manual_run", 1388), ("x_video_publish_batch", 314),
                    ("x_video_publish_task", 360), ("x_post_manual_material_reservation", 1425)):
                c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, value))
            install_schema(c)
            c.execute("CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,status TEXT,publish_approved INTEGER,drama_language TEXT)")
            c.execute("INSERT INTO x_authorized_account VALUES(43,'active',1,'ja')")
        actor = {"tenant_key": "tenant", "user_id": "operator", "name": "Operator"}
        account = {"id": 43, "owner_tenant_key": "tenant", "owner_user_id": "operator",
            "status": "active", "publish_approved": True, "publish_eligible": True, "drama_language": "ja"}
        material = {"material_id": "5188422", "language": "ja", "drama_name": "Drama 5188422",
            "drama_description": "A complete and safe drama description."}
        now = datetime(2026, 11, 2, 8, 45, 15, tzinfo=timezone.utc)
        preview = self.personal.preview(actor, {"source": "pool", "rows": [
            {"account_id": 43, "material_id": "5188422", "caption": "Frozen caption"}]},
            {43: account}, {"5188422": material}, now=now)
        batch = self.personal.commit(actor, preview["preview_token"], "original-task-361", {43: account},
            {"5188422": material}, now=now)
        self.assertEqual((315, 361, 1389), (batch["id"], batch["tasks"][0]["id"], batch["tasks"][0]["manual_run_id"]))
        with mock.patch("features.x_posts.service.utc_now", return_value="2026-11-03T10:52:15Z"):
            self.store.claim_manual_run()
        with mock.patch("features.x_posts.service.utc_now", return_value="2026-11-03T10:53:21Z"):
            self.store.record_manual_failure(1389, "x_post_manual_preflight_failed",
                "read-only MySQL connection failed: OperationalError")
        with contextlib.closing(connect(self.path, False)) as c:
            c.execute("INSERT INTO x_video_publish_pool_item VALUES(1008,'tenant','operator',43,'5188422',"
                "'processing',1,'material','ja','{}','361','315','','created','updated')")
            c.execute("INSERT INTO x_video_publish_pool_slot VALUES(836,'tenant','operator',43,'43','2026-11-02',"
                "'16:45','original','created','',1008,'5188422','361','315','created')")
            c.execute(x_manual_recovery.SCHEMA)
            run = dict(c.execute("SELECT * FROM x_post_manual_run WHERE id=1389").fetchone())
            own_identity = x_manual_recovery.fingerprint(x_manual_recovery.identity(run))
            c.execute("INSERT INTO x_post_manual_resume_audit VALUES(1389,?,?,'root','first','authorized','backup','{}')",
                (own_identity, recovery.FIRST_PLAN))
            self.original = recovery.collect(c)
        self.addCleanup(mock.patch.stopall)
        self.post = mock.patch("features.x_posts.service.XApiClient.create_post",
            side_effect=AssertionError("maintenance must not call Post")).start()
        mock.patch.object(recovery, "IDENTITY", own_identity).start()
        mock.patch.object(recovery, "EXPECTED", {k: recovery.fingerprint(v) for k, v in self.original.items()}).start()

    def apply(self):
        saved = helper.plan(self.path)
        result = helper.apply(self.path, saved, saved["plan_sha256"], Path(self.tmp.name) / "backup", "root", "exact incident")
        return saved, result

    def candidate(self):
        value = material_candidate(self.store.get_manual_run(1389), 43, "5188422")
        value["material_language"] = "ja"
        return value

    def execute(self, sql, params=()):
        with contextlib.closing(connect(self.path, False)) as c:
            return c.execute(sql, params).fetchall()

    def test_original_run_repreflight_creates_one_queue_preserves_history(self):
        saved, result = self.apply()
        self.assertFalse(result["platform_called"])
        self.assertEqual(0, result["queues_created"])
        self.assertEqual([1389], self.authorized())
        planned = self.store.create_manual_plan(1389, [self.candidate()])
        self.assertTrue(planned["created"])
        replay = self.store.create_manual_plan(1389, [self.candidate()])
        self.assertFalse(replay["created"])
        with contextlib.closing(connect(self.path)) as c:
            grant, consumed = recovery.existing(c), recovery.existing(c, recovery.CONSUMED)
            self.assertEqual(saved["plan_sha256"], consumed["plan_sha256"])
            self.assertEqual(self.original, json.loads(grant["evidence_json"]))
            self.assertEqual(self.original["reservation"], recovery.collect(c)["reservation"])
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0])
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM x_video_publish_task").fetchone()[0])
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM x_post_publish_log").fetchone()[0])
        self.assertTrue(helper.apply(self.path, saved, saved["plan_sha256"], self.tmp.name,
            "root", "same")['already_applied'])
        self.post.assert_not_called()
        with contextlib.closing(connect(self.path, False)) as c:
            c.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(XPostError, "admission_already_consumed"):
                recovery.validate_admission(c, 1389, [self.candidate()], [])

    def authorized(self):
        with contextlib.closing(connect(self.path)) as c:
            return x_manual_recovery.authorized_run_ids(c)

    def test_apply_replay_does_not_reset_new_failed_preflight(self):
        saved, _ = self.apply()
        self.store.record_manual_failure(1389, "new_failure", "new failure must remain")
        helper.apply(self.path, saved, saved["plan_sha256"], self.tmp.name, "root", "same")
        self.assertEqual("new_failure", self.store.get_manual_run(1389)["error_code"])
        with self.assertRaisesRegex(XPostError, "admission_attempt_ended"):
            self.store.create_manual_plan(1389, [self.candidate()])

    def test_new_active_material_reservation_after_grant_is_rejected(self):
        self.apply()
        other = self.store.create_manual_run(["6000001"], [44], "another-manual-task", {"user_id": "operator", "name": "Operator"})
        self.execute("INSERT INTO x_post_manual_material_reservation(manual_run_id,material_key,state,release_reason,created_at,updated_at) "
            "VALUES(?,'5188422','active','','now','now')", (other["id"],))
        with self.assertRaisesRegex(XPostError, "material_reserved"):
            self.store.create_manual_plan(1389, [self.candidate()])
        self.assertEqual([], self.execute("SELECT id FROM x_post_queue WHERE manual_run_id=1389"))

    def test_new_material_queue_after_grant_is_rejected(self):
        self.apply()
        other = self.store.create_manual_run(["5188422"], [44], "other-plan-task", {"user_id": "operator", "name": "Operator"})
        self.store.create_manual_plan(other["id"], [material_candidate(other, 44, "5188422")])
        with self.assertRaisesRegex(XPostError, "queue_exists"):
            self.store.create_manual_plan(1389, [self.candidate()])

    def test_fresh_account_unknown_still_blocks(self):
        self.apply()
        other = self.store.create_manual_run(["6000001"], [43], "other-unknown-task", {"user_id": "operator", "name": "Operator"})
        planned = self.store.create_manual_plan(other["id"], [material_candidate(other, 43, "6000001")])
        log = self.store.reserve_log(planned["queues"][0]["id"])
        self.execute("UPDATE x_post_publish_log SET unknown_outcome=1,status='failed' WHERE id=?", (log["id"],))
        with self.assertRaises(XPostError) as blocked:
            self.store.create_manual_plan(1389, [self.candidate()])
        self.assertEqual("x_post_unknown_outcome", blocked.exception.code)
        self.assertEqual([], self.execute("SELECT id FROM x_post_queue WHERE manual_run_id=1389"))

    def test_other_released_run_still_rejected(self):
        self.apply()
        other = self.store.create_manual_run(["6000001"], [44], "other-failed-task", {"user_id": "operator", "name": "Operator"})
        self.execute("UPDATE x_post_manual_material_reservation SET state='released',release_reason='failed' WHERE manual_run_id=?", (other["id"],))
        with self.assertRaisesRegex(XPostError, "占用状态"):
            self.store.create_manual_plan(other["id"], [material_candidate(other, 44, "6000001")])

    def test_no_grant_does_not_allow_original_released_reservation(self):
        with self.assertRaisesRegex(XPostError, "占用状态"):
            self.store.create_manual_plan(1389, [self.candidate()])

    def test_account_and_pool_drift_checked_in_queue_transaction(self):
        for sql, error in (("UPDATE x_authorized_account SET status='locked'", "account_unavailable"),
                ("UPDATE x_video_publish_pool_item SET status='failed'", "pool_item_drift")):
            with self.subTest(error=error):
                saved, _ = self.apply() if not self.execute("SELECT 1 FROM sqlite_master WHERE name=?", (recovery.GRANTS,)) else (None, None)
                self.execute(sql)
                with self.assertRaisesRegex(XPostError, error):
                    self.store.create_manual_plan(1389, [self.candidate()])
                self.execute("UPDATE x_authorized_account SET status='active'")

    def test_wrong_language_and_partial_failure_cannot_consume_grant(self):
        self.apply()
        wrong = dict(self.candidate(), material_language="en")
        with self.assertRaises(XPostError):
            self.store.create_manual_plan(1389, [wrong])
        with self.assertRaisesRegex(XPostError, "normal_preflight_must_pass"):
            self.store.create_manual_plan(1389, [], preflight_failures=[{"account_id": 43,
                "material_id": "5188422", "error_code": "account_unavailable", "error_message": "failed"}])
        with contextlib.closing(connect(self.path)) as c:
            self.assertIsNone(recovery.existing(c, recovery.CONSUMED))

    def test_plan_apply_cas_and_code_drift(self):
        saved = helper.plan(self.path)
        with mock.patch.object(helper, "code_proof", return_value={}):
            with self.assertRaisesRegex(XPostError, "path_code"):
                helper.apply(self.path, saved, saved["plan_sha256"], self.tmp.name, "root", "same")
        self.execute("UPDATE x_post_manual_run SET error_message='changed' WHERE id=1389")
        with self.assertRaisesRegex(XPostError, "run_drift"):
            helper.apply(self.path, saved, saved["plan_sha256"], self.tmp.name, "root", "same")

    def test_admission_transaction_rollback(self):
        saved = helper.plan(self.path)
        self.execute("CREATE TRIGGER fail_admission BEFORE UPDATE OF status ON x_post_manual_run "
            "WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            helper.apply(self.path, saved, saved["plan_sha256"], self.tmp.name, "root", "same")
        with contextlib.closing(connect(self.path)) as c:
            self.assertIsNone(recovery.existing(c))
            self.assertEqual(self.original, recovery.collect(c))

    def test_queue_and_consumption_rollback_together(self):
        self.apply()
        with mock.patch.object(recovery, "consume", side_effect=sqlite3.IntegrityError("injected")):
            with self.assertRaises(XPostError):
                self.store.create_manual_plan(1389, [self.candidate()])
        with contextlib.closing(connect(self.path)) as c:
            self.assertIsNone(recovery.existing(c, recovery.CONSUMED))
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0])
            self.assertEqual("queued", c.execute("SELECT status FROM x_post_manual_run WHERE id=1389").fetchone()[0])
        self.assertTrue(self.store.create_manual_plan(1389, [self.candidate()])["created"])

    def test_audits_append_only_and_released_trigger_unchanged(self):
        self.apply()
        self.store.create_manual_plan(1389, [self.candidate()])
        for table in (recovery.GRANTS, recovery.CONSUMED):
            with self.assertRaises(sqlite3.IntegrityError):
                self.execute("DELETE FROM " + table)
            with self.assertRaises(sqlite3.IntegrityError):
                self.execute("UPDATE " + table + " SET plan_sha256='changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                self.execute("INSERT OR REPLACE INTO " + table + " SELECT * FROM " + table)
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute("UPDATE x_post_manual_material_reservation SET state='active',release_reason='' WHERE id=1426")


if __name__ == "__main__":
    unittest.main()
