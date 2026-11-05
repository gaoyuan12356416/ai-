"""Real ledger isolation and existing-video/comment recovery regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
from scripts import test_pipeline_youtube_native as fixtures
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec, AdapterRegistry
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.youtube_isolation import identity_resources
from features.pipeline_dispatch.youtube_readback import reconcile_video, comment_continuation


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = datetime.now(timezone.utc)
        self.store = DispatchStore(Path(self.temp.name)/'dispatch.db', lambda: self.now)
        self.store.register_node('cpu-main', ['publish:youtube'])
        self.store.channel_enabled('youtube', True)
        for worker in ('one', 'two'): self.store.worker_heartbeat('cpu-main', worker, 'publish')
        self.channel = 'UC'+'A'*22

    def offer(self, ident, material, *, status='queued', active=False, url=None):
        url = url or 'https://media.example/'+material+'.mp4'
        resources, _, _ = identity_resources(self.channel, material, url)
        return dict(source_key='youtube:publish:'+str(ident), source_id=str(ident),channel='youtube',family='publish',
            stage='publish',status=status,source_status=status,scheduled_at=stamp(self.now-timedelta(hours=1)),
            source_created_at=stamp(self.now-timedelta(hours=1)), resource_keys=resources,capabilities=['publish:youtube'],
            payload={},artifact={},execution_available=status=='queued',youtube_channel_id=self.channel,
            youtube_material_id=material,youtube_source_url=url,youtube_write_active=active,unknown_outcome=status=='unknown')

    def claim(self, worker='one'): return self.store.claim('cpu-main',worker,'publish')['job']

    def test_ended_unknown_does_not_block_other_material_or_fifo(self):
        old=self.offer(1,'a',status='unknown'); legacy=dict(old,resource_keys=['youtube:channel:'+self.channel])
        saved=self.store.offer(legacy)
        self.store.offer(self.offer(2,'a')); later=self.store.offer(self.offer(3,'b'))
        self.assertIsNone(self.claim())
        self.store.offer(old)
        self.assertEqual(self.claim()['id'],later['id'])
        with self.store.transaction() as c:
            self.assertTrue(c.execute('SELECT 1 FROM pipeline_legacy_resource WHERE job_id=?',(saved['id'],)).fetchone())
            self.assertFalse(c.execute('SELECT 1 FROM pipeline_legacy_resource WHERE job_id=? AND resource_key=?',(saved['id'],'youtube:channel:'+self.channel)).fetchone())

    def test_active_claim_and_native_owner_keep_channel_serial(self):
        first=self.store.offer(self.offer(1,'a')); self.claim()
        self.store.offer(self.offer(2,'b'))
        self.store.offer(self.offer(1,'a',status='unknown',active=True))
        self.assertIsNone(self.claim('two'))
        # Even after the central lease expires, an uncleared native owner is
        # not evidence that an external write has stopped.
        self.now+=timedelta(seconds=121);self.store.expire()
        self.store.register_node('cpu-main',['publish:youtube']);self.store.worker_heartbeat('cpu-main','two','publish')
        self.store.offer(self.offer(1,'a',status='unknown',active=True))
        self.assertIsNone(self.claim('two'))
        self.store.offer(self.offer(1,'a',status='unknown'))
        self.assertEqual(self.claim('two')['source_id'],'2')

    def test_same_source_under_other_material_id_remains_fenced(self):
        self.store.offer(self.offer(1,'a',status='unknown'))
        self.store.offer(self.offer(2,'alias',url='https://media.example/a.mp4?signature=new'))
        self.assertIsNone(self.claim())

    def test_no_media_identity_keeps_old_channel_protection(self):
        old=self.offer(1,'',status='unknown',url='invalid')
        self.store.offer(old);self.store.offer(self.offer(2,'b'))
        self.assertIsNone(self.claim())


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.YouTubeNativeTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups);self.addCleanup(self.f.tearDown)
        self.proof={'state':'succeeded','visibility':'public','preserved_status':{'privacyStatus':'public'},'published_at':'2026-11-05T00:00:13Z'}
        self.f.bridge.client.read_reviewed_video_state=Mock(return_value=self.proof)
        with self.f.workflow.db(True) as c:
            c.execute("UPDATE drama_youtube_publish SET status='unknown',video_state='unknown',video_id='video_123',video_attempt_count=1,thumbnail_status='succeeded',processing_status='succeeded',public_status='running',reviewed_phase='public',unknown_outcome=1,error_code='youtube_visibility_unconfirmed',lease_owner='',lease_expires_at_utc='' WHERE id=?",(self.f.task['id'],))

    def task(self):return self.f.store.youtube_task(self.f.task['id'])

    def test_public_readback_preserves_video_and_enables_only_unattempted_comment(self):
        f=self.f;result=f.bridge.reconcile(dict(f.job,status='unknown'))
        self.assertEqual(self.task()['video_state'],'published')
        self.assertEqual(self.task()['video_attempt_count'],1)
        self.assertEqual(result['continuation']['kind'],'comment')
        self.assertEqual(self.task()['video_published_at_utc'],self.proof['published_at'])
        with f.workflow.db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM drama_youtube_sync_outbox").fetchone()[0],2)
        # Claim the comment generation through the real bridge; forbidden upload
        # operations cannot be granted for this descriptor.
        job=dict(f.job,generation=2,payload={'continuation':result['continuation']})
        claim={'claim_token':'new-comment-claim-token-long'}
        desc=f.bridge.reserve(job,claim)
        self.assertTrue(desc['request']['comment_only'])
        f.bridge.checkpoint(job,claim,'youtube_schedule_readback',{'video_id':'video_123','confirmed':self.proof})
        op={'operation_id':'comment-operation-0001','request_sha256':'a'*64,'operation':'youtube_comment','account_resource':desc['account_resource'],
            'details':{'video_id':'video_123','channel_id':self.task()['channel_id'],'text_sha256':__import__('hashlib').sha256(b'hello').hexdigest()}}
        f.bridge.begin_operation(job,claim,op)
        f.bridge.checkpoint(job,claim,'youtube_comment',{'video_id':'video_123','comment_id':'comment_1'})
        f.bridge.complete(job,{'video_id':'video_123','comment_id':'comment_1','status':'published','publish_at':''},claim)
        self.assertEqual((self.task()['status'],self.task()['comment_attempt_count']),('published',1))
        self.assertIsNone(comment_continuation(self.task()))

    def test_uncertain_comment_is_never_retried(self):
        with self.f.workflow.db(True) as c:c.execute("UPDATE drama_youtube_publish SET comment_attempt_count=1,comment_status='unknown' WHERE id=?",(self.f.task['id'],))
        result=self.f.bridge.reconcile(dict(self.f.job,status='unknown'))
        self.assertEqual((self.task()['video_state'],self.task()['unknown_outcome']),('published',1))
        self.assertNotIn('continuation',result)

    def test_active_owner_and_changed_row_cannot_be_reconciled(self):
        with self.f.workflow.db(True) as c:c.execute("UPDATE drama_youtube_publish SET lease_owner='active' WHERE id=?",(self.f.task['id'],))
        before=self.task();self.assertEqual(reconcile_video(self.f.bridge,before),before)
        self.f.bridge.client.read_reviewed_video_state.assert_not_called()
        with self.f.workflow.db(True) as c:c.execute("UPDATE drama_youtube_publish SET lease_owner='' WHERE id=?",(self.f.task['id'],))
        def race(*a,**kw):
            with self.f.workflow.db(True) as c:c.execute("UPDATE drama_youtube_publish SET lease_owner='new-owner' WHERE id=?",(self.f.task['id'],))
            return self.proof
        self.f.bridge.client.read_reviewed_video_state.side_effect=race
        reconcile_video(self.f.bridge,self.task());self.assertEqual(self.task()['video_state'],'unknown')

    def test_private_nonmatching_read_keeps_unknown(self):
        self.proof.update(visibility='private')
        before=self.task();reconcile_video(self.f.bridge,before);self.assertEqual(self.task(),before)

    def test_registry_comment_rearms_central_unknown_then_claims(self):
        f=self.f
        class Driver:
            def reconcile(self,job):return f.bridge.reconcile(job)
            def reserve(self,row,job,claim):return f.bridge.reserve(job,claim)
        adapter=LedgerAdapter(LedgerSpec('youtube','publish',str(f.store.db_path),'drama_youtube_publish'),Driver())
        registry=AdapterRegistry([adapter])
        dispatch=DispatchStore(Path(f.temp.name)/'dispatch.db')
        job=dispatch.offer(adapter.project(adapter.read(f.task['id'])))
        with dispatch.transaction() as c:c.execute("UPDATE pipeline_job SET status='unknown',generation=1 WHERE id=?",(job['id'],))
        job=dispatch.get_job(job['id']);verified=registry.reconcile(job)
        dispatch.reconcile_native(job['id'],verified);dispatch.rearm_native_schedule(job,verified)
        ready=dispatch.get_job(job['id']);self.assertEqual(ready['status'],'ready')
        dispatch.offer(adapter.project(adapter.read(f.task['id'])))
        self.assertEqual(dispatch.get_job(job['id'])['status'],'ready')
        descriptor=registry.reserve(dict(ready,generation=2),{'claim_token':'registry-comment-claim-long'})
        self.assertTrue(descriptor['request']['comment_only'])


class VisibilityTests(unittest.TestCase):
    def test_delayed_readback_never_repeats_visibility_write(self):
        from scripts.test_pipeline_executors import Context
        from features.pipeline_dispatch.executors import NodeExecutors
        from features.pipeline_dispatch.youtube_cover import inline_image
        import io
        from PIL import Image
        with tempfile.TemporaryDirectory() as root:
            stream=io.BytesIO();Image.new('RGB',(640,360)).save(stream,format='JPEG')
            private={'state':'succeeded','visibility':'private','preserved_status':{'privacyStatus':'private'}}
            public={'state':'succeeded','visibility':'public','preserved_status':{'privacyStatus':'public'}}
            api=Mock();api.read_reviewed_video_state.side_effect=[private,private,private,public]
            api.publish_comment.return_value='comment1'
            executor=NodeExecutors({'state_root':root,'backend':'cpu'},platform_clients={'youtube_publish':api})
            payload={'request':{'video_id':'video_123','channel_id':'channel','comment_text':'hello','approved_cover':inline_image(stream.getvalue())}}
            with patch('features.pipeline_dispatch.executors.time.sleep'):
                result=executor.youtube_publish(payload,{'url':'https://media.example/a.mp4','sha256':'a'*64,'size':1},Context())
            self.assertEqual(result['status'],'published')
            api.make_video_public.assert_called_once();api.upload.assert_not_called();api.begin_resumable.assert_not_called()
            api.publish_comment.assert_called_once()


if __name__=='__main__':unittest.main()
