import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from features.socialkit_analytics.collector import (
    CollectionError, Dimensions, collect_days, income_statements, mysql_settings,
)
from features.socialkit_analytics.refresh import default_range, run_refresh
from features.socialkit_analytics.schema import EFFECT_METRICS, empty_common, row_key, stable_key
from features.socialkit_analytics.storage import cleanup_warehouse, read_manifest, snapshot_path, verify_warehouse


def effect(day='2026-11-01', code='AbC', revenue=1899):
    result = empty_common(day)
    result.update(site_id='2236', short_code=code, country='JP', device_os='ios',
                  material_type='none', creation_source='automatic_material', matched=1)
    result.update({f: 0 for f in EFFECT_METRICS})
    result.update(revenue_cents=revenue, clicks=3, installs=1, conversions=1)
    return result


def publication(day='2026-11-01', task_id=1):
    result = empty_common(day)
    result.update(task_id=task_id, language='EN', account_country='US', reason='ADS_MATERIAL',
                  task_status=3, planned=1, success=1, failed=0, tasks=1)
    return result


def dataset(day='2026-11-01', revenue=1899):
    return dict(date=day, effects=[effect(day, revenue=revenue)], publications=[publication(day)],
                source_rows=1, source_updated_at='2026-11-02 09:00:00',
                quality=dict(unmatched_rows=0, unmatched={f: 0 for f in EFFECT_METRICS}))


class FakeWriter:
    def __init__(self):
        self.days = []
        self.failed_code = None
    def start(self, *args):
        self.started = args
    def write_day(self, version, day):
        self.days.append(copy.deepcopy(day))
    def finish(self, version, manifest):
        self.manifest = manifest
    def failed(self, version, code):
        self.failed_code = code


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.dimensions = Dimensions([
            dict(id=644, team_id=2, external_account_id='tt-123', account_name='Same Name', platform=3, is_deleted=0),
            dict(id=645, team_id=37, external_account_id='tt-123', account_name='Same Name', platform=3, is_deleted=0),
        ], [dict(drama_app_id=1479, drama_content_id='drama-a', drama_name='Title')])
        self.link = dict(team_id=2, drama_app_id=1479, drama_content_id='drama-a', external_account_id='tt-123',
                         platform_code=1, material_id=0, material_pool_type=0, creation_source=1)

    def test_exact_code_and_platform_mapping_preserve_unknown_metrics(self):
        good = self.dimensions.effect(effect(code='AbC'), {'AbC': self.link})
        bad = self.dimensions.effect(effect(code='abc'), {'AbC': self.link})
        self.assertEqual(good['platform'], 'tiktok')
        self.assertEqual(good['product'], 'Dramawave')
        self.assertEqual(good['material_type'], 'none')
        self.assertEqual(good['account'], stable_key(2, 'tiktok', 'tt-123'))
        self.assertEqual(bad['matched'], 0)
        self.assertEqual(bad['platform'], 'unattributed')
        self.assertEqual(bad['revenue_cents'], 1899)
        self.assertNotEqual(row_key('effects', good), row_key('effects', bad))

    def test_same_name_and_external_id_do_not_merge_teams(self):
        one = self.dimensions.common('2026-11-01', 2, 'tiktok', 'tt-123', 1479, 'drama-a')
        two = self.dimensions.common('2026-11-01', 37, 'tiktok', 'tt-123', 1479, 'drama-a')
        self.assertEqual(one['account_name'], two['account_name'])
        self.assertNotEqual(one['account'], two['account'])

    def test_raw_unknown_task_status_survives(self):
        raw = dict(id=77, schedule_date='2026-11-01', team_id=2, account_id=644, account_platform=3,
                   drama_app_id=1479, drama_content_id='drama-a', drama_name='', account_lang='EN',
                   account_country='US', reason='ADS_MATERIAL', task_status=6, item_total=1,
                   item_success=0, item_failed=0)
        value = self.dimensions.publication(raw)
        self.assertEqual(value['task_status'], 6)
        self.assertEqual(value['drama_name'], 'Title')
        self.assertEqual(value['failed'], 0)


class CollectionTests(unittest.TestCase):
    def test_drama_names_are_limited_to_referenced_keys_and_cached(self):
        records = []
        for day in ['2026-10-31', '2026-11-01']:
            item = effect(day)
            records += [dict(item, kind='effect'),
                        dict(kind='control', date=day, source_rows=1, source_updated_at='',
                             **{f: item[f] for f in EFFECT_METRICS})]
        class Reader:
            def records(self, statements):
                return iter(records)
        class Source:
            def __init__(self):
                self.name_queries = []
            def dimensions(self):
                return [], [], [2]
            def links(self, codes):
                return {'AbC': dict(team_id=2, drama_app_id=1479, drama_content_id='only-this',
                                   external_account_id='account', platform_code=2, material_id=3,
                                   material_pool_type=0, creation_source=1)}
            def drama_names(self, keys):
                self.name_queries.append(set(keys))
                return []
            def publications(self, day, teams):
                return []
        source = Source()
        result = list(collect_days(Reader(), source, '2026-10-31', '2026-11-01'))
        self.assertEqual(source.name_queries, [{(1479, 'only-this')}])
        self.assertEqual(result[1]['effects'][0]['drama_name'], 'only-this')
        self.assertEqual(result[1]['effects'][0]['revenue_cents'], 1899)

    def test_control_failure_is_not_empty_day(self):
        item = effect()
        record = dict(item, kind='effect')
        control = dict(kind='control', date=item['date'], source_rows=1, source_updated_at='',
                       **{f: item[f] for f in EFFECT_METRICS})
        control['revenue_cents'] += 1
        class Reader:
            def records(self, statements):
                return iter([record, control])
        class Source:
            def dimensions(self):
                return [], [], []
        with self.assertRaisesRegex(CollectionError, 'control totals'):
            list(collect_days(Reader(), Source(), '2026-11-01', '2026-11-01'))

    def test_sql_has_index_exact_key_and_no_timestamp_incremental_scan(self):
        sql = '\n'.join(income_statements('2026-11-01'))
        self.assertIn('FORCE INDEX(c_dt)', sql)
        self.assertIn('BINARY campaign,BINARY country,BINARY platform', sql)
        self.assertIn("channel='socialkit'", sql)
        self.assertNotIn('updated_at>', sql)

    def test_reader_writer_endpoint_separation(self):
        env = dict(ADMIN_MAPPING_MYSQL_HOST='101.32.56.53', ADMIN_MAPPING_MYSQL_PORT='63353',
                   ADMIN_MAPPING_MYSQL_USER='test', ADMIN_MAPPING_MYSQL_PASSWORD='not-a-real-secret')
        with self.assertRaises(CollectionError):
            mysql_settings(env, 'reader')


class RefreshTests(unittest.TestCase):
    def invoke(self, root, rows, verify=lambda *args: None):
        writer = FakeWriter()
        result = run_refresh({}, root, rows[0]['date'], rows[-1]['date'], reader=object(), source=object(),
                             writer=writer, collect=lambda *args: iter(rows), verify=verify)
        return result, writer

    def test_default_fourteen_complete_beijing_days(self):
        self.assertEqual(default_range(datetime(2026, 11, 2, 3, tzinfo=timezone.utc)),
                         ('2026-10-19', '2026-11-01'))

    def test_recalculation_replaces_amount_and_preserves_earlier_history(self):
        with tempfile.TemporaryDirectory() as root:
            self.invoke(root, [dataset('2026-10-31', 100), dataset('2026-11-01', 1899)])
            current, _ = self.invoke(root, [dataset('2026-11-01', 1600)])
            conn = sqlite3.connect(snapshot_path(root, current))
            try:
                self.assertEqual(conn.execute('SELECT date,revenue_cents FROM effects ORDER BY date').fetchall(),
                                 [('2026-10-31', 100), ('2026-11-01', 1600)])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0], 2)
            finally:
                conn.close()
            self.assertTrue((Path(root) / 'previous.json').exists())
            self.assertEqual(len(list((Path(root) / 'snapshots').glob('*.sqlite3'))), 2)
            self.assertEqual(current['date_manifest']['2026-10-31']['controls']['effects']['revenue_cents'], 100)
            self.assertEqual(current['date_manifest']['2026-11-01']['controls']['effects']['revenue_cents'], 1600)
            self.assertEqual(current['date_manifest']['2026-11-01']['controls']['publications']['success'], 1)

    def test_replica_failure_keeps_current_pointer_and_marks_attempt_failed(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.invoke(root, [dataset()])
            def fail(*args):
                raise CollectionError('verification mismatch')
            with self.assertRaises(CollectionError):
                self.invoke(root, [dataset(revenue=9999)], verify=fail)
            self.assertEqual(read_manifest(root)['version'], first['version'])
            status = json.loads((Path(root) / 'status.json').read_text())
            self.assertEqual(status['refresh_status'], 'failed')
            self.assertNotIn('verification mismatch', json.dumps(status))

    def test_warm_failure_does_not_undo_published_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('features.socialkit_analytics.query.warm_snapshot', side_effect=RuntimeError('warm failed'), create=True):
                current, _ = self.invoke(root, [dataset()])
            self.assertEqual(read_manifest(root)['version'], current['version'])
            status = json.loads((Path(root) / 'status.json').read_text())
            self.assertEqual(status['refresh_status'], 'complete')
            self.assertEqual(status['precompute_warning'], 'RuntimeError')

    def test_precomputed_cleanup_keeps_current_and_previous_only(self):
        with tempfile.TemporaryDirectory() as root:
            first, _ = self.invoke(root, [dataset()])
            folder = Path(root) / 'precomputed'
            folder.mkdir(exist_ok=True)
            for name in [first['version'] + '-overview.json', first['version'] + '-options.json', 'orphan-overview.json']:
                (folder / name).write_text('{}')
            self.invoke(root, [dataset(revenue=1600)])
            self.assertTrue((folder / (first['version'] + '-overview.json')).exists())
            self.assertFalse((folder / 'orphan-overview.json').exists())

    def test_successful_zero_day_is_complete_and_not_missing(self):
        day = dataset()
        day.update(effects=[], publications=[], source_rows=0)
        with tempfile.TemporaryDirectory() as root:
            current, _ = self.invoke(root, [day])
            self.assertEqual(current['coverage']['effects']['2026-11-01'], 'complete')
            self.assertEqual(current['date_manifest']['2026-11-01']['effect_rows'], 0)

    def test_replica_verification_rejects_different_amount(self):
        class Reader:
            def query(self, sql):
                return [dict(kind='effects', date='2026-11-01', rows=1, **{f: 0 for f in EFFECT_METRICS})]
        expected = dict(date='2026-11-01', effects=dict(rows=1, **{f: (1899 if f == 'revenue_cents' else 0) for f in EFFECT_METRICS}),
                        publications=dict(rows=0, tasks=0, planned=0, success=0, failed=0))
        with self.assertRaises(CollectionError):
            verify_warehouse(Reader(), 'test-version', [expected], timeout=0)

    def test_cleanup_preserves_versions_referenced_by_historical_dates(self):
        class Reader:
            def query(self, statements):
                self.sql = statements[0]
                return [dict(version='obsolete')]
        class Writer:
            def __init__(self):
                self.calls = []
            def delete_version_rows(self, table, version):
                self.calls.append((table, version))
                return 0
        reader, writer = Reader(), Writer()
        manifests = [dict(version='current', date_manifest={'2026-10-19': dict(version='historic')}),
                     dict(version='previous', date_manifest={})]
        cleanup_warehouse(reader, writer, manifests)
        for version in ['current', 'previous', 'historic']:
            self.assertIn('0x' + version.encode().hex(), reader.sql)
        self.assertEqual({version for _, version in writer.calls}, {'obsolete'})
        self.assertEqual(writer.calls[-1], ('socialkit_refresh_runs', 'obsolete'))

    def test_cleanup_is_bounded_without_deleting_unfinished_run_record(self):
        class Reader:
            def query(self, statements):
                return [dict(version='old')]
        class Writer:
            def __init__(self):
                self.calls = []
            def delete_version_rows(self, table, version):
                self.calls.append(table)
                return 1000
        writer = Writer()
        result = cleanup_warehouse(Reader(), writer, [dict(version='new', date_manifest={})], max_batches=2)
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['deleted_rows'], 2000)
        self.assertNotIn('socialkit_refresh_runs', writer.calls)

    def test_cleanup_time_budget_is_visible_without_mutating(self):
        class Reader:
            def query(self, statements):
                return [dict(version='old')]
        class Writer:
            def delete_version_rows(self, *args):
                raise AssertionError('zero time budget must not mutate')
        result = cleanup_warehouse(Reader(), Writer(), [dict(version='new', date_manifest={})], budget_seconds=0)
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['batches'], 0)

    def test_retained_batch_loses_stale_days_but_keeps_previous_references(self):
        class Reader:
            def query(self, statements):
                return [dict(kind=kind, version='historic', date=day)
                        for kind in ['effects', 'publications']
                        for day in ['2026-10-19', '2026-10-20', '2026-10-21']]
        class Writer:
            def __init__(self):
                self.calls = []
            def delete_fact_date(self, table, version, day):
                self.calls.append((table, version, day))
                return 0
            def delete_version_rows(self, *args):
                raise AssertionError('a referenced run must not be deleted')
        current = dict(version='new', date_manifest={'2026-10-19': dict(version='historic')})
        previous = dict(version='previous', date_manifest={'2026-10-20': dict(version='historic')})
        writer = Writer()
        result = cleanup_warehouse(Reader(), writer, [current, previous])
        self.assertFalse(result['incomplete'])
        self.assertEqual(set(writer.calls), {('socialkit_effect_daily', 'historic', '2026-10-21'),
                                           ('socialkit_publish_daily', 'historic', '2026-10-21')})

    def test_pair_cleanup_and_obsolete_cleanup_share_one_budget(self):
        class Reader:
            def query(self, statements):
                return [dict(kind='effects', version='historic', date='2026-10-20'),
                        dict(kind='obsolete_version', version='unused')]
        class Writer:
            def __init__(self):
                self.pairs = []
            def delete_fact_date(self, table, version, day):
                self.pairs.append((table, version, day))
                return 1000
            def delete_version_rows(self, *args):
                raise AssertionError('pair deletion already consumed the shared budget')
        writer = Writer()
        result = cleanup_warehouse(Reader(), writer,
                                   [dict(version='new', date_manifest={'2026-10-19': dict(version='historic')})],
                                   max_batches=2)
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['batches'], 2)
        self.assertEqual(result['deleted_rows'], 2000)


if __name__ == '__main__':
    unittest.main()
