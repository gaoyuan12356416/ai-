"""Scoped settlements preserve historical receipts and unrelated accounts."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from scripts import settle_x_run446_blockers as r


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.paths={k:str(self.root/(k+".sqlite3")) for k in ("auto","native","central")}
        self.clock=dt.datetime(2026,11,4,8,30,tzinfo=dt.timezone.utc)
        self.executes("auto","""
          CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,account_id TEXT,status TEXT,execution_queue_id INTEGER,execution_run_id INTEGER,claim_token TEXT DEFAULT '',claim_worker TEXT DEFAULT '',lease_expires_at_utc TEXT DEFAULT '',publish_id TEXT DEFAULT '',publish_url TEXT DEFAULT '',material_id TEXT DEFAULT '',unknown_outcome INTEGER DEFAULT 0,error_code TEXT DEFAULT '',error_message TEXT DEFAULT '',claim_phase TEXT DEFAULT '',next_attempt_at_utc TEXT DEFAULT '',finished_at_utc TEXT DEFAULT '',updated_at TEXT DEFAULT '');
          CREATE TABLE x_auto_material_ledger(task_id INTEGER,last_task_status TEXT,updated_at TEXT);
          CREATE TABLE x_auto_event(id INTEGER PRIMARY KEY,run_id INTEGER,task_id INTEGER,event_type TEXT,from_status TEXT,to_status TEXT,message TEXT,details_json TEXT,created_at TEXT);
          CREATE TABLE x_auto_run(id INTEGER PRIMARY KEY,status TEXT,updated_at TEXT DEFAULT '',finished_at_utc TEXT DEFAULT '');
        """)
        self.executes("native","""
          CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,account_id INTEGER,manual_run_id INTEGER,status TEXT,updated_at TEXT DEFAULT '');
          CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,account_id INTEGER,status TEXT,attempt_count INTEGER,unknown_outcome INTEGER,error_code TEXT,x_post_id TEXT DEFAULT '',published_at TEXT DEFAULT '',error_message TEXT DEFAULT '',updated_at TEXT DEFAULT '');
          CREATE TABLE x_post_manual_run(id INTEGER PRIMARY KEY,status TEXT,expected_count INTEGER,queued_count INTEGER,published_count INTEGER,failed_count INTEGER,unknown_count INTEGER,finished_at TEXT DEFAULT '',updated_at TEXT DEFAULT '');
          CREATE TABLE x_run446_media_close_audit(queue_id INTEGER PRIMARY KEY,plan_sha256 TEXT,evidence_json TEXT,created_at TEXT);
          CREATE TABLE pipeline_x_media_only_no_post_audit(job_id INTEGER PRIMARY KEY,plan_sha256 TEXT,evidence_json TEXT,created_at TEXT);
          CREATE TABLE pipeline_x_prepost_recovery(source_key TEXT,stage TEXT,from_generation INTEGER,to_generation INTEGER,prior_attempts INTEGER,state TEXT,started_at TEXT,plan_sha256 TEXT);
          CREATE TABLE pipeline_x_operation(source_key TEXT,operation_id TEXT,generation INTEGER,operation TEXT,request_sha256 TEXT);
          CREATE TABLE pipeline_x_checkpoint(source_key TEXT,generation INTEGER,phase TEXT);
        """)
        self.executes("central","""
          CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,stage TEXT,status TEXT,hold_reason TEXT,generation INTEGER,error_code TEXT,external_intent INTEGER DEFAULT 0,canonical_receipt TEXT DEFAULT '',lease_expires_at TEXT DEFAULT '',claim_hash TEXT DEFAULT '',worker_id TEXT DEFAULT '',node_id TEXT DEFAULT '',updated_at TEXT DEFAULT '',source_status TEXT DEFAULT '',source_meta_json TEXT DEFAULT '{}');
          CREATE TABLE pipeline_x_media_only_no_post_audit(job_id INTEGER PRIMARY KEY,plan_sha256 TEXT,evidence_json TEXT,created_at TEXT);
          CREATE TABLE pipeline_x_media_only_reconciliation_apply(job_id INTEGER PRIMARY KEY,plan_sha256 TEXT,state TEXT);
          CREATE TABLE pipeline_operation(job_id INTEGER,operation_id TEXT,generation INTEGER,operation TEXT,request_sha256 TEXT);
          CREATE TABLE pipeline_checkpoint(job_id INTEGER,generation INTEGER,phase TEXT);
          CREATE TABLE pipeline_publication(source_key TEXT);
          CREATE TABLE pipeline_receipt(job_id INTEGER);
          CREATE TABLE pipeline_resource(job_id INTEGER);
          CREATE TABLE pipeline_legacy_resource(job_id INTEGER);
          CREATE TABLE pipeline_event(id INTEGER PRIMARY KEY,job_id INTEGER,event_type TEXT,details_json TEXT,created_at TEXT);
        """)
        for task,(run,account,qid,parent) in r.SCOPE.items():
            status="pending" if task==2781 else "failed" if task==2572 else "retry_wait"
            self.change("auto","INSERT OR IGNORE INTO x_auto_run(id,status) VALUES(?,'running')",(run,))
            self.change("auto","INSERT INTO x_auto_task(id,run_id,account_id,status,execution_queue_id,execution_run_id,unknown_outcome) VALUES(?,?,?,?,?,?,?)",(task,run,str(account),status,qid,parent,int(task!=2781)))
            if qid:
                native_status="queued" if task==2387 else "failed"
                self.change("native","INSERT INTO x_post_queue(id,account_id,manual_run_id,status) VALUES(?,?,?,?)",(qid,account,parent,native_status))
                self.change("native","INSERT INTO x_post_publish_log(id,queue_id,account_id,status,attempt_count,unknown_outcome,error_code) VALUES(?,?,?,?,1,0,'coordinator_unavailable')",(qid+1,qid,account,"reserved" if task==2387 else "failed"))
                self.change("native","INSERT INTO x_post_manual_run(id,status,expected_count,queued_count,published_count,failed_count,unknown_count) VALUES(?,'running',1,1,0,0,0)",(parent,))
            if task in (2572,2702):
                self.change("native","INSERT INTO x_run446_media_close_audit VALUES(?,?,?,'old')",(qid,"closed",json.dumps({"finding":"media_upload_stopped_before_post_intent","plan":{"plan_sha256":"closed"}})))
        for task,account in ((2731,"49"),(2780,"71")):
            self.change("auto","INSERT INTO x_auto_task(id,run_id,account_id,status,error_code) VALUES(?,446,?,'failed','x_account_publish_not_approved')",(task,account))
        self.change("auto","INSERT INTO x_auto_task(id,run_id,account_id,status,execution_queue_id,execution_run_id) VALUES(2642,417,'63','retry_wait',5452,1476)")
        self.change("central","INSERT INTO pipeline_job(id,source_key,stage,status,hold_reason,generation,error_code) VALUES(32064,'x:queue:5452','publish','unknown','operator_unknown_receipt_reconciliation',1,'pipeline_lease_lost')")
        self.change("central","INSERT INTO pipeline_job(id,source_key,stage,status,hold_reason,generation,error_code,claim_hash,worker_id,node_id) VALUES(31995,'x:queue:5523','publish','failed','',3,'x_recovery_generation_changed','hash','worker','cpu-main')")
        op={"operation_id":"old-init","generation":1,"operation":"x:POST:/2/media/upload/initialize","request_sha256":"abc"}
        original={"finding":"exact_generation_stopped_in_media_upload_before_Post","plan":{"plan_sha256":"original","proof":{"native_ops":[op],"central_ops":[op]}}}
        for label in ("native","central"):
            self.change(label,"INSERT INTO pipeline_x_media_only_no_post_audit VALUES(31995,'original',?,'old')",(json.dumps(original),))
        self.change("central","INSERT INTO pipeline_x_media_only_reconciliation_apply VALUES(31995,'original','committed')")
        self.change("native","INSERT INTO pipeline_x_prepost_recovery VALUES('x:queue:5523','publish',1,2,1,'armed','','original')")
        self.change("native","INSERT INTO pipeline_x_operation VALUES('x:queue:5523',?,?,?,?)",tuple(op.values()))
        self.change("central","INSERT INTO pipeline_operation VALUES(31995,?,?,?,?)",tuple(op.values()))
        self.evidence={"job_id":31995,"generation":3,"checked_at":self.clock.isoformat(),"worker_execution_stopped":True,"no_later_success":True,
                       "receipt":{"route":"jobs/fail","error_code":"x_recovery_generation_changed","unknown_outcome":False,"acked":False,"payload_sha256":"a"*64},
                       "receipts":[{"route":"jobs/fail","acked_at":None,"payload_sha256":"a"*64,"payload":{"job_id":31995,"generation":3,"error_code":"x_recovery_generation_changed","unknown_outcome":False,"claim_token_sha256":"hash","worker_id":"worker","node_id":"cpu-main"}}]}

    def executes(self,label,sql):
        with contextlib.closing(sqlite3.connect(self.paths[label])) as c,c:c.executescript(sql)
    def change(self,label,sql,params=()):
        with contextlib.closing(sqlite3.connect(self.paths[label])) as c,c:c.execute(sql,params)
    def query(self,label,sql,params=()):
        with contextlib.closing(sqlite3.connect(self.paths[label])) as c:return c.execute(sql,params).fetchall()
    def plan(self,task):return r.make_plan(self.paths,task,self.evidence if task==2387 else None,self.clock)
    def apply(self,plan):return r.apply_plan(plan,self.root/"backups","operator",self.clock)

    def test_closed_unknown_only_updates_original_auto_task(self):
        native_before=self.query("native","SELECT * FROM x_post_publish_log")
        for task in (2572,2702):
            result=self.apply(self.plan(task));self.assertTrue(result["applied"])
            self.assertEqual(self.query("auto","SELECT status,unknown_outcome FROM x_auto_task WHERE id=?",(task,)),[("failed",0)])
        self.assertEqual(self.query("native","SELECT * FROM x_post_publish_log"),native_before)
        self.assertEqual(self.query("auto","SELECT id,error_code FROM x_auto_task WHERE id IN (2731,2780) ORDER BY id"),[(2731,"x_account_publish_not_approved"),(2780,"x_account_publish_not_approved")])

    def test_missing_or_unclosed_native_audit_rejects(self):
        self.change("native","DELETE FROM x_run446_media_close_audit WHERE queue_id=5393")
        with self.assertRaises(r.RecoveryRejected):self.plan(2572)
        self.change("native","UPDATE x_post_publish_log SET unknown_outcome=1 WHERE queue_id=5857")
        with self.assertRaisesRegex(r.RecoveryRejected,"canonical_not_closed"):self.plan(2702)

    def test_pending_settlement_preserves_old_unknown_and_other_accounts(self):
        old=self.query("auto","SELECT * FROM x_auto_task WHERE id=2642")
        hold=self.query("central","SELECT * FROM pipeline_job WHERE id=32064")
        self.apply(self.plan(2781))
        self.assertEqual(self.query("auto","SELECT status,error_code,execution_queue_id FROM x_auto_task WHERE id=2781"),[("failed","x_post_account_needs_review",None)])
        self.assertEqual(self.query("auto","SELECT * FROM x_auto_task WHERE id=2642"),old)
        self.assertEqual(self.query("central","SELECT * FROM pipeline_job WHERE id=32064"),hold)

    def test_5523_known_generation_failure_preserves_attempts_audits_and_receipt(self):
        before=json.dumps(self.evidence,sort_keys=True)
        ops=self.query("native","SELECT * FROM pipeline_x_operation")
        self.apply(self.plan(2387))
        self.assertEqual(self.query("native","SELECT status,attempt_count,unknown_outcome,error_code FROM x_post_publish_log WHERE queue_id=5523"),[("failed",1,0,"x_recovery_generation_changed")])
        self.assertEqual(self.query("auto","SELECT status,unknown_outcome FROM x_auto_task WHERE id=2387"),[("failed",0)])
        self.assertEqual(self.query("native","SELECT * FROM pipeline_x_operation"),ops)
        self.assertEqual(json.dumps(self.evidence,sort_keys=True),before)

    def test_later_operation_or_actual_post_refuses_5523(self):
        self.change("central","INSERT INTO pipeline_operation VALUES(31995,'later',2,'x:POST:/2/tweets','xyz')")
        with self.assertRaisesRegex(r.RecoveryRejected,"post_or_later_operation"):self.plan(2387)
        self.change("central","DELETE FROM pipeline_operation WHERE generation=2")
        self.change("native","UPDATE x_post_publish_log SET x_post_id='123' WHERE queue_id=5523")
        with self.assertRaisesRegex(r.RecoveryRejected,"native_5523_changed"):self.plan(2387)

    def test_5523_requires_unchanged_original_operation_scope_and_fresh_failure(self):
        self.evidence["checked_at"]=(self.clock-dt.timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(r.RecoveryRejected,"node_evidence_required"):self.plan(2387)
        self.evidence["checked_at"]=self.clock.isoformat()
        self.change("central","INSERT INTO pipeline_operation VALUES(31995,'new-old',1,'x:POST:/2/media/upload/m/append','xyz')")
        with self.assertRaisesRegex(r.RecoveryRejected,"operations_changed_since_no_post_audit"):self.plan(2387)

    def test_drift_or_live_claim_rejected_before_any_write(self):
        plan=self.plan(2702)
        self.change("auto","UPDATE x_auto_task SET claim_token='live' WHERE id=2702")
        with self.assertRaisesRegex(r.RecoveryRejected,"task_active_or_published"):self.apply(plan)
        self.assertEqual(self.query("auto","SELECT unknown_outcome FROM x_auto_task WHERE id=2702"),[(1,)])

    def test_cases_are_independent_and_reapply_fails_closed(self):
        self.evidence["receipt"]["unknown_outcome"]=True
        with self.assertRaises(r.RecoveryRejected):self.plan(2387)
        plan=self.plan(2572);self.apply(plan)
        with self.assertRaises(r.RecoveryRejected):self.apply(plan)
        self.apply(self.plan(2781))


if __name__=="__main__":unittest.main()
