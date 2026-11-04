"""Cross-node lock races and real HTTP/outbox/native-ledger rejection closure."""
import concurrent.futures
import contextlib
import datetime as dt
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from features.pipeline_dispatch.core import DispatchStore, DispatchError, stamp
from features.pipeline_dispatch.client import CoordinatorClient, CoordinatorError
from features.pipeline_dispatch.receipt_outbox import ReceiptOutbox
from features.pipeline_dispatch.service import Coordinator, handler_type
from scripts.test_pipeline_x_native import NativeTests


class OverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name)/'dispatch.db', lambda: self.now)
        self.store.channel_enabled('x', True)
        for node in ('cpu-main', 'hk-gpu'):
            self.store.register_node(node, ['prepare', 'publish'])
            for stage in ('prepare', 'publish'):
                for i in range(3): self.store.worker_heartbeat(node, stage+str(i), stage)

    def offer(self, identity, stage, resources=None, **extra):
        return self.store.offer(dict(source_key='x:queue:'+str(identity), source_id=str(identity),
            channel='x', family='queue', batch_key='batch-'+str(identity), stage=stage,
            resource_keys=resources or ['x:account:1'], capabilities=[stage],
            scheduled_at=stamp(self.now), source_created_at=stamp(self.now), **extra))

    def claim(self, stage, node='cpu-main', index=0):
        return self.store.claim(node, stage+str(index), stage)['job']

    def test_publish_and_next_prepare_overlap_but_each_lane_serializes(self):
        self.offer(1, 'publish'); published = self.claim('publish')
        self.offer(2, 'prepare'); prepared = self.claim('prepare', 'hk-gpu')
        self.assertIsNotNone(prepared)
        self.offer(3, 'prepare'); self.assertIsNone(self.claim('prepare', index=1))
        self.offer(4, 'publish'); self.assertIsNone(self.claim('publish', 'hk-gpu'))
        with self.store.transaction() as c:
            locks = {r['resource_key']:r['job_id'] for r in c.execute('SELECT * FROM pipeline_resource')}
        self.assertEqual(locks, {'x:account:1':published['id'], 'prepare:x:account:1':prepared['id']})
        with self.assertRaisesRegex(DispatchError, 'operation_resource_not_held'):
            self.store.begin_operation(prepared, {'operation_id':'forbidden', 'request_sha256':'a'*64,
                'account_resource':'x:account:1', 'operation':'create'})

    def test_preparing_next_does_not_prevent_older_ready_publish(self):
        self.offer(2, 'prepare'); self.assertIsNotNone(self.claim('prepare'))
        self.offer(1, 'publish'); self.assertIsNotNone(self.claim('publish', 'hk-gpu'))

    def test_publish_never_overtakes_older_preparation_same_appointment(self):
        self.offer(1, 'prepare'); self.claim('prepare')
        self.offer(2, 'publish'); self.assertIsNone(self.claim('publish', 'hk-gpu'))
        self.offer(3, 'publish', ['x:account:other'])
        self.assertEqual(self.claim('publish', 'hk-gpu')['source_id'], '3')

    def test_unknown_and_legacy_fences_still_block_preparation(self):
        self.offer(1, 'publish'); current = self.claim('publish')
        self.store.fail(current, 'uncertain', unknown=True)
        self.offer(2, 'prepare'); self.assertIsNone(self.claim('prepare', 'hk-gpu'))
        self.offer(3, 'prepare', ['x:account:legacy'])
        with self.store.transaction() as c:
            c.execute('INSERT INTO pipeline_legacy_resource VALUES(?,?)', ('x:account:legacy', current['id']))
        self.assertIsNone(self.claim('prepare', 'hk-gpu'))

    def test_relay_account_is_serial_per_stage(self):
        self.offer(1, 'publish', ['x:account:1','x:account:relay']); self.claim('publish')
        self.offer(2, 'prepare', ['x:account:2','x:account:relay']); self.assertIsNotNone(self.claim('prepare','hk-gpu'))
        self.offer(3, 'prepare', ['x:account:3','x:account:relay']); self.assertIsNone(self.claim('prepare'))

    def test_two_nodes_compete_one_preparation_and_expired_claim_cannot_unfence(self):
        self.offer(1, 'prepare'); self.offer(2, 'prepare')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs=list(pool.map(lambda node:self.claim('prepare',node), ('cpu-main','hk-gpu')))
        old=next(j for j in jobs if j)
        self.assertEqual(sum(j is not None for j in jobs),1)
        self.now += dt.timedelta(seconds=121)
        for node in ('cpu-main','hk-gpu'):
            self.store.register_node(node,['prepare','publish'])
            self.store.worker_heartbeat(node,'prepare0','prepare')
        new=self.claim('prepare','hk-gpu')
        # Fairness may select the second task; either way the new fence survives.
        if new['id'] == old['id']:
            with self.assertRaises(DispatchError): self.store.fail(old,'stale',canonical_no_write=True)
        with self.store.transaction() as c:
            self.assertEqual(c.execute('SELECT job_id FROM pipeline_resource WHERE resource_key=?',('prepare:x:account:1',)).fetchone()[0],new['id'])


class RejectionTests(NativeTests):
    # Run this class's tests only, without repeating inherited native tests.
    def setUp(self):
        super().setUp()
        self.now=dt.datetime(2026,11,4,tzinfo=dt.timezone.utc)
        self.dispatch=DispatchStore(self.root/'dispatch.db',lambda:self.now)
        self.dispatch.channel_enabled('x',True)
        self.dispatch.register_node('hk-gpu',['prepare'])
        self.dispatch.worker_heartbeat('hk-gpu','worker','prepare')
        queue=self.queue(random=True)
        self.queue_id=queue['id']
        self.dispatch.offer(dict(source_key='x:queue:'+str(queue['id']),source_id=str(queue['id']),
            channel='x',family='queue',stage='prepare',resource_keys=['x:account:2'],capabilities=['prepare'],
            scheduled_at=stamp(self.now),source_created_at=stamp(self.now)))
        job=self.dispatch.claim('hk-gpu','worker','prepare')['job']
        self.identity=dict(job_id=job['id'],**{k:job[k] for k in ('node_id','worker_id','generation','claim_token')})
        descriptor=self.bridge.reserve(job,self.identity)
        bridge=self.bridge
        class Registry:
            def checkpoint(self,job,claim,phase,receipt): return bridge.checkpoint(job,claim,phase,receipt)
            def fail(self,job,claim): return bridge.fail(job,claim,claim)
            def reconcile(self,job):
                row=bridge.store.get_queue(int(job['source_id']))
                return {'definitive_failure':row['status']=='failed',
                    'result':bridge.reconcile(job),
                    'next_offer':{'source_key':job['source_key'],'source_attempt_count':0},
                    'canonical_receipt':{'source_key':job['source_key'],'status':row['status'],'publish_id':''}}
        self.authority=Coordinator(self.dispatch,Registry(),internal_token='i'*32,master_key='m'*32)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_type(self.authority))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.client=CoordinatorClient('http://127.0.0.1:'+str(self.server.server_port),'i'*32)
        self.clock=1000.0
        self.outbox=ReceiptOutbox(self.root/'outbox.db',clock=lambda:self.clock)
        self.request={**self.identity,'checkpoint_id':'rejected-1','phase':'x_preflight','receipt':{
            'source_url':descriptor['request']['source_url'],
            'artifact':{'url':'https://media.example.test/image.png','sha256':'b'*64,'size':2048,
                        'mime':'image/png','duration_seconds':0,'profile':'source','width':720,'height':1280}}}

    def reject(self):
        self.outbox.put('jobs/checkpoint',self.request,'rejected-1')
        with self.assertRaisesRegex(CoordinatorError,'x_random_requires_video') as error:
            self.outbox.flush(self.client,only='rejected-1')
        self.assertEqual(error.exception.status,422)

    def test_rejection_aborts_executor_and_outbox_drains_without_reclaim(self):
        self.reject()
        self.assertEqual(self.bridge.store.get_queue(self.queue_id)['status'],'failed')
        job=self.dispatch.get_job(self.identity['job_id'])
        self.assertEqual((job['status'],job['error_code']),('failed','x_random_requires_video'))
        self.outbox.put('jobs/fail',{**self.identity,'receipt_id':'failure-1','error_code':'checkpoint_not_accepted','unknown_outcome':False})
        self.clock+=16
        self.assertTrue(self.outbox.flush(self.client))
        self.assertEqual(self.dispatch.get_job(job['id'])['error_code'],'x_random_requires_video')
        self.now+=dt.timedelta(seconds=121); self.dispatch.expire()
        self.assertEqual(self.dispatch.get_job(job['id'])['status'],'failed')
        with self.dispatch.transaction() as c:
            for table in ('pipeline_resource','pipeline_operation','pipeline_checkpoint','pipeline_publication'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        log=self.bridge.store.reserve_log(self.queue_id)
        self.assertEqual((log['attempt_count'],log['unknown_outcome']),(0,0))

    def test_rejected_checkpoint_replay_rejects_changed_facts_and_wrong_identity(self):
        self.reject(); self.clock+=16
        self.assertTrue(self.outbox.flush(self.client))
        for altered in ({'claim_token':'wrong'*8},{'receipt':{}},{'generation':999}):
            with self.subTest(altered=list(altered)), self.assertRaises(CoordinatorError):
                self.client.post('jobs/checkpoint',{**self.request,**altered})

    def test_transport_error_does_not_close_or_ack_checkpoint(self):
        from features.pipeline_dispatch.adapters import AdapterError
        def offline(*args): raise AdapterError('canonical_bridge_unavailable')
        self.authority.registry.checkpoint=offline
        with self.assertRaises(CoordinatorError): self.client.post('jobs/checkpoint',self.request)
        self.assertEqual(self.dispatch.get_job(self.identity['job_id'])['status'],'running')
        with self.dispatch.transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_preparation_rejection').fetchone()[0],0)

    def test_old_generations_only_ack_same_rejected_input(self):
        previous=dict(self.request)
        self.now+=dt.timedelta(seconds=121)
        self.dispatch.register_node('hk-gpu',['prepare'])
        self.dispatch.worker_heartbeat('hk-gpu','worker','prepare')
        job=self.dispatch.claim('hk-gpu','worker','prepare')['job']
        self.identity=dict(job_id=job['id'],**{k:job[k] for k in ('node_id','worker_id','generation','claim_token')})
        self.bridge.reserve(job,self.identity)
        self.request={**self.request,**self.identity,'checkpoint_id':'rejected-new'}
        with self.assertRaisesRegex(CoordinatorError,'x_random_requires_video'):
            self.client.post('jobs/checkpoint',self.request)
        saved=self.dispatch.get_job(job['id'])
        self.assertTrue(self.client.post('jobs/checkpoint',previous)['rejected_checkpoint'])
        self.assertTrue(self.client.post('jobs/fail',{**previous,'receipt_id':'old-fail',
            'error_code':'coordinator_operation_failed','unknown_outcome':False})['accepted'])
        self.assertEqual(self.dispatch.get_job(job['id']),saved)
        with self.assertRaises(CoordinatorError):
            self.client.post('jobs/checkpoint',{**previous,'checkpoint_id':'altered','receipt':{}})
        self.assertEqual(self.dispatch.get_job(job['id']),saved)

    def test_prior_operation_blocks_native_failure_mutation(self):
        with self.dispatch.transaction() as c:
            c.execute('INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                ('prior',self.identity['job_id'],0,'hk-gpu','write','a'*64,'x:account:2',stamp(self.now),stamp(self.now)))
        with self.assertRaisesRegex(CoordinatorError,'preparation_rejection_conflict'):
            self.client.post('jobs/checkpoint',self.request)
        self.assertEqual(self.bridge.store.reserve_log(self.queue_id)['status'],'reserved')
        self.assertEqual(self.dispatch.get_job(self.identity['job_id'])['status'],'running')


def load_tests(loader, tests, pattern):
    suite=loader.loadTestsFromTestCase(OverlapTests)
    suite.addTests(RejectionTests(name) for name in RejectionTests.__dict__ if name.startswith('test_'))
    return suite


if __name__=='__main__': unittest.main()
