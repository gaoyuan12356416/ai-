"""Source preparation keeps a single short snapshot, never a lifetime cache."""
import unittest
from unittest.mock import patch

import test_sql_source as sql_fixtures
from meta_violation_cleanup import sql_source
from meta_violation_cleanup.cover_source import CoverSource


class CoverSnapshotTests(unittest.TestCase):
    setUp = sql_fixtures.SqlSourceTests.setUp
    credential_route = sql_fixtures.SqlSourceTests.credential_route
    context_row = sql_fixtures.SqlSourceTests.context_row

    def make_source(self):
        return CoverSource({'env_file': None, 'shared_ledger': 'unused.sqlite3'})

    def task(self, identifier=1):
        return {'id': identifier, 'candidate': {
            'ad_id': '100', 'old_creative_id': '5000', 'root_id': '1479',
            'material_ids': ['9010'], 'account_id': '7000', 'user_id': '804'}}

    def process(self, cover='https://example.com/one.jpg', context=None):
        row = self.context_row() if context is None else context

        def respond(sql):
            if 'ads_facebook_auto_created_data a' in sql:
                return [row]
            if 'ads_source FORCE INDEX(PRIMARY)' in sql:
                return [{'id': '900', 'source_type': 3, 'source_id': '9010'}]
            if 'ads_custom_source FORCE INDEX(PRIMARY)' in sql:
                return [{'id': '9010', 'data_source_id': 'Drama01', 'data_source': 6,
                         'language': 'en', 'product': 'DramaWave'}]
            self.assertIn('ads_drama_resource FORCE INDEX(content_id)', sql)
            return [{'app_id': '1479', 'content_id': 'Drama01', 'language': 'en', 'cover': cover}]

        return sql_fixtures.FakeMysql(respond)

    def test_lineage_material_and_cover_share_one_readonly_snapshot(self):
        source = self.make_source()
        process = self.process()
        with patch.object(sql_source.subprocess, 'Popen', return_value=process) as popen:
            result = source.prepare_candidates([self.task()])
        self.assertTrue(result['1']['validated'])
        self.assertEqual(result['1']['cover_url'], 'https://example.com/one.jpg')
        self.assertEqual(result['1']['identity']['material_ids'], ['9010'])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(source.safe_metrics['sessions'], 1)
        self.assertEqual(len(process.commands), 6)  # proof, setup, lineage, source, material, cover
        self.assertIn('CONSISTENT SNAPSHOT, READ ONLY', process.commands[1])
        self.assertEqual(popen.call_args.kwargs['env']['SQL_GATE_BYPASS'], '0')
        self.assertIsNone(source._active)
        self.assertEqual(process.poll(), 0)

    def test_next_wave_reads_changed_cover_and_route_in_a_new_snapshot(self):
        source = self.make_source()
        processes = [self.process(), self.process(cover='https://example.com/two.jpg'),
                     self.process(context=self.context_row(available=False))]
        with patch.object(sql_source.subprocess, 'Popen', side_effect=processes) as popen:
            first = source.prepare_candidates([self.task()])['1']
            second = source.prepare_candidates([self.task()])['1']
            third = source.prepare_candidates([self.task()])['1']
        self.assertNotEqual(first['cover_url'], second['cover_url'])
        self.assertEqual(second['cover_url'], 'https://example.com/two.jpg')
        self.assertFalse(third['validated'])
        self.assertEqual(third['error'], 'publishing_credential_unavailable')
        self.assertEqual(popen.call_count, 3)
        self.assertTrue(all(p.poll() == 0 for p in processes))

    def test_maximum_64_candidates_per_snapshot_is_retained(self):
        source = self.make_source()
        processes = [self.process(), self.process()]
        with patch.object(sql_source.subprocess, 'Popen', side_effect=processes) as popen:
            result = source.prepare_candidates([self.task(n) for n in range(65)])
        self.assertEqual(len(result), 65)
        self.assertTrue(all(row['validated'] for row in result.values()))
        self.assertEqual(popen.call_count, 2)
        self.assertTrue(all(p.poll() == 0 for p in processes))

    def test_error_releases_snapshot_and_does_not_return_partial_context(self):
        source = self.make_source()
        process = self.process()
        with patch.object(sql_source.subprocess, 'Popen', return_value=process):
            with patch.object(source, 'material_metadata', side_effect=sql_source.SqlSourceError('sql_query_timeout')):
                with self.assertRaisesRegex(sql_source.SqlSourceError, 'sql_query_timeout'):
                    source.prepare_candidates([self.task()])
        self.assertIsNone(source._active)
        self.assertIsNone(sql_source._ACTIVE_SESSION)
        self.assertEqual(process.poll(), 0)
        with patch.object(sql_source.subprocess, 'Popen', return_value=self.process()):
            self.assertTrue(source.prepare_candidates([self.task()])['1']['validated'])

    def test_empty_wave_does_not_acquire_sql(self):
        with patch.object(sql_source.subprocess, 'Popen') as popen:
            self.assertEqual(self.make_source().prepare_candidates([]), {})
        popen.assert_not_called()

    def test_freshness_starts_before_sql_snapshot_not_after_queries(self):
        source = self.make_source()
        clock = [10.]
        original = source.material_metadata
        def delayed(ids):
            clock[0] = 45.
            return original(ids)
        with patch.object(sql_source.subprocess, 'Popen', return_value=self.process()), \
             patch('meta_violation_cleanup.cover_source.time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(source, 'material_metadata', side_effect=delayed):
            result = source.prepare_candidates([self.task()])['1']
        self.assertTrue(result['validated'])
        self.assertEqual(result['_prepared_at'], 10.)
        self.assertGreater(clock[0] - result['_prepared_at'], 30.)


if __name__ == '__main__':
    unittest.main()
