"""Fixed incident contracts share crash-safe recovery without widening unknowns."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from features.pipeline_dispatch.x_prepost_recovery import digest, encoded, queue_hash
from scripts import reconcile_x_5524_prepost as legacy
from scripts import reconcile_x_audited_media_only as recovery
from scripts import test_x_5524_prepost_reconciliation as fixture
from scripts.x_media_only_reconciliation import AuditedMediaOnlyReconciler


class FixedIncidentTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ReconciliationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.contract=copy.deepcopy(vars(legacy._engine().c))
        self.contract.update(PLAN_KIND=recovery.KIND,AUDIT=recovery.AUDIT,JOURNAL=recovery.JOURNAL,
            HOLD='operator_media_only_verified_prepost_pending',EVENT='audited_media_only_verified_no_post',
            AUDIT_ERROR='audited_media_append_only',INCIDENT_SHA='fixture-reviewed-contract')
        self.engine=AuditedMediaOnlyReconciler(self.contract)

    def plan(self):
        return self.engine.plan(self.f.central,self.f.runtime.POST_DB_PATH,self.f.now)

    def evidence(self,saved):
        return [self.engine.node_evidence(saved,node,outbox.path,self.f.now,
            history_dir=self.f.history_dir,release_root=self.f.release_root) for node,outbox in self.f.outboxes.items()]

    def apply(self,saved,**kwargs):
        return self.engine.apply(saved,self.evidence(saved),self.f.root/'backups','offline-reviewer',self.f.now,**kwargs)

    def test_shared_engine_preserves_unknown_and_is_crash_safe_and_idempotent(self):
        saved=self.plan();old=copy.deepcopy(saved['proof'])
        self.assertEqual(saved['incident_sha256'],'fixture-reviewed-contract')
        with self.assertRaisesRegex(legacy.RecoveryRejected,'native_arm'):self.apply(saved,stop_after='native_armed')
        self.assertEqual(self.f.store.get_job(self.f.pub['id'])['status'],'unknown')
        self.assertEqual(self.f.log()['attempt_count'],1)
        self.apply(saved)
        with self.f.bridge.connect() as n,self.f.store.transaction() as c:
            for conn in (c,n):
                audit=json.loads(conn.execute('SELECT evidence_json FROM '+recovery.AUDIT).fetchone()[0])
                self.assertEqual(audit['plan']['proof'],old)
                self.assertEqual(audit['plan']['proof']['log']['unknown_outcome'],1)
                with self.assertRaisesRegex(sqlite3.IntegrityError,'append_only'):
                    conn.execute('INSERT OR REPLACE INTO '+recovery.AUDIT+' SELECT * FROM '+recovery.AUDIT)
        self.assertTrue(self.apply(saved)['already_applied'])

    def test_other_queue_contract_or_manifest_cannot_reuse_plan(self):
        saved=self.plan()
        for changed in ({'QID':99999},{'INCIDENT_SHA':'another-review'}):
            other=AuditedMediaOnlyReconciler(dict(self.contract,**changed))
            with self.assertRaises(legacy.RecoveryRejected):other.validate_plan(saved)
        with self.assertRaisesRegex(legacy.RecoveryRejected,'queue_not_in'):recovery.contract(5452)
        path=self.f.root/'unreviewed.json';path.write_text(json.dumps({'version':1,'items':[]}))
        with mock.patch.object(recovery,'MANIFEST_PATH',path):
            with self.assertRaisesRegex(legacy.RecoveryRejected,'manifest_changed'):recovery.incidents()

    def test_real_manifest_has_exact_nine_scopes_and_both_node_receipt_sets(self):
        items=recovery.incidents()
        self.assertEqual(set(items),{5485,5487,5501,5508,5532,5534,5522,5523,5459})
        self.assertTrue(all(sum(map(len,i['RECEIPTS_BY_NODE'].values()))==4 for i in items.values()))
        self.assertEqual(items[5459]['PUBLISH_NODE'],'cpu-main')
        self.assertEqual({n:len(v) for n,v in items[5459]['RECEIPTS_BY_NODE'].items()},{'cpu-main':2,'hk-gpu':2})
        self.assertTrue(all(i['PREP_GENERATION']==1 and i['OP_COUNT']>0 for i in items.values()))
        self.assertEqual(len({i['AUTO_TASK_ID'] for i in items.values()}),9)

    def test_cpu_failure_with_gpu_prepare_receipt_uses_correct_worker_and_error(self):
        error='coordinator_operation_failed';pub=self.f.pub['id']
        payload=dict(self.f.payload,node_id='cpu-main',error_code=error)
        with self.f.outboxes['hk-gpu'].connect() as o:o.execute('DELETE FROM receipt_outbox')
        self.f.outboxes['cpu-main'].put('jobs/fail',payload,legacy.RECEIPT_ID)
        prep={'job_id':self.f.prep['id'],'generation':1,'phase':'x_preflight','worker_id':'prepare-worker'}
        self.f.outboxes['hk-gpu'].put('jobs/checkpoint',prep,'prepare-checkpoint')
        for outbox in self.f.outboxes.values():
            with outbox.connect() as o:o.execute('UPDATE receipt_outbox SET acked_at=1')
        with self.f.bridge.connect() as n,self.f.store.transaction() as c:
            n.execute('UPDATE x_post_publish_log SET error_code=?,error_message=?',(error,error))
            c.execute('UPDATE pipeline_job SET node_id=?,error_code=? WHERE id=?',('cpu-main',error,pub))
            c.execute('UPDATE pipeline_claim_history SET node_id=? WHERE job_id=?',('cpu-main',pub))
            c.execute('UPDATE pipeline_operation SET node_id=? WHERE job_id=?',('cpu-main',pub))
            c.execute("UPDATE pipeline_event SET details_json=? WHERE job_id=? AND event_type='failed'",(encoded({'code':error,'unknown':True}),pub))
            c.execute('UPDATE pipeline_delivery SET request_sha256=? WHERE delivery_id=?',(digest(payload),legacy.RECEIPT_ID))
            proof=self.engine.collect(c,n)
        expected={k:digest(proof[k]) for k in ('log','native_ops','central_ops','claims','history')};expected['queue']=queue_hash(proof['queue'])
        records={}
        for node,outbox in self.f.outboxes.items():
            records[node]={}
            with outbox.connect() as o:
                o.row_factory=sqlite3.Row
                for r in o.execute('SELECT id,route,payload FROM receipt_outbox'):
                    raw=json.loads(r['payload']);safe=dict(raw)
                    if 'claim_token' in safe:safe['claim_token_sha256']=hashlib.sha256(safe.pop('claim_token').encode()).hexdigest()
                    records[node][r['id']]=[r['route'],digest(raw),digest(safe)]
        audit={'previous_sha':self.contract['RELEASE'],'children_before':[str(self.contract['WORKER_PID'])]}
        (self.f.history_dir/'audit.before.json').write_text(json.dumps(audit))
        self.contract.update(PUBLISH_NODE='cpu-main',ERROR=error,EXPECTED=expected,RECEIPT_SHA=digest(payload),RECEIPTS_BY_NODE=records,
            HISTORY_AUDIT_FILE='audit.before.json',HISTORY_FILES={'audit.before.json':hashlib.sha256((self.f.history_dir/'audit.before.json').read_bytes()).hexdigest()})
        self.engine=AuditedMediaOnlyReconciler(self.contract)
        saved=self.plan();evidence=self.evidence(saved)
        self.assertIn('historical_worker',next(e for e in evidence if e['node_id']=='cpu-main'))
        self.assertNotIn('historical_worker',next(e for e in evidence if e['node_id']=='hk-gpu'))
        self.apply(saved);self.assertEqual(self.f.store.get_job(pub)['status'],'ready')

    def test_rgba_runtime_and_integer_child_pid_binding_is_explicit(self):
        audit={'runtime_sha':self.contract['RELEASE'],'child_pids_before':[self.contract['WORKER_PID']]}
        path=self.f.history_dir/'audit.json';path.write_text(json.dumps(audit))
        self.contract.update(HISTORY_RELEASE_KEY='runtime_sha',HISTORY_CHILDREN_KEY='child_pids_before',
            HISTORY_FILES={'audit.json':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.engine=AuditedMediaOnlyReconciler(self.contract)
        self.apply(self.plan());self.assertEqual(self.f.log()['attempt_count'],1)
        self.contract['WORKER_PID']+=1;other=AuditedMediaOnlyReconciler(self.contract)
        with self.assertRaisesRegex(legacy.RecoveryRejected,'worker_pid_release_not_bound'):
            other.historical_worker(self.f.history_dir,self.f.release_root)

    def test_committed_origin_must_match_reviewed_hashes_for_later_observation(self):
        saved=self.plan();self.engine.validate_saved_origin(saved)
        forged=copy.deepcopy(saved);forged['proof']['log']['unknown_outcome']=0
        forged['plan_sha256']=legacy.document_hash(forged)
        with self.assertRaisesRegex(legacy.RecoveryRejected,'original_log_changed'):self.engine.validate_saved_origin(forged)


if __name__=='__main__':unittest.main()
