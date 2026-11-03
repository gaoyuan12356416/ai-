"""Exact second recovery preserves the first audit and all episode occupancy."""
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
from features.pipeline_dispatch.x_schedule_recovery import SCHEMA, fingerprint, identity
from scripts import recover_x_drama_episode_selection as recovery
from scripts.test_x_post_multi_schedule_store import base_candidate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "posts.sqlite3"
        self.store = service.XPostStore(self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            c.execute(SCHEMA)
            for run_id, (day, minute, attempted, finished) in recovery.EXPECTED.items():
                c.execute("INSERT INTO x_post_schedule_run(id,slot_key,source_type,run_date,publish_time,config_version,"
                    "account_ids_json,expected_count,body_template,status,error_code,error_message,created_at,updated_at,"
                    "finished_at,plan_attempted_at,random_template_enabled,duration_policy_version) "
                    "VALUES(?,?,'drama',?,?,27,?,35,?,'failed_preflight','x_post_drama_episode_already_used',?,?,?,?,?,1,2)",
                    (run_id, 'xpost:schedule:v1:drama:' + day + ':' + minute.replace(':', ''), day, minute,
                     json.dumps(recovery.ACCOUNTS), service.DEFAULT_DRAMA_POST_TEMPLATE,
                     '短剧集数已被其他发布队列占用', day + 'T00:00:00Z', finished, finished, attempted))
                row = dict(c.execute("SELECT * FROM x_post_schedule_run WHERE id=?", (run_id,)).fetchone())
                c.execute("INSERT INTO x_post_schedule_zero_write_resume_audit VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, fingerprint(identity(row)), 'first-plan', 'operator', 'first recovery',
                     '2026-11-03T10:48:00Z', 'original-backup', '{"first":true}'))
            c.commit()
        self.repo = Path(self.temp.name) / "repair-repo"
        (self.repo / "features/x_posts").mkdir(parents=True)
        self.deployed = self.repo / "features/x_posts/service.py"
        self.deployed.write_bytes(Path(service.__file__).read_bytes())
        for args in (("init", "-q"), ("config", "core.autocrlf", "false"), ("add", "."),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "episode filter")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def rows(self, table):
        with contextlib.closing(recovery.connect(self.path)) as c:
            return recovery.rows(c, "SELECT * FROM " + table + " ORDER BY " + ("run_id" if table.endswith("audit") else "id"))

    def plan(self):
        return recovery.plan(self.path, [1029, 1031], self.commit, self.deployed, self.repo)

    def apply(self, saved):
        return recovery.apply(self.path, saved, saved["plan_sha256"], Path(self.temp.name) / "backups",
            "operator", "recover exact known episode selection rejection")

    def update(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute(sql, params)
            c.commit()

    def test_apply_preserves_first_audit_frozen_rows_and_admits_original_slot(self):
        before = self.rows("x_post_schedule_run")
        first_audits = self.rows("x_post_schedule_zero_write_resume_audit")
        result = self.apply(self.plan())
        self.assertFalse(result["platform_called"])
        self.assertEqual(self.rows("x_post_schedule_zero_write_resume_audit"), first_audits)
        after = self.rows("x_post_schedule_run")
        self.assertEqual([identity(row) for row in after], [identity(row) for row in before])
        self.assertTrue(all(row["status"] == "claimed" and row["plan_attempted_at"] == "" for row in after))
        audit = self.rows(recovery.TABLE)
        self.assertEqual([json.loads(row["evidence_json"])["previous_run"] for row in audit], before)
        self.assertEqual(self.rows("x_post_queue"), [])
        with mock.patch.dict(os.environ, {"PIPELINE_X_DISPATCH_ENABLED": "1",
                "PIPELINE_X_DISPATCH_CUTOVER_AT": "2026-11-02T11:17:00Z"}):
            due = self.store.due_schedule_slots(dt.datetime(2026, 11, 3, 19, tzinfo=service.BEIJING_TZ))["items"]
        self.assertEqual({row["slot_key"] for row in due}, {row["slot_key"] for row in before})
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
            self.update("DELETE FROM " + recovery.TABLE + " WHERE run_id=1029")

    def test_repeated_apply_does_not_reset_later_attempt(self):
        saved = self.plan()
        self.apply(saved)
        self.update("UPDATE x_post_schedule_run SET status='running',plan_attempted_at='later-attempt' WHERE id=1029")
        self.assertTrue(self.apply(saved)["already_applied"])
        self.assertEqual(self.rows("x_post_schedule_run")[0]["plan_attempted_at"], "later-attempt")

    def test_deployed_service_drift_rejected(self):
        saved = self.plan()
        self.deployed.write_bytes(self.deployed.read_bytes() + b'\n# drift\n')
        with self.assertRaisesRegex(recovery.RecoveryRejected, "deployed_service"):
            self.apply(saved)
        self.assertTrue(all(row["status"] == "failed_preflight" for row in self.rows("x_post_schedule_run")))

    def test_unknown_and_wrong_failure_time_rejected(self):
        self.update("UPDATE x_post_schedule_run SET unknown_count=1 WHERE id=1029")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "not_exact_zero_write"):
            self.plan()
        self.update("UPDATE x_post_schedule_run SET unknown_count=0,plan_attempted_at='different-attempt' WHERE id=1029")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "not_exact_zero_write"):
            self.plan()

    def test_residual_queue_is_rejected_even_with_zero_counters(self):
        pool = self.store.add_drama_pool_items(["content-1"], [{"content_id": "content-1",
            "drama_name": "Drama", "description": "A complete episode description.", "language": "en",
            "labels": "", "name_tag": "#Drama", "free_episode_count": 1}],
            actor={"user_id": "test", "name": "Test"})["items"][0]
        payload = base_candidate(2, "example", 501, "content-1")
        payload.update(idempotency_key="residual", schedule_run_id=1029, source_type="drama",
            episode_key="content-1:1", episode_number=1, run_date="2026-11-02",
            drama_pool_item_id=pool["id"], drama_pool_created_at=pool["created_at"], drama_replay_generation=1,
            created_at="2026-11-03T10:53:01Z", updated_at="2026-11-03T10:53:01Z",
            random_template_enabled=1, duration_policy_version=2)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO x_post_queue(" + ",".join(payload) + ") VALUES(" +
                ",".join("?" for _ in payload) + ")", list(payload.values()))
            c.commit()
        with self.assertRaisesRegex(recovery.RecoveryRejected, "queue_exists"):
            self.plan()

    def test_second_row_failure_rolls_back_both_rows_and_new_audits(self):
        saved = self.plan()
        before, audits = self.rows("x_post_schedule_run"), self.rows("x_post_schedule_zero_write_resume_audit")
        self.update("CREATE TRIGGER interrupt_retry BEFORE UPDATE ON x_post_schedule_run "
            "WHEN NEW.id=1031 AND NEW.status='claimed' BEGIN SELECT RAISE(ABORT,'interrupt'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "interrupt"):
            self.apply(saved)
        self.assertEqual(self.rows("x_post_schedule_run"), before)
        self.assertEqual(self.rows("x_post_schedule_zero_write_resume_audit"), audits)
        with contextlib.closing(recovery.connect(self.path)) as c:
            self.assertIsNone(recovery.existing(c, 1029))
            self.assertIsNone(recovery.existing(c, 1031))
        self.update("DROP TRIGGER interrupt_retry")
        self.assertTrue(self.apply(saved)["applied"])

    def test_first_audit_or_original_account_scope_drift_rejected(self):
        saved = self.plan()
        self.update("UPDATE x_post_schedule_zero_write_resume_audit SET reason='changed' WHERE run_id=1029")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "plan_state_drift"):
            self.apply(saved)
        self.update("UPDATE x_post_schedule_run SET account_ids_json='[2]' WHERE id=1029")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "frozen_identity"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
