"""Real SQLite + native bridge regression; every upstream boundary is offline."""
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.test_pipeline_x_native import NativeTests
from features.x_posts.log_management import LogManager, dispatch
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch.adapters import LedgerAdapter, default_specs


class Controls(unittest.TestCase):
    def setUp(self):
        self.n = NativeTests()
        self.n.setUp()
        self.addCleanup(self.n.doCleanups)
        self.store = DispatchStore(self.n.root/'dispatch.sqlite')
        self.adapter = LedgerAdapter(default_specs({'x':self.n.runtime.POST_DB_PATH})[0], driver=object())
        self.manager = LogManager(self.n.runtime.POST_DB_PATH, self.store.path)
        self.actor = {'tenant_key':'tenant','user_id':'admin','name':'Admin'}

    def queue(self, **kwargs):
        q=self.n.queue(**kwargs)
        self.offer(q)
        return q

    def offer(self,q):
        return self.store.offer(self.adapter.project(self.adapter.read(q['id'])))

    def selection(self,q):
        with contextlib.closing(self.manager.connect(readonly=True)) as c:
            s=self.manager.snapshot(c,q['id'])
            return {'queue_id':q['id'],'version':self.manager.version(s)}

    def action(self,action,*queues,payload=None):
        payload=payload or {'operation_id':uuid.uuid4().hex,'action':action,'items':[self.selection(q) for q in queues]}
        return self.manager.apply(payload,self.actor)

    def execute(self,sql,args=()):
        with self.n.bridge.connect() as c:
            c.execute(sql,args)

    def test_manual_source_and_filter(self):
        manual=self.queue(); self.queue(auto=True)
        self.assertEqual([i['queue_id'] for i in self.manager.query({'task_source':'manual_publish'})['items']],[manual['id']])
        self.assertEqual(self.manager.query({'task_source':'auto_publish'})['pagination']['total'],1)

    def test_stop_survives_collector_and_reset_restores_native_execution(self):
        q=self.queue()
        original=self.n.bridge.store.get_queue(q['id'])
        self.assertEqual(self.action('stop',q)['succeeded'],1)
        self.assertEqual(self.manager.query({'status':'stopped'})['pagination']['total'],1)
        self.assertEqual(self.offer(q)['status'],'backlog_paused')
        with self.assertRaisesRegex(Exception,'canonical_backlog_paused'):
            self.n.bridge.reserve(self.n.job(q,'prepare'),self.n.job(q,'prepare'))
        self.assertEqual(self.action('reset',q)['succeeded'],1)
        self.assertEqual(self.offer(q)['status'],'queued')
        current=self.n.bridge.store.get_queue(q['id'])
        self.assertEqual({k:v for k,v in original.items() if k not in {'status','updated_at'}}, {k:v for k,v in current.items() if k not in {'status','updated_at'}})
        self.assertEqual(self.n.bridge.reserve(self.n.job(q,'prepare',3),self.n.job(q,'prepare',3))['kind'],'x_preflight')

    def test_failed_preflight_restarts_with_same_identity(self):
        q=self.queue(auto=True)
        log=self.n.bridge.store.reserve_log(q['id'])
        self.n.bridge.store.mark_failed(log['id'],'offline_failure','fixture',False)
        self.offer(q)
        result=self.action('reset',q)
        self.assertEqual(result['succeeded'],1,result)
        self.assertEqual(self.n.bridge.reserve(self.n.job(q,'prepare',3),self.n.job(q,'prepare',3))['kind'],'x_preflight')

    def test_failed_upload_preserves_artifact_and_reaches_new_publish_generation(self):
        q=self.queue()
        prep,_,artifact=self.n.preflight(q)
        self.n.bridge.complete(prep,{'artifact':artifact},prep)
        job=self.offer(q)
        native=self.n.job(q,'publish')
        self.n.bridge.reserve(native,native)
        log=self.n.bridge.store.reserve_log(q['id'])
        self.n.bridge.store.mark_publishing(log['id'])
        self.n.bridge.store.mark_failed(log['id'],'upload_interrupted_no_post','explicit upload failure',False)
        self.offer(q)
        self.assertEqual(self.action('reset',q)['succeeded'],1)
        next_job=self.n.job(q,'publish',3)
        self.assertEqual(self.n.bridge.reserve(next_job,next_job)['artifact']['sha256'],artifact['sha256'])
        with self.n.bridge.connect() as c:
            audit=json.loads(c.execute("SELECT before_json FROM x_post_queue_control_audit WHERE action='reset'").fetchone()[0])
        self.assertEqual(audit['log']['attempt_count'],1)
        self.assertEqual(self.n.bridge.store.get_log(log['id'])['attempt_count'],0)

    def test_unknown_published_active_and_stale_selection_are_local_skips(self):
        good=self.queue(); unknown=self.queue(); published=self.queue(); active=self.queue()
        for q in (unknown,published):
            log=self.n.bridge.store.reserve_log(q['id'])
            self.n.bridge.store.mark_failed(log['id'],'fixture','fixture',q==unknown)
        self.execute("UPDATE x_post_publish_log SET x_post_id='123' WHERE queue_id=?",(published['id'],))
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='running' WHERE source_key=?",('x:queue:'+str(active['id']),))
        result=self.action('reset',good,unknown,published,active)
        self.assertEqual((result['succeeded'],result['skipped']),(1,3))
        payload={'operation_id':uuid.uuid4().hex,'action':'reset','items':[self.selection(good)]}
        self.action('stop',good)
        self.assertEqual(self.action('reset',payload=payload)['skipped'],1)

    def test_idempotent_response_loss_does_not_reset_newer_state(self):
        q=self.queue()
        payload={'operation_id':uuid.uuid4().hex,'action':'reset','items':[self.selection(q)]}
        first=self.action('reset',payload=payload)
        self.action('stop',q)
        second=self.action('reset',payload=payload)
        self.assertEqual(first['succeeded'],second['succeeded'])
        self.assertTrue(second['items'][0]['replayed'])
        self.assertEqual(self.manager.query({'status':'stopped'})['pagination']['total'],1)

    def test_lease_and_unknown_resources_never_released(self):
        q=self.queue();job=self.offer(q)
        with self.store.transaction() as c:
            c.execute('INSERT INTO pipeline_resource(resource_key,job_id,generation,quarantined) VALUES(?,?,0,1)',('x:queue:'+str(q['id']),job['id']))
        self.assertEqual(self.action('reset',q)['skipped'],1)
        self.assertEqual(self.action('stop',q)['skipped'],1)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM pipeline_resource').fetchone()[0],1)

    def test_request_rollback_on_dispatch_failure(self):
        q=self.queue();before=self.selection(q)
        with self.store.transaction() as c:
            c.execute("CREATE TRIGGER fail_test BEFORE UPDATE ON pipeline_job BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.assertEqual(self.action('stop',q)['skipped'],1)
        self.assertEqual(self.selection(q),before)

    def test_read_only_query_does_not_initialize_audit(self):
        self.queue();self.manager.query({})
        with self.n.bridge.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='x_post_queue_control_audit'").fetchone())

    def test_gateway_admin_and_same_origin(self):
        from types import SimpleNamespace
        handler=mock.Mock(command='POST')
        handler._require_cookie_admin.return_value=False
        app={'json_response':mock.Mock()}
        with mock.patch.object(LogManager,'apply') as apply:
            dispatch(handler,SimpleNamespace(query=''),app)
            apply.assert_not_called()
        handler._require_cookie_admin.return_value=True
        with mock.patch('features.x_video_publish.gateway.read_json',return_value=None),mock.patch.object(LogManager,'apply') as apply:
            dispatch(handler,SimpleNamespace(query=''),app)
            apply.assert_not_called()


if __name__=='__main__':
    unittest.main()
