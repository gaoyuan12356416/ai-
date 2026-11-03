"""Legacy proof is native initialize rejection, never fabricated dispatch history."""
import contextlib
import copy
import datetime as dt
import hashlib
import json
import unittest
from unittest import mock

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec, AdapterError
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch.receipt_outbox import ReceiptOutbox
from features.x_posts.random_template_store import PreparationStore
from scripts import recover_x_legacy_initialize_5280 as recovery
from scripts import test_pipeline_x_native as native


class LegacyInitializeRecoveryTests(unittest.TestCase):
    verify = native.NativeTests.verify
    queue = native.NativeTests.queue
    operation = native.NativeTests.operation

    def setUp(self):
        native.NativeTests.setUp(self)
        self.now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=4)
        self.central = self.root / 'central.sqlite'
        self.store = DispatchStore(self.central, lambda: self.now)
        self.store.channel_enabled('x', True)
        self.caps = ['publish:x', 'prepare:x', 'backend:cpu']
        self.adapter = LedgerAdapter(LedgerSpec('x', 'queue', str(self.runtime.POST_DB_PATH), 'x_post_queue'), self.bridge)
        self.outboxes = {}
        for node in ('cpu-main', 'hk-gpu'):
            self.store.register_node(node, self.caps)
            self.outboxes[node] = ReceiptOutbox(self.root / (node + '.sqlite'))
        q = self.queue(auto=True, random=True)
        with self.bridge.connect() as c:
            c.execute("UPDATE x_post_queue SET media_validation_mode='preflight',preflight_sha256=?,preflight_size=4096,preflight_duration=30 WHERE id=?", ('c'*64,q['id']))
            c.execute('CREATE TABLE x_authorized_account(id INTEGER PRIMARY KEY,x_user_id TEXT,username TEXT,status TEXT,publish_approved INTEGER)')
            c.execute("INSERT INTO x_authorized_account VALUES(2,'2',?,'active',1)", (q['account_username'],))
        q = self.bridge.store.get_queue(q['id'])
        self.q = q
        request = dict(job_key='x-random-v1-'+'d'*64,queue_id=q['id'],content_id=str(q['content_id']),
                       source_url=q['material_url'],source_sha256=q['preflight_sha256'],source_size=q['preflight_size'],
                       source_duration=q['preflight_duration'],profile='x-random-overlay-h264-720x1280-v1',
                       asset_manifest_sha256='a'*64,max_duration_seconds=140,duration_policy_version=q['duration_policy_version'])
        result = dict(request, output_url='https://media.example.test/original.mp4',output_sha256='b'*64,
                      output_size=2048,probe={'duration':30},recipe={'asset_set_sha256':'a'*64,'chosen':'original'})
        prep = PreparationStore(self.runtime.POST_DB_PATH)
        prep.freeze(q['id'],request);prep.complete(q['id'],result)
        self.artifact = recovery.artifact_from_receipt(request,result)
        log = self.bridge.store.reserve_log(q['id'])
        self.bridge._links(q,log)
        self.bridge.store.mark_publishing(log['id'])
        error = 'X媒体初始化失败(HTTP 403): Your account is temporarily locked.'
        self.bridge.store.mark_failed(log['id'],'x_upstream_error',error,unknown_outcome=False)
        # The fixture models the actual pre-dispatch native path: no pipeline
        # claim/operation/outbox is created to manufacture stronger evidence.
        self.old_offer = self.adapter.project(self.adapter.read(q['id']))
        self.old_offer['execution_available'] = False
        self.old_offer['hold_reason'] = 'adapter_unavailable'
        self.old_offer['artifact'] = {k:v for k,v in self.artifact.items() if k!='recipe'}
        self.old_offer['artifact']['duration_seconds'] = 0.0
        self.job = self.store.offer(self.old_offer)
        log = self.log()
        self.exact = dict(queue_id=q['id'],log_id=log['id'],account_id=2,username=q['account_username'],x_user_id='2',
                          manual_run_id=q['manual_run_id'],job_id=self.job['id'],run_date=q['run_date'],
                          failed_at=log['updated_at'],error_sha256=hashlib.sha256(error.encode()).hexdigest(),
                          artifact_sha256='b'*64,artifact_size=2048,artifact_duration=30)
        patch = mock.patch.object(recovery,'EXACT',self.exact);patch.start();self.addCleanup(patch.stop)

    def log(self):
        with self.bridge.connect() as c:
            return dict(c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(self.q['id'],)).fetchone())

    def plan(self):
        return recovery.make_plan(self.central,self.runtime.POST_DB_PATH,self.now)

    def evidence(self,plan):
        return [recovery.node_evidence(plan,node,outbox.path,self.now) for node,outbox in self.outboxes.items()]

    def apply(self,plan,**kwargs):
        return recovery.apply_plan(plan,self.evidence(plan),self.root/'backups','offline-reviewer',confirm_account_unlocked=True,now=self.now,**kwargs)

    def claim(self):
        self.store.register_node('cpu-main',self.caps)
        self.store.worker_heartbeat('cpu-main','publish-worker','publish')
        result = self.store.claim('cpu-main','publish-worker','publish')['job']
        if result:result['executor_backend']='cpu'
        return result

    def test_real_next_claim_preserves_original_then_counts_attempt_two_once(self):
        plan=self.plan();old=self.log()
        self.assertEqual(plan['item']['from_generation'],0)
        self.apply(plan)
        armed=self.log()
        self.assertEqual({k:v for k,v in armed.items() if k not in ('status','updated_at')},
                         {k:v for k,v in old.items() if k not in ('status','updated_at')})
        with self.bridge.connect() as c:
            for table in ('pipeline_x_claim','pipeline_x_operation','pipeline_x_checkpoint'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            auth=dict(c.execute('SELECT * FROM '+recovery.TABLE).fetchone())
            self.assertEqual((auth['from_generation'],auth['to_generation'],auth['prior_attempts'],auth['state']),(0,1,1,'armed'))
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_claim_history').fetchone()[0],0)
            artifact=json.loads(c.execute('SELECT artifact_json FROM pipeline_job WHERE id=?',(self.job['id'],)).fetchone()[0])
            self.assertEqual(artifact,self.artifact)
            audit=json.loads(c.execute('SELECT proof_json FROM '+recovery.JOURNAL).fetchone()[0])
            self.assertEqual(audit['original']['prior_log'],old)
            self.assertEqual(json.loads(audit['original']['prior_job']['artifact_json'])['duration_seconds'],0)
        self.assertTrue(self.apply(plan)['already_applied'])
        self.store.offer(self.adapter.project(self.adapter.read(self.q['id'])))
        job=self.claim();self.assertEqual(job['generation'],1)
        with self.assertRaises(AdapterError):self.bridge.reserve(dict(job,generation=2),dict(job,generation=2))
        desc=self.bridge.reserve(job,job)
        self.assertEqual(desc['artifact'],self.artifact)
        self.assertEqual(desc['request']['body'],old['post_text'])
        initial=json.dumps({'media_type':'video/mp4','total_bytes':2048,'media_category':'tweet_video'},separators=(',',':')).encode()
        op=self.operation(job,'POST','/2/media/upload/initialize',initial,'only-retry')
        self.store.begin_operation(job,op);self.bridge.begin_operation(job,job,op)
        self.assertEqual(self.log()['attempt_count'],2)
        with self.assertRaises(AdapterError):self.bridge.begin_operation(job,job,op)
        self.assertEqual(self.log()['attempt_count'],2)
        with self.assertRaises(recovery.RecoveryRejected):self.plan()

    def test_interrupted_stage_and_native_arm_remain_unclaimable_and_resume(self):
        plan=self.plan()
        for phase in ('central_staged','native_armed'):
            with self.assertRaises(recovery.RecoveryRejected):self.apply(plan,stop_after=phase)
            self.store.offer(self.adapter.project(self.adapter.read(self.q['id'])))
            self.assertEqual(self.store.get_job(self.job['id'])['hold_reason'],recovery.HOLD)
            self.assertIsNone(self.claim())
        self.apply(plan)
        self.assertEqual(self.store.get_job(self.job['id'])['status'],'ready')
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_event WHERE event_type=?',(recovery.EVENT,)).fetchone()[0],1)

    def test_unknown_success_attempt_and_operation_evidence_refuse(self):
        for field,value in [('unknown_outcome',1),('x_media_id','oldmedia'),('x_post_id','123'),('attempt_count',2)]:
            with self.subTest(field=field):
                old=self.log()[field]
                with self.bridge.connect() as c:c.execute('UPDATE x_post_publish_log SET '+field+'=?',(value,))
                with self.assertRaises(recovery.RecoveryRejected):self.plan()
                with self.bridge.connect() as c:c.execute('UPDATE x_post_publish_log SET '+field+'=?',(old,))
        with self.bridge.connect() as c:
            c.execute('INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)',('unexpected','x:queue:'+str(self.q['id']),0,'x:POST:/2/tweets','a'*64,self.now.isoformat()))
        with self.assertRaises(recovery.RecoveryRejected):self.plan()

    def test_stale_legacy_failure_after_admission_never_creates_retry_loop(self):
        plan=self.plan();self.apply(plan)
        self.store.offer(self.old_offer)
        self.assertEqual(self.store.get_job(self.job['id'])['status'],'failed')
        self.assertIsNone(self.claim())
        self.assertTrue(self.apply(plan)['already_applied'])
        self.assertIsNone(self.claim())
        self.assertEqual(self.log()['attempt_count'],1)

    def test_readonly_plan_does_not_create_history_and_claim_drift_refuses(self):
        def dump(path):
            with contextlib.closing(recovery.read_connection(path)) as c:return '\n'.join(c.iterdump())
        before=(dump(self.central),dump(self.runtime.POST_DB_PATH))
        plan=self.plan()
        self.assertEqual((dump(self.central),dump(self.runtime.POST_DB_PATH)),before)
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_job SET generation=1 WHERE id=?',(self.job['id'],))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.log()['status'],'failed')

    def test_any_node_receipt_even_acked_known_failure_refuses(self):
        plan=self.plan()
        for route,payload in [('jobs/fail',{'unknown_outcome':False}),('jobs/fail',{'unknown_outcome':True}),('jobs/complete',{'post_id':'123'})]:
            with self.subTest(route=route,payload=payload):
                identity=self.outboxes['hk-gpu'].put(route,dict(payload,job_id=self.job['id'],generation=0))
                with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
                with self.outboxes['hk-gpu'].connect() as c:c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?',(identity,))
                with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
                with self.outboxes['hk-gpu'].connect() as c:c.execute('DELETE FROM receipt_outbox WHERE id=?',(identity,))
        self.assertEqual(self.log()['status'],'failed')

    def test_drift_exact_scope_and_stale_evidence_refuse(self):
        plan=self.plan()
        tampered=copy.deepcopy(plan);tampered['item']['queue_id']+=1;tampered['plan_sha256']=recovery.document_hash(tampered)
        with self.assertRaises(recovery.RecoveryRejected):recovery.validate_plan(tampered)
        evidence=self.evidence(plan);evidence[0]['captured_at']=(self.now-dt.timedelta(seconds=121)).isoformat()
        with self.assertRaises(recovery.RecoveryRejected):
            recovery.apply_plan(plan,evidence,self.root/'backups','reviewer',confirm_account_unlocked=True,now=self.now)
        with self.assertRaises(recovery.RecoveryRejected):
            recovery.apply_plan(plan,self.evidence(plan),self.root/'backups','reviewer',now=self.now)
        with self.bridge.connect() as c:c.execute("UPDATE x_post_publish_log SET error_message='different initialize error'")
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.log()['status'],'failed')

    def test_account_and_frozen_artifact_changes_refuse(self):
        plan=self.plan()
        with self.bridge.connect() as c:c.execute("UPDATE x_authorized_account SET status='error'")
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        with self.bridge.connect() as c:
            c.execute("UPDATE x_authorized_account SET status='active'")
            row=c.execute('SELECT result_json FROM x_post_random_template_preparation').fetchone()
            result=json.loads(row[0]);result['recipe']['chosen']='changed'
            c.execute('UPDATE x_post_random_template_preparation SET result_json=?',(json.dumps(result),))
        with self.assertRaises(recovery.RecoveryRejected):self.apply(plan)
        self.assertEqual(self.log()['status'],'failed')


if __name__=='__main__':unittest.main()
