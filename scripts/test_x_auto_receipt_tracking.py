"""Rearm observation without choosing material or issuing any publish request."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock
from datetime import timedelta

from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import SCHEMA, encoded
from scripts import rearm_x_auto_receipt_tracking as recovery
from scripts import test_x_auto_receipt_reconciliation as receipts
from scripts import test_x_auto_post_publisher as publisher


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.base = receipts.ReceiptReconciliationTests(); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture; self.store = self.base.store
        self.native = self.base.native; self.root = self.native.parent
        self.central = self.root/'central.sqlite3'; self.task_id = self.base.task.id
        with self.store._transaction() as a:
            a.execute('UPDATE x_auto_task SET unknown_outcome=0')
            a.execute("UPDATE x_auto_run SET shanghai_date='2026-11-03'")
        with sqlite3.connect(self.native) as n:
            n.row_factory = sqlite3.Row
            n.execute("UPDATE x_post_queue SET status='queued'")
            n.execute("UPDATE x_post_publish_log SET status='reserved',x_post_id='',x_post_url='',published_at=''")
            n.execute('CREATE TABLE pipeline_canonical_hold(source_key TEXT PRIMARY KEY)')
            n.execute(SCHEMA)
            queue = dict(n.execute('SELECT * FROM x_post_queue').fetchone())
            self.prior = {'queue_id':9002,'source_key':'x:queue:9002','prior_log':{'id':9003,'account_id':640},'job_id':31}
            self.plan_sha = sorted(recovery.PREPOST_PLANS)[0]
            n.execute('INSERT INTO '+recovery.AUTH+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('x:queue:9002','publish',1,2,1,recovery.queue_hash(queue),recovery.log_hash(dict(n.execute('SELECT * FROM x_post_publish_log').fetchone())),'a',self.plan_sha,'armed','reviewer','2026-11-03T10:47:53Z','',encoded(self.prior)))
        with sqlite3.connect(self.central) as c:
            c.executescript('''CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,channel TEXT,family TEXT,source_id TEXT,stage TEXT,source_meta_json TEXT,status TEXT,hold_reason TEXT);
                CREATE TABLE pipeline_event(id INTEGER PRIMARY KEY,job_id INTEGER,event_type TEXT,details_json TEXT,created_at TEXT);
                CREATE TABLE pipeline_x_prepost_recovery_apply(source_key TEXT,stage TEXT,from_generation INTEGER,plan_sha256 TEXT,state TEXT,proof_json TEXT,created_at TEXT,updated_at TEXT);''')
            c.execute('INSERT INTO pipeline_job VALUES(31,?,?,?,?,?,?,?,?)',('x:queue:9002','x','queue','9002','publish',encoded({'canonical_fingerprint':canonical_fingerprint(queue,'id')}),'ready',''))
            c.execute('INSERT INTO '+recovery.PREPOST+' VALUES(?,?,?,?,?,?,?,?)',('x:queue:9002','publish',1,self.plan_sha,'committed',encoded(self.prior),'2026-11-03T10:47:53Z','2026-11-03T10:47:53Z'))
        self.runtime = {'dispatch_enabled':True,'pid':123,'auto_db':str(Path(self.store.db_path).resolve())}
        for name, value in (('runtime_proof',lambda _: copy.deepcopy(self.runtime)),('utcnow',self.fixture.clock)):
            patcher = mock.patch.object(recovery,name,value); patcher.start(); self.addCleanup(patcher.stop)

    def plan(self):
        return recovery.make_plan(self.store.db_path,self.native,self.central,[self.task_id])

    def apply(self, saved):
        return recovery.apply(saved,self.root/'backups','reviewer')

    def test_observes_original_queue_then_receipt_without_publish_or_reselection(self):
        saved = self.plan(); self.assertEqual(len(saved['items']),1)
        native, central = self.native.read_bytes(),self.central.read_bytes()
        result = self.apply(saved); task = self.store.get_task(self.task_id)
        self.assertEqual(result['changed_task_ids'],[self.task_id]);self.assertEqual(result['publish_requests'],0)
        self.assertEqual(task.status,'retry_wait');self.assertEqual(task.execution_queue_id,9002)
        self.assertEqual(task.execution_run_id,9001);self.assertIsNone(task.execution_log_id)
        self.assertEqual(task.error_code,'x_auto_publish_outcome_unknown')
        with self.store._reader() as a:
            self.assertEqual(a.execute('SELECT publish_attempt_count FROM x_auto_task WHERE id=?',(self.task_id,)).fetchone()[0],saved['items'][0]['task']['publish_attempt_count'])
        self.assertEqual(self.store.get_run(task.run_id).status,'running')
        self.assertEqual((self.native.read_bytes(),self.central.read_bytes()),(native,central))
        with self.store._reader() as a:
            audit=json.loads(a.execute('SELECT evidence_json FROM '+recovery.AUDIT).fetchone()[0])
            self.assertEqual(audit['previous']['task'],saved['items'][0]['task'])
            self.assertEqual(audit['previous']['run']['status'],'failed')
        bridge=publisher.FakeBridge(task)
        forbidden=lambda *args,**kwargs:self.fail('observation must not publish, select, recover or record a failure')
        bridge.publish_queue=forbidden;bridge.recover_run=forbidden;bridge.record_failure=forbidden
        executor=self.fixture._executor(bridge);executor.dispatch_enabled=True
        executor.candidate_loader=forbidden;executor.media_preflight=forbidden
        first=executor.execute_next('observer');self.assertEqual(first['task']['status'],'retry_wait')
        bridge.queue_status='published';self.fixture.clock.value+=timedelta(seconds=31)
        second=executor.execute_next('observer');self.assertEqual(second['task']['status'],'published')
        self.assertEqual(second['task']['publish_id'],'1900000000000000000')
        self.assertEqual(self.apply(saved)['already_applied_task_ids'],[self.task_id])
        self.assertEqual(self.store.get_task(self.task_id).status,'published')

    def test_unknown_unrestored_active_or_cancelled_task_is_skipped(self):
        for sql in ("UPDATE x_auto_task SET unknown_outcome=1", "UPDATE x_auto_task SET claim_token='active'",
                    "UPDATE x_auto_run SET status='canceled'"):
            with self.subTest(sql=sql):
                with self.store._transaction() as a:
                    a.execute(sql)
                self.assertFalse(self.plan()['items'])
                with self.store._transaction() as a:
                    a.execute("UPDATE x_auto_task SET unknown_outcome=0,claim_token=''")
                    a.execute("UPDATE x_auto_run SET status='failed'")
        with sqlite3.connect(self.central) as c:c.execute("UPDATE "+recovery.PREPOST+" SET state='staged'")
        self.assertFalse(self.plan()['items'])

    def test_other_recovery_plan_not_in_this_incident_is_rejected(self):
        with sqlite3.connect(self.central) as c:c.execute('UPDATE '+recovery.PREPOST+' SET plan_sha256=?',('f'*64,))
        self.assertEqual(self.plan()['skipped'][0]['reason'],'exact_incident_recovery_required')

    def test_identity_mismatch_unknown_or_missing_authority_is_rejected(self):
        baseline=self.native.read_bytes()
        for sql in ("UPDATE x_post_queue SET material_id='another'", "UPDATE x_post_manual_run SET external_task_key='other'",
                    "UPDATE x_post_publish_log SET account_id=999", "UPDATE x_post_publish_log SET unknown_outcome=1",
                    'DELETE FROM '+recovery.AUTH):
            with self.subTest(sql=sql):
                with sqlite3.connect(self.native) as n:n.execute(sql)
                self.assertFalse(self.plan()['items'])
                self.native.write_bytes(baseline)

    def test_auto_or_runtime_drift_aborts_before_writes(self):
        saved=self.plan()
        with self.store._transaction() as a:a.execute("UPDATE x_auto_task SET error_code='new-error'")
        with self.assertRaisesRegex(recovery.RecoveryRejected,'plan_state_drift_task'):self.apply(saved)
        self.assertEqual(self.store.get_task(self.task_id).status,'failed')
        self.runtime['pid']=999
        with self.assertRaisesRegex(recovery.RecoveryRejected,'runtime_changed'):self.apply(saved)

    def test_new_success_skips_rearm_for_receipt_helper(self):
        saved=self.plan()
        with sqlite3.connect(self.native) as n:n.execute("UPDATE x_post_queue SET status='published'")
        result=self.apply(saved)
        self.assertFalse(result['changed_task_ids']);self.assertEqual(self.store.get_task(self.task_id).status,'failed')
        self.assertEqual(result['skipped'][0]['reason'],'native_published_use_receipt_reconciliation')

    def test_single_auto_transaction_rolls_back_audit_task_and_parent(self):
        saved=self.plan()
        with self.store._transaction() as a:
            a.execute("CREATE TRIGGER block_resume BEFORE UPDATE ON x_auto_material_ledger BEGIN SELECT RAISE(ABORT,'test_rollback'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'test_rollback'):self.apply(saved)
        self.assertEqual(self.store.get_task(self.task_id).status,'failed')
        self.assertEqual(self.store.get_run(self.base.task.run_id).status,'failed')
        with self.store._reader() as a:self.assertFalse(recovery.exists(a,recovery.AUDIT))

    def test_audit_is_append_only_and_does_not_reset_later_failure(self):
        saved=self.plan();self.apply(saved)
        with self.store._transaction() as a:
            for sql in ('DELETE FROM '+recovery.AUDIT,'UPDATE '+recovery.AUDIT+" SET evidence_json='{}'",'INSERT OR REPLACE INTO '+recovery.AUDIT+' SELECT * FROM '+recovery.AUDIT):
                with self.assertRaisesRegex(sqlite3.IntegrityError,'append_only'):a.execute(sql)
            a.execute("UPDATE x_auto_task SET status='failed',error_code='new-failure'")
        self.apply(saved)
        self.assertEqual(self.store.get_task(self.task_id).error_code,'new-failure')
        self.assertEqual(self.store.get_task(self.task_id).status,'failed')

    def test_attribution_audit_also_restores_only_observation(self):
        with sqlite3.connect(self.central) as c:
            c.execute('DELETE FROM '+recovery.PREPOST)
            proof={'plan_sha256':sorted(recovery.ATTRIBUTION_PLANS)[0], 'resolver_context_sha256':'a'*64,
                'previous_execution':{'id':31,'source_key':'x:queue:9002','status':'failed','error_code':'x_attribution_resolution_unavailable','external_intent':0}}
            c.execute('INSERT INTO pipeline_event VALUES(1,31,?,?,?)',('audited_zero_write_attribution_recovery',encoded(proof),'2026-11-03T10:50:52Z'))
        saved=self.plan();self.assertEqual(saved['items'][0]['authority']['kind'],'attribution')
        self.assertEqual(self.apply(saved)['changed_task_ids'],[self.task_id])

    def test_native_upload_progress_does_not_require_stopping_workers(self):
        saved=self.plan()
        with sqlite3.connect(self.native) as n:
            n.execute("UPDATE x_post_queue SET status='publishing'")
            n.execute("UPDATE x_post_publish_log SET status='media_uploading',attempt_count=2")
            n.execute("UPDATE "+recovery.AUTH+" SET state='consumed',started_at='2026-11-03T12:00:00Z'")
        self.assertEqual(self.apply(saved)['changed_task_ids'],[self.task_id])

    def test_multiple_tasks_same_parent_are_updated_in_one_transaction(self):
        with self.store._transaction() as a:
            columns=[r[1] for r in a.execute('PRAGMA table_info(x_auto_task)')]
            task=dict(a.execute('SELECT * FROM x_auto_task WHERE id=?',(self.task_id,)).fetchone())
            task.update(id=self.task_id+1,account_id='641',material_id='another',content_id='DramaTwo',execution_run_id=9005,execution_queue_id=9006)
            a.execute('INSERT INTO x_auto_task('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[task[k] for k in columns])
            ledger=dict(a.execute('SELECT * FROM x_auto_material_ledger WHERE task_id=?',(self.task_id,)).fetchone())
            ledger.update(material_id='another',content_id='DramaTwo',task_id=self.task_id+1,canonical_queue_id=9006)
            if 'id' in ledger:ledger['id']+=1
            a.execute('INSERT INTO x_auto_material_ledger('+','.join(ledger)+') VALUES('+','.join('?' for _ in ledger)+')',list(ledger.values()))
        with sqlite3.connect(self.native) as n:
            n.row_factory=sqlite3.Row
            q=dict(n.execute('SELECT * FROM x_post_queue').fetchone());q.update(id=9006,manual_run_id=9005,account_id=641,material_id='another',content_id='DramaTwo')
            n.execute('INSERT INTO x_post_queue VALUES('+','.join('?' for _ in q)+')',list(q.values()))
            n.execute("INSERT INTO x_post_publish_log VALUES(9007,9006,641,'reserved',0,'','','',1)")
            n.execute("INSERT INTO x_post_manual_run SELECT 9005,trigger_source,?,template_ref,template_version,'[641]','[\"another\"]',body_template FROM x_post_manual_run",('x-auto-task-'+str(self.task_id+1),))
            proof=dict(self.prior,queue_id=9006,source_key='x:queue:9006',prior_log={'id':9007,'account_id':641},job_id=32)
            auth=dict(n.execute('SELECT * FROM '+recovery.AUTH).fetchone());auth.update(source_key='x:queue:9006',proof_json=encoded(proof),queue_sha256=recovery.queue_hash(q),log_sha256=recovery.log_hash(dict(n.execute('SELECT * FROM x_post_publish_log WHERE id=9007').fetchone())))
            n.execute('INSERT INTO '+recovery.AUTH+' VALUES('+','.join('?' for _ in auth)+')',list(auth.values()))
        with sqlite3.connect(self.central) as c:
            c.execute('INSERT INTO pipeline_job VALUES(32,?,?,?,?,?,?,?,?)',('x:queue:9006','x','queue','9006','publish',encoded({'canonical_fingerprint':canonical_fingerprint(q,'id')}),'ready',''))
            c.execute('INSERT INTO '+recovery.PREPOST+' SELECT ?,stage,from_generation,plan_sha256,state,?,created_at,updated_at FROM '+recovery.PREPOST,('x:queue:9006',encoded(proof)))
        saved=recovery.make_plan(self.store.db_path,self.native,self.central,[self.task_id,self.task_id+1])
        self.assertEqual(len(saved['items']),2)
        self.assertEqual(self.apply(saved)['changed_task_ids'],[self.task_id,self.task_id+1])
        with self.store._reader() as a:
            self.assertEqual(a.execute('SELECT count(*) FROM '+recovery.AUDIT).fetchone()[0],2)
            self.assertEqual(a.execute("SELECT count(*) FROM x_auto_event WHERE event_type='run_receipt_tracking_resumed'").fetchone()[0],1)


class RuntimeProofTests(unittest.TestCase):
    def test_reads_actual_process_env_and_reviewed_files_not_cli_boolean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);proc=root/'123';proc.mkdir();cwd=proc/'cwd';cwd.mkdir()
            (cwd/'reviewed.py').write_text('approved code')
            files={'reviewed.py':{hashlib.sha256((cwd/'reviewed.py').read_bytes()).hexdigest()}}
            (proc/'stat').write_text('123 (python3) '+' '.join(['S']+['0']*18+['12345']))
            (proc/'cmdline').write_bytes(b'/usr/bin/python3\0/opt/x-post-automation/current/scripts/x_auto_post_service.py\0')
            auto=root/'auto.sqlite3';auto.touch()
            env=('PIPELINE_X_DISPATCH_ENABLED=1\0PIPELINE_X_CANONICAL_BRIDGE_ENABLED=1\0X_AUTO_POST_DB_PATH='+str(auto)+'\0SECRET=not-returned\0').encode()
            (proc/'environ').write_bytes(env)
            with mock.patch.object(recovery,'main_pid',return_value='123'),mock.patch.object(recovery,'RUNTIME_HASHES',files):
                proof=recovery.runtime_proof(auto,root);self.assertTrue(proof['dispatch_enabled'])
                self.assertNotIn('SECRET',encoded(proof));self.assertNotIn('not-returned',encoded(proof))
                (proc/'environ').write_bytes(env.replace(b'DISPATCH_ENABLED=1',b'DISPATCH_ENABLED=0'))
                with self.assertRaisesRegex(recovery.RecoveryRejected,'dispatch_runtime_not_enabled'):recovery.runtime_proof(auto,root)
                (proc/'environ').write_bytes(env);(cwd/'reviewed.py').write_text('changed code')
                with self.assertRaisesRegex(recovery.RecoveryRejected,'runtime_code_not_reviewed'):recovery.runtime_proof(auto,root)


class Exact5524ObservationTests(unittest.TestCase):
    def setUp(self):
        self.case=TrackingTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        with self.case.store._transaction() as a:
            a.execute('UPDATE x_auto_task SET unknown_outcome=1')
            task=dict(a.execute('SELECT * FROM x_auto_task').fetchone())
        with sqlite3.connect(self.case.native) as n:
            n.row_factory=sqlite3.Row
            queue=dict(n.execute('SELECT * FROM x_post_queue').fetchone())
            log=dict(n.execute('SELECT * FROM x_post_publish_log').fetchone());log.update(unknown_outcome=1,status='failed')
        original={'kind':'x_5524_verified_no_post','version':1,'proof':{'queue':queue,'log':log}}
        original['plan_sha256']=recovery.document_hash(original)
        self.original=original
        for name,value in (('SPECIAL_TASK', {k:task[k] for k in recovery.SPECIAL_TASK}),
                           ('SPECIAL_TASK_SHA',recovery.digest(task)),('SPECIAL_PLAN_SHA',original['plan_sha256']),('SPECIAL_JOB_ID',31)):
            p=mock.patch.object(recovery,name,value);p.start();self.addCleanup(p.stop)
        for path in (self.case.native,self.case.central):
            with sqlite3.connect(path) as c:
                c.execute('CREATE TABLE '+recovery.SPECIAL_AUDIT+' (job_id INTEGER,plan_sha256 TEXT,evidence_json TEXT,created_at TEXT)')
                c.execute('INSERT INTO '+recovery.SPECIAL_AUDIT+' VALUES(31,?,?,?)',(original['plan_sha256'],encoded({'plan':original}),'2026-11-03T11:59:00Z'))
        with sqlite3.connect(self.case.central) as c:
            c.execute('CREATE TABLE '+recovery.SPECIAL_JOURNAL+' (job_id INTEGER,plan_sha256 TEXT,state TEXT)')
            c.execute('INSERT INTO '+recovery.SPECIAL_JOURNAL+" VALUES(31,?,'committed')",(original['plan_sha256'],))
        with sqlite3.connect(self.case.native) as n:
            n.execute('UPDATE '+recovery.AUTH+' SET plan_sha256=?,proof_json=?',(original['plan_sha256'],encoded({'reconciliation':recovery.SPECIAL_AUDIT,'original_proof':original['proof']})))

    def test_preserves_unknown_until_unique_success_without_any_publish_call(self):
        saved=self.case.plan();self.assertEqual(saved['items'][0]['authority']['kind'],'5524_reconciled_unknown_observation')
        self.case.apply(saved);task=self.case.store.get_task(self.case.task_id)
        self.assertTrue(task.unknown_outcome);self.assertEqual(task.status,'retry_wait')
        bridge=publisher.FakeBridge(task)
        forbidden=lambda *args,**kwargs:self.fail('only read-only receipt observation is authorized')
        bridge.publish_queue=forbidden;bridge.recover_run=forbidden;bridge.record_failure=forbidden
        executor=self.case.fixture._executor(bridge);executor.dispatch_enabled=True
        first=executor.execute_next('observer');self.assertEqual(first['task']['status'],'retry_wait')
        self.assertTrue(first['task']['unknown_outcome']);self.assertEqual(first['task']['execution_queue_id'],9002)
        bridge.queue_status='published';self.case.fixture.clock.value+=timedelta(seconds=31)
        result=executor.execute_next('observer');self.assertEqual(result['task']['status'],'published')
        self.assertFalse(result['task']['unknown_outcome']);self.assertEqual(result['task']['publish_id'],'1900000000000000000')
        self.assertEqual(bridge.publish_calls,0);self.assertEqual(bridge.record_calls,0)
        self.case.apply(saved);self.assertEqual(self.case.store.get_task(self.case.task_id).status,'published')

    def test_missing_native_audit_or_uncommitted_central_record_rejects(self):
        with sqlite3.connect(self.case.central) as c:c.execute("UPDATE "+recovery.SPECIAL_JOURNAL+" SET state='staged'")
        self.assertFalse(self.case.plan()['items'])
        with sqlite3.connect(self.case.central) as c:c.execute("UPDATE "+recovery.SPECIAL_JOURNAL+" SET state='committed'")
        with sqlite3.connect(self.case.native) as n:n.execute('DELETE FROM '+recovery.SPECIAL_AUDIT)
        self.assertFalse(self.case.plan()['items'])

    def test_auto_snapshot_drift_or_new_native_unknown_remains_isolated(self):
        saved=self.case.plan()
        with self.case.store._transaction() as a:a.execute("UPDATE x_auto_task SET error_message='new unknown'")
        self.assertFalse(self.case.plan()['items'])
        with self.assertRaises(recovery.RecoveryRejected):self.case.apply(saved)
        with self.case.store._transaction() as a:
            a.execute('UPDATE x_auto_task SET error_message=?',(saved['items'][0]['task']['error_message'],))
        with sqlite3.connect(self.case.native) as n:n.execute('UPDATE x_post_publish_log SET unknown_outcome=1')
        self.assertFalse(self.case.plan()['items'])
        self.assertTrue(self.case.store.get_task(self.case.task_id).unknown_outcome)

    def test_reconciliation_plan_hash_cannot_be_substituted(self):
        with sqlite3.connect(self.case.native) as n:n.execute('UPDATE '+recovery.AUTH+' SET plan_sha256=?',('f'*64,))
        self.assertFalse(self.case.plan()['items'])


if __name__ == '__main__': unittest.main()
