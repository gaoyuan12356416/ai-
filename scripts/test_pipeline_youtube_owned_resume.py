"""A durable dispatch cover claim cannot deadlock its own channel recovery."""
import datetime as dt
import json
import unittest

from scripts import test_pipeline_youtube_preparation as fixtures
from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchError, DispatchStore, stamp
from features.pipeline_dispatch.cutover import Cutover
from features.pipeline_dispatch.service import Coordinator


class OwnedCoverResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.YouTubePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.now = dt.datetime(2026, 11, 3, 2, tzinfo=dt.timezone.utc)
        self.dispatch = DispatchStore(self.fixture.root / 'dispatch.sqlite', lambda: self.now)
        self.adapter = LedgerAdapter(LedgerSpec('youtube', 'preparation', self.fixture.store.db_path,
                                    'youtube_auto_preparation', state='state'), self.fixture.bridge)
        self.coordinator = Coordinator(self.dispatch, AdapterRegistry([self.adapter]),
                                       internal_token='i' * 32, master_key='m' * 32)
        self.control = Cutover(self.coordinator)
        # Freeze a held plan before this new task's creation time.
        self.plan = self.control.plan('youtube', '2026-11-01T00:00:00Z')
        self.control.apply(self.plan['plan_id'], self.plan['sha256'])

    def enable(self):
        return self.control.enable(self.plan['plan_id'], self.plan['sha256'])

    def snapshot(self):
        with self.fixture.workflow.db() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM youtube_auto_preparation')]

    def test_lost_descriptor_owned_by_dispatch_allows_resume_without_native_changes(self):
        self.fixture.bridge.reserve(self.fixture.job, self.fixture.claim)
        before = self.snapshot()
        offer = self.adapter.project(self.adapter.read(self.fixture.identity))
        self.assertFalse(offer['legacy_inflight'])
        self.assertFalse(offer['pause_eligible'])
        self.assertTrue(offer['execution_available'])
        self.assertTrue(self.enable()['enabled'])
        self.assertEqual(self.snapshot(), before)

    def test_prefix_without_matching_claim_and_changed_identity_still_block_enable(self):
        self.fixture.bridge.reserve(self.fixture.job, self.fixture.claim)
        original = self.snapshot()[0]
        variants = [('lease_token', 'pipeline:unrelated'), ('version', 2)]
        for column, value in variants:
            with self.subTest(column=column):
                with self.fixture.workflow.db(True) as conn:
                    conn.execute('UPDATE youtube_auto_preparation SET '+column+'=? WHERE id=?', (value, self.fixture.identity))
                with self.assertRaisesRegex(DispatchError, 'legacy_items_still_running'):
                    self.enable()
                with self.fixture.workflow.db(True) as conn:
                    conn.execute('UPDATE youtube_auto_preparation SET '+column+'=? WHERE id=?', (original[column], self.fixture.identity))
        with self.fixture.workflow.db(True) as conn:
            conn.execute('DELETE FROM pipeline_youtube_claim')
        with self.assertRaisesRegex(DispatchError, 'legacy_items_still_running'):
            self.enable()

    def test_preparation_only_admission_keeps_capacity_and_uploads_paused(self):
        self.fixture.bridge.reserve(self.fixture.job, self.fixture.claim)
        cfg = self.dispatch.config()
        result = self.coordinator.route('/internal/pipeline/admin/cutover-enable',
                    {'plan_id': self.plan['plan_id'], 'sha256': self.plan['sha256'], 'preparation_only': True}, {'internal': True})
        self.assertTrue(result['preparation_only'])
        after = self.dispatch.config()
        for key in ('hosts', 'channel_limits', 'global_publish_limit'):
            self.assertEqual(after[key], cfg[key])
        self.dispatch.register_node('cpu-main', ['prepare:youtube', 'publish:youtube'])
        self.dispatch.worker_heartbeat('cpu-main', 'prep', 'prepare')
        self.dispatch.worker_heartbeat('cpu-main', 'pub', 'publish')
        self.dispatch.offer({'source_key': 'youtube:publish:next', 'source_id': 'next', 'channel': 'youtube',
                             'family': 'publish', 'stage': 'publish', 'scheduled_at': stamp(self.now),
                             'resource_keys': ['youtube:channel:other'], 'capabilities': ['publish:youtube']})
        self.assertIsNone(self.dispatch.claim('cpu-main', 'pub', 'publish')['job'])
        self.assertIsNotNone(self.dispatch.claim('cpu-main', 'prep', 'prepare')['job'])

    def test_full_enable_releases_stage_pause_and_stop_disables_both(self):
        self.control.enable(self.plan['plan_id'], self.plan['sha256'], preparation_only=True)
        self.assertIn('youtube', self.dispatch.config()['preparation_only_channels'])
        self.enable()
        self.assertNotIn('youtube', self.dispatch.config()['preparation_only_channels'])
        self.dispatch.channel_enabled('youtube', False)
        self.assertNotIn('youtube', self.dispatch.config()['enabled_channels'])
        with self.assertRaisesRegex(DispatchError, 'preparation_only_invalid'):
            self.control.enable(self.plan['plan_id'], self.plan['sha256'], preparation_only='false')


if __name__ == '__main__':
    unittest.main()
