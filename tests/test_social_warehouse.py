import copy
import json
import tempfile
import unittest
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from features.social_analytics import warehouse_reader as reader, warehouse_refresh as refresh
from features.social_analytics.backend_scope import restrict
from features.social_analytics.report import summarize, parameters, csv_export
from features.social_analytics.warehouse_schema import FACT_TABLES, METRICS, CATALOG_TABLE
from tests.test_social_analytics import catalog, metric, DAY


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = {'SOCIAL_ANALYTICS_WAREHOUSE_ROOT': str(self.root)}
        self.alice = {'tenant_key': 't', 'user_id': 'alice', 'role': 'user'}
        self.admin = dict(self.alice, role='admin')
        self.query = {'start': [DAY], 'end': [DAY], 'platform': ['youtube'], 'group_by': ['owner,drama']}
        self.catalog = [catalog(owner=owner, campaign=owner, publications=[{'id': owner, 'date': DAY, 'kind': 'video'}])
                        for owner in ('alice', 'bob')]
        self.metrics = [dict(metric(campaign=owner, cents=cents), site_id='2284') for owner, cents in [('alice', 105), ('bob', 209)]]
        self.data = dict(catalog=self.catalog, metrics=self.metrics, titles={'42': {'drama_english_name': 'Title', 'drama_english_status': 'matched'}},
                         coverage={'youtube:2284': [DAY]}, start=DAY, end=DAY, catalog_errors={}, source_metadata={})
        self.manifest = {key: self.data[key] for key in ('start', 'end', 'coverage', 'catalog_errors', 'source_metadata')}
        self.manifest.update(version='test', generated_at=datetime.now(timezone.utc).isoformat(), database='snapshots/test.sqlite3')
        refresh.snapshot(self.root, 'test', self.data, self.manifest)
        refresh.atomic_json(self.root / 'current.json', self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_financial_parity_and_permissions_without_source_queries(self):
        cat, facts, coverage, _ = restrict(self.catalog, self.metrics, DAY)
        for actor in [self.alice, self.admin, dict(self.admin, tenant_key='other')]:
            expected = summarize(cat, facts, actor, parameters(self.query), self.manifest['generated_at'], metric_coverage=coverage)
            with patch('features.social_analytics.routes.query_rows', side_effect=AssertionError('HTTP queried SQL')):
                actual = reader.build_report(self.env, actor, self.query, include_titles=True)
            for field in ('totals', 'trend', 'pagination', 'quality'):
                self.assertEqual(actual[field], expected[field])
            self.assertTrue(all(row['drama_english_name'] == 'Title' for row in actual['rows']))
        self.assertEqual(reader.build_report(self.env, self.alice, self.query)['totals']['revenue_cents'], 105)
        self.assertEqual(reader.build_report(self.env, self.admin, self.query)['totals']['revenue_cents'], 314)

    def test_missing_source_date_is_not_zero_and_stale_is_visible(self):
        query = dict(self.query, start=['2026-10-18'])
        result = reader.build_report(self.env, self.admin, query)
        self.assertFalse(result['quality']['complete'])
        self.assertIsNone(result['trend'][0]['revenue'])
        self.manifest['generated_at'] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        refresh.atomic_json(self.root / 'current.json', self.manifest)
        result = reader.build_report(self.env, self.admin, self.query)
        self.assertTrue(result['meta']['snapshot_stale'])
        self.assertTrue(any('更新已延迟' in w for w in result['warnings']))

    def test_dispatch_path_and_csv_use_snapshot(self):
        from features.social_analytics.routes import build_report
        with patch('features.social_analytics.routes.read_catalogs', side_effect=AssertionError('source catalog read')):
            result = build_report(self.env, self.alice, self.query, ai_backend_only=True, include_titles=True, export=True)
        self.assertIn('Title', csv_export(result).decode('utf-8-sig'))

    def test_table_keys_preserve_case_sites_and_duplicate_evidence(self):
        data = copy.deepcopy(self.data)
        data['metrics'] += [dict(self.metrics[0], campaign_id='ALICE')]
        data['catalog'] += [copy.deepcopy(self.catalog[0])]
        values = refresh.table_rows(data, 'version')
        self.assertEqual(len(values[FACT_TABLES['youtube']]), 3)
        self.assertEqual(len(values[CATALOG_TABLE]), 2)

    def test_verification_rejects_mismatched_warehouse_totals(self):
        values = refresh.table_rows(self.data, 'v')
        fake = type('Fake', (), {'query': lambda _, statements: []})()
        with patch.object(refresh.time, 'monotonic', side_effect=[0, 100]):
            with self.assertRaisesRegex(RuntimeError, 'verification_failed'):
                refresh.verify(fake, 'v', values)

    def test_failed_refresh_preserves_published_snapshot(self):
        before = (self.root / 'current.json').read_bytes()
        with patch.object(refresh, 'Reader'), patch.object(refresh, 'collect', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                refresh.refresh({}, self.root)
        self.assertEqual((self.root / 'current.json').read_bytes(), before)
        self.assertEqual(json.loads((self.root / 'status.json').read_text())['status'], 'failed')

    def test_lost_commit_ack_retries_same_immutable_batch_without_duplicates(self):
        class TransientError(Exception):
            pass
        records, attempts = {}, []
        class Connection:
            def cursor(self):
                return self
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def executemany(self, sql, rows):
                self.asserted_sql = sql
                attempts.append(list(rows))
                for row in rows:
                    records.setdefault(row[0], row)
            def commit(self):
                if len(attempts) == 1:
                    raise TransientError(2013, 'lost acknowledgement')
            def rollback(self):
                pass
            def close(self):
                pass
        writer = refresh.Writer.__new__(refresh.Writer)
        writer.db = Connection()
        writer.connect = lambda: setattr(writer, 'db', Connection())
        fake = types.SimpleNamespace(err=types.SimpleNamespace(OperationalError=TransientError))
        with patch.dict('sys.modules', {'pymysql': fake}), patch.object(refresh.time, 'sleep'):
            writer.write_batch('idempotent insert', [('key', 123)])
        self.assertEqual(attempts, [[('key', 123)], [('key', 123)]])
        self.assertEqual(list(records.values()), [('key', 123)])

    def test_batched_titles_keep_ambiguous_names_unresolved(self):
        fake = types.SimpleNamespace(query=lambda statements: (
            [{'batch': 0, 'content_id': 'a', 'series_code': 's'}, {'batch': 0, 'content_id': 'b', 'series_code': 't'}]
            if 'content_id' in statements[0] else
            [{'batch': 0, 'series_code': 's', 'name': 'One'}, {'batch': 0, 'series_code': 's', 'name': 'Two'},
             {'batch': 0, 'series_code': 't', 'name': 'Title'}]))
        result = refresh.collect_titles(fake, ['a', 'b', 'c'])
        self.assertEqual(result['a']['drama_english_status'], 'ambiguous')
        self.assertEqual(result['b']['drama_english_name'], 'Title')
        self.assertEqual(result['c']['drama_english_status'], 'missing')


if __name__ == '__main__':
    unittest.main()
