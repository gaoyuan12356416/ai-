"""Recovery admits only reviewed zero-queue manual runs; it never publishes."""
import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_posts import service
from features.pipeline_dispatch.x_manual_recovery import authorized_run_ids
from scripts import recover_x_unstarted_manual as recovery
from scripts.test_x_post_priority_manual_store import ACTOR, material_candidate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "posts.sqlite3"
        self.store = service.XPostStore(self.path)
        self.env = mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1",
            "PIPELINE_X_DISPATCH_CUTOVER_AT": "2026-11-02T11:17:00Z"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def create(self, material="501", account=2):
        with mock.patch.object(service, "utc_now", return_value="2026-11-02T08:00:00Z"):
            return self.store.create_manual_run([material], [account], "test-" + material, ACTOR)

    def plan(self, run):
        return recovery.plan(self.path, [run["id"]], [run["run_date"]])

    def apply(self, saved):
        return recovery.apply(self.path, saved, saved["plan_sha256"],
            Path(self.temp.name) / "backup", "operator", "resume reviewed backlog")

    def test_exact_admission_retains_frozen_identity_and_never_creates_queue(self):
        first, other = self.create(), self.create("502", 3)
        self.assertFalse(self.store.claim_manual_run()["found"])
        saved = self.plan(first)
        result = self.apply(saved)
        self.assertTrue(result["applied"])
        self.assertFalse(result["platform_called"])
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_queue").fetchone()[0], 0)
        claimed = self.store.claim_manual_run()
        self.assertEqual(claimed["run"]["id"], first["id"])
        self.assertEqual(claimed["run"]["created_at"], first["created_at"])
        self.assertEqual(self.store.get_manual_run(other["id"])["status"], "queued")
        # Retrying an already committed action while its planner runs is a no-op.
        repeated = self.apply(saved)
        self.assertTrue(repeated["already_applied"])
        self.assertFalse(repeated["applied"])
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_manual_resume_audit").fetchone()[0], 1)

    def test_started_or_queued_publication_is_never_admitted(self):
        run = self.create()
        self.store.create_manual_plan(run["id"], [material_candidate(run, 2, "501")])
        with self.assertRaisesRegex(recovery.RecoveryRejected, "already_started"):
            self.plan(run)

    def test_unknown_and_wrong_date_are_rejected(self):
        run = self.create()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "outside_authorized_scope"):
            recovery.plan(self.path, [run["id"]], ["2026-11-01"])
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE x_post_manual_run SET unknown_count=1 WHERE id=?", (run["id"],))
            c.commit()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "already_started"):
            self.plan(run)

    def test_row_drift_after_plan_is_rejected_before_admission(self):
        run = self.create()
        saved = self.plan(run)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE x_post_manual_run SET status='running' WHERE id=?", (run["id"],))
            c.commit()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "already_started"):
            self.apply(saved)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='x_post_manual_resume_audit'").fetchone())

    def test_frozen_identity_drift_revokes_only_exact_authorization(self):
        run = self.create()
        self.apply(self.plan(run))
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE x_post_manual_resume_audit SET identity_sha256=? WHERE run_id=?", ("0" * 64, run["id"]))
            c.commit()
        self.assertFalse(self.store.claim_manual_run()["found"])

    def test_absent_audit_table_read_does_not_write_schema(self):
        with contextlib.closing(recovery.connect(self.path)) as c:
            before = c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            self.assertEqual(authorized_run_ids(c), [])
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], before)

    def test_released_reservation_rejected(self):
        run = self.create()
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE x_post_manual_material_reservation SET state='released',release_reason='rejected' WHERE manual_run_id=?", (run["id"],))
            c.commit()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "reservation_not_active"):
            self.plan(run)


if __name__ == "__main__":
    unittest.main()
