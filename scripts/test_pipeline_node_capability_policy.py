"""Unavailable tools stop receiving new work without draining a healthy node."""
import datetime as dt
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from features.pipeline_dispatch.core import DispatchError, DispatchStore, stamp


class NodeCapabilityPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'dispatch.sqlite3'
        self.clock = lambda: dt.datetime(2026, 11, 3, 6, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(self.path, self.clock)
        self.capabilities = ['prepare:youtube', 'youtube_prepare', 'prepare:tt', 'publish:x']
        for node in ('cpu-main', 'hk-gpu'):
            self.store.register_node(node, self.capabilities)
            for stage in ('prepare', 'publish'):
                self.store.worker_heartbeat(node, stage, stage)
        for channel in ('youtube', 'tt', 'x'):
            self.store.channel_enabled(channel, True)
        self.before_config = self.store.config()

    def restrict(self):
        self.store = DispatchStore(self.path, self.clock, capability_exclusions={
            'hk-gpu': ['prepare:youtube', 'youtube_prepare']})

    def offer(self, key, channel='youtube', stage='prepare'):
        return self.store.offer({'source_key': key, 'source_id': key, 'channel': channel,
            'family': 'preparation', 'stage': stage, 'scheduled_at': stamp(self.clock()),
            'resource_keys': [key], 'capabilities': [stage + ':' + channel]})

    def claim(self, node, stage='prepare'):
        return self.store.claim(node, stage, stage)['job']

    def test_cached_registration_is_filtered_before_next_heartbeat(self):
        self.offer('cover:one')
        self.restrict()
        self.assertIsNone(self.claim('hk-gpu'))
        self.assertEqual(self.claim('cpu-main')['source_key'], 'cover:one')
        self.assertEqual(self.store.config(), self.before_config)

    def test_repeated_registration_cannot_restore_disabled_tool_other_work_continues(self):
        self.restrict()
        for _ in range(2):
            self.store.register_node('hk-gpu', self.capabilities)
        with closing(self.store.connect()) as db:
            caps = json.loads(db.execute("SELECT capabilities_json FROM pipeline_node WHERE node_id='hk-gpu'").fetchone()[0])
        self.assertEqual(set(caps), {'prepare:tt', 'publish:x'})
        self.offer('cover:two')
        self.offer('tt:one', 'tt')
        self.offer('x:one', 'x', 'publish')
        self.assertEqual(self.claim('hk-gpu')['source_key'], 'tt:one')
        self.assertEqual(self.claim('hk-gpu', 'publish')['source_key'], 'x:one')
        self.assertEqual(self.store.config(), self.before_config)

    def test_active_claim_survives_policy_and_can_finish(self):
        self.offer('cover:active')
        active = self.claim('hk-gpu')
        before = self.store.get_job(active['id'])
        self.restrict()
        self.assertEqual(self.store.get_job(active['id']), before)
        self.store.heartbeat(active, 'cover_generation')
        self.store.complete(active, 'cover-complete', {'dispatch_completed': True, 'artifact': {
            'url': 'https://media.example/cover.jpg', 'sha256': 'a' * 64, 'size': 1024}}, canonical_verified=True)
        self.assertEqual(self.store.get_job(active['id'])['status'], 'completed')
        self.assertEqual(self.store.config(), self.before_config)

    def test_invalid_policy_rejected_and_removal_allows_explicit_re_registration(self):
        for value in ([], {'hk-gpu': 'prepare:youtube'}, {'hk-gpu': [None]}, {1: []}):
            with self.subTest(value=value), self.assertRaisesRegex(DispatchError, 'node_capability_policy_invalid'):
                DispatchStore(self.path, self.clock, capability_exclusions=value)
        self.restrict()
        self.store.register_node('hk-gpu', self.capabilities)
        self.store = DispatchStore(self.path, self.clock)
        self.store.register_node('hk-gpu', self.capabilities)
        self.offer('cover:restored')
        self.assertIsNotNone(self.claim('hk-gpu'))


if __name__ == '__main__':
    unittest.main()
