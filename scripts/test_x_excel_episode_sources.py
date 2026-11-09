"""Exact episode identity/source checks; no real SQL, media or X requests."""

import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.x_video_publish.episode_sources import EpisodeRepository
from features.x_video_publish.material_identity import is_episode_id, normalize_material_id
from features.x_video_publish.sources import SourceClient, SourceRepository, for_main, ids
from features.x_video_publish.errors import PublishingError


EPISODE = 'abcdef0123456789abcdef0123456789'
OTHER = '1234567890abcdef1234567890abcdef'
MISSING = 'ffffffffffffffffffffffffffffffff'


def resource(mid=EPISODE, **changes):
    return dict(resource_id=mid, app_id=1479, resource_type=2, content_id='series-en',
                drama_name='Fixture Drama', drama_description='A fixture drama description.',
                drama_labels='Romance,Drama', language='en', series_code='22835',
                unlocked_episodes_count=10, sub_number=6, sub_name='Episode 6',
                sub_url='https://media.example.test/episode.mp4', **changes)


def deploy(**changes):
    value = dict(content_id='series-en', app_id=1479, app='ios', language='en', deploy_time=0)
    value.update(changes)
    return value


class FakeSQL:
    schema = 'fixture'

    def __init__(self, episodes, deployments=None):
        self.episodes = episodes
        self.deployments = [deploy()] if deployments is None else deployments
        self.calls = []

    def select(self, sql, params):
        self.calls.append((sql, params))
        return [{'row_kind': kind, 'row_json': json.dumps(row)}
                for kind, rows in [('episode', self.episodes), ('deploy', self.deployments)]
                for row in rows]


class EpisodeSourcesTest(unittest.TestCase):
    def setUp(self):
        self.probe = patch('features.x_video_publish.episode_sources.probe_source_metadata_duration', return_value=120.5)
        self.probe_mock = self.probe.start()
        self.addCleanup(self.probe.stop)
        self.allowlist = patch('features.x_video_publish.episode_sources._allowed_hosts', return_value=('media.example.test',))
        self.allowlist_mock = self.allowlist.start()
        self.addCleanup(self.allowlist.stop)

    def resolve(self, rows, requested=None, deployments=None):
        sql = FakeSQL(rows, deployments)
        return EpisodeRepository(sql, sql.schema).resolve(requested or [EPISODE])

    def test_identity_is_canonical_and_numeric_ids_unchanged(self):
        self.assertEqual(normalize_material_id(EPISODE.upper()), EPISODE)
        self.assertEqual(normalize_material_id('5595933'), '5595933')
        self.assertTrue(is_episode_id('1' * 32))
        self.assertFalse(is_episode_id('5595933'))
        self.assertEqual(ids([EPISODE.upper(), EPISODE, '5595933']), [EPISODE, '5595933'])
        for bad in [123, None, True, '', '0', '001', '1.0', '1e7', 'a' * 31, 'g' * 32, ' ' + EPISODE]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_material_id(bad)

    def test_exact_episode_succeeds_without_other_series_episodes(self):
        sql = FakeSQL([resource()])
        result = SourceRepository(sql).resolve([EPISODE.upper()])
        self.assertEqual(result['errors'], {})
        item = result['items'][EPISODE]
        self.assertEqual(item['episode_number'], 6)
        self.assertEqual(item['source_kind'], 'free_episode')
        self.assertEqual(item['material_key'], EPISODE)
        self.assertEqual(item['source_type'], 'material')
        self.assertEqual(item['duration_seconds'], 120.5)
        self.assertEqual(item['duration_status'], 'verified')
        self.assertEqual(item['language'], 'en')
        self.assertEqual(item['content_id'], 'series-en')
        self.assertEqual(item['series_code'], '22835')
        self.assertEqual(len(sql.calls), 1)
        statement, params = sql.calls[0]
        self.assertEqual(params, [EPISODE, EPISODE])
        self.assertIn('WHERE r.id IN (%s)', statement)
        self.assertNotIn('ORDER BY r.sub_number', statement)
        self.probe_mock.assert_called_once()
        self.assertIn('deadline', self.probe_mock.call_args.kwargs)

    def test_bad_row_does_not_block_another_valid_episode(self):
        bad = resource(OTHER)
        bad['sub_number'] = 11
        result = self.resolve([resource(), bad], [EPISODE, OTHER, MISSING])
        self.assertEqual(set(result['items']), {EPISODE})
        self.assertEqual(result['errors'][OTHER]['code'], 'episode_not_free')
        self.assertEqual(result['errors'][MISSING]['code'], 'free_episode_not_found')
        self.probe_mock.assert_called_once()

    def test_metadata_zero_or_paid_episode_cannot_publish(self):
        for fields, code in [({'sub_number': 0}, 'episode_not_free'),
                             ({'sub_number': 11}, 'episode_not_free'),
                             ({'unlocked_episodes_count': 0}, 'episode_not_free'),
                             ({'resource_type': 1}, 'free_episode_unavailable'),
                             ({'app_id': 1000}, 'free_episode_unavailable')]:
            row = resource()
            row.update(fields)
            with self.subTest(fields=fields):
                result = self.resolve([row])
                self.assertEqual(result['errors'][EPISODE]['code'], code)
        self.probe_mock.assert_not_called()

    def test_missing_or_unsafe_metadata_is_rejected_before_probe(self):
        for field, value in [('sub_url', ''), ('sub_url', 'https://user:password@media.example.test/a.mp4'),
                             ('sub_url', 'file:///tmp/movie.mp4'), ('language', ''),
                             ('drama_name', ''), ('drama_description', ''), ('content_id', ''),
                             ('series_code', '')]:
            row = resource()
            row[field] = value
            with self.subTest(field=field, value=value):
                result = self.resolve([row])
                self.assertEqual(result['errors'][EPISODE]['code'], 'free_episode_metadata_invalid')
        self.probe_mock.assert_not_called()

    def test_optional_labels_do_not_gate_a_selected_free_episode(self):
        row = resource()
        row['drama_labels'] = ''
        item = self.resolve([row])['items'][EPISODE]
        self.assertEqual(item['tag'], 'Fixture Drama')
        self.assertEqual(item['name_tag'], '#Fixture_Drama')

    def test_duplicate_exact_resource_is_not_silently_selected(self):
        result = self.resolve([resource(), resource()])
        self.assertEqual(result['errors'][EPISODE]['code'], 'free_episode_ambiguous')
        self.probe_mock.assert_not_called()

    def test_missing_or_future_deployment_is_row_local(self):
        for deployments, code in [([], 'drama_deploy_time_missing'),
                                  ([deploy(deploy_time=4102444800)], 'drama_not_yet_deliverable'),
                                  ([deploy(language='fr')], 'drama_deploy_time_missing')]:
            with self.subTest(code=code):
                result = self.resolve([resource()], deployments=deployments)
                self.assertEqual(result['errors'][EPISODE]['code'], code)
        self.probe_mock.assert_not_called()

    def test_deployment_language_aliases_preserve_resource_language(self):
        for language, deployment in [('pt', 'pt-br'), ('pt-br', 'pt'), ('ja', 'jp')]:
            row = resource()
            row['language'] = language
            with self.subTest(language=language, deployment=deployment):
                item = self.resolve([row], deployments=[deploy(language=deployment)])['items'][EPISODE]
                self.assertEqual(item['language'], language)
        row = resource()
        row['language'] = 'pt'
        result = self.resolve([row], deployments=[deploy(language='pt-pt')])
        self.assertEqual(result['errors'][EPISODE]['code'], 'drama_deploy_time_missing')

    def test_probe_failure_stays_on_its_episode(self):
        self.probe_mock.side_effect = [ValueError('signed URL must not leak'), 90]
        result = self.resolve([resource(), resource(OTHER)], [EPISODE, OTHER])
        self.assertEqual(set(result['items']), {OTHER})
        error = result['errors'][EPISODE]
        self.assertEqual(error['code'], 'material_duration_missing')
        self.assertNotIn('signed', error['message'])

    def test_probe_requires_finite_positive_within_video_ceiling(self):
        for duration, code in [(0, 'material_duration_missing'), (float('nan'), 'material_duration_missing'),
                               (float('inf'), 'material_duration_missing'), (14401, 'material_duration_exceeds_limit')]:
            self.probe_mock.return_value = duration
            with self.subTest(duration=duration):
                result = self.resolve([resource()])
                self.assertEqual(result['errors'][EPISODE]['code'], code)

    def test_hundred_episodes_use_bounded_parallel_probes(self):
        requested = [f'{number:032x}' for number in range(1, 101)]
        lock = threading.Lock()
        active, maximum = 0, 0

        def probe(url, *, deadline):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                self.assertGreater(deadline, time.monotonic())
                self.assertLessEqual(deadline, time.monotonic() + 8)
                time.sleep(0.002)
                return 90
            finally:
                with lock:
                    active -= 1

        self.probe_mock.side_effect = probe
        result = self.resolve([resource(mid) for mid in requested], requested)
        self.assertEqual(set(result['items']), set(requested))
        self.assertEqual(result['errors'], {})
        self.assertEqual(maximum, 2)

    def test_expired_network_budget_is_visible_and_still_allows_cached_result(self):
        def probe(url, *, deadline):
            self.assertLess(deadline, time.monotonic())
            # The shared duration probe checks its validated cache before budget.
            if url.endswith('/cached.mp4'):
                return 91
            raise ValueError('metadata deadline exceeded')

        self.probe_mock.side_effect = probe
        cached = resource(OTHER)
        cached['sub_url'] = 'https://media.example.test/cached.mp4'
        sql = FakeSQL([resource(), cached])
        result = EpisodeRepository(sql, sql.schema).resolve([EPISODE, OTHER], deadline=time.monotonic() - 1)
        self.assertEqual(set(result['items']), {OTHER})
        self.assertEqual(result['errors'][EPISODE]['code'], 'material_duration_missing')
        self.assertIn('稍后重试', result['errors'][EPISODE]['message'])

    def test_only_main_adapter_defers_failed_duration_read(self):
        self.probe_mock.side_effect = ValueError('metadata deadline exceeded')
        mysql = FakeSQL([resource()])
        app = {'ADMIN_MAPPING_MYSQL_HOST': '101.32.56.53', 'ADMIN_MAPPING_MYSQL_PORT': 63350,
               'ADMIN_MAPPING_MYSQL_USER': 'fixture', 'ADMIN_MAPPING_MYSQL_PASSWORD': 'fixture'}
        with patch('features.x_video_publish.runtime_source.GatedReadOnlyMySQL', return_value=mysql):
            main = for_main(app)
        pending = main.resolve([EPISODE])
        item = pending['items'][EPISODE]
        self.assertEqual(item['duration_status'], 'pending')
        self.assertIsNone(item['duration_seconds'])
        self.assertIsNone(item['source_duration'])
        self.assertEqual(pending['errors'], {})
        self.assertLessEqual(self.probe_mock.call_args.kwargs['deadline'], time.monotonic() + 8)
        # The execution runner constructs this repository with its strict default.
        strict = SourceRepository(mysql).resolve([EPISODE])
        self.assertEqual(strict['items'], {})
        self.assertEqual(strict['errors'][EPISODE]['code'], 'material_duration_missing')

    def test_pending_is_not_an_allowlist_or_identity_bypass(self):
        self.probe_mock.side_effect = ValueError('metadata unavailable')
        for hosts, code in [((), 'media_allowlist_not_configured'),
                            (('other.example.test',), 'media_host_not_allowed')]:
            self.allowlist_mock.return_value = hosts
            with self.subTest(hosts=hosts):
                result = SourceRepository(FakeSQL([resource()]), allow_pending_episode_duration=True).resolve([EPISODE])
                self.assertEqual(result['items'], {})
                self.assertEqual(result['errors'][EPISODE]['code'], code)
        self.probe_mock.assert_not_called()
        self.allowlist_mock.return_value = ('media.example.test',)
        paid = resource()
        paid['sub_number'] = 11
        result = SourceRepository(FakeSQL([paid]), allow_pending_episode_duration=True).resolve([EPISODE])
        self.assertEqual(result['errors'][EPISODE]['code'], 'episode_not_free')
        self.probe_mock.assert_not_called()

    def test_pending_budget_keeps_all_exact_valid_rows_without_faking_zero_duration(self):
        requested = [f'{number:032x}' for number in range(1, 101)]
        self.probe_mock.side_effect = ValueError('metadata deadline exceeded')
        mysql = FakeSQL([resource(mid) for mid in requested])
        result = EpisodeRepository(mysql, mysql.schema, allow_pending_duration=True).resolve(requested, deadline=time.monotonic() - 1)
        self.assertEqual(set(result['items']), set(requested))
        self.assertEqual(result['errors'], {})
        self.assertTrue(all(item['duration_status'] == 'pending' and item['duration_seconds'] is None
                            for item in result['items'].values()))

    def test_main_pending_policy_does_not_change_numeric_material_failures(self):
        repository = SourceRepository(FakeSQL([]), allow_pending_episode_duration=True)
        numeric_error = {'items': {}, 'errors': {'5595933': {'code': 'material_duration_missing'}}}
        with patch.object(repository, '_read_exact_bundle', return_value=([], ([], []))), \
                patch.object(repository, '_resolve_rows', return_value=numeric_error):
            result = repository.resolve(['5595933'])
        self.assertEqual(result, numeric_error)
        self.probe_mock.assert_not_called()

    def test_mixed_resolver_preserves_numeric_path(self):
        sql = FakeSQL([resource()])
        repository = SourceRepository(sql)
        with patch.object(repository, '_read_exact_bundle', return_value=([], ([], []))) as read:
            with patch.object(repository, '_resolve_rows', return_value={'items': {'5595933': {'material_id': '5595933'}}, 'errors': {}}):
                result = repository.resolve(['5595933', EPISODE])
        read.assert_called_once_with('s.id IN (%s)', ['5595933'])
        self.assertEqual(set(result['items']), {'5595933', EPISODE})

    def test_source_client_normalizes_before_trusted_broker(self):
        with patch('features.x_video_publish.sources.broker', return_value={'items': {}, 'errors': {}}) as call:
            SourceClient('fixture-token').resolve([EPISODE.upper(), '5595933'])
        self.assertEqual(call.call_args.args[1]['material_ids'], [EPISODE, '5595933'])
        for invalid in [[], ['1.2'], ['x' * 32], [5595933]]:
            with self.subTest(invalid=invalid), self.assertRaises(PublishingError):
                ids(invalid)


if __name__ == '__main__':
    unittest.main()
