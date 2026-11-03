"""Exact historical reconciliation, crash-safe admission and existing runtime fences."""
import contextlib
import copy
import datetime as dt
import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.x_prepost_recovery import digest, queue_hash
from scripts import reconcile_x_5524_prepost as recovery
from scripts import test_x_prepost_recovery as fixture


class ReconciliationTests(unittest.TestCase):
    verify = fixture.PrepostRecoveryTests.verify
    queue = fixture.PrepostRecoveryTests.queue
    operation = fixture.PrepostRecoveryTests.operation
    log = fixture.PrepostRecoveryTests.log

    def claim(self, stage):
        self.store.register_node('hk-gpu', self.caps)
        self.store.worker_heartbeat('hk-gpu', stage+'-worker', stage)
        job = self.store.claim('hk-gpu', stage+'-worker', stage)['job']
        job['executor_backend'] = 'cpu'
        return job

    def patch(self, name, value):
        patch = mock.patch.object(recovery, name, value)
        patch.start(); self.addCleanup(patch.stop)

    def setUp(self):
        fixture.PrepostRecoveryTests.setUp(self)
        self.patch('QID', self.q['id']); self.patch('LOG_ID', self.log()['id'])
        self.patch('PREP_ID', self.prep['id']); self.patch('JOB_ID', self.pub['id'])
        self.patch('KEY', self.pub['source_key']); self.patch('ACCOUNT_ID', 2)
        self.patch('PARENT_ID', self.q['manual_run_id']); self.patch('WORKER', self.pub['worker_id'])
        self.patch('CLAIM_SHA', hashlib.sha256(self.pub['claim_token'].encode()).hexdigest())
        self.payload = dict(job_id=self.pub['id'], generation=1, claim_token=self.pub['claim_token'],
            node_id='hk-gpu', worker_id=self.pub['worker_id'], receipt_id=recovery.RECEIPT_ID,
            error_code='pipeline_lease_lost', unknown_outcome=True,
            metrics={'platform_upload_seconds': 239.902})
        self.patch('RECEIPT_SHA', digest(self.payload))
        safe = dict(self.payload)
        safe['claim_token_sha256'] = hashlib.sha256(safe.pop('claim_token').encode()).hexdigest()
        self.patch('EXPECTED_RECEIPTS', {recovery.RECEIPT_ID: ('jobs/fail', digest(self.payload), digest(safe))})
        for outbox in self.outboxes.values():
            with outbox.connect() as c: c.execute('DELETE FROM receipt_outbox')
        rid = self.outboxes['hk-gpu'].put('jobs/fail', self.payload, recovery.RECEIPT_ID)
        with self.outboxes['hk-gpu'].connect() as c: c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?', (rid,))
        with self.bridge.connect() as n, self.store.transaction() as c:
            n.execute("UPDATE x_post_publish_log SET unknown_outcome=1,error_code='pipeline_lease_lost',error_message='pipeline_lease_lost' WHERE id=?", (recovery.LOG_ID,))
            n.execute("UPDATE x_post_manual_run SET status='needs_review',unknown_count=1,failed_count=0,error_code='',error_message='' WHERE id=?", (recovery.PARENT_ID,))
            c.execute("UPDATE pipeline_job SET status='unknown',finished_at='',error_code='pipeline_lease_lost' WHERE id=?", (recovery.JOB_ID,))
            c.execute('INSERT INTO pipeline_resource VALUES(?,?,1,1,0)', ('x:account:2', recovery.JOB_ID))
            c.execute("UPDATE pipeline_event SET details_json=? WHERE job_id=? AND event_type='failed'", (json.dumps({'code':'pipeline_lease_lost','unknown':True}), recovery.JOB_ID))
            n_op = dict(n.execute("SELECT * FROM pipeline_x_operation WHERE operation_id='old-append'").fetchone())
            c_op = dict(c.execute("SELECT * FROM pipeline_operation WHERE operation_id='old-append'").fetchone())
            for index in range(81):
                for conn, table, source in ((n,'pipeline_x_operation',n_op),(c,'pipeline_operation',c_op)):
                    row = dict(source, operation_id='historical-append-'+str(index))
                    conn.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', list(row.values()))
            c.execute('INSERT INTO pipeline_delivery VALUES(?,?,?,?,?,?)', (recovery.RECEIPT_ID,recovery.JOB_ID,1,recovery.CLAIM_SHA,recovery.RECEIPT_SHA,json.dumps({'accepted':True,'status':'unknown'})))
        self.history_dir = self.root/'historical'; self.history_dir.mkdir()
        self.release_root = self.root/'release'; self.release_root.mkdir()
        audit = {'previous_sha':recovery.RELEASE,'children_before':[str(recovery.WORKER_PID)]}
        (self.history_dir/'audit.json').write_text(json.dumps(audit))
        (self.history_dir/'node.service.before').write_text('original reviewed unit')
        self.patch('HISTORY_FILES', {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.history_dir.iterdir()})
        (self.release_root/'worker.py').write_text('reviewed historical worker fixture')
        self.patch('RELEASE_FILES', {'worker.py':hashlib.sha256((self.release_root/'worker.py').read_bytes()).hexdigest()})
        with self.bridge.connect() as n, self.store.transaction() as c:
            proof = recovery.collect(c,n)
        expected = {k:digest(proof[k]) for k in ('native_ops','central_ops','claims','history')}
        expected.update(queue=queue_hash(proof['queue']), log=digest(proof['log']))
        self.patch('EXPECTED', expected)

    def plan(self):
        return recovery.plan(self.central, self.runtime.POST_DB_PATH, self.now)

    def evidence(self, plan):
        return [recovery.node_evidence(plan,node,outbox.path,self.now,
                    history_dir=self.history_dir,release_root=self.release_root) for node,outbox in self.outboxes.items()]

    def apply(self, saved, **kwargs):
        return recovery.apply(saved,self.evidence(saved),self.root/'backups','offline-reviewer',self.now,**kwargs)

    def test_exact_reconciliation_preserves_evidence_then_existing_runtime_consumes_once(self):
        saved = self.plan(); before = copy.deepcopy(saved['proof'])
        result = self.apply(saved)
        self.assertFalse(result['platform_called'])
        self.assertEqual(self.log()['attempt_count'],1)
        self.assertEqual(self.log()['unknown_outcome'],0)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'ready')
        with self.bridge.connect() as n, self.store.transaction() as c:
            for conn in (n,c):
                audit = json.loads(conn.execute('SELECT evidence_json FROM '+recovery.AUDIT).fetchone()[0])
                self.assertEqual(audit['plan']['proof'],before)
                self.assertTrue(next(e for e in audit['node_evidence'] if e['node_id']=='hk-gpu')['receipts'][0]['payload']['unknown_outcome'])
                with self.assertRaisesRegex(sqlite3.IntegrityError,'append_only'):
                    conn.execute('DELETE FROM '+recovery.AUDIT)
                with self.assertRaisesRegex(sqlite3.IntegrityError,'append_only'):
                    conn.execute('INSERT OR REPLACE INTO '+recovery.AUDIT+' SELECT * FROM '+recovery.AUDIT)
            self.assertEqual(n.execute('SELECT count(*) FROM pipeline_x_operation').fetchone()[0],83)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_operation').fetchone()[0],83)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_claim_history').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_resource').fetchone()[0],0)
        with self.assertRaises(AdapterError): self.bridge.fail(self.pub,self.pub,{'unknown_outcome':True})
        retry = self.claim('publish'); self.assertEqual(retry['generation'],2)
        self.assertEqual(self.bridge.reserve(retry,retry)['artifact'],self.artifact)
        op = self.operation(retry,'POST','/2/media/upload/initialize',self.initial,'next-init')
        self.store.begin_operation(retry,op); self.bridge.begin_operation(retry,retry,op)
        self.assertEqual(self.log()['attempt_count'],2)
        self.assertTrue(self.apply(saved)['already_applied'])
        self.assertEqual(self.log()['attempt_count'],2)
        with self.assertRaises(AdapterError): self.bridge.begin_operation(retry,retry,op)

    def test_interruption_after_each_commit_resumes_without_losing_hold(self):
        saved = self.plan()
        with self.assertRaisesRegex(recovery.RecoveryRejected,'central_stage'):
            self.apply(saved,stop_after='central_staged')
        self.assertEqual(self.log()['unknown_outcome'],1)
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],recovery.HOLD)
        self.assertIsNone(self.store.claim('hk-gpu','blocked-worker','publish')['job'])
        with self.assertRaisesRegex(recovery.RecoveryRejected,'native_arm'):
            self.apply(saved,stop_after='native_armed')
        self.assertEqual(self.log()['unknown_outcome'],0)
        self.assertEqual(self.log()['attempt_count'],1)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'unknown')
        with self.assertRaises(AdapterError): self.bridge.fail(self.pub,self.pub,{'unknown_outcome':True})
        self.apply(saved)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'ready')

    def test_pending_success_or_additional_unknown_receipt_rejected(self):
        saved = self.plan()
        for payload in ({'job_id':recovery.JOB_ID,'generation':1,'result':{'post_id':'123'}},
                        {'job_id':recovery.JOB_ID,'generation':1,'unknown_outcome':True},
                        {'job_id':recovery.JOB_ID,'generation':1,'result':{}}):
            rid=self.outboxes['cpu-main'].put('jobs/complete',payload)
            with self.assertRaises(recovery.RecoveryRejected): self.apply(saved)
            with self.outboxes['cpu-main'].connect() as c: c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(rid,))
            with self.assertRaises(recovery.RecoveryRejected): self.apply(saved)
            with self.outboxes['cpu-main'].connect() as c: c.execute('DELETE FROM receipt_outbox WHERE id=?',(rid,))
        self.assertEqual(self.log()['unknown_outcome'],1)

    def test_original_receipt_version_or_worker_proof_cannot_be_substituted(self):
        saved=self.plan(); evidence=self.evidence(saved)
        for mutate in ('receipt','raw-body','worker','stale'):
            forged=copy.deepcopy(evidence);gpu=next(e for e in forged if e['node_id']=='hk-gpu')
            if mutate=='receipt':gpu['receipts'][0]['payload_sha256']='0'*64
            elif mutate=='raw-body':gpu['receipts'][0]['payload']['metrics']['platform_upload_seconds']=1
            elif mutate=='worker':gpu['historical_worker']['pid']+=1
            else:gpu['captured_at']='2020-01-01T00:00:00Z'
            with self.assertRaises(recovery.RecoveryRejected):
                recovery.apply(saved,forged,self.root/'backups','reviewer',self.now)
        (self.release_root/'worker.py').write_text('unreviewed worker')
        with self.assertRaisesRegex(recovery.RecoveryRejected,'historical_worker_file_changed'):self.evidence(saved)

    def test_added_post_or_any_operation_frozen_body_and_active_lease_reject(self):
        saved=self.plan()
        with self.bridge.connect() as n:
            n.execute('INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)',('late',recovery.KEY,1,'x:POST:/2/tweets','a'*64,self.now.isoformat()))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(saved)
        with self.bridge.connect() as n:
            n.execute("DELETE FROM pipeline_x_operation WHERE operation_id='late'")
            n.execute("UPDATE x_post_publish_log SET post_text='changed'")
        with self.assertRaises(recovery.RecoveryRejected):self.apply(saved)
        with self.bridge.connect() as n:n.execute('UPDATE x_post_publish_log SET post_text=?',(saved['proof']['log']['post_text'],))
        with self.store.transaction() as c:c.execute("UPDATE pipeline_job SET lease_expires_at='2099-01-01T00:00:00Z' WHERE id=?",(recovery.JOB_ID,))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(saved)
        self.assertEqual(self.log()['unknown_outcome'],1)

    def test_native_transaction_error_rolls_back_authority_and_unknown(self):
        saved=self.plan()
        with self.bridge.connect() as n:
            n.execute("CREATE TRIGGER stop_arm BEFORE UPDATE ON x_post_publish_log WHEN NEW.unknown_outcome=0 BEGIN SELECT RAISE(ABORT,'stop_arm'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'stop_arm'):self.apply(saved)
        self.assertEqual(self.log()['unknown_outcome'],1)
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],recovery.HOLD)
        with self.bridge.connect() as n:
            self.assertFalse(recovery.exists(n,recovery.AUTH))
            self.assertFalse(recovery.exists(n,recovery.AUDIT))
            n.execute('DROP TRIGGER stop_arm')
        self.apply(saved)

    def test_post_appearing_between_native_and_central_commit_keeps_hold(self):
        saved=self.plan()
        with self.assertRaises(recovery.RecoveryRejected):self.apply(saved,stop_after='native_armed')
        with self.store.transaction() as c:
            c.execute('INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?)',
                ('late',recovery.JOB_ID,'hk-gpu','x:POST:/2/tweets','a'*64,'x:account:2',self.now.isoformat(),self.now.isoformat()))
        with self.assertRaisesRegex(recovery.RecoveryRejected,'proof_state_drift'):self.apply(saved)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'unknown')
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],recovery.HOLD)

    def test_authorization_mutation_after_native_commit_rejected(self):
        saved=self.plan()
        with self.assertRaises(recovery.RecoveryRejected):self.apply(saved,stop_after='native_armed')
        with self.bridge.connect() as n:
            n.execute('UPDATE '+recovery.AUTH+' SET to_generation=3')
        with self.assertRaisesRegex(recovery.RecoveryRejected,'authorization_changed'):self.apply(saved)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'unknown')
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],recovery.HOLD)

    def test_final_central_transaction_failure_retains_hold_and_quarantine(self):
        saved=self.plan()
        with self.store.transaction() as c:
            c.execute("CREATE TRIGGER stop_admission BEFORE DELETE ON pipeline_resource BEGIN SELECT RAISE(ABORT,'stop_admission'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'stop_admission'):self.apply(saved)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'unknown')
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],recovery.HOLD)
        with self.store.transaction() as c:
            self.assertEqual(c.execute('SELECT quarantined FROM pipeline_resource').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT state FROM '+recovery.JOURNAL).fetchone()[0],'staged')
            c.execute('DROP TRIGGER stop_admission')
        self.assertEqual(self.log()['attempt_count'],1)
        self.apply(saved)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'],'ready')

    def test_evidence_expiring_during_backup_cannot_stage_recovery(self):
        saved=self.plan(); evidence=self.evidence(saved)
        with mock.patch.object(recovery,'utcnow',side_effect=[self.now,self.now,self.now+dt.timedelta(seconds=121)]):
            with self.assertRaisesRegex(recovery.RecoveryRejected,'node_evidence_stale'):
                recovery.apply(saved,evidence,self.root/'backups','reviewer')
        self.assertEqual(self.log()['unknown_outcome'],1)
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],'')

    def test_additional_authorization_after_native_arm_is_rejected(self):
        saved=self.plan()
        with self.assertRaises(recovery.RecoveryRejected):self.apply(saved,stop_after='native_armed')
        with self.bridge.connect() as n:
            row=dict(n.execute('SELECT * FROM '+recovery.AUTH).fetchone())
            row.update(from_generation=2,to_generation=3)
            n.execute('INSERT INTO '+recovery.AUTH+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
        with self.assertRaisesRegex(recovery.RecoveryRejected,'authorization_changed'):self.apply(saved)
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'],recovery.HOLD)


if __name__ == '__main__': unittest.main()
