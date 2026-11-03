"""Offline read-only resolver and exact central-only recovery integration."""
import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

from features.x_posts.attribution import AttributionError, SCHEMA
from scripts import recover_x_attribution_failures as recovery
from scripts import test_x_prepost_recovery as fixture


class AttributionRecoveryTests(unittest.TestCase):
    verify=fixture.PrepostRecoveryTests.verify
    queue=fixture.PrepostRecoveryTests.queue
    operation=fixture.PrepostRecoveryTests.operation
    claim=fixture.PrepostRecoveryTests.claim

    def setUp(self):
        fixture.PrepostRecoveryTests.setUp(self)
        self.target=self.queue(identity=3,auto=True)
        self.store.offer(self.adapter.project(self.adapter.read(self.target['id'])))
        prep=self.claim('prepare');desc=self.bridge.reserve(prep,prep)
        receipt={'source_url':desc['request']['source_url'],'artifact':self.artifact}
        self.bridge.checkpoint(prep,prep,'x_preflight',receipt)
        self.store.checkpoint(prep,'attribute-preflight','x_preflight',receipt,canonical_verified=True)
        self.bridge.complete(prep,{'artifact':self.artifact},prep)
        self.store.complete(prep,'attribute-prep-complete',{'artifact':self.artifact,'canonical_receipt':'native-ready'},canonical_verified=True)
        self.store.offer(self.adapter.project(self.adapter.read(self.target['id'])))
        self.failed=self.claim('publish')
        with mock.patch.object(self.bridge,'_links',side_effect=AttributionError(recovery.ERROR)):
            with self.assertRaises(AttributionError):self.bridge.reserve(self.failed,self.failed)
        self.store.fail(self.failed,recovery.ERROR,canonical_no_write=True)
        with self.bridge.connect() as c:c.execute(SCHEMA)
        p={'job_id':self.failed['id'],'generation':self.failed['generation'],'error_code':recovery.ERROR,'unknown_outcome':False}
        rid=self.outboxes['cpu-main'].put('jobs/fail',p)
        with self.outboxes['cpu-main'].connect() as c:c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(rid,))
        self.now+=dt.timedelta(seconds=180)
        for name,value in [('QUEUE_IDS',(self.target['id'],)),('DAYS',tuple(self.days))]:
            patch=mock.patch.object(recovery,name,value);patch.start();self.addCleanup(patch.stop)

    def plan(self):return recovery.make_plan(self.central,self.runtime.POST_DB_PATH,[self.target['id']],self.now)

    def nodes(self,plan):return [recovery.node_evidence(plan,node,outbox.path,self.now) for node,outbox in self.outboxes.items()]

    def resolver(self,plan,**changes):
        with self.bridge.connect() as c:log=dict(c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(self.target['id'],)).fetchone())
        context={'version':'x-attribution-v2','channel_id':'21','sub_user_id':'42','af_c_id':str(self.target['id']),
                 'tenant':'tenant','owner':'owner','channel_name':'channel','material_id':'501','material_name':'video','content_id':'episode',
                 'language':'en','drama_name':'drama','tag':'tag','frozen_at':log['created_at']}
        context.update(changes)
        def opener(request,timeout):
            self.assertEqual(request.full_url,'http://127.0.0.1:8787/internal/x-posts/attribution/resolve')
            response=io.BytesIO(json.dumps({'context':context}).encode());response.status=200;return response
        return recovery.resolve_evidence(plan,'private-offline-token-'+'x'*32,self.now,opener)

    def apply(self,plan,resolution=None,nodes=None):
        return recovery.apply_plan(plan,nodes or self.nodes(plan),resolution or self.resolver(plan),self.root/'backups','offline-reviewer',self.now)

    def dump_native(self):
        with self.bridge.connect() as c:return '\n'.join(c.iterdump())

    def test_readonly_probe_and_apply_preserve_native_then_next_generation_reserves(self):
        before=self.dump_native();plan=self.plan();evidence=self.resolver(plan)
        self.assertTrue(evidence['items'][0]['valid']);self.assertEqual(self.dump_native(),before)
        result=self.apply(plan,evidence);self.assertFalse(result['native_modified']);self.assertFalse(result['platform_called'])
        self.assertEqual(self.dump_native(),before)
        job=self.store.get_job(self.failed['id']);self.assertEqual((job['status'],job['generation']),('ready',1))
        self.apply(plan,evidence)
        retry=self.claim('publish');self.assertEqual(retry['generation'],2)
        desc=self.bridge.reserve(retry,retry);self.assertEqual(desc['artifact'],self.artifact)
        with self.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT attempt_count FROM x_post_publish_log WHERE queue_id=?',(self.target['id'],)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_x_operation WHERE source_key=?',(retry['source_key'],)).fetchone()[0],0)
        with self.store.transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_event WHERE event_type=?',(recovery.EVENT,)).fetchone()[0],1)

    def test_resolver_identity_failure_or_stale_evidence_is_rejected(self):
        plan=self.plan();wrong=self.resolver(plan,af_c_id='999999')
        self.assertFalse(wrong['items'][0]['valid'])
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan,wrong)
        valid=self.resolver(plan);valid['captured_at']=(self.now-dt.timedelta(seconds=121)).isoformat()
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan,valid)
        self.assertEqual(self.store.get_job(self.failed['id'])['status'],'failed')

    def test_pending_or_unknown_receipt_blocks_even_without_native_post(self):
        plan=self.plan()
        rid=self.outboxes['hk-gpu'].put('jobs/fail',{'job_id':self.failed['id'],'generation':1,'unknown_outcome':True})
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        with self.outboxes['hk-gpu'].connect() as c:c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(rid,))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)

    def test_frozen_source_drift_or_attempt_cannot_be_rearmed(self):
        plan=self.plan()
        with self.bridge.connect() as c:c.execute("UPDATE x_post_queue SET description='changed' WHERE id=?",(self.target['id'],))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        with self.bridge.connect() as c:c.execute('UPDATE x_post_publish_log SET attempt_count=1 WHERE queue_id=?',(self.target['id'],))
        with self.assertRaises(recovery.RecoveryRejected):self.plan()

    def test_nonincident_queue_is_not_authorized(self):
        with self.assertRaises(recovery.RecoveryRejected):recovery.make_plan(self.central,self.runtime.POST_DB_PATH,[self.q['id']],self.now)
        plan=self.plan();plan['items'][0]['queue_id']=self.q['id'];plan['plan_sha256']=recovery.document_hash(plan)
        with self.assertRaises(recovery.RecoveryRejected):recovery.resolve_evidence(plan,'x'*32,self.now)
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)

    def test_additional_renderer_receipt_fields_preserved_but_artifact_change_rejected(self):
        with self.store.transaction() as c:
            prepared=c.execute("SELECT id,artifact_json FROM pipeline_job WHERE source_key=? AND stage='prepare'",(self.failed['source_key'],)).fetchone()
            artifact=json.loads(prepared['artifact_json']);artifact['output_size']=artifact['size']
            c.execute('UPDATE pipeline_job SET artifact_json=? WHERE id=?',(json.dumps(artifact),prepared['id']))
        plan=self.plan()
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_job SET artifact_json=? WHERE id=?',(json.dumps(dict(artifact,sha256='c'*64)),prepared['id']))
        with self.assertRaises(recovery.RecoveryRejected):self.plan()
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_job SET artifact_json=? WHERE id=?',(json.dumps(artifact),prepared['id']))
        self.apply(plan)
        with self.store.transaction() as c:
            self.assertEqual(json.loads(c.execute('SELECT artifact_json FROM pipeline_job WHERE id=?',(prepared['id'],)).fetchone()[0]),artifact)

    def test_historical_post_receipt_blocks_admission(self):
        plan=self.plan()
        rid=self.outboxes['hk-gpu'].put('jobs/complete',{'job_id':self.failed['id'],'generation':0,'result':{'post_id':'123'}})
        with self.outboxes['hk-gpu'].connect() as c:c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(rid,))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.store.get_job(self.failed['id'])['status'],'failed')

    def test_later_exact_incident_preserves_history_and_rejects_new_failure(self):
        job=self.store.get_job(self.failed['id'])
        with self.bridge.connect() as c:
            log_id=c.execute('SELECT id FROM x_post_publish_log WHERE queue_id=?',(self.target['id'],)).fetchone()[0]
        with self.store.transaction() as c:
            prep_id=c.execute("SELECT id FROM pipeline_job WHERE source_key=? AND stage='prepare'",(self.failed['source_key'],)).fetchone()[0]
            history=[tuple(row) for row in c.execute('SELECT * FROM pipeline_claim_history ORDER BY job_id,generation')]
        exact={'log_id':log_id,'prepare_job_id':prep_id,'publish_job_id':job['id'],
               'publish_generation':job['generation'],'finished_at':job['finished_at'],
               'account_id':self.target['account_id'],'manual_run_id':self.target['manual_run_id']}
        with mock.patch.object(recovery,'EXACT_FAILURES',{self.target['id']:exact}):
            before=self.dump_native();plan=self.plan()
            self.apply(plan)
            retry=self.claim('publish')
            self.store.fail(retry,recovery.ERROR,canonical_no_write=True)
            # Reapplying the exact prior plan cannot reset this later generation.
            self.apply(plan)
            self.assertEqual(self.store.get_job(job['id'])['status'],'failed')
            self.assertEqual(self.store.get_job(job['id'])['generation'],2)
            self.assertEqual(self.dump_native(),before)
            with self.assertRaisesRegex(recovery.RecoveryRejected,'exact_failure_identity_changed'):
                self.plan()
        with self.store.transaction() as c:
            current=[tuple(row) for row in c.execute('SELECT * FROM pipeline_claim_history ORDER BY job_id,generation')]
            self.assertTrue(all(row in current for row in history))

    def test_exact_failure_anchor_change_and_transaction_interruption_refuse_reset(self):
        plan=self.plan()
        with mock.patch.object(recovery,'EXACT_FAILURES',{self.target['id']:dict(recovery.EXACT_FAILURES[5451])}):
            with self.assertRaisesRegex(recovery.RecoveryRejected,'exact_failure_identity_changed'):
                self.apply(plan)
        before=self.dump_native();job=self.store.get_job(self.failed['id'])
        with self.store.transaction() as c:
            c.execute("CREATE TRIGGER interrupt_attribution_audit BEFORE INSERT ON pipeline_event "
                      "WHEN NEW.event_type='"+recovery.EVENT+"' BEGIN SELECT RAISE(ABORT,'interrupt'); END")
        with self.assertRaisesRegex(Exception,'interrupt'):
            self.apply(plan)
        self.assertEqual(self.store.get_job(self.failed['id']),job)
        self.assertEqual(self.dump_native(),before)
        with self.store.transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_event WHERE event_type=?',(recovery.EVENT,)).fetchone()[0],0)
            c.execute('DROP TRIGGER interrupt_attribution_audit')
        self.apply(plan)


if __name__=='__main__':unittest.main()
