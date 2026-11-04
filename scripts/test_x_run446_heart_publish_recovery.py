"""Rearm only the same prepared queue, and reject any evidence of publication."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from scripts import recover_x_run446_heart_publish as r


class HeartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.paths={k:str(self.root/(k+".sqlite")) for k in ("auto","native","central")}
        self.now=dt.datetime(2026,11,4,10,tzinfo=dt.timezone.utc)
        self.evidence={"job_id":33431,"generation":1,"node_id":"cpu-main","worker_execution_stopped":True,
                       "outbox_pending_for_scope":0,"attribution_verified":True,"fix_revision":"f"*40,"checked_at":self.now.isoformat()}
        task=dict(id=2712,run_id=446,template_id=4,template_version=6,account_id="46",execution_run_id=1826,
                  execution_queue_id=5908,status="failed",error_code=r.ERROR,unknown_outcome=0,publish_id="",publish_url="",
                  claim_token="",claim_worker="",lease_expires_at_utc="",material_id="12345",body_template="Frozen body",
                  language="en",account_snapshot_version=1,account_snapshot_json='{}',scheduled_at_utc="2026-11-03T12:59:00Z",
                  error_message="failure",next_attempt_at_utc="",finished_at_utc="2026-11-04T09:10:14Z",updated_at="old")
        self.table("auto","x_auto_task",[task])
        self.table("auto","x_auto_operator_retry",[dict(task_id=2712,generation=1,prior_run_id=1657,external_task_key=r.EXTERNAL_KEY,
            identity_sha256=r.fingerprint(r.frozen_identity(SimpleNamespace(**dict(task,account_snapshot={})))),config_sha256="a"*64)])
        self.table("auto","x_auto_run",[dict(id=446,config_sha256="a"*64,status="running",finished_at_utc="",updated_at="old")])
        self.table("auto","x_auto_material_ledger",[dict(task_id=2712,material_id="12345",canonical_queue_id=5908,last_task_status="failed",updated_at="old")])
        self.empty("auto","x_auto_event","run_id INTEGER,task_id INTEGER,event_type TEXT,from_status TEXT,to_status TEXT,message TEXT,details_json TEXT,created_at TEXT")
        queue=dict(id=5908,account_id=46,manual_run_id=1826,status="queued",material_id="12345",content_id="7788",
                   body_template="Frozen body",preflight_sha256="b"*64,preflight_size=3000,material_url="https://media.test/original.mp4")
        self.table("native","x_post_queue",[queue])
        self.table("native","x_post_publish_log",[dict(id=5853,queue_id=5908,account_id=46,status="reserved",attempt_count=0,
            unknown_outcome=0,x_media_id="",x_post_id="",x_post_url="",started_at="",published_at="")])
        self.table("native","x_post_manual_run",[dict(id=1826,trigger_source="auto_template",external_task_key=r.EXTERNAL_KEY,
            template_ref="x-auto-template-4",template_version=6,expected_count=1,queued_count=1,published_count=0,unknown_count=0)])
        self.table("native","x_authorized_account",[dict(id=46,status="active",publish_approved=1)])
        for name in ("pipeline_x_operation","pipeline_canonical_hold"):
            self.empty("native",name,"source_key TEXT")
        self.empty("native","x_post_repost_ledger","queue_id INTEGER")
        request=dict(job_key="prepared",queue_id=5908,content_id="7788",profile="test",source_sha256="b"*64,source_size=3000,
            source_url="https://media.test/source.mp4",source_duration=299.5,max_duration_seconds=1200,asset_manifest_sha256="c"*64)
        result=dict(request,output_url="https://media.test/final.mp4",output_sha256="d"*64,output_size=5000,
            probe={"duration":299.5},recipe={"asset_set_sha256":"c"*64})
        self.table("native","x_post_random_template_preparation",[dict(queue_id=5908,status="ready",request_json=json.dumps(request),result_json=json.dumps(result))])
        self.table("native","pipeline_x_artifact",[dict(queue_id=5908,source_url=queue["material_url"],artifact_json=json.dumps({"url":request["source_url"],"sha256":"b"*64,"size":3000}))])
        self.table("native","pipeline_x_claim",[dict(source_key=r.KEY,stage="prepare",generation=1,queue_id=5908,log_id=5853,claim_hash="prep")])
        self.table("native","pipeline_x_checkpoint",[dict(source_key=r.KEY,generation=1,phase="x_preflight")])
        artifact=r.artifact_from_receipt(request,result)
        jobs=[]
        for jid,stage,status,claim in [(33356,"prepare","completed","prep"),(33431,"publish","failed","pub")]:
            jobs.append(dict(id=jid,source_key=r.KEY,source_id="5908",channel="x",family="queue",stage=stage,status=status,generation=1,
                external_intent=0,hold_reason="",source_meta_json=json.dumps({"canonical_fingerprint":r.canonical_fingerprint(queue,"id")}),
                artifact_json=json.dumps(artifact),lease_expires_at="2026-11-04T09:12:13Z",claim_hash=claim,node_id="cpu-main",worker_id=stage,
                error_code=r.ERROR if stage=="publish" else "",canonical_receipt="" if stage=="publish" else "proof",
                started_at="2026-11-04T09:10:12Z",finished_at="2026-11-04T09:10:13Z",updated_at="old",source_status="queued",error_message=""))
        self.table("central","pipeline_job",jobs)
        self.table("central","pipeline_claim_history",[dict(job_id=j["id"],generation=1,claim_hash=j["claim_hash"],node_id=j["node_id"],worker_id=j["worker_id"]) for j in jobs])
        for table in ("pipeline_operation","pipeline_resource","pipeline_legacy_resource","pipeline_checkpoint","pipeline_receipt"):
            self.empty("central",table,"job_id INTEGER")
        self.empty("central","pipeline_publication","source_key TEXT")
        self.table("central","pipeline_delivery",[dict(delivery_id="failed",job_id=33431,generation=1,claim_hash="pub",response_json='{"accepted":true,"status":"failed"}')])
        self.table("central","pipeline_event",[dict(id=1,job_id=33431,event_type="failed",details_json=json.dumps({"code":r.ERROR,"unknown":False}),created_at="old")])

    def table(self,db,name,items):
        first=items[0];cols=','.join(k+(" INTEGER" if isinstance(v,int) else " TEXT") for k,v in first.items())
        with contextlib.closing(sqlite3.connect(self.paths[db])) as c,c:
            c.execute("CREATE TABLE "+name+"("+cols+")")
            c.executemany("INSERT INTO "+name+" VALUES("+','.join('?'*len(first))+")",[list(i.values()) for i in items])

    def empty(self,db,name,columns):
        self.sql(db,"CREATE TABLE "+name+"("+columns+")")

    def sql(self,db,sql,args=()):
        with contextlib.closing(sqlite3.connect(self.paths[db])) as c,c:
            return c.execute(sql,args).fetchall()

    def dump(self,db):
        with contextlib.closing(sqlite3.connect(self.paths[db])) as c:return '\n'.join(c.iterdump())

    def plan(self):return r.make_plan(self.paths,self.evidence,self.now)
    def apply(self,plan):return r.apply_plan(plan,self.root/"backup","authorized-run446",self.now)

    def test_same_artifact_native_immutable_and_auto_tracks_existing_queue(self):
        native=self.dump("native");artifact=self.sql("central","SELECT artifact_json FROM pipeline_job WHERE id=33431")
        plan=self.plan();self.assertEqual(native,self.dump("native"));result=self.apply(plan)
        self.assertEqual(native,self.dump("native"));self.assertFalse(result["platform_called"])
        self.assertEqual(artifact,self.sql("central","SELECT artifact_json FROM pipeline_job WHERE id=33431"))
        self.assertEqual(self.sql("central","SELECT status,generation,claim_hash FROM pipeline_job WHERE id=33431"),[("ready",1,"")])
        self.assertEqual(self.sql("auto","SELECT status,execution_queue_id,execution_run_id,material_id,body_template FROM x_auto_task"),[("retry_wait",5908,1826,"12345","Frozen body")])
        with self.assertRaises(r.RecoveryRejected):self.apply(plan)

    def test_native_mutation_is_blocked_by_authorizer(self):
        with contextlib.closing(r.connect(self.paths,True)) as c:
            c.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.DatabaseError):c.execute("UPDATE native.x_post_queue SET status='failed'")
            c.rollback()

    def test_unknown_attempt_or_post_never_rearms(self):
        for col,value in [("attempt_count",1),("unknown_outcome",1),("x_post_id","real-post")]:
            old=self.sql("native","SELECT "+col+" FROM x_post_publish_log")[0][0]
            self.sql("native","UPDATE x_post_publish_log SET "+col+"=?",(value,))
            with self.assertRaisesRegex(r.RecoveryRejected,"native_publish_evidence"):self.plan()
            self.sql("native","UPDATE x_post_publish_log SET "+col+"=?",(old,))

    def test_new_operation_or_failed_ack_missing_rejects(self):
        self.sql("central","INSERT INTO pipeline_operation VALUES(33431)")
        with self.assertRaisesRegex(r.RecoveryRejected,"dispatch_write_or_fence"):self.plan()
        self.sql("central","DELETE FROM pipeline_operation")
        self.sql("central","DELETE FROM pipeline_delivery")
        with self.assertRaisesRegex(r.RecoveryRejected,"failed_ack_missing"):self.plan()

    def test_unchanged_material_body_required_even_when_plan_hash_valid(self):
        plan=self.plan();self.sql("native","UPDATE x_post_queue SET body_template='different'")
        with self.assertRaisesRegex(r.RecoveryRejected,"frozen_selection_changed"):self.apply(plan)
        self.assertEqual(self.sql("central","SELECT status FROM pipeline_job WHERE id=33431"),[("failed",)])

    def test_stale_or_unverified_fix_and_forged_plan_reject(self):
        plan=self.plan();plan["proof"]["job_id"]=33333
        with self.assertRaisesRegex(r.RecoveryRejected,"plan_hash_changed"):self.apply(plan)
        self.evidence["attribution_verified"]=False
        with self.assertRaisesRegex(r.RecoveryRejected,"recovery_not_verified"):self.plan()
        self.evidence["attribution_verified"]=True;self.evidence["checked_at"]="2026-11-03T10:00:00Z"
        with self.assertRaisesRegex(r.RecoveryRejected,"evidence_stale"):self.plan()

    def test_new_generation_is_not_in_scope(self):
        self.sql("central","UPDATE pipeline_job SET generation=2 WHERE id=33431")
        with self.assertRaisesRegex(r.RecoveryRejected,"dispatch_stages_changed"):self.plan()

    def test_transaction_rollback_preserves_both_databases_on_late_failure(self):
        plan=self.plan()
        self.sql("auto","CREATE TRIGGER reject_event BEFORE INSERT ON x_auto_event BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"test failure"):self.apply(plan)
        self.assertEqual(self.sql("central","SELECT status FROM pipeline_job WHERE id=33431"),[("failed",)])
        self.assertEqual(self.sql("auto","SELECT status FROM x_auto_task"),[("failed",)])


if __name__=="__main__":unittest.main()
