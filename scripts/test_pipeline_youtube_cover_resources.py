"""Covers can prepare under an upload hold; platform writes remain fenced."""
import concurrent.futures
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, stamp


class CoverResourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = dt.datetime(2026, 11, 3, 2, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(temp.name) / 'dispatch.sqlite', lambda: self.now)
        self.adapter = LedgerAdapter(LedgerSpec('youtube', 'preparation', 'unused', 'youtube_auto_preparation',
                                               state='state'), driver=object())
        self.store.channel_enabled('youtube', True)
        for node in ('cpu-main', 'hk-gpu'):
            self.store.register_node(node, ['prepare:youtube', 'publish:youtube'])
            for worker, stage in [('prep', 'prepare'), ('pub', 'publish')]:
                self.store.worker_heartbeat(node, worker, stage)

    def cover(self, identity='a' * 32):
        return self.adapter.project({'id': identity, 'state': 'queued_generation', 'version': 1,
            'created_at': stamp(self.now), 'scheduled_at': stamp(self.now),
            'body': json.dumps({'channel': {'channel_id': 'UC-example'}, 'current_version': 1})})

    def publication(self, identity, **extra):
        return dict(source_key='youtube:publish:' + identity, source_id=identity, family='publish',
                    channel='youtube', stage='publish', capabilities=['publish:youtube'],
                    scheduled_at=stamp(self.now), source_created_at=stamp(self.now),
                    resource_keys=['youtube:channel:UC-example'], **extra)

    def test_cover_runs_while_historical_unknown_still_blocks_new_upload(self):
        held = self.store.offer(self.publication('old', status='unknown', unknown_outcome=True))
        offer = self.cover()
        self.assertEqual(offer['resource_keys'], ['youtube:cover:' + 'a' * 32])
        self.store.offer(offer)
        self.store.offer(self.publication('new'))
        cover = self.store.claim('cpu-main', 'prep', 'prepare')['job']
        self.assertEqual(cover['source_id'], 'a' * 32)
        self.assertIsNone(self.store.claim('hk-gpu', 'pub', 'publish')['job'])
        self.assertEqual(self.store.get_job(held['id'])['status'], 'unknown')

    def test_never_claimed_existing_cover_refreshes_without_touching_upload_lock(self):
        offer = self.cover()
        held = self.store.offer(self.publication('old', status='unknown', unknown_outcome=True))
        old = self.store.offer(dict(offer, resource_keys=['youtube:channel:UC-example']))
        self.assertIsNone(self.store.claim('cpu-main', 'prep', 'prepare')['job'])
        refreshed = self.store.offer(offer)
        self.assertEqual((refreshed['id'], refreshed['generation']), (old['id'], 0))
        self.assertEqual(refreshed['resource_keys'], offer['resource_keys'])
        self.assertIsNotNone(self.store.claim('cpu-main', 'prep', 'prepare')['job'])
        self.store.offer(self.publication('new'))
        self.assertIsNone(self.store.claim('hk-gpu', 'pub', 'publish')['job'])
        self.assertEqual(self.store.get_job(held['id'])['status'], 'unknown')

    def test_running_and_paused_cover_resources_are_not_rewritten(self):
        offer = self.cover()
        old = self.store.offer(dict(offer, resource_keys=['youtube:channel:UC-example']))
        active = self.store.claim('cpu-main', 'prep', 'prepare')['job']
        self.store.offer(offer)
        self.assertEqual(self.store.get_job(old['id'])['resource_keys'], active['resource_keys'])
        paused_offer = self.cover('b' * 32)
        paused = self.store.offer(dict(paused_offer, resource_keys=['youtube:channel:UC-other'], status='backlog_paused'))
        self.store.offer(paused_offer)
        self.assertEqual(self.store.get_job(paused['id'])['resource_keys'], ['youtube:channel:UC-other'])
        self.assertEqual(self.store.get_job(paused['id'])['status'], 'backlog_paused')

    def test_two_nodes_cannot_generate_the_same_cover_concurrently(self):
        self.store.offer(self.cover())
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda node: self.store.claim(node, 'prep', 'prepare')['job'], ('cpu-main', 'hk-gpu')))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_separate_covers_on_same_channel_can_prepare_together(self):
        self.store.offer(self.cover('a' * 32))
        self.store.offer(self.cover('b' * 32))
        self.assertIsNotNone(self.store.claim('cpu-main', 'prep', 'prepare')['job'])
        self.assertIsNotNone(self.store.claim('hk-gpu', 'prep', 'prepare')['job'])

    def test_publish_projection_keeps_channel_resource_and_missing_identity_stays_blocked(self):
        adapter = LedgerAdapter(LedgerSpec('youtube', 'publish', 'unused', 'drama_youtube_publish'), driver=object())
        offer = adapter.project({'id': 1, 'status': 'queued', 'channel_id': 'UC-example', 'created_at': stamp(self.now)})
        self.assertEqual(offer['resource_keys'], ['youtube:channel:UC-example'])
        missing = self.adapter.project({'id': 'c' * 32, 'state': 'queued_generation', 'version': 1,
                                        'created_at': stamp(self.now), 'body': '{}'})
        self.assertEqual(missing['resource_keys'], [])


if __name__ == '__main__':
    unittest.main()
