import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.x_video_publish import transport
from features.x_video_publish.errors import PublishingError
from features.pipeline_dispatch import maintenance, views
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.failure_context import display_code


class IdentityRetryTests(unittest.TestCase):
    def test_identity_recovers_after_restart_and_reads_current_permissions(self):
        transient = PublishingError('service_unavailable', 'unavailable', 503)
        with patch.object(transport, 'request', side_effect=[transient, transient, {'actor': {'role': 'user'}}]) as request, \
             patch.object(transport.time, 'sleep') as sleep:
            self.assertEqual(transport.broker('identity', {'tenant_key': 't', 'user_id': 'u'}, 'x' * 32),
                             {'actor': {'role': 'user'}})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permission_loss_and_nontransient_errors_are_not_retried(self):
        for code, status in [('identity_permission_denied', 403), ('invalid_request', 400), ('unknown_failure', 503)]:
            with patch.object(transport, 'request', side_effect=PublishingError(code, 'failure', status)) as request:
                with self.assertRaises(PublishingError):
                    transport.broker('identity', {}, 'x' * 32)
                self.assertEqual(request.call_count, 1)

    def test_source_is_not_retried_and_identity_failure_is_bounded(self):
        failure = PublishingError('identity_unavailable', 'failure', 503)
        with patch.object(transport, 'request', side_effect=failure) as request, patch.object(transport.time, 'sleep'):
            with self.assertRaises(PublishingError):
                transport.broker('source', {}, 'x' * 32)
            self.assertEqual(request.call_count, 1)
            request.reset_mock()
            with self.assertRaises(PublishingError):
                transport.broker('identity', {}, 'x' * 32)
            self.assertEqual(request.call_count, 6)


class DrainTests(unittest.TestCase):
    def test_scope_keeps_other_channels_and_other_x_lane_available(self):
        main = {'channel': 'x', 'source_id': '15'}
        tag = {'channel': 'x', 'source_id': '9000000000015'}
        facebook = {'channel': 'fb', 'source_id': '15'}
        self.assertTrue(maintenance.matches('drama-material-api.service', main))
        self.assertFalse(maintenance.matches('drama-material-api.service', facebook))
        self.assertTrue(maintenance.matches('x-tag-post.service', tag))
        self.assertFalse(maintenance.matches('x-tag-post.service', main))
        self.assertTrue(maintenance.matches('pipeline-coordinator.service', facebook))

    def test_hold_blocks_new_claim_but_existing_claim_heartbeats_and_completes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DispatchStore(Path(folder) / 'db')
            store.channel_enabled('x', True)
            store.register_node('cpu-main', ['publish:x'], {})
            store.worker_heartbeat('cpu-main', 'one', 'publish')
            store.worker_heartbeat('cpu-main', 'two', 'publish')
            for i in (1, 2):
                store.offer({'source_key': 'x:queue:' + str(i), 'source_id': str(i), 'channel': 'x',
                             'family': 'queue', 'stage': 'publish', 'status': 'ready',
                             'resource_keys': ['x:account:' + str(i)], 'capabilities': ['publish:x'],
                             'execution_available': True})
            claim = store.claim('cpu-main', 'one', 'publish')['job']
            self.assertIsNotNone(claim)
            with patch.object(maintenance, 'active_holds', return_value=['x-post-automation.service']):
                result = store.claim('cpu-main', 'two', 'publish')
                self.assertIsNone(result['job'])
                self.assertIn('service_draining', result['idle_reasons'])
                self.assertTrue(store.heartbeat(claim)['accepted'])
            # Releasing the startup hold admits the next original queue.
            self.assertIsNotNone(store.claim('cpu-main', 'two', 'publish')['job'])


class FailureDisplayTests(unittest.TestCase):
    def test_real_codes_and_phase_are_preserved(self):
        row = dict(channel='x', stage='publish', status='failed', error_code='x_upstream_error',
                   error_message='X media (HTTP 403): Your account is temporarily locked.')
        self.assertEqual(display_code(row), 'x_account_temporarily_locked')
        row.update(status='unknown', error_code='coordinator_unavailable', error_message='')
        self.assertEqual(display_code(row, {'x_media_initialized'}), 'x_upload_interrupted:coordinator_unavailable')
        self.assertEqual(display_code(row, {'x_media_initialized', 'x_media_uploaded'}),
                         'x_post_outcome_unknown:coordinator_unavailable')

    def test_summary_uses_actual_locked_and_unknown_codes_without_changing_state(self):
        with tempfile.TemporaryDirectory() as folder:
            now = dt.datetime(2026, 11, 13, 8, tzinfo=dt.timezone.utc)
            store = DispatchStore(Path(folder) / 'db', clock=lambda: now)
            store.source_health('x', 'queue')
            for i, status, code, message in [(1, 'failed', 'x_upstream_error', 'Your account is temporarily locked.'),
                                              (2, 'unknown', 'coordinator_operation_failed', '')]:
                job = store.offer({'source_key': 'x:queue:' + str(i), 'source_id': str(i), 'channel': 'x',
                                   'stage': 'publish', 'family': 'queue', 'resource_keys': ['x:account:' + str(i)]})
                with store.transaction() as c:
                    c.execute('UPDATE pipeline_job SET generation=1,status=?,error_code=?,error_message=?,finished_at=? WHERE id=?',
                              (status, code, message, stamp(now), job['id']))
                    if i == 2:
                        c.execute('INSERT INTO pipeline_checkpoint VALUES(?,?,?,?,?,?)',
                                  ('init', job['id'], 1, 'x_media_initialized', 'a' * 64, stamp(now)))
            actor = {'is_admin': True, 'allowed_channels': ['x'], 'allowed_modules': ['x_accounts']}
            result = views.query(store, 'summary', actor, {})
            row = next(r for r in result['channels'] if r['stage'] == 'publish')
            self.assertEqual(row['abnormal'], 2)
            self.assertEqual(row['abnormal_reasons'], {'x_account_temporarily_locked': 1,
                                                       'x_upload_interrupted:coordinator_operation_failed': 1})
            self.assertEqual(store.get_job(job['id'])['status'], 'unknown')
            views.invalidate_cache(store)


if __name__ == '__main__':
    unittest.main()
