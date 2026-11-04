"""Exact original queues are rearmed once, with no native or Auto writes."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import recover_x_run446_prepares as r


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = self.root / "central.sqlite3"
        self.n = self.root / "native.sqlite3"
        self.a = self.root / "auto.sqlite3"
        self.clock = dt.datetime(2026, 11, 4, 8, tzinfo=dt.timezone.utc)
        with contextlib.closing(sqlite3.connect(self.c)) as c, c:
            c.executescript("""
              CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,source_id TEXT,channel TEXT,family TEXT,stage TEXT,status TEXT,error_code TEXT,generation INTEGER,external_intent INTEGER,hold_reason TEXT,canonical_receipt TEXT,finished_at TEXT,lease_expires_at TEXT,artifact_json TEXT,claim_hash TEXT,node_id TEXT,worker_id TEXT,started_at TEXT,updated_at TEXT,source_status TEXT);
              CREATE TABLE pipeline_publication(source_key TEXT,job_id INTEGER);
              CREATE TABLE pipeline_delivery(job_id INTEGER,response_json TEXT);
              CREATE TABLE pipeline_claim_history(job_id INTEGER,generation INTEGER,claim_hash TEXT);
              CREATE TABLE pipeline_event(id INTEGER PRIMARY KEY,job_id INTEGER,event_type TEXT,details_json TEXT,created_at TEXT);
            """)
            for table in ("pipeline_operation", "pipeline_checkpoint", "pipeline_receipt", "pipeline_resource", "pipeline_legacy_resource"):
                c.execute("CREATE TABLE " + table + "(job_id INTEGER)")
        with contextlib.closing(sqlite3.connect(self.n)) as n, n:
            n.executescript("""
              CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,account_id INTEGER,manual_run_id INTEGER,status TEXT,material_id TEXT,body_template TEXT);
              CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,status TEXT,attempt_count INTEGER,unknown_outcome INTEGER,x_media_id TEXT,x_post_id TEXT,started_at TEXT,published_at TEXT);
              CREATE TABLE x_post_repost_ledger(queue_id INTEGER);
              CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,status TEXT,publish_approved INTEGER,last_verified_at TEXT);
              CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,trigger_source TEXT,external_task_key TEXT,template_ref TEXT,template_version INTEGER);
            """)
            for table in ("pipeline_x_claim", "pipeline_x_operation", "pipeline_x_checkpoint", "pipeline_canonical_hold"):
                n.execute("CREATE TABLE " + table + "(source_key TEXT)")
        with contextlib.closing(sqlite3.connect(self.a)) as a, a:
            a.execute("CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,template_id INTEGER,template_version INTEGER,account_id TEXT,material_id TEXT,execution_run_id INTEGER,execution_queue_id INTEGER,status TEXT,unknown_outcome INTEGER,publish_id TEXT)")
        for job, queue, task, account, run in r.SCOPE:
            self.change(self.c, "INSERT INTO pipeline_job VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job, "x:queue:" + str(queue), str(queue), "x", "queue", "prepare", "failed", "x_upstream_error", 1, 0, "", "", "2026-11-04T02:00:00Z", "2026-11-04T02:02:00Z", "{}", "hash", "hk-gpu", "worker", "2026-11-04T01:59:59Z", "old", "queued"))
            self.change(self.c, "INSERT INTO pipeline_delivery VALUES(?,?)", (job, '{"accepted":true,"duplicate":true,"status":"failed"}'))
            self.change(self.c, "INSERT INTO pipeline_claim_history VALUES(?,1,'hash')", (job,))
            self.change(self.c, "INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,'failed',?,'old')", (job, '{"code":"x_upstream_error","unknown":false}'))
            self.change(self.n, "INSERT INTO x_post_queue VALUES(?,?,?,'queued',?,'frozen body')", (queue, account, run, str(queue + 100)))
            self.change(self.n, "INSERT INTO x_post_publish_log VALUES(?,?,'reserved',0,0,'','','','')", (queue + 1, queue))
            self.change(self.n, "INSERT INTO x_authorized_account VALUES(?,'active',1,'2026-11-04T06:00:00Z')", (account,))
            self.change(self.n, "INSERT INTO x_post_manual_run VALUES(?,'auto_template',?,'x-auto-template-4',6)", (run, "x-auto-task-" + str(task)))
            self.change(self.a, "INSERT INTO x_auto_task VALUES(?,446,4,6,?,?,?,?,'retry_wait',0,'')", (task, str(account), str(queue + 100), run, queue))

    def change(self, path, sql, values=()):
        with contextlib.closing(sqlite3.connect(path)) as c, c:
            c.execute(sql, values)

    def dump(self, path):
        with contextlib.closing(sqlite3.connect(path)) as c:
            return "\n".join(c.iterdump())

    def plan(self):
        return r.make_plan(self.c, self.n, self.a, self.clock)

    def apply(self, plan):
        return r.apply_plan(plan, plan["plan_sha256"], self.root / "backup", "user-authorized-run446", clock=lambda: self.clock)

    def test_plan_readonly_and_apply_preserves_native_auto_and_backup(self):
        before = [self.dump(p) for p in (self.c, self.n, self.a)]
        plan = self.plan()
        self.assertEqual(before, [self.dump(p) for p in (self.c, self.n, self.a)])
        result = self.apply(plan)
        self.assertEqual(self.dump(result["backup_path"]), before[0])
        self.assertEqual([self.dump(p) for p in (self.n, self.a)], before[1:])
        with contextlib.closing(sqlite3.connect(self.c)) as c:
            self.assertEqual(c.execute("SELECT id,status,generation,claim_hash FROM pipeline_job ORDER BY id").fetchall(), [(32665,"queued",1,""),(32724,"queued",1,"")])
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_event WHERE event_type='audited_run446_prepare_recovery'").fetchone()[0], 2)
        with self.assertRaises(r.RecoveryRejected):
            self.apply(plan)

    def test_operation_or_native_claim_rejects_before_mutation(self):
        for path, sql in ((self.c,"INSERT INTO pipeline_operation VALUES(32665)"), (self.n,"INSERT INTO pipeline_x_claim VALUES('x:queue:5819')")):
            with self.subTest(sql=sql):
                self.change(path, sql)
                with self.assertRaises(r.RecoveryRejected): self.plan()
                self.change(path, "DELETE FROM " + sql.split()[2])

    def test_revoked_or_stale_verification_is_not_rearmed(self):
        self.change(self.n, "UPDATE x_authorized_account SET status='revoked' WHERE id=79")
        with self.assertRaisesRegex(r.RecoveryRejected, "account_not_verified"): self.plan()
        self.change(self.n, "UPDATE x_authorized_account SET status='active',last_verified_at='2026-11-03T06:00:00Z' WHERE id=79")
        with self.assertRaisesRegex(r.RecoveryRejected, "account_not_verified"): self.plan()

    def test_native_unknown_or_post_rejects(self):
        for column, value in (("unknown_outcome",1),("attempt_count",1),("x_post_id","123")):
            with self.subTest(column=column):
                self.change(self.n, "UPDATE x_post_publish_log SET " + column + "=? WHERE queue_id=5819", (value,))
                with self.assertRaisesRegex(r.RecoveryRejected, "native_write_evidence"): self.plan()
                self.change(self.n, "UPDATE x_post_publish_log SET " + column + "=? WHERE queue_id=5819", ("" if column=="x_post_id" else 0,))

    def test_frozen_material_drift_fails_apply(self):
        plan = self.plan()
        self.change(self.n, "UPDATE x_post_queue SET body_template='changed' WHERE id=5819")
        with self.assertRaisesRegex(r.RecoveryRejected,"plan_state_drift"): self.apply(plan)
        with contextlib.closing(sqlite3.connect(self.c)) as c:
            self.assertEqual(c.execute("SELECT status FROM pipeline_job WHERE id=32665").fetchone()[0],"failed")

    def test_other_queue_or_task_binding_is_rejected(self):
        self.change(self.a,"UPDATE x_auto_task SET execution_queue_id=9999 WHERE id=2740")
        with self.assertRaisesRegex(r.RecoveryRejected,"auto_identity_changed"): self.plan()

    def test_forged_hash_or_nonfailure_ack_is_rejected(self):
        plan=self.plan(); plan["items"][0]["job_id"]=1
        with self.assertRaisesRegex(r.RecoveryRejected,"plan_hash_mismatch"): self.apply(plan)
        self.change(self.c,"UPDATE pipeline_delivery SET response_json=? WHERE job_id=32665", ('{"accepted":true,"status":"published"}',))
        with self.assertRaisesRegex(r.RecoveryRejected,"failure_ack_missing"): self.plan()


if __name__ == "__main__":
    unittest.main()
