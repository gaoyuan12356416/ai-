"""Offline checks for read-only probes, nonblocking snapshots and task permissions."""
import copy
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_youtube_auto_service as fixtures
from features.youtube_auto_publish.player_cards import ERROR_TTL, TTL, PlayerCardStatusCache, YouTubeCardProbe, result

VIDEO = 'AbcdEFG_123'
CHANNEL = 'UC' + 'a' * 22
SOURCE = dict(app_id='1479', channel_local_id='258', youtube_account_id='250', channel_id=CHANNEL, video_id=VIDEO)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.item = {'id': VIDEO, 'snippet': {'channelId': CHANNEL},
                     'status': {'privacyStatus': 'public', 'uploadStatus': 'processed', 'embeddable': True},
                     'contentDetails': {}}
        self.response = SimpleNamespace(status_code=200, json=lambda: {'items': [self.item]})
        self.session = Mock(); self.session.get.return_value = self.response
        self.repo = Mock(); self.client = Mock(); self.client.refresh_access_token.return_value = 'private-test-token'
        self.card = Mock(return_value={'eligible': True, 'state': 'ready'})
        self.probe = YouTubeCardProbe(self.repo, client=self.client, card_check=self.card, session_factory=lambda: self.session)

    def test_verified_player_uses_only_read_endpoint_and_safe_result(self):
        value = self.probe(SOURCE)
        self.assertTrue(value['eligible']); self.assertEqual(value['state'], 'ready')
        call = self.session.get.call_args
        self.assertEqual(call.args[0], 'https://www.googleapis.com/youtube/v3/videos')
        self.assertEqual(call.kwargs['params']['id'], VIDEO)
        self.assertFalse(call.kwargs['allow_redirects']); self.assertFalse(self.session.trust_env)
        self.client.verify_channel_identity.assert_called_once_with('private-test-token', CHANNEL)
        self.assertNotIn('private-test-token', str(value)); self.session.close.assert_called_once()
        self.session.post.assert_not_called()

    def test_nonplayer_and_failed_metadata_never_query_credentials(self):
        for card, expected in [({'state': 'not_player', 'card_type': 'summary_large_image'}, 'not_player'),
                               ({'state': 'unavailable'}, 'unavailable'), ({'state': 'invalid_player'}, 'unavailable')]:
            self.card.return_value = card
            self.assertEqual(self.probe(SOURCE)['state'], expected)
        self.repo.credential.assert_not_called(); self.session.get.assert_not_called()

    def test_unlisted_private_unprocessed_and_nonembeddable_are_not_green(self):
        for changes, expected in [({'privacyStatus': 'unlisted'}, 'not_public'), ({'privacyStatus': 'private'}, 'not_public'),
                                  ({'uploadStatus': 'uploaded'}, 'not_public'), ({'embeddable': False}, 'not_embeddable')]:
            self.item['status'] = dict(privacyStatus='public', uploadStatus='processed', embeddable=True, **{})
            self.item['status'].update(changes)
            value = self.probe(SOURCE)
            self.assertEqual(value['state'], expected); self.assertFalse(value['eligible'])

    def test_region_and_age_restrictions_are_distinct(self):
        for details, expected in [({'regionRestriction': {'blocked': ['RU']}}, 'restricted'),
                                  ({'regionRestriction': {'allowed': []}}, 'restricted'),
                                  ({'regionRestriction': {'allowed': ['US']}}, 'restricted'),
                                  ({'regionRestriction': {'blocked': []}}, 'ready'),
                                  ({'contentRating': {'ytRating': 'ytAgeRestricted'}}, 'restricted')]:
            self.item['contentDetails'] = details
            self.assertEqual(self.probe(SOURCE)['state'], expected)

    def test_missing_or_different_video_and_channel_fail_closed(self):
        for items in [[], [dict(self.item, id='differentID')],
                      [dict(self.item, snippet={'channelId': 'UC' + 'b' * 22})], [self.item, self.item]]:
            self.response.json = lambda items=items: {'items': items}
            self.assertEqual(self.probe(SOURCE)['state'], 'unavailable')

    def test_reuses_credentials_only_for_same_channel_and_clears_unauthorized(self):
        self.probe(SOURCE); self.probe(SOURCE)
        self.repo.credential.assert_called_once(); self.client.verify_channel_identity.assert_called_once()
        self.response.status_code = 401
        self.assertEqual(self.probe(SOURCE)['state'], 'unavailable')
        self.response.status_code = 200
        self.probe(SOURCE)
        self.assertEqual(self.repo.credential.call_count, 2)
        self.probe(dict(SOURCE, channel_id='UC' + 'b' * 22))
        self.assertEqual(self.repo.credential.call_count, 3)

    def test_invalid_http_response_and_network_failure_close_session(self):
        self.response.status_code = 503
        self.assertEqual(self.probe(SOURCE)['state'], 'unavailable')
        self.session.get.side_effect = RuntimeError('private-test-token')
        with self.assertRaises(RuntimeError): self.probe(SOURCE)
        self.assertEqual(self.session.close.call_count, 2)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000.0

    def cache(self, probe, **options):
        cache = PlayerCardStatusCache(probe, clock=lambda: self.now, **options)
        self.addCleanup(cache.close)
        return cache

    def wait_value(self, cache, source=SOURCE):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = cache.get(source)
            if value['state'] not in ('checking', 'pending'):
                return value
            time.sleep(.005)
        self.fail('background probe did not complete')

    def test_get_returns_before_probe_finishes_and_deduplicates(self):
        release, started = threading.Event(), threading.Event()
        calls = []
        def probe(source):
            calls.append(source); started.set(); release.wait(3)
            return result('ready')
        cache = self.cache(probe)
        try:
            self.assertEqual(cache.get(SOURCE)['state'], 'checking')
            self.assertTrue(started.wait(1))
            for _ in range(10): self.assertFalse(cache.get(SOURCE)['eligible'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(set(calls[0]), set(SOURCE))
        finally: release.set()
        self.assertTrue(self.wait_value(cache)['eligible'])

    def test_expired_success_loses_green_and_failure_is_not_stale_success(self):
        probe = Mock(return_value=result('ready'))
        cache = self.cache(probe)
        cache.get(SOURCE); before = self.wait_value(cache)
        self.assertTrue(before['eligible'])
        self.now += TTL + 1
        probe.side_effect = RuntimeError('SECRET credential request')
        checking = cache.get(SOURCE)
        self.assertEqual(checking['state'], 'checking'); self.assertFalse(checking['eligible'])
        failed = self.wait_value(cache)
        self.assertEqual(failed['state'], 'unavailable'); self.assertNotIn('SECRET', str(failed))
        calls = probe.call_count
        self.now += ERROR_TTL - 1
        self.assertEqual(cache.get(SOURCE), failed); self.assertEqual(probe.call_count, calls)
        self.now += 2
        self.assertEqual(cache.get(SOURCE)['state'], 'checking')

    def test_pending_bound_and_lru_do_not_evict_inflight(self):
        release = threading.Event()
        def probe(source): release.wait(3); return result('ready')
        cache = self.cache(probe, max_pending=1, max_entries=1)
        other = dict(SOURCE, video_id='AbcdEFG_124')
        try:
            cache.get(SOURCE)
            self.assertEqual(cache.get(other)['state'], 'pending')
            self.assertEqual(len(cache._pending), 1); self.assertEqual(len(cache._entries), 1)
        finally: release.set()
        self.wait_value(cache)
        self.assertEqual(cache.get(other)['state'], 'checking')
        self.wait_value(cache, other)
        self.assertEqual(len(cache._entries), 1)

    def test_invalid_identity_and_closed_executor_are_safe(self):
        probe = Mock(return_value=result('ready'))
        cache = self.cache(probe)
        self.assertEqual(cache.get(dict(SOURCE, video_id='https://evil.invalid'))['state'], 'unavailable')
        probe.assert_not_called()
        cache.close()
        self.assertEqual(cache.get(SOURCE)['state'], 'unavailable')


class TaskProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowCase(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.task = self.fixture.enqueue()
        self.ledger = self.fixture.store.tasks[self.task['publish_id']]
        self.ledger.update(SOURCE, video_state='published', status='published')
        self.cards = Mock()
        self.cards.get.return_value = result('checking', checked_at='', expires_at='')
        self.fixture.service.player_cards = self.cards

    def test_compact_revision_changes_on_badge_but_stable_snapshot_is_unchanged(self):
        service, actor = self.fixture.service, self.fixture.actor
        first = service.list_tasks(actor, compact=True)
        self.assertEqual(first['items'][0]['x_player_card']['state'], 'checking')
        self.assertTrue(service.list_tasks(actor, compact=True, since=first['revision'])['unchanged'])
        self.cards.get.return_value = result('ready', checked_at='2026-09-17T04:00:00Z', expires_at='2026-09-17T04:15:00Z')
        second = service.list_tasks(actor, compact=True, since=first['revision'])
        self.assertNotIn('unchanged', second); self.assertTrue(second['items'][0]['x_player_card']['eligible'])
        self.assertNotEqual(first['revision'], second['revision'])

    def test_other_owners_and_tenants_never_enqueue_probe(self):
        service = self.fixture.service
        self.assertEqual(service.list_tasks(self.fixture.other, compact=True)['items'], [])
        self.assertEqual(service.list_tasks(self.fixture.foreign, compact=True)['items'], [])
        with self.assertRaises(Exception): service.get_task(self.fixture.other, self.task['id'])
        self.cards.get.assert_not_called()

    def test_comment_failure_keeps_marker_and_nonpublic_tasks_never_probe(self):
        service = self.fixture.service
        self.ledger.update(status='partial_failed', reviewed_phase='comment', comment_status='failed')
        value = service.get_task(self.fixture.actor, self.task['id'])['task']
        self.assertEqual(value['status'], 'comment_failed'); self.assertIsNotNone(value['x_player_card'])
        self.cards.get.reset_mock(); self.ledger['video_state'] = 'private'
        value = service.get_task(self.fixture.actor, self.task['id'])['task']
        self.assertIsNone(value['x_player_card']); self.cards.get.assert_not_called()


if __name__ == '__main__': unittest.main()
