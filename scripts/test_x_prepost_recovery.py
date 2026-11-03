"""Real two-ledger recovery with an offline X credential boundary."""
import contextlib
import copy
import datetime as dt
import json
import unittest

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec, AdapterError
from features.pipeline_dispatch.core import DispatchStore, DispatchError
from features.pipeline_dispatch.receipt_outbox import ReceiptOutbox
from scripts import recover_x_prepost_failures as recovery
from scripts import test_pipeline_x_native as native


class PrepostRecoveryTests(unittest.TestCase):
    verify = native.NativeTests.verify
    queue = native.NativeTests.queue
    operation = native.NativeTests.operation

    def setUp(self):
        native.NativeTests.setUp(self)
        self.now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)
        self.central = self.root / 'central.sqlite'
        self.store = DispatchStore(self.central, lambda: self.now)
        self.store.channel_enabled('x', True)
        self.caps = ['prepare:x', 'publish:x', 'backend:cpu']
        self.adapter = LedgerAdapter(LedgerSpec('x', 'queue', str(self.runtime.POST_DB_PATH), 'x_post_queue'), self.bridge)
        self.outboxes = {}
        for node in ('cpu-main', 'hk-gpu'):
            self.store.register_node(node, self.caps)
            self.outboxes[node] = ReceiptOutbox(self.root / (node + '.sqlite'))
        self.q = self.queue(auto=True)
        self.store.offer(self.adapter.project(self.adapter.read(self.q['id'])))
        self.prep = self.claim('prepare')
        desc = self.bridge.reserve(self.prep, self.prep)
        self.artifact = {'url': 'https://media.example.test/final.mp4', 'sha256': 'b'*64,
                         'size': 2048, 'mime': 'video/mp4', 'duration_seconds': 30,
                         'width': 720, 'height': 1280, 'profile': 'source'}
        receipt = {'source_url': desc['request']['source_url'], 'artifact': self.artifact}
        self.bridge.checkpoint(self.prep, self.prep, 'x_preflight', receipt)
        self.store.checkpoint(self.prep, 'preflight', 'x_preflight', receipt, canonical_verified=True)
        self.bridge.complete(self.prep, {'artifact': self.artifact}, self.prep)
        self.store.complete(self.prep, 'prep-complete', {'artifact': self.artifact, 'canonical_receipt': 'native-final'}, canonical_verified=True)
        self.store.offer(self.adapter.project(self.adapter.read(self.q['id'])))
        self.pub = self.claim('publish')
        self.desc = self.bridge.reserve(self.pub, self.pub)
        self.initial = json.dumps({'media_type': 'video/mp4', 'total_bytes': 2048, 'media_category': 'tweet_video'}, separators=(',', ':')).encode()
        op = self.operation(self.pub, 'POST', '/2/media/upload/initialize', self.initial, 'old-initialize')
        self.store.begin_operation(self.pub, op)
        self.bridge.begin_operation(self.pub, self.pub, op)
        receipt = {'media_id': 'oldmedia', 'operation_id': op['operation_id']}
        self.bridge.checkpoint(self.pub, self.pub, 'x_media_initialized', receipt)
        self.store.checkpoint(self.pub, 'initialized', 'x_media_initialized', receipt, canonical_verified=True)
        op = self.operation(self.pub, 'POST', '/2/media/upload/oldmedia/append', b'chunk', 'old-append')
        self.store.begin_operation(self.pub, op)
        self.bridge.begin_operation(self.pub, self.pub, op)
        self.failure = {'error_code': 'x_upstream_error', 'unknown_outcome': False}
        result = self.bridge.fail(self.pub, self.pub, self.failure)
        self.store.fail(self.pub, self.failure['error_code'], canonical_no_write=result['canonical_no_write'])
        payload = dict(self.failure, job_id=self.pub['id'], generation=self.pub['generation'])
        identity = self.outboxes['cpu-main'].put('jobs/fail', payload)
        with self.outboxes['cpu-main'].connect() as c:
            c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?', (identity,))
        self.now += dt.timedelta(seconds=180)
        self.days = [(self.now + dt.timedelta(hours=8)).date().isoformat()]

    def claim(self, stage):
        self.store.register_node('cpu-main', self.caps)
        self.store.worker_heartbeat('cpu-main', stage + '-worker', stage)
        job = self.store.claim('cpu-main', stage + '-worker', stage)['job']
        job['executor_backend'] = 'cpu'
        return job

    def plan(self):
        return recovery.make_plan(self.central, self.runtime.POST_DB_PATH, [self.q['id']], 'publish', self.days, self.now)

    def evidence(self, plan):
        return [recovery.node_evidence(plan, node, outbox.path, self.now) for node, outbox in self.outboxes.items()]

    def apply(self, plan, **kwargs):
        return recovery.apply_plan(plan, self.evidence(plan), self.root/'backups', 'offline-reviewer', now=self.now, **kwargs)

    def log(self):
        with self.bridge.connect() as c:
            return dict(c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?', (self.q['id'],)).fetchone())

    def test_next_generation_same_artifact_counts_second_attempt_once(self):
        plan = self.plan()
        old = self.log()
        self.apply(plan)
        armed = self.log()
        self.assertEqual(armed['attempt_count'], 1)
        self.assertEqual(armed['error_code'], old['error_code'])
        self.assertEqual(armed['started_at'], old['started_at'])
        offer = self.adapter.project(self.adapter.read(self.q['id']))
        self.assertFalse(offer['reconcile_only'])
        self.assertEqual(offer['source_attempt_count'], 1)
        self.assertEqual(offer['status'], 'queued')
        # Collector does not undo the explicit admission.
        self.store.offer(offer)
        with self.assertRaises(AdapterError):
            self.bridge.fail(self.pub, self.pub, self.failure)
        job = self.claim('publish')
        self.assertEqual(job['generation'], 2)
        desc = self.bridge.reserve(job, job)
        self.assertEqual(desc, self.desc)
        op = self.operation(job, 'POST', '/2/media/upload/initialize', self.initial, 'new-initialize')
        self.store.begin_operation(job, op)
        self.bridge.begin_operation(job, job, op)
        self.assertEqual(self.log()['attempt_count'], 2)
        with self.assertRaises(AdapterError): self.bridge.begin_operation(job, job, op)
        self.assertEqual(self.log()['attempt_count'], 2)
        self.bridge.checkpoint(job, job, 'x_media_initialized', {'media_id': 'newmedia', 'operation_id': op['operation_id']})
        self.bridge.checkpoint(job, job, 'x_media_uploaded', {'media_id': 'newmedia', 'artifact_sha256': self.artifact['sha256']})
        body = json.dumps({'text': desc['request']['body'], 'media': {'media_ids': ['newmedia']}}, ensure_ascii=False, separators=(',', ':')).encode()
        post = self.operation(job, 'POST', '/2/tweets', body, 'only-post')
        self.store.begin_operation(job, post)
        self.bridge.begin_operation(job, job, post)
        self.bridge.checkpoint(job, job, 'x_source_published', {'media_id': 'newmedia', 'post_id': '123456'})
        self.assertEqual(self.bridge.fail(job, job, self.failure)['status'], 'published')
        self.assertEqual(self.log()['status'], 'published')
        with self.assertRaises(recovery.RecoveryRejected): self.plan()
        with self.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_x_operation').fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT state FROM pipeline_x_prepost_recovery").fetchone()[0], 'consumed')
            saved = json.loads(c.execute('SELECT proof_json FROM pipeline_x_prepost_recovery').fetchone()[0])
            self.assertEqual(saved['prior_log'], old)

    def test_interrupted_native_arm_is_safe_and_same_plan_resumes(self):
        plan = self.plan()
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan, stop_after='native_armed')
        self.assertEqual(self.store.get_job(self.pub['id'])['status'], 'failed')
        self.assertEqual(self.store.get_job(self.pub['id'])['hold_reason'], 'operator_prepost_recovery_pending')
        with self.assertRaises(AdapterError): self.bridge.fail(self.pub, self.pub, self.failure)
        self.apply(plan)
        self.assertEqual(self.store.get_job(self.pub['id'])['status'], 'ready')
        self.assertTrue(self.apply(plan)['applied'][0]['already_applied'])

    def test_reject_unknown_or_published_or_post_intent_even_old_generation(self):
        for field, value in [('unknown_outcome', 1), ('x_post_id', '123'), ('x_media_id', 'finalized')]:
            with self.subTest(field=field):
                with self.bridge.connect() as c: c.execute('UPDATE x_post_publish_log SET '+field+'=?', (value,))
                with self.assertRaises(recovery.RecoveryRejected): self.plan()
                with self.bridge.connect() as c: c.execute('UPDATE x_post_publish_log SET '+field+'=?', (0 if field=='unknown_outcome' else '',))
        for path in ('/2/tweets', '/2/media/upload/oldmedia/finalize', '/2/users/2/retweets'):
            with self.bridge.connect() as c:
                c.execute('INSERT INTO pipeline_x_operation VALUES(?,?,?,?,?,?)', ('unsafe',self.pub['source_key'],0,'x:POST:'+path,'a'*64,self.now.isoformat()))
            with self.assertRaises(recovery.RecoveryRejected): self.plan()
            with self.bridge.connect() as c: c.execute("DELETE FROM pipeline_x_operation WHERE operation_id='unsafe'")

    def test_pending_any_node_and_success_receipt_reject(self):
        plan = self.plan()
        p = {'job_id': self.pub['id'], 'generation': 0, 'result': {'post_id': '123'}}
        key = self.outboxes['hk-gpu'].put('jobs/complete', p)
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)
        with self.outboxes['hk-gpu'].connect() as c: c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?', (key,))
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)
        self.assertEqual(self.log()['status'], 'failed')

    def test_plan_drift_and_wrong_date_reject_before_native_mutation(self):
        plan = self.plan()
        with self.bridge.connect() as c: c.execute("UPDATE x_post_queue SET description='changed'")
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)
        self.assertEqual(self.log()['status'], 'failed')
        with self.assertRaises(recovery.RecoveryRejected):
            recovery.make_plan(self.central, self.runtime.POST_DB_PATH, [self.q['id']], 'publish', ['2020-01-01'], self.now)

    def test_zero_attempt_preparation_reuses_frozen_request(self):
        q = self.queue(identity=3, auto=True, random=True)
        self.store.offer(self.adapter.project(self.adapter.read(q['id'])))
        prep = self.claim('prepare')
        desc = self.bridge.reserve(prep, prep)
        failure = {'error_code': 'media_download_failed', 'unknown_outcome': False}
        result = self.bridge.fail(prep, prep, failure)
        self.store.fail(prep, failure['error_code'], canonical_no_write=result['canonical_no_write'])
        key = self.outboxes['cpu-main'].put('jobs/fail', dict(failure, job_id=prep['id'], generation=prep['generation']))
        with self.outboxes['cpu-main'].connect() as c: c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?', (key,))
        self.now += dt.timedelta(seconds=180)
        plan = recovery.make_plan(self.central, self.runtime.POST_DB_PATH, [q['id']], 'prepare', self.days, self.now)
        self.apply(plan)
        self.store.offer(self.adapter.project(self.adapter.read(q['id'])))
        retry = self.claim('prepare')
        self.assertEqual(retry['generation'], 2)
        self.assertEqual(self.bridge.reserve(retry, retry), desc)
        with self.assertRaises(AdapterError): self.bridge.fail(prep, prep, failure)
        with self.bridge.connect() as c:
            log = c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?', (q['id'],)).fetchone()
            self.assertEqual((log['attempt_count'],log['status']), (0,'reserved'))
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_x_operation WHERE source_key=?', (prep['source_key'],)).fetchone()[0], 0)

    def test_historical_unknown_receipt_is_not_reclassified(self):
        plan = self.plan()
        key = self.outboxes['hk-gpu'].put('jobs/fail', {'job_id': self.pub['id'], 'generation': 0, 'unknown_outcome': True})
        with self.outboxes['hk-gpu'].connect() as c: c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?', (key,))
        with self.assertRaises(recovery.RecoveryRejected): self.apply(plan)
        with self.store.transaction() as c:
            c.execute("INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,'failed',?,?)", (self.pub['id'],json.dumps({'unknown':True,'code':'old_timeout'}),self.now.isoformat()))
        with self.assertRaises(recovery.RecoveryRejected): self.plan()

    def test_artifact_drift_and_wrong_next_generation_never_get_permit(self):
        plan = self.plan()
        self.apply(plan)
        job = self.claim('publish')
        with self.assertRaises(AdapterError): self.bridge.reserve(dict(job,generation=3), dict(job,generation=3))
        with self.bridge.connect() as c:
            c.execute('UPDATE pipeline_x_artifact SET artifact_json=?', (json.dumps(dict(self.artifact,sha256='c'*64)),))
        with self.assertRaises(AdapterError): self.bridge.reserve(job,job)
        self.assertEqual(self.log()['attempt_count'],1)

    def test_zero_attempt_publish_authorization_is_consumed_on_first_upload(self):
        q = self.queue(identity=3, auto=True)
        self.store.offer(self.adapter.project(self.adapter.read(q['id'])))
        prep = self.claim('prepare')
        desc = self.bridge.reserve(prep, prep)
        receipt = {'source_url':desc['request']['source_url'],'artifact':self.artifact}
        self.bridge.checkpoint(prep, prep, 'x_preflight', receipt)
        self.store.checkpoint(prep, 'zero-preflight', 'x_preflight', receipt, canonical_verified=True)
        self.bridge.complete(prep, {'artifact':self.artifact}, prep)
        self.store.complete(prep, 'zero-prep-complete', {'artifact':self.artifact,'canonical_receipt':'native-zero-final'}, canonical_verified=True)
        self.store.offer(self.adapter.project(self.adapter.read(q['id'])))
        pub = self.claim('publish')
        desc = self.bridge.reserve(pub, pub)
        result = self.bridge.fail(pub, pub, self.failure)
        self.store.fail(pub, self.failure['error_code'], canonical_no_write=result['canonical_no_write'])
        key = self.outboxes['cpu-main'].put('jobs/fail', dict(self.failure,job_id=pub['id'],generation=pub['generation']))
        with self.outboxes['cpu-main'].connect() as c: c.execute('UPDATE receipt_outbox SET acked_at=1 WHERE id=?', (key,))
        self.now += dt.timedelta(seconds=180)
        plan = recovery.make_plan(self.central,self.runtime.POST_DB_PATH,[q['id']],'publish',self.days,self.now)
        self.assertEqual(plan['items'][0]['prior_attempts'],0)
        self.apply(plan)
        self.store.offer(self.adapter.project(self.adapter.read(q['id'])))
        retry = self.claim('publish')
        self.assertEqual(self.bridge.reserve(retry,retry),desc)
        op = self.operation(retry,'POST','/2/media/upload/initialize',self.initial,'first-zero-initialize')
        op['account_resource']='x:account:3'
        self.store.begin_operation(retry,op)
        self.bridge.begin_operation(retry,retry,op)
        with self.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT attempt_count FROM x_post_publish_log WHERE queue_id=?',(q['id'],)).fetchone()[0],1)
            self.assertEqual(c.execute('SELECT state FROM pipeline_x_prepost_recovery WHERE source_key=?',(pub['source_key'],)).fetchone()[0],'consumed')


if __name__ == '__main__': unittest.main()
