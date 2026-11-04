import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import recover_x_run446 as recovery


@contextlib.contextmanager
def database(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


class Run446CloseTests(unittest.TestCase):
    def setUp(self):
        recovery.use_incident(5393)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {name: str(self.root / (name + ".sqlite3")) for name in ("native", "auto", "central")}
        self.now = dt.datetime(2026, 11, 4, 6, tzinfo=dt.timezone.utc)
        self.old = "2026-11-02T20:11:52Z"
        self._fixtures()

    def table(self, conn, name, columns, values=()):
        conn.execute("CREATE TABLE " + name + " (" + columns + ")")
        for row in values:
            names = list(row)
            conn.execute("INSERT INTO " + name + " (" + ",".join(names) + ") VALUES (" + ",".join("?" for _ in names) + ")", [row[k] for k in names])

    def _fixtures(self):
        ops = [{"operation_id": "op" + str(i), "generation": 1,
                "operation": "x:POST:/2/media/upload/initialize" if i == 0 else "x:POST:/2/media/upload/123/append",
                "request_sha256": str(i) * 64} for i in range(6)]
        with database(self.paths["native"]) as c:
            self.table(c, "x_post_queue", "id INTEGER,account_id INTEGER,manual_run_id INTEGER,status TEXT,delivery_mode TEXT",
                       [{"id": 5393, "account_id": 46, "manual_run_id": 1418, "status": "failed", "delivery_mode": "direct"}])
            self.table(c, "x_post_publish_log", "id INTEGER,queue_id INTEGER,account_id INTEGER,status TEXT,unknown_outcome INTEGER,attempt_count INTEGER,error_code TEXT,error_message TEXT,x_media_id TEXT,x_post_id TEXT,x_post_url TEXT,published_at TEXT,updated_at TEXT",
                       [{"id": 5327, "queue_id": 5393, "account_id": 46, "status": "failed", "unknown_outcome": 1, "attempt_count": 1,
                         "error_code": recovery.ERROR, "error_message": recovery.ERROR, "x_media_id": "", "x_post_id": "", "x_post_url": "", "published_at": "", "updated_at": self.old}])
            self.table(c, "x_post_manual_run", "id INTEGER,trigger_source TEXT,status TEXT,expected_count INTEGER,queued_count INTEGER,published_count INTEGER,failed_count INTEGER,unknown_count INTEGER,finished_at TEXT,updated_at TEXT",
                       [{"id": 1418, "trigger_source": "auto_template", "status": "needs_review", "expected_count": 1, "queued_count": 1,
                         "published_count": 0, "failed_count": 0, "unknown_count": 1, "finished_at": self.old, "updated_at": self.old}])
            self.table(c, "pipeline_x_operation", "operation_id TEXT,generation INTEGER,operation TEXT,request_sha256 TEXT,source_key TEXT",
                       [dict(o, source_key=recovery.KEY) for o in ops[:5]])
            self.table(c, "pipeline_x_checkpoint", "source_key TEXT,generation INTEGER,phase TEXT",
                       [{"source_key": recovery.KEY, "generation": 1, "phase": "x_media_initialized"}])
            self.table(c, "pipeline_x_claim", "source_key TEXT,generation INTEGER,stage TEXT,claim_hash TEXT,queue_id INTEGER,log_id INTEGER",
                       [{"source_key": recovery.KEY, "generation": 1, "stage": "publish", "claim_hash": "c" * 64, "queue_id": 5393, "log_id": 5327}])
            self.table(c, "pipeline_canonical_hold", "source_key TEXT,reason TEXT")
            self.table(c, "x_post_repost_ledger", "queue_id INTEGER")
        with database(self.paths["auto"]) as c:
            self.table(c, "x_auto_task", "id INTEGER,run_id INTEGER,account_id TEXT,status TEXT,execution_run_id INTEGER,execution_queue_id INTEGER,execution_log_id INTEGER,material_id TEXT,publish_id TEXT,unknown_outcome INTEGER",
                       [{"id": tid, "run_id": 446, "account_id": str(aid), "status": "failed", "execution_run_id": 1657 if tid == 2712 else None,
                         "execution_queue_id": None, "execution_log_id": None, "material_id": "", "publish_id": "", "unknown_outcome": 0}
                        for tid, aid in ((2712, 46), (2731, 49), (2780, 71))])
        with database(self.paths["central"]) as c:
            jobs = []
            for jid, stage, status in ((31501, "prepare", "completed"), (31602, "publish", "unknown")):
                jobs.append({"id": jid, "source_key": recovery.KEY, "source_id": "5393", "channel": "x", "family": "queue",
                             "stage": stage, "status": status, "generation": 1, "node_id": "cpu-main", "worker_id": "old-worker",
                             "claim_hash": "c" * 64, "external_intent": 1, "canonical_receipt": "", "hold_reason": "", "error_code": recovery.CENTRAL_ERROR,
                             "lease_expires_at": self.old, "source_meta_json": '{"unknown_outcome":true}', "source_status": "failed",
                             "finished_at": "", "updated_at": self.old})
            self.table(c, "pipeline_job", "id INTEGER,source_key TEXT,source_id TEXT,channel TEXT,family TEXT,stage TEXT,status TEXT,generation INTEGER,node_id TEXT,worker_id TEXT,claim_hash TEXT,external_intent INTEGER,canonical_receipt TEXT,hold_reason TEXT,error_code TEXT,lease_expires_at TEXT,source_meta_json TEXT,source_status TEXT,finished_at TEXT,updated_at TEXT", jobs)
            self.table(c, "pipeline_node", "node_id TEXT", [{"node_id": n} for n in ("cpu-main", "hk-gpu")])
            self.table(c, "pipeline_operation", "operation_id TEXT,generation INTEGER,operation TEXT,request_sha256 TEXT,job_id INTEGER",
                       [dict(o, job_id=31602) for o in ops])
            self.table(c, "pipeline_checkpoint", "job_id INTEGER,generation INTEGER,phase TEXT",
                       [{"job_id": 31602, "generation": 1, "phase": "x_media_initialized"}])
            self.table(c, "pipeline_claim_history", "job_id INTEGER,generation INTEGER,claim_hash TEXT,node_id TEXT,worker_id TEXT", [{"job_id": 31602, "generation": 1, "claim_hash": "c" * 64, "node_id": "cpu-main", "worker_id": "old-worker"}])
            self.table(c, "pipeline_receipt", "receipt_id TEXT,job_id INTEGER")
            self.table(c, "pipeline_delivery", "delivery_id TEXT,job_id INTEGER,generation INTEGER,claim_hash TEXT,request_sha256 TEXT,response_json TEXT",
                       [{"delivery_id": "failure", "job_id": 31602, "generation": 1, "claim_hash": "c" * 64, "request_sha256": "d" * 64,
                         "response_json": '{"accepted":true,"status":"unknown"}'}])
            self.table(c, "pipeline_event", "id INTEGER PRIMARY KEY,job_id INTEGER,event_type TEXT,details_json TEXT,created_at TEXT",
                       [{"id": i + 1, "job_id": 31602, "event_type": "failed", "details_json": json.dumps({"unknown": True, "code": code}), "created_at": self.old}
                        for i, code in enumerate((recovery.ERROR, recovery.ERROR, recovery.CENTRAL_ERROR))])
            self.table(c, "pipeline_resource", "resource_key TEXT,job_id INTEGER,generation INTEGER,quarantined INTEGER,legacy INTEGER",
                       [{"resource_key": "x:account:46", "job_id": 31602, "generation": 1, "quarantined": 1, "legacy": 0},
                        {"resource_key": "x:account:49", "job_id": 99999, "generation": 1, "quarantined": 1, "legacy": 0}])
            self.table(c, "pipeline_legacy_resource", "resource_key TEXT,job_id INTEGER")
            self.table(c, "pipeline_publication", "source_key TEXT")
        self.evidence = {"kind": "x_run446_media_only_evidence", "queue_id": 5393, "worker_stopped": True,
                         "historical_permit_guard_verified": True, "worker_proof": {"pid_absent": True},
                         "historical_code_proof": {"sha256": "e" * 64}, "nodes": []}
        for name in ("cpu-main", "hk-gpu"):
            receipts = []
            if name == "cpu-main":
                receipts.append({"id": "failure", "route": "jobs/fail", "acked_at": self.old, "payload_sha256": "d" * 64,
                                 "payload": {"job_id": 31602, "generation": 1, "worker_id": "old-worker", "unknown_outcome": True,
                                             "error_code": recovery.ERROR, "claim_token_sha256": "c" * 64}})
            self.evidence["nodes"].append({"node_id": name, "checked_at": self.now.isoformat(), "complete_scope": True, "receipts": receipts})

    def plan(self):
        return recovery.make_plan(**self.paths, evidence=self.evidence, now=self.now)

    def apply(self, plan):
        return recovery.apply_plan(plan, self.root / "backups", "operator", now=self.now)

    def test_closes_only_old_uncertainty_preserves_attempts_tasks_and_other_resource(self):
        auto_before = Path(self.paths["auto"]).read_bytes()
        plan = self.plan()
        result = self.apply(plan)
        self.assertFalse(result["old_queue_retried"])
        self.assertFalse(result["task_modified"])
        self.assertEqual(Path(self.paths["auto"]).read_bytes(), auto_before)
        with database(self.paths["native"]) as c:
            self.assertEqual(c.execute("SELECT status,attempt_count,unknown_outcome,error_code FROM x_post_publish_log").fetchone(), ("failed", 1, 0, recovery.ERROR))
            self.assertEqual(c.execute("SELECT status FROM x_post_queue").fetchone(), ("failed",))
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_x_operation").fetchone()[0], 5)
            self.assertEqual(c.execute("SELECT status,failed_count,unknown_count FROM x_post_manual_run").fetchone(), ("completed_with_errors", 1, 0))
        with database(self.paths["central"]) as c:
            self.assertEqual(c.execute("SELECT status,generation,external_intent FROM pipeline_job WHERE id=31602").fetchone(), ("failed", 1, 1))
            self.assertEqual(c.execute("SELECT resource_key FROM pipeline_resource").fetchall(), [("x:account:49",)])
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_operation").fetchone()[0], 6)
        self.assertTrue(self.apply(plan)["already_applied"])

    def test_rejects_central_only_post_intent(self):
        with database(self.paths["central"]) as c:
            c.execute("UPDATE pipeline_operation SET operation='x:POST:/2/tweets' WHERE operation_id='op5'")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "post_finalize"):
            self.plan()

    def test_rejects_pending_or_late_receipt(self):
        self.evidence["nodes"][0]["receipts"][0]["acked_at"] = None
        with self.assertRaisesRegex(recovery.RecoveryRejected, "pending"):
            self.plan()
        self.evidence["nodes"][0]["receipts"][0]["acked_at"] = self.old
        self.evidence["nodes"][0]["receipts"][0]["payload"]["phase"] = "x_media_uploaded"
        with self.assertRaisesRegex(recovery.RecoveryRejected, "later_or_success"):
            self.plan()

    def test_rejects_material_or_post_state_drift_without_writes(self):
        plan = self.plan()
        with database(self.paths["native"]) as c:
            c.execute("UPDATE x_post_publish_log SET x_post_id='1234'")
        with self.assertRaisesRegex(recovery.RecoveryRejected, "post_evidence"):
            self.apply(plan)
        with database(self.paths["central"]) as c:
            self.assertEqual(c.execute("SELECT status FROM pipeline_job WHERE id=31602").fetchone()[0], "unknown")
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_resource WHERE job_id=31602").fetchone()[0], 1)

    def test_rolls_back_all_changes_if_central_update_fails(self):
        plan = self.plan()
        with database(self.paths["central"]) as c:
            c.execute("CREATE TRIGGER stop_close BEFORE UPDATE OF status ON pipeline_job BEGIN SELECT RAISE(ABORT,'injected_failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected_failure"):
            self.apply(plan)
        with database(self.paths["native"]) as c:
            self.assertEqual(c.execute("SELECT unknown_outcome FROM x_post_publish_log").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT status FROM x_post_manual_run").fetchone()[0], "needs_review")
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (recovery.AUDIT,)).fetchone())

    def test_rejects_changed_evidence_hash(self):
        plan = self.plan()
        plan["evidence"]["worker_proof"]["pid_absent"] = False
        with self.assertRaisesRegex(recovery.RecoveryRejected, "hash_changed"):
            self.apply(plan)

    def test_second_fixed_incident_closes_191_media_operations_without_replay(self):
        with database(self.paths["native"]) as c:
            c.execute("UPDATE x_post_queue SET id=5857,account_id=120,manual_run_id=1812")
            c.execute("UPDATE x_post_publish_log SET id=5777,queue_id=5857,account_id=120,error_code='coordinator_operation_failed',error_message='coordinator_operation_failed'")
            c.execute("UPDATE x_post_manual_run SET id=1812")
            c.execute("UPDATE pipeline_x_claim SET queue_id=5857,log_id=5777")
            for table in ("pipeline_x_operation", "pipeline_x_checkpoint", "pipeline_x_claim"):
                c.execute("UPDATE " + table + " SET source_key='x:queue:5857'")
            for i in range(5, 191):
                c.execute("INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?)",
                          ("op" + str(i), 1, "x:POST:/2/media/upload/123/append", str(i) * 64, "x:queue:5857"))
        with database(self.paths["central"]) as c:
            for table in ("pipeline_operation", "pipeline_checkpoint", "pipeline_claim_history", "pipeline_delivery", "pipeline_resource"):
                c.execute("UPDATE " + table + " SET job_id=32801 WHERE job_id=31602")
            c.execute("UPDATE pipeline_job SET id=32799 WHERE stage='prepare'")
            c.execute("UPDATE pipeline_job SET id=32801 WHERE stage='publish'")
            c.execute("UPDATE pipeline_job SET source_key='x:queue:5857',source_id='5857',error_code='coordinator_operation_failed'")
            c.execute("UPDATE pipeline_resource SET resource_key='x:account:120' WHERE job_id=32801")
            c.execute("DELETE FROM pipeline_event")
            c.execute("INSERT INTO pipeline_event VALUES(1,32801,'failed',?,?)", (json.dumps({"unknown": True, "code": "coordinator_operation_failed"}), self.old))
            for i in range(6, 191):
                c.execute("INSERT INTO pipeline_operation VALUES(?,?,?,?,?)", ("op" + str(i), 1, "x:POST:/2/media/upload/123/append", str(i) * 64, 32801))
        evidence = copy_evidence = json.loads(json.dumps(self.evidence))
        evidence["queue_id"] = 5857
        evidence["worker_stopped"] = False
        evidence["worker_execution_stopped"] = True
        payload = copy_evidence["nodes"][0]["receipts"][0]["payload"]
        payload.update(job_id=32801, error_code="coordinator_operation_failed")
        plan = recovery.make_plan(**self.paths, evidence=evidence, now=self.now, queue_id=5857)
        result = self.apply(plan)
        self.assertEqual(result["queue_id"], 5857)
        with database(self.paths["native"]) as c:
            self.assertEqual(c.execute("SELECT status,attempt_count,unknown_outcome FROM x_post_publish_log").fetchone(), ("failed", 1, 0))
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_x_operation").fetchone()[0], 191)


if __name__ == "__main__":
    unittest.main()
