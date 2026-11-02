"""Real SQLite checks for submission and authoritative FB confirmation timing."""
import contextlib
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch import views
from features.pipeline_dispatch.core import DispatchStore, DispatchError, stamp


class PhaseMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / 'dispatch.sqlite', clock=lambda: self.now)
        self.addCleanup(views.invalidate_cache, self.store)
        self.admin = {'is_admin': True, 'allowed_channels': ['x', 'fb', 'tt', 'youtube']}
        self.store.register_node('cpu-main', ['publish:' + channel for channel in self.admin['allowed_channels']])
        self.store.worker_heartbeat('cpu-main', 'publisher', 'publish')
        for channel in self.admin['allowed_channels']: self.store.channel_enabled(channel, True)

    def claim(self, key, channel='fb', owner='alice'):
        self.store.offer({'source_key': key, 'source_id': key, 'channel': channel, 'family': 'video',
            'stage': 'publish', 'owner_user_id': owner, 'owner_tenant_key': 'tenant',
            'required_module': 'fb_video_publish', 'resource_keys': [channel + ':account:' + key],
            'capabilities': ['publish:' + channel], 'scheduled_at': stamp(self.now), 'source_created_at': stamp(self.now)})
        self.store.worker_heartbeat('cpu-main', 'publisher', 'publish')
        return self.store.claim('cpu-main', 'publisher', 'publish')['job']

    def finish(self, claim, post_id='123', metrics=None, published_at=None):
        proof = {'source_key': claim['source_key'], 'publish_id': post_id, 'published_at': published_at or stamp(self.now)}
        self.store.record_publication(claim['id'], proof, canonical_verified=True)
        self.store.complete(claim, 'complete-' + claim['source_key'], {'published': True, 'canonical_receipt': json.dumps(proof), 'phase_metrics': metrics or {}}, canonical_verified=True)

    def metric(self, channel, phase, actor=None):
        views.invalidate_cache(self.store)
        data = views.query(self.store, 'summary', actor or self.admin, {})
        return next(row for row in data['efficiency'] if (row['channel'], row['stage'], row['phase']) == (channel, 'publish', phase))

    def test_submission_is_not_upload_and_existing_processing_survives(self):
        for channel, metrics in [('fb', {'publish': 17}), ('tt', {'publish_initialize': 3, 'platform_processing': 75}), ('youtube', {'platform_upload': 11})]:
            job = self.claim(channel, channel)
            self.finish(job, metrics=metrics)
        self.assertEqual(self.metric('fb', 'platform_submit')['p50_seconds'], 17)
        self.assertEqual(self.metric('tt', 'platform_submit')['p50_seconds'], 3)
        self.assertEqual(self.metric('tt', 'platform_processing')['p50_seconds'], 75)
        for channel in ('fb', 'tt'):
            metric = self.metric(channel, 'platform_upload')
            self.assertEqual((metric['applicable'], metric['samples'], metric['p50_seconds']), (False, 0, None))
        self.assertEqual(self.metric('youtube', 'platform_upload')['p50_seconds'], 11)
        self.assertTrue(self.metric('youtube', 'platform_processing')['applicable'])
        self.assertIsNone(self.metric('youtube', 'platform_processing')['p50_seconds'])

    def test_exact_checkpoint_identity_and_replays_count_once(self):
        job = self.claim('good')
        receipt = {'kind': 'success', 'post_id': '123', 'access_token': 'NEVER_STORE', 'operation_id': 'op-1'}
        self.store.checkpoint(job, 'checkpoint-good', 'fb_publish_attempt', receipt, canonical_verified=True)
        self.assertTrue(self.store.checkpoint(job, 'checkpoint-good', 'fb_publish_attempt', receipt, canonical_verified=True)['duplicate'])
        with self.assertRaises(DispatchError):
            self.store.checkpoint(job, 'checkpoint-good', 'fb_publish_attempt', {**receipt, 'post_id': '456'}, canonical_verified=True)
        self.now += dt.timedelta(seconds=250)
        self.finish(job)
        self.store.record_publication(job['id'], {'source_key': 'good', 'publish_id': '123', 'published_at': stamp(self.now + dt.timedelta(seconds=100))}, canonical_verified=True)
        metric = self.metric('fb', 'platform_confirmation_wait')
        self.assertEqual((metric['samples'], metric['p50_seconds']), (1, 250))
        with contextlib.closing(self.store.connect()) as conn:
            events = conn.execute("SELECT details_json FROM pipeline_event WHERE job_id=? AND event_type='checkpoint'", (job['id'],)).fetchall()
        self.assertEqual(len(events), 1)
        evidence = json.loads(events[0][0])
        self.assertEqual(set(evidence), {'phase', 'generation', 'checkpoint_id', 'post_id', 'kind'})
        self.assertNotIn('NEVER_STORE', events[0][0])

    def test_failed_unknown_mismatched_or_unverified_receipts_produce_no_wait(self):
        for index, receipt in enumerate(({'kind': 'credential_failure', 'post_id': '123'}, {'kind': 'unknown', 'post_id': '123'}, {'kind': 'success', 'post_id': '456'})):
            job = self.claim('bad-' + str(index))
            self.store.checkpoint(job, 'checkpoint-' + str(index), 'fb_publish_attempt', receipt, canonical_verified=True)
            self.now += dt.timedelta(seconds=10)
            self.finish(job)
        job = self.claim('unverified')
        with self.assertRaises(DispatchError): self.store.checkpoint(job, 'not-verified', 'fb_publish_attempt', {'kind': 'success', 'post_id': '123'})
        self.finish(job)
        self.assertEqual(self.metric('fb', 'platform_confirmation_wait')['samples'], 0)

    def test_old_hash_only_and_other_generation_or_negative_interval_are_not_filled(self):
        for kind in ('old', 'other-generation', 'negative'):
            job = self.claim(kind)
            self.store.checkpoint(job, 'checkpoint-' + kind, 'fb_publish_attempt', {'kind': 'success', 'post_id': '123'}, canonical_verified=True)
            with self.store.transaction() as conn:
                if kind == 'old': conn.execute("UPDATE pipeline_event SET details_json=? WHERE job_id=? AND event_type='checkpoint'", (json.dumps({'phase': 'fb_publish_attempt'}), job['id']))
                if kind == 'other-generation': conn.execute('UPDATE pipeline_checkpoint SET generation=generation+1 WHERE job_id=?', (job['id'],))
            self.finish(job, published_at=stamp(self.now - dt.timedelta(seconds=1)) if kind == 'negative' else None)
        self.assertEqual(self.metric('fb', 'platform_confirmation_wait')['samples'], 0)

    def test_confirmation_samples_respect_owner_scope(self):
        for owner, seconds in [('alice', 10), ('bob', 900)]:
            job = self.claim(owner, owner=owner)
            self.store.checkpoint(job, 'checkpoint-' + owner, 'fb_publish_attempt', {'kind': 'success', 'post_id': '123'}, canonical_verified=True)
            self.now += dt.timedelta(seconds=seconds)
            self.finish(job)
        actor = {'is_admin': False, 'user_id': 'alice', 'tenant_key': 'tenant', 'allowed_channels': ['fb'], 'allowed_modules': ['fb_video_publish']}
        metric = self.metric('fb', 'platform_confirmation_wait', actor)
        self.assertEqual((metric['samples'], metric['p50_seconds']), (1, 10))
        self.assertEqual(self.metric('fb', 'platform_confirmation_wait')['samples'], 2)

    def test_confirmation_query_bounds_recent_scoped_publications_before_event_lookup(self):
        for index, owner in enumerate(('alice', 'alice', 'alice', 'bob')):
            job = self.claim('bounded-' + str(index), owner=owner)
            self.store.checkpoint(job, 'checkpoint-bounded-' + str(index), 'fb_publish_attempt', {'kind': 'success', 'post_id': '123'}, canonical_verified=True)
            self.now += dt.timedelta(seconds=10)
            self.finish(job)
        with self.store.transaction() as conn:
            conn.executemany("INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                             [(job['id'], 'heartbeat', '{}', '2026-01-01T00:00:00Z')] * 1000)
        original_connect = self.store.connect
        statements = []
        def traced_connect():
            conn = original_connect()
            conn.set_trace_callback(statements.append)
            return conn
        actor = {'is_admin': False, 'user_id': 'alice', 'tenant_key': 'tenant', 'allowed_channels': ['fb'], 'allowed_modules': ['fb_video_publish']}
        with mock.patch.object(self.store, 'connect', traced_connect), mock.patch.object(views, 'MAX_TIMING_SAMPLES', 2):
            metric = self.metric('fb', 'platform_confirmation_wait', actor)
        # The newest publication belongs to Bob; apply owner scope before LIMIT.
        self.assertEqual((metric['samples'], metric['p50_seconds']), (2, 10))
        statement = next(sql for sql in statements if sql.startswith('WITH recent AS'))
        with contextlib.closing(original_connect()) as conn:
            plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + statement)]
        self.assertTrue(any('SEARCH f USING INDEX pipeline_publication_time' in step and 'published_at>' in step and 'published_at<' in step for step in plan), plan)
        self.assertTrue(any('SEARCH e USING INDEX pipeline_event_job_type' in step and 'job_id=? AND event_type=?' in step for step in plan), plan)
        self.assertFalse(any('SCAN e' in step or 'AUTOMATIC' in step for step in plan), plan)


if __name__ == '__main__': unittest.main()
