"""Exact schedule preflight recovery retains frozen scope and never publishes."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from features.x_posts import service
from features.pipeline_dispatch.x_schedule_recovery import authorized_run_ids, identity
from scripts import recover_x_zero_write_schedules as recovery
from scripts.test_x_post_multi_schedule_store import base_candidate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "posts.sqlite3"
        self.store = service.XPostStore(self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,x_user_id TEXT,status TEXT,"
                "publish_approved INTEGER,drama_language TEXT,deleted_at TEXT)")
            c.executemany("INSERT INTO x_authorized_account VALUES(?,?,'active',1,'en','')", [(2, 'u2'), (3, 'u3')])
            c.commit()
        patch = mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1",
            "PIPELINE_X_DISPATCH_CUTOVER_AT": "2026-11-02T11:17:00Z"})
        patch.start()
        self.addCleanup(patch.stop)

    def create(self, run_id=1029, day="2026-11-02", code="x_upstream_error", status="failed_preflight"):
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO x_post_schedule_run(id,slot_key,source_type,run_date,publish_time,config_version,"
                "account_ids_json,expected_count,body_template,status,error_code,error_message,created_at,updated_at,"
                "finished_at,material_pool_snapshot_max_id,random_template_enabled,duration_policy_version) "
                "VALUES(?,?,'material',?,?,9,'[2,3]',2,?,?,?,'original error',?,?,?,4444,1,2)",
                (run_id, 'slot-' + str(run_id), day, '08:%02d' % (run_id % 60), service.DEFAULT_MATERIAL_POST_TEMPLATE, status, code,
                 day + 'T00:00:00Z', day + 'T00:10:00Z', day + 'T00:10:00Z'))
            c.commit()
        return run_id

    def saved(self, ids):
        return recovery.plan(self.path, ids, ["2026-11-02", "2026-11-03"])

    def apply(self, saved):
        return recovery.apply(self.path, saved, saved["plan_sha256"],
            Path(self.temp.name) / "backup", "operator", "explicit backlog recovery")

    def row(self, run_id):
        with contextlib.closing(recovery.connect(self.path)) as c:
            return dict(c.execute("SELECT * FROM x_post_schedule_run WHERE id=?", (run_id,)).fetchone())

    def update(self, sql, values=()):
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute(sql, values)
            c.commit()

    def test_exact_admission_preserves_scope_snapshot_and_unrelated_running_scan(self):
        target = self.create()
        other = self.create(1039, "2026-11-03", "", "claimed")
        before, active = self.row(target), self.row(other)
        saved = self.saved([target])
        result = self.apply(saved)
        self.assertFalse(result["platform_called"])
        self.assertEqual(result["queues_created"], 0)
        self.assertEqual(identity(self.row(target)), identity(before))
        self.assertEqual(self.row(target)["material_pool_snapshot_max_id"], 4444)
        self.assertEqual(self.row(other), active)
        due = self.store.due_schedule_slots(dt.datetime(2026, 11, 3, 18, tzinfo=service.BEIJING_TZ))["items"]
        self.assertIn(before["slot_key"], [r["slot_key"] for r in due])
        recovered = next(r for r in due if r["slot_key"] == before["slot_key"])
        self.assertEqual(recovered["account_ids"], [2, 3])
        self.assertEqual(recovered["run_date"], "2026-11-02")
        with contextlib.closing(recovery.connect(self.path)) as c:
            audit = json.loads(c.execute("SELECT evidence_json FROM x_post_schedule_zero_write_resume_audit").fetchone()[0])
            self.assertEqual(audit["previous_run"], before)
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_queue").fetchone()[0], 0)
        self.assertTrue(Path(result["backup_path"]).is_file())

    def test_unreviewed_old_claim_is_not_admitted_and_absent_table_is_readonly(self):
        self.create(status="claimed", code="")
        with contextlib.closing(recovery.connect(self.path)) as c:
            count = c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            self.assertEqual(authorized_run_ids(c), [])
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], count)
        self.assertEqual(self.store.due_schedule_slots(dt.datetime(2026, 11, 3, 18, tzinfo=service.BEIJING_TZ))["items"], [])

    def test_repeated_apply_after_progress_is_noop(self):
        run_id = self.create()
        saved = self.saved([run_id])
        self.apply(saved)
        self.update("UPDATE x_post_schedule_run SET status='running',queued_count=1 WHERE id=?", (run_id,))
        self.assertTrue(self.apply(saved)["already_applied"])

    def test_shared_service_failures_supported(self):
        for run_id, code in ((1031, "x_accounts_unavailable"), (1036, "x_post_source_query_failed")):
            self.create(run_id, "2026-11-03", code)
        self.assertEqual(len(self.saved([1031, 1036])["items"]), 2)

    def test_new_explicit_day_and_known_reservation_fifo_rejection_preserve_history(self):
        run_id = self.create(1054, "2026-11-04", "x_post_pool_fifo_conflict")
        self.update("UPDATE x_post_schedule_run SET error_message=?,plan_attempted_at=? WHERE id=?",
            ("素材计划必须使用当前素材池最新的可用记录", "2026-11-04T00:09:59Z", run_id))
        previous = self.row(run_id)
        proof = dict(kind="manual_reservation_fifo", commit="a" * 40,
            runner_path="/runtime/scripts/x_post_schedule_runner.py", repository="/source", files={})
        with mock.patch.object(recovery, "verify_reservation_repair", return_value=proof):
            saved = recovery.plan(self.path, [run_id], ["2026-11-04"], reservation_fifo_commit="a" * 40)
            result = self.apply(saved)
            self.assertTrue(result["applied"])
            self.assertTrue(self.apply(saved)["already_applied"])
        current = self.row(run_id)
        self.assertEqual(identity(current), identity(previous))
        self.assertEqual(current["status"], "claimed")
        self.assertEqual(current["plan_attempted_at"], "")
        with contextlib.closing(recovery.connect(self.path)) as c:
            evidence = json.loads(c.execute("SELECT evidence_json FROM x_post_schedule_zero_write_resume_audit WHERE run_id=?", (run_id,)).fetchone()[0])
            self.assertEqual(evidence["previous_run"], previous)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0], 0)

    def test_reservation_fix_does_not_rearm_unknown_transaction_failure(self):
        run_id = self.create(1054, "2026-11-04", "x_post_pool_fifo_conflict")
        self.update("UPDATE x_post_schedule_run SET plan_attempted_at=? WHERE id=?",
            ("2026-11-04T00:09:59Z", run_id))
        with mock.patch.object(recovery, "verify_reservation_repair", return_value={"kind": "manual_reservation_fifo"}):
            with self.assertRaisesRegex(recovery.RecoveryRejected, "known_rejection_evidence_mismatch"):
                recovery.plan(self.path, [run_id], ["2026-11-04"], reservation_fifo_commit="a" * 40)

    def test_existing_queue_blocks_recovery_even_when_summary_counters_are_zero(self):
        run_id = self.create()
        payload = base_candidate(2, "example", 501, "content-1")
        payload.update(idempotency_key="existing-queue", schedule_run_id=run_id,
            material_key="501", run_date="2026-11-02", created_at="2026-11-02T00:00:00Z",
            updated_at="2026-11-02T00:00:00Z", random_template_enabled=1, duration_policy_version=2)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO x_post_queue(" + ",".join(payload) + ") VALUES(" +
                ",".join("?" for _ in payload) + ")", list(payload.values()))
            c.commit()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "queue_exists"):
            self.saved([run_id])

    def test_unknown_or_plan_write_attempt_or_active_scan_is_rejected(self):
        run_id = self.create()
        for column, value in (("unknown_count", 1), ("plan_attempted_at", "2026-11-02T00:01:00Z"),
                              ("started_at", "2026-11-02T00:01:00Z"), ("status", "claimed")):
            before = self.row(run_id)[column]
            self.update("UPDATE x_post_schedule_run SET " + column + "=? WHERE id=?", (value, run_id))
            with self.assertRaisesRegex(recovery.RecoveryRejected, "not_zero_write"):
                self.saved([run_id])
            self.update("UPDATE x_post_schedule_run SET " + column + "=? WHERE id=?", (before, run_id))

    def test_explicit_id_date_and_error_scope(self):
        run_id = self.create()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "exact_unique"):
            self.saved([run_id, run_id])
        with self.assertRaisesRegex(recovery.RecoveryRejected, "outside_authorized_scope"):
            recovery.plan(self.path, [run_id], ["2026-11-01"])
        self.update("UPDATE x_post_schedule_run SET error_code='x_publish_unknown' WHERE id=?", (run_id,))
        with self.assertRaisesRegex(recovery.RecoveryRejected, "not_zero_write"):
            self.saved([run_id])

    def test_known_unavailable_account_is_preserved_and_all_unavailable_rejected(self):
        run_id = self.create()
        self.update("UPDATE x_authorized_account SET status='error' WHERE id=3")
        saved = self.saved([run_id])
        self.assertEqual(saved["items"][0]["currently_unavailable_account_ids"], [3])
        self.apply(saved)
        self.assertEqual(self.row(run_id)["account_ids_json"], "[2,3]")
        other = self.create(1031, "2026-11-03")
        self.update("UPDATE x_authorized_account SET status='error' WHERE id=2")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "all_accounts_unavailable"):
            self.saved([other])

    def test_account_and_frozen_row_drift_rejected(self):
        run_id = self.create()
        saved = self.saved([run_id])
        self.update("UPDATE x_authorized_account SET drama_language='ja' WHERE id=2")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "state_drift"):
            self.apply(saved)
        self.update("UPDATE x_authorized_account SET drama_language='en' WHERE id=2")
        self.update("UPDATE x_post_schedule_run SET material_pool_snapshot_max_id=4445 WHERE id=?", (run_id,))
        with self.assertRaisesRegex(recovery.RecoveryRejected, "state_drift"):
            self.apply(saved)

    def test_drift_during_backup_is_rejected_in_transaction(self):
        run_id = self.create()
        saved = self.saved([run_id])
        real_connect = recovery.connect
        def racing_connect(path, readonly=True):
            if not readonly:
                self.update("UPDATE x_post_schedule_run SET body_template='changed' WHERE id=?", (run_id,))
            return real_connect(path, readonly)
        with mock.patch.object(recovery, "connect", side_effect=racing_connect):
            with self.assertRaisesRegex(recovery.RecoveryRejected, "state_drift"):
                self.apply(saved)
        self.assertEqual(self.row(run_id)["status"], "failed_preflight")

    def test_identity_drift_revokes_cutover_exception(self):
        run_id = self.create()
        self.apply(self.saved([run_id]))
        self.update("UPDATE x_post_schedule_run SET account_ids_json='[3,2]' WHERE id=?", (run_id,))
        with contextlib.closing(recovery.connect(self.path)) as c:
            self.assertEqual(authorized_run_ids(c), [])

    def known_rejection_fixture(self):
        self.create(1039, "2026-11-03", "x_post_pool_fifo_conflict")
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.executemany("INSERT INTO x_authorized_account VALUES(?,?,'active',1,'en','')",
                [(i, 'u%s' % i) for i in range(4, 91)])
            c.execute("UPDATE x_post_schedule_run SET slot_key='xpost:schedule:v1:material:2026-11-03:1651',"
                "publish_time='16:51',config_version=33,expected_count=89,account_ids_json=?,error_message=?,"
                "plan_attempted_at='2026-11-03T10:32:40Z',finished_at='2026-11-03T10:32:40Z' WHERE id=1039",
                (json.dumps(list(range(2, 91))), "素材语言尚未达到冻结批次容量"))
            c.commit()
        repo = Path(self.temp.name) / "repair-repo"
        (repo / "scripts").mkdir(parents=True)
        runner = repo / "scripts/x_post_schedule_runner.py"
        runner.write_bytes((Path(__file__).parent / "x_post_schedule_runner.py").read_bytes())
        for arguments in (("init", "-q"), ("config", "core.autocrlf", "false"), ("add", "."),
                          ("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "capacity fix")):
            subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        return {"known_rejection_commit": commit, "runner_path": str(runner), "repository": str(repo)}

    def known_plan(self, evidence):
        return recovery.plan(self.path, [1039], ["2026-11-03"], **evidence)

    def test_known_rejection_preserves_complete_attempt_audit_and_is_idempotent(self):
        evidence = self.known_rejection_fixture()
        before = self.row(1039)
        with self.assertRaisesRegex(recovery.RecoveryRejected, "not_zero_write"):
            self.saved([1039])
        saved = self.known_plan(evidence)
        result = self.apply(saved)
        self.assertFalse(result["platform_called"])
        after = self.row(1039)
        self.assertEqual(after["plan_attempted_at"], "")
        self.assertEqual(after["status"], "claimed")
        self.assertEqual(identity(after), identity(before))
        self.assertEqual(after["material_pool_snapshot_max_id"], 4444)
        with contextlib.closing(recovery.connect(self.path)) as c:
            audit = json.loads(c.execute("SELECT evidence_json FROM x_post_schedule_zero_write_resume_audit WHERE run_id=1039").fetchone()[0])
        self.assertEqual(audit["previous_run"], before)
        self.assertEqual(audit["repair_evidence"]["commit"], evidence["known_rejection_commit"])
        self.update("UPDATE x_post_schedule_run SET status='running',plan_attempted_at='new attempt' WHERE id=1039")
        self.assertTrue(self.apply(saved)["already_applied"])
        self.assertEqual(self.row(1039)["plan_attempted_at"], "new attempt")

    def test_known_rejection_interrupt_rolls_back_audit_and_fence_together(self):
        evidence = self.known_rejection_fixture()
        saved = self.known_plan(evidence)
        self.update("CREATE TRIGGER interrupt_recovery BEFORE UPDATE ON x_post_schedule_run "
            "WHEN NEW.status='claimed' BEGIN SELECT RAISE(ABORT,'simulated interruption'); END")
        before = self.row(1039)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated interruption"):
            self.apply(saved)
        self.assertEqual(self.row(1039), before)
        with contextlib.closing(recovery.connect(self.path)) as c:
            self.assertIsNone(recovery.existing(c, 1039))
        self.update("DROP TRIGGER interrupt_recovery")
        self.assertTrue(self.apply(saved)["applied"])

    def test_known_rejection_never_allows_unknown_or_unrecognized_attempt(self):
        evidence = self.known_rejection_fixture()
        self.update("UPDATE x_post_schedule_run SET unknown_count=1 WHERE id=1039")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "not_zero_write"):
            self.known_plan(evidence)
        self.update("UPDATE x_post_schedule_run SET unknown_count=0,error_message='unknown response' WHERE id=1039")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "evidence_mismatch"):
            self.known_plan(evidence)

    def test_known_rejection_queue_and_deployed_code_drift_are_rejected(self):
        evidence = self.known_rejection_fixture()
        saved = self.known_plan(evidence)
        runner = Path(evidence["runner_path"])
        old = runner.read_bytes()
        runner.write_bytes(old + b'\n# drift\n')
        with self.assertRaisesRegex(recovery.RecoveryRejected, "deployed_runner"):
            self.apply(saved)
        runner.write_bytes(old)
        payload = base_candidate(2, "example", 501, "content-1")
        payload.update(idempotency_key="existing-known-queue", schedule_run_id=1039,
            material_key="501", run_date="2026-11-03", created_at="2026-11-03T10:32:40Z",
            updated_at="2026-11-03T10:32:40Z", random_template_enabled=1, duration_policy_version=2)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO x_post_queue(" + ",".join(payload) + ") VALUES(" +
                ",".join("?" for _ in payload) + ")", list(payload.values()))
            c.commit()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "queue_exists"):
            self.known_plan(evidence)


if __name__ == "__main__":
    unittest.main()
