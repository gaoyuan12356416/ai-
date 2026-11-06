import datetime as dt
import json
import sqlite3
import unittest
from scripts.recover_fb_support_switch_upload import proof


class RecoveryProofTests(unittest.TestCase):
    def setUp(self):
        self.c,self.n,self.o=[sqlite3.connect(':memory:') for _ in range(3)]
        for db in (self.c,self.n,self.o):
            db.row_factory=sqlite3.Row;self.addCleanup(db.close)
        self.c.executescript('''
        CREATE TABLE pipeline_job(id,source_key,stage,status,generation,node_id,worker_id,error_code,hold_reason,canonical_receipt);
        INSERT INTO pipeline_job VALUES(35396,'x:queue:6553','publish','unknown',1,'cpu-main','owner','coordinator_unavailable','','');
        CREATE TABLE pipeline_publication(source_key);
        CREATE TABLE pipeline_operation(operation_id,job_id,generation,operation,request_sha256);
        INSERT INTO pipeline_operation VALUES('operation',35396,1,'x:POST:/2/media/upload/initialize','hash');
        CREATE TABLE pipeline_checkpoint(job_id,phase);
        CREATE TABLE pipeline_worker(node_id,worker_id,last_seen_at,observation_json);
        ''')
        self.c.execute('INSERT INTO pipeline_worker VALUES(?,?,?,?)',('cpu-main','owner',dt.datetime.now(dt.timezone.utc).isoformat(),'{"phase":"idle"}'))
        self.n.executescript('''
        CREATE TABLE x_post_queue(id,status,delivery_mode,account_id,random_template_enabled);
        INSERT INTO x_post_queue VALUES(6553,'failed','direct',7,0);
        CREATE TABLE x_post_publish_log(queue_id,status,error_code,unknown_outcome,attempt_count,x_post_id,x_media_id,published_at,post_text);
        INSERT INTO x_post_publish_log VALUES(6553,'failed','coordinator_unavailable',1,1,'','','','body');
        CREATE TABLE pipeline_canonical_hold(source_key);
        CREATE TABLE x_post_repost_ledger(queue_id);
        CREATE TABLE pipeline_x_prepost_recovery(source_key);
        CREATE TABLE pipeline_x_operation(operation_id,source_key,generation,operation,request_sha256);
        INSERT INTO pipeline_x_operation VALUES('operation','x:queue:6553',1,'x:POST:/2/media/upload/initialize','hash');
        CREATE TABLE pipeline_x_checkpoint(source_key,phase);
        CREATE TABLE pipeline_x_claim(source_key,stage,generation,descriptor_json);
        CREATE TABLE pipeline_x_artifact(queue_id,artifact_json);
        CREATE TABLE x_post_random_template_preparation(queue_id);
        ''')
        artifact={'sha256':'frozen_hash','size':12,'url':'https://fixture.invalid/video.mp4'}
        desc={'kind':'x_publish','request':{'body':'body'},'account_resource':'x:account:7','artifact':artifact}
        self.n.execute('INSERT INTO pipeline_x_claim VALUES(?,?,?,?)',('x:queue:6553','publish',1,json.dumps(desc)))
        self.n.execute('INSERT INTO pipeline_x_artifact VALUES(?,?)',(6553,json.dumps(artifact)))
        self.o.execute('CREATE TABLE receipt_outbox(id,route,payload,acked_at)')
        self.o.execute('INSERT INTO receipt_outbox VALUES(?,?,?,?)',('failed','jobs/fail',json.dumps({'job_id':35396,'generation':1,'error_code':'coordinator_unavailable','unknown_outcome':True}),1))

    def test_media_only_acknowledged_failure_can_reuse_exact_artifact(self):
        self.assertEqual(proof(self.c,self.n,self.o)['artifact']['sha256'],'frozen_hash')

    def test_post_or_finalize_intent_is_not_recoverable(self):
        for name in ('x:POST:/2/tweets','x:POST:/2/media/upload/id/finalize'):
            self.n.execute('UPDATE pipeline_x_operation SET operation=?',(name,))
            with self.assertRaises(AssertionError):proof(self.c,self.n,self.o)

    def test_unacked_receipt_is_not_recoverable(self):
        self.o.execute('UPDATE receipt_outbox SET acked_at=NULL')
        with self.assertRaises(AssertionError):proof(self.c,self.n,self.o)

    def test_later_receipt_or_changed_media_is_not_recoverable(self):
        self.n.execute("UPDATE pipeline_x_artifact SET artifact_json='{}'")
        with self.assertRaises(AssertionError):proof(self.c,self.n,self.o)

    def test_busy_owner_is_not_recovered(self):
        self.c.execute('UPDATE pipeline_worker SET observation_json=?',('{"phase":"upload"}',))
        with self.assertRaises(AssertionError):proof(self.c,self.n,self.o)

    def test_review_parent_only_for_the_exact_single_interrupted_task(self):
        self.n.execute('ALTER TABLE x_post_queue ADD COLUMN manual_run_id')
        self.n.execute('UPDATE x_post_queue SET manual_run_id=2228')
        self.n.execute('CREATE TABLE x_post_manual_run(id,status,unknown_count)')
        self.n.execute("INSERT INTO x_post_manual_run VALUES(2228,'needs_review',1)")
        self.assertTrue(proof(self.c,self.n,self.o))
        self.n.execute('INSERT INTO x_post_queue(id,manual_run_id) VALUES(9999,2228)')
        with self.assertRaises(AssertionError):proof(self.c,self.n,self.o)

    def test_explicitly_stopped_parent_is_preserved(self):
        self.n.execute('ALTER TABLE x_post_queue ADD COLUMN manual_run_id')
        self.n.execute('UPDATE x_post_queue SET manual_run_id=2228')
        self.n.execute('CREATE TABLE x_post_manual_run(id,status,unknown_count)')
        self.n.execute("INSERT INTO x_post_manual_run VALUES(2228,'stopped',1)")
        with self.assertRaises(AssertionError):proof(self.c,self.n,self.o)


if __name__=='__main__':unittest.main()
