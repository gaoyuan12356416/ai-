"""Temporary reads must not become failed posts or relax an uncertain write."""
import datetime as dt
import contextlib
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from scripts.test_pipeline_fb_source_locks import FBSourceLockTests
from features.fb_video_publish.templates import TemplateError
from features.fb_video_publish.client import GatewayError
from features.fb_video_publish.identity_client import IdentityClient
from features.fb_video_publish.group_sources import GroupPoolSource
from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.core import DispatchStore, DispatchError, stamp
from features.pipeline_dispatch.service import Coordinator


class BackpressureTests(FBSourceLockTests):
    def test_temporary_preflight_returns_original_task_without_attempt(self):
        for index, code in enumerate(('source_busy', 'source_query_timeout', 'identity_unavailable', 'mapping_busy'), 30):
            job = self.video_job(index, str(index))
            with patch.object(self.bridge, '_video_preflight', side_effect=TemplateError(code, code, 503)):
                with self.assertRaisesRegex(AdapterError, '^canonical_source_unavailable$'):
                    self.bridge.reserve(job, self.claim)
            with self.video.connect() as c:
                row = c.execute('SELECT * FROM fb_video_task WHERE task_id=?', (job['source_id'],)).fetchone()
                self.assertEqual((row['status'], row['execution_lease'], row['graph_post_id']), ('queued', '', ''))
                self.assertEqual(row['error_code'], code)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM fb_video_attempt').fetchone()[0], 0)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM pipeline_fb_claim').fetchone()[0], index - 30)
            # The exact original task can complete reservation on its next turn.
            self.bridge.reserve(dict(job, generation=2), self.claim)

    def test_permanent_permission_failure_stays_failed(self):
        job = self.video_job(40)
        with patch.object(self.bridge, '_video_preflight', side_effect=GatewayError('identity_permission_denied', 'denied', 403)):
            with self.assertRaises(GatewayError):
                self.bridge.reserve(job, self.claim)
        with self.video.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM fb_video_task').fetchone()[0], 'failed')

    def test_capacity_rejection_never_claims_a_native_task(self):
        job = self.video_job(41)
        self.bridge._preflight_slots.acquire()
        self.bridge._preflight_slots.acquire()
        try:
            with patch.object(self.video, 'claim', wraps=self.video.claim) as claim:
                with self.assertRaisesRegex(AdapterError, '^canonical_source_busy$'):
                    self.bridge.reserve(job, self.claim)
                claim.assert_not_called()
        finally:
            self.bridge._preflight_slots.release()
            self.bridge._preflight_slots.release()
        self.bridge.reserve(job, self.claim)

    def test_waiting_credentials_take_priority_over_new_reservations(self):
        job = self.video_job(49)
        self.bridge._credential_waiters = 1
        with self.assertRaisesRegex(AdapterError, '^canonical_source_busy$'):
            self.bridge.reserve(job, self.claim)
        self.bridge._credential_waiters = 0
        self.bridge.reserve(job, self.claim)

    def test_started_attempt_cannot_be_returned_to_queue(self):
        job = self.video_job(42)
        self.bridge.reserve(job, self.claim)
        _, task, descriptor = self.bridge._stored(job, self.claim)
        self.bridge.begin_operation(job, self.claim, self.operation(descriptor))
        with self.assertRaisesRegex(AdapterError, 'requires_reconciliation'):
            self.bridge._defer_preflight(job, task, 'source_busy')
        with self.video.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM fb_video_task').fetchone()[0], 'running')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM fb_video_attempt').fetchone()[0], 1)

    def test_busy_credential_select_retries_before_one_native_write_permit(self):
        job = self.video_job(43)
        descriptor = self.bridge.reserve(job, self.claim)
        credentials = self.runtime.manual_worker._task_credentials({ 'page_id': '100' })
        with patch.object(self.runtime.manual_worker, '_task_credentials', side_effect=[TemplateError('source_busy', 'busy', 503), credentials]), \
                patch('features.pipeline_dispatch.fb_native.time.sleep'):
            self.bridge.begin_operation(job, self.claim, self.operation(descriptor))
        with self.video.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM fb_video_attempt').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM pipeline_fb_operation').fetchone()[0], 1)

    def test_recovery_preserves_original_attempt_and_rejects_unknown(self):
        from features.fb_auto_posts.publisher import GraphResult
        from scripts.recover_fb_unpublished import plan, apply
        job = self.video_job(44)
        task = self.video.claim('execute', 'fixture', task_id=job['source_id'])
        self.video.record_outcome(task, GraphResult('definite_failure', error_code='source_busy'))
        store = DispatchStore(self.temp.name + '/recover.sqlite', clock=lambda: self.now)
        store.channel_enabled('fb', True)
        store.register_node('cpu-main', ['publish:fb', 'backend:cpu'])
        store.worker_heartbeat('cpu-main', 'w', 'publish')
        store.offer({**job, 'scheduled_at':stamp(self.now), 'source_created_at':stamp(self.now),
            'resource_keys':['fb:page:100'], 'fb_page_id':'100','fb_material_id':'44','capabilities':['publish:fb']})
        claim = store.claim('cpu-main', 'w', 'publish')['job']
        store.fail(claim, 'source_busy')
        store.channel_enabled('fb', False)
        with contextlib.closing(store.connect()) as c:
            c.execute('ATTACH DATABASE ? AS native', (self.temp.name + '/native.sqlite',))
            before = dict(c.execute('SELECT * FROM native.fb_video_task').fetchone())
            prepared = plan(c, [claim['id']])
            self.assertEqual(prepared['excluded'], [])
            c.execute('BEGIN IMMEDIATE')
            apply(c, prepared); c.commit()
            after = dict(c.execute('SELECT * FROM native.fb_video_task').fetchone())
            for field in ('task_id','snapshot_json','prepared_json','short_url','long_url','planned_at_utc','ledger_id'):
                self.assertEqual(before[field], after[field])
            self.assertEqual(after['status'], 'queued')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM native.fb_video_attempt').fetchone()[0], 1)
            self.assertTrue(plan(c, [claim['id']])['items'][0]['already_recovered'])
            c.execute("UPDATE native.fb_video_task SET status='failed',error_code='source_busy'")
            c.execute("UPDATE pipeline_job SET status='failed',error_code='source_busy',generation=2 WHERE id=?", (claim['id'],))
            self.assertEqual(plan(c, [claim['id']], True)['excluded'][0]['reason'], 'no_unissued_query_permit')
            c.execute("INSERT INTO pipeline_operation VALUES('unissued',?,2,'cpu-main','fb_video_publish',?,'fb:page:100','intent',?,?)",
                      (claim['id'], 'a'*64, stamp(self.now), stamp(self.now)))
            continued = plan(c, [claim['id']], True)
            self.assertEqual(continued['excluded'], [])
            apply(c, continued);c.commit()
            self.assertEqual(c.execute("SELECT status FROM pipeline_operation WHERE operation_id='unissued'").fetchone()[0], 'confirmed_unissued')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_event WHERE event_type='authorized_fb_unpublished_query_retry'").fetchone()[0], 1)
            # A distinct unknown task is excluded even if its label says busy.
            c.execute("UPDATE pipeline_job SET source_key='fb:video:other',source_id='other',status='unknown' WHERE id=?", (claim['id'],))
            c.execute("DELETE FROM pipeline_event WHERE event_type='authorized_fb_unpublished_recovery'")
            c.commit()
            self.assertEqual(plan(c, [claim['id']])['excluded'][0]['reason'], 'central_not_failed_idle')

    def test_coordinator_cooldown_preserves_schedule_and_blocks_intent(self):
        store = DispatchStore(self.temp.name + '/dispatch.sqlite', clock=lambda: self.now)
        store.channel_enabled('fb', True)
        store.register_node('cpu-main', ['publish:fb', 'backend:cpu'])
        store.worker_heartbeat('cpu-main', 'w', 'publish')
        original = stamp(self.now)
        store.offer(dict(source_key='fb:video:one', source_id='one', channel='fb', family='video', stage='publish',
            resource_keys=['fb:page:100'], fb_page_id='100', fb_material_id='1000',
            capabilities=['publish:fb'], scheduled_at=original, source_created_at=original))
        registry = SimpleNamespace(reserve=lambda *args: (_ for _ in ()).throw(AdapterError('canonical_source_unavailable')))
        coordinator = Coordinator(store, registry, internal_token='i' * 32, master_key='m' * 32)
        with patch.object(coordinator, 'recover_prepared', return_value=False):
            job = store.claim('cpu-main', 'w', 'publish')['job']
            self.assertIsNotNone(job)
            self.assertIsNone(coordinator.reserve_job(job)['job'])
        self.assertEqual(store.get_job(job['id'])['status'], 'queued')
        self.assertEqual(store.get_job(job['id'])['scheduled_at'], original)
        self.assertIsNone(store.claim('cpu-main', 'w', 'publish')['job'])
        self.now += dt.timedelta(seconds=31)
        store.worker_heartbeat('cpu-main', 'w', 'publish')
        store.register_node('cpu-main', ['publish:fb', 'backend:cpu'])
        job = store.claim('cpu-main', 'w', 'publish')['job']
        self.assertEqual(job['generation'], 2)
        with store.transaction() as c:
            c.execute('UPDATE pipeline_job SET external_intent=1 WHERE id=?', (job['id'],))
        with self.assertRaisesRegex(DispatchError, 'requires_reconciliation'):
            store.defer_blocked(job, 'canonical_source_busy')

    def test_authorized_rejected_history_does_not_become_a_new_unknown(self):
        store = DispatchStore(self.temp.name + '/observation.sqlite', clock=lambda: self.now)
        offer = dict(source_key='fb:auto:50', source_id='50', channel='fb', family='auto', stage='publish',
                     source_status='ready', source_attempt_count=1, status='unknown', resource_keys=['fb:page:100'],
                     unknown_outcome=False, publish_id='', legacy_inflight=False)
        job = store.offer(dict(offer, status='queued'))
        with store.transaction() as c:
            c.execute('UPDATE pipeline_job SET generation=2 WHERE id=?', (job['id'],))
            store.event(c, job['id'], 'authorized_fb_unpublished_recovery', dict(source_key=job['source_key'],
                        actor='operator_authorized', generation=1, attempts=1), store.now())
        self.assertEqual(store.offer(offer)['status'], 'queued')
        # Neither a fresh unknown nor any new write can inherit old permission.
        self.assertEqual(store.offer(dict(offer, unknown_outcome=True))['status'], 'unknown')
        with store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='queued',external_intent=1 WHERE id=?", (job['id'],))
        self.assertEqual(store.offer(offer)['status'], 'unknown')


class SourceErrorsTests(unittest.TestCase):
    def test_group_wrapper_retries_only_transient_select(self):
        for code, transient, calls in [('source_busy', True, 2), ('source_query_rejected', False, 1)]:
            mysql = SimpleNamespace(schema='ads_ai', select=MagicMock(side_effect=TemplateError(code, code, 503)))
            source = GroupPoolSource(SimpleNamespace(mysql=mysql))
            with self.assertRaises(Exception) as error:
                source._select('SELECT * FROM ads_facebook_page_group_ins', [], maximum=10)
            self.assertEqual(error.exception.code, 'group_pool_source_unavailable')
            self.assertEqual(error.exception.retryable, transient)
            self.assertEqual(mysql.select.call_count, calls)

    def test_identity_preserves_safe_503_and_permission_error(self):
        client = IdentityClient({'FB_AUTO_POST_INTERNAL_TOKEN': 'a' * 32})
        for status, code in [(503, 'mapping_busy'), (503, 'mapping_unavailable'), (403, 'identity_permission_denied')]:
            session = MagicMock()
            response = session.__enter__.return_value.post.return_value.__enter__.return_value
            response.status_code = status
            response.iter_content.return_value = [json.dumps({'code': code}).encode()]
            with patch('features.fb_video_publish.identity_client.requests.Session', return_value=session):
                with self.assertRaises(GatewayError) as error:
                    client.resolve('tenant', 'owner')
            self.assertEqual((error.exception.code, error.exception.status), (code, status))


if __name__ == '__main__':
    unittest.main()
