"""Exercise credential failure and explicit retry through real SQLite ledgers."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from scripts.test_pipeline_youtube_native import YouTubeNativeTests
from features.drama_synthesis.youtube import YouTubeHTTPError
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec, AdapterRegistry
from features.pipeline_dispatch.core import DispatchStore


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = YouTubeNativeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)

    def test_refresh_exhaustion_records_no_upload_intent_and_safe_failure(self):
        f = self.f
        f.bridge.client.refresh_access_token = Mock(side_effect=YouTubeHTTPError('youtube_token_refresh_unavailable', 'temporary', retryable=True))
        with patch('features.pipeline_dispatch.youtube_native.time.sleep'):
            with self.assertRaises(YouTubeHTTPError):
                f.prepare_upload()
        self.assertEqual(f.bridge.client.refresh_access_token.call_count, 2)
        self.assertEqual(f.store.youtube_task(f.task['id'])['video_attempt_count'], 0)
        result = f.bridge.fail(f.job, f.claim, {'error_code':'youtube_token_refresh_unavailable','unknown_outcome':False})
        self.assertTrue(result['canonical_no_write'])
        task = f.store.youtube_task(f.task['id'])
        self.assertEqual((task['status'], task['unknown_outcome'], task['video_attempt_count']), ('failed',0,0))
        with f.workflow.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_youtube_operation').fetchone()[0],0)

    def test_identity_retry_then_success_grants_exactly_one_upload_intent(self):
        f = self.f
        f.bridge.client.verify_channel_identity = Mock(side_effect=[YouTubeHTTPError('youtube_channel_identity_unavailable','temporary'), None])
        with patch('features.pipeline_dispatch.youtube_native.time.sleep'):
            f.prepare_upload()
        self.assertEqual(f.bridge.client.verify_channel_identity.call_count, 2)
        self.assertEqual(f.store.youtube_task(f.task['id'])['video_attempt_count'],1)
        result = f.bridge.fail(f.job, f.claim, {'error_code':'crash_after_grant','unknown_outcome':True})
        self.assertFalse(result['canonical_no_write'])
        self.assertEqual(f.store.youtube_task(f.task['id'])['status'],'unknown')

    def test_revoked_authorization_never_retried(self):
        f = self.f
        f.bridge.client.refresh_access_token = Mock(side_effect=YouTubeHTTPError('youtube_token_refresh_failed','expired'))
        with self.assertRaises(YouTubeHTTPError): f.prepare_upload()
        self.assertEqual(f.bridge.client.refresh_access_token.call_count,1)
        self.assertEqual(f.store.youtube_task(f.task['id'])['video_attempt_count'],0)

    def test_queued_display_is_waiting_and_failure_remains_visible(self):
        f=self.f
        body={'id':'prep1','status':'uploading','phase':'upload','publish_id':f.task['id'],
              'creator':{'tenant_key':'t','user_id':'user1'},'channel':{'name':'channel'},
              'material':{'name':'video'},'versions':[],'current_version':1,'publish_at':''}
        with f.workflow.db(True) as c:
            c.execute("UPDATE drama_youtube_publish SET status='queued' WHERE id=?",(f.task['id'],))
        self.assertEqual(f.workflow._dto(body,body['creator'],summary=True)['status'],'queued')
        with f.workflow.db(True) as c:
            c.execute("UPDATE drama_youtube_publish SET status='failed',error_code='pipeline_lease_lost' WHERE id=?",(f.task['id'],))
        self.assertEqual(f.workflow._dto(body,body['creator'],summary=True)['status'],'publish_failed')

    def test_operator_retry_rearms_only_same_unstarted_task_once(self):
        f = self.f
        adapter = LedgerAdapter(LedgerSpec('youtube','publish',str(f.store.db_path),'drama_youtube_publish',module='youtube_auto_publish'),f.bridge)
        with tempfile.TemporaryDirectory() as root:
            dispatch = DispatchStore(Path(root)/'dispatch.db')
            # Current production shape: central failure, canonical failure,
            # an acknowledged old claim, and no platform write permit.
            f.bridge.fail(f.job,f.claim,{'error_code':'pipeline_lease_lost','unknown_outcome':False})
            offer = adapter.project(adapter.read(f.task['id']))
            central = dispatch.offer(offer)
            with dispatch.transaction() as c:
                c.execute("UPDATE pipeline_job SET status='failed',generation=1 WHERE id=?",(central['id'],))
            dispatch.offer(offer)
            self.assertEqual(dispatch.get_job(central['id'])['status'],'failed')
            f.store.retry_reviewed_youtube(f.task['id'])
            retry = adapter.project(adapter.read(f.task['id']))
            self.assertTrue(retry['source_revision'].isdigit())
            restored = dispatch.offer(retry)
            self.assertEqual((restored['id'],restored['status'],restored['generation']), (central['id'],'queued',1))
            dispatch.offer(retry)
            with closing(dispatch.connect()) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM pipeline_event WHERE event_type='youtube_publish_retry_requested'").fetchone()[0],1)
            successor = dict(f.job,generation=2)
            descriptor = f.bridge.reserve(successor,{'claim_token':'new-claim-token-long-enough'})
            self.assertEqual(descriptor['request']['title'],f.task['title'])
            self.assertEqual(descriptor['artifact']['url'],f.task['source_url'])

    def test_retry_projection_cannot_release_unknown_or_written_task(self):
        f = self.f
        adapter = LedgerAdapter(LedgerSpec('youtube','publish',str(f.store.db_path),'drama_youtube_publish',module='youtube_auto_publish'),f.bridge)
        f.prepare_upload()
        f.bridge.fail(f.job,f.claim,{'error_code':'upload_uncertain','unknown_outcome':True})
        with self.assertRaises(Exception): f.store.retry_reviewed_youtube(f.task['id'])
        with tempfile.TemporaryDirectory() as root:
            dispatch=DispatchStore(Path(root)/'d.db')
            offer=adapter.project(adapter.read(f.task['id']))
            central=dispatch.offer(offer)
            with dispatch.transaction() as c:
                c.execute("UPDATE pipeline_job SET status='unknown',generation=1 WHERE id=?",(central['id'],))
            dispatch.offer(dict(offer,source_revision='999'))
            self.assertEqual(dispatch.get_job(central['id'])['status'],'unknown')

    def test_confirmed_appointment_stays_parked_but_actual_unknown_is_fenced(self):
        f=self.f
        adapter=LedgerAdapter(LedgerSpec('youtube','publish',str(f.store.db_path),'drama_youtube_publish',module='youtube_auto_publish'),f.bridge)
        due='2099-11-04T00:00:00Z'
        with f.workflow.db(True) as c:
            c.execute("UPDATE drama_youtube_publish SET status='scheduled',schedule_status='armed',publish_at=?,schedule_confirmed_publish_at=?,video_id='same_video1',video_attempt_count=1,lease_owner='',lease_expires_at_utc='' WHERE id=?",(due,due,f.task['id']))
        offer=adapter.project(adapter.read(f.task['id']))
        self.assertEqual(offer['status'],'unknown') # Attempt count alone is not a failed appointment.
        continuation={'video_id':'same_video1','scheduled_at':due}
        self.assertEqual(offer['confirmed_youtube_schedule'],continuation)
        with tempfile.TemporaryDirectory() as root:
            dispatch=DispatchStore(Path(root)/'d.db')
            job=dispatch.offer(offer)
            with dispatch.transaction() as c:
                c.execute("UPDATE pipeline_job SET status='ready',generation=1,payload_json=?,scheduled_at=? WHERE id=?",(json.dumps({'continuation':continuation}),due,job['id']))
                c.execute('DELETE FROM pipeline_legacy_resource WHERE job_id=?',(job['id'],))
            for _ in range(3):dispatch.offer(offer)
            saved=dispatch.get_job(job['id'])
            self.assertEqual((saved['status'],saved['generation'],saved['scheduled_at']),('ready',1,due))
            with closing(dispatch.connect()) as c:
                self.assertFalse(c.execute('SELECT 1 FROM pipeline_legacy_resource WHERE job_id=?',(job['id'],)).fetchone())
            with f.workflow.db(True) as c:c.execute("UPDATE drama_youtube_publish SET status='unknown',unknown_outcome=1 WHERE id=?",(f.task['id'],))
            dispatch.offer(adapter.project(adapter.read(f.task['id'])))
            self.assertEqual(dispatch.get_job(job['id'])['status'],'unknown')


if __name__ == '__main__': unittest.main()
