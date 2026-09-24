"""Read-only analytics contracts; all records and source metrics are synthetic."""
import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from features.youtube_analytics import report, routes


START, END = '2026-09-20', '2026-09-22'
OWNER = dict(tenant_key='tenant-a', user_id='alice', role='user')
ADMIN = dict(tenant_key='tenant-a', user_id='admin', role='admin')


def params(**kwargs):
    values = dict(start=START, end=END)
    values.update(kwargs)
    return report.parameters({k: v if isinstance(v, list) else [str(v)] for k, v in values.items()})


def link_url(campaign_id, campaign):
    return 'https://www.dramawavew2a.com/ads/101/2284/view?' + urlencode(
        {'af_c_id': campaign_id, 'c': campaign, 'af_channel': 'synthetic-sub-user'})


def metric(campaign_id='task-a', campaign='campaign-a', day=START, **kwargs):
    value = dict(date=day, campaign_id=campaign_id, campaign=campaign,
                 clicks=100, views=80, installs=10, conversions=2,
                 revenue_cents=1250, refund_cents=50, updated_at=day + ' 10:00:00')
    value.update(kwargs)
    return value


def catalog_row(key=('task-a', 'campaign-a'), link_id=1, **kwargs):
    value = dict(key=key, link_id=link_id, tenant='tenant-a', owner='alice',
                 owner_label='甲用户', drama='drama-a', drama_label='短剧甲',
                 channel='channel-a', channel_label='频道甲', language='zh', language_label='zh',
                 link_type='auto', link_type_label='自动发布', date=START, excluded=False)
    value.update(kwargs)
    return value


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class CatalogFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'jobs.sqlite3'
        # Relevant columns and ownership keys mirror the existing production DDL;
        # fixtures intentionally omit tokens, publishing clients, and credentials.
        with database(self.db_path) as c:
            c.executescript('''
                CREATE TABLE drama_admin_user(user_id TEXT, tenant_key TEXT, name TEXT);
                CREATE TABLE drama_material_short_link(
                    id INTEGER PRIMARY KEY, job_id TEXT, material_kind TEXT, content_id TEXT,
                    long_url TEXT, created_at_utc TEXT, published_at_utc TEXT, publish_state TEXT);
                CREATE TABLE youtube_link_attribution(
                    task_id TEXT PRIMARY KEY, link_id INTEGER NOT NULL UNIQUE,
                    context TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE youtube_manual_short_link(
                    tenant TEXT NOT NULL, owner TEXT NOT NULL, operation_id TEXT NOT NULL,
                    request_json TEXT NOT NULL, context_json TEXT NOT NULL,
                    link_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant,owner,operation_id));
                CREATE TABLE drama_youtube_publish(
                    job_id TEXT, source_kind TEXT, operator_user_id TEXT, operator_name TEXT,
                    channel_id TEXT, preparation_id TEXT, content_id TEXT);
                CREATE TABLE youtube_auto_preparation(id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,body TEXT);
                CREATE TABLE drama_material_job(job_id TEXT PRIMARY KEY,drama_name TEXT,language TEXT);
                INSERT INTO drama_admin_user VALUES('alice','tenant-a','甲用户');
                INSERT INTO drama_admin_user VALUES('bob','tenant-a','乙用户');
                INSERT INTO drama_admin_user VALUES('eve','tenant-b','跨租户用户');
            ''')

    def add_link(self, number=1, owner='alice', tenant='tenant-a', kind='auto',
                 campaign_id=None, campaign=None, state='published', **context_values):
        campaign_id = campaign_id or 'task-' + str(number)
        campaign = campaign or 'campaign-' + str(number)
        context = dict(tenant=tenant, owner=owner, content_id='drama-' + str(number),
                       drama_name='短剧' + str(number), channel_id='channel-' + str(number),
                       channel_name='频道' + str(number), language='zh')
        context.update(context_values)
        material_kind = 'youtube_manual' if kind == 'manual' else 'custom_source'
        with database(self.db_path) as c:
            c.execute('INSERT INTO drama_material_short_link VALUES(?,?,?,?,?,?,?,?)',
                      (number, campaign_id, material_kind, context['content_id'],
                       link_url(campaign_id, campaign), START + 'T00:00:00Z', START + 'T00:01:00Z', state))
            if kind == 'auto':
                c.execute('INSERT INTO youtube_link_attribution VALUES(?,?,?,?)',
                          (campaign_id, number, json.dumps(context), START))
            elif kind == 'manual':
                # Actual manual context ownership is carried in table columns.
                context.pop('owner', None)
                context.pop('tenant', None)
                c.execute('INSERT INTO youtube_manual_short_link VALUES(?,?,?,?,?,?,?)',
                          (tenant, owner, 'operation-' + str(number), '{}', json.dumps(context), number, START))
        return context

    def add_publication(self, number=1, owner='alice', channel='channel-1', prep='', operator_name='旧用户'):
        with database(self.db_path) as c:
            c.execute('INSERT INTO drama_youtube_publish VALUES(?,?,?,?,?,?,?)',
                      ('task-' + str(number), 'custom_source', owner, operator_name, channel, prep, 'drama-' + str(number)))

class CatalogTests(CatalogFixture, unittest.TestCase):
    def test_modern_automatic_and_manual_link_contexts(self):
        self.add_link(1)
        self.add_link(2, kind='manual')
        rows = report.read_catalog(self.db_path)
        self.assertEqual([r['link_type'] for r in rows], ['auto', 'manual'])
        self.assertEqual([r['key'] for r in rows], [('task-1', 'campaign-1'), ('task-2', 'campaign-2')])
        self.assertEqual({r['owner'] for r in rows}, {'alice'})
        self.assertEqual(rows[0]['owner_label'], '甲用户')

    def test_frozen_context_wins_over_current_job_and_publication(self):
        self.add_link(1, drama_name='冻结剧名', channel_name='冻结频道')
        self.add_publication(1, owner='bob', channel='changed-channel')
        with database(self.db_path) as c:
            c.execute('INSERT INTO drama_material_job VALUES(?,?,?)', ('task-1', '当前剧名', 'en'))
        row = report.read_catalog(self.db_path)[0]
        self.assertEqual((row['owner'], row['drama_label'], row['channel_label'], row['language']),
                         ('alice', '冻结剧名', '冻结频道', 'zh'))

    def test_historical_link_uses_preparation_and_unique_tenant_mapping(self):
        self.add_link(1, kind='historical')
        self.add_publication(1, prep='prep-1')
        with database(self.db_path) as c:
            c.execute('INSERT INTO youtube_auto_preparation VALUES(?,?,?,?)',
                      ('prep-1', 'tenant-a', 'alice', json.dumps(dict(
                          material=dict(macro_name='历史冻结剧', language='en'), channel=dict(name='历史频道')))))
        row = report.read_catalog(self.db_path)[0]
        self.assertEqual((row['tenant'], row['drama_label'], row['channel_label'], row['language']),
                         ('tenant-a', '历史冻结剧', '历史频道', 'en'))

    def test_historical_without_preparation_uses_job_and_unique_user(self):
        self.add_link(1, kind='historical')
        self.add_publication(1)
        with database(self.db_path) as c:
            c.execute('INSERT INTO drama_material_job VALUES(?,?,?)', ('task-1', '旧任务剧名', 'es'))
        row = report.read_catalog(self.db_path)[0]
        self.assertEqual((row['tenant'], row['drama_label'], row['language']), ('tenant-a', '旧任务剧名', 'es'))

    def test_unresolved_historical_owner_and_internal_canary_are_excluded(self):
        self.add_link(1, kind='historical')
        self.add_publication(1)
        self.add_link(2, kind='historical')
        self.add_publication(2, owner='bob', operator_name='internal-deployment-canary')
        with database(self.db_path) as c:
            c.execute('INSERT INTO drama_admin_user VALUES(?,?,?)', ('alice', 'tenant-b', '重名账号'))
        unique, conflicts = report.unique_catalog(report.read_catalog(self.db_path))
        self.assertEqual(unique, {})
        self.assertEqual(len(conflicts), 2)

    def test_unpublished_and_non_matching_destinations_are_not_attributed(self):
        self.add_link(1, state='pending')
        self.add_link(2, state='failed')
        self.add_link(3)
        with database(self.db_path) as c:
            c.execute('UPDATE drama_material_short_link SET long_url=? WHERE id=3',
                      (link_url('task-3', 'campaign-3').replace('www.dramawavew2a.com', 'example.test'),))
        self.assertEqual(report.read_catalog(self.db_path), [])

    def test_catalog_reads_leave_database_and_schema_byte_identical(self):
        self.add_link(1)
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        with patch('subprocess.run', side_effect=AssertionError('No platform or shell calls')):
            report.read_catalog(self.db_path)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).digest(), before)
        self.assertEqual(sorted(p.name for p in self.db_path.parent.iterdir()), ['jobs.sqlite3'])

    def test_repeated_historical_publications_preserve_single_attribution_amount(self):
        self.add_link(1, kind='historical')
        self.add_publication(1)
        self.add_publication(1)
        catalog = report.read_catalog(self.db_path)
        self.assertEqual(len(catalog), 2)
        value = report.summarize(catalog, [metric('task-1', 'campaign-1')], OWNER, params(), 'synthetic-fetch')
        self.assertEqual(value['totals']['clicks'], 100)
        self.assertEqual(value['totals']['links'], 1)


class AggregationTests(unittest.TestCase):
    def summarize(self, catalog=None, metrics=None, actor=OWNER, **kwargs):
        return report.summarize(catalog if catalog is not None else [catalog_row()],
                                metrics if metrics is not None else [metric()], actor, params(**kwargs), 'synthetic-fetch')

    def test_attribution_requires_both_case_sensitive_campaign_parts(self):
        rows = [metric(), metric('task-a', 'Campaign-a', clicks=999),
                metric('TASK-A', 'campaign-a', clicks=999), metric('task-other', 'campaign-a', clicks=999)]
        self.assertEqual(self.summarize(metrics=rows)['totals']['clicks'], 100)

    def test_url_key_decodes_unicode_and_rejects_duplicates_or_missing_parts(self):
        self.assertEqual(report.attribution_key(link_url('TaskA', '剧甲 * 支付')), ('TaskA', '剧甲 * 支付'))
        for url in (link_url('', 'c'), link_url('id', ''), link_url('id', 'c') + '&c=extra',
                    link_url('id', 'c').replace('https:', 'http:'),
                    link_url('id', 'c').replace('/view?', '/view/?:')):
            with self.subTest(url=url):
                self.assertIsNone(report.attribution_key(url))

    def test_owner_and_tenant_scope_applies_to_options_and_totals(self):
        catalog = [catalog_row(), catalog_row(('b', 'b'), 2, owner='bob', owner_label='乙'),
                   catalog_row(('c', 'c'), 3, tenant='tenant-b', owner='eve', owner_label='外部')]
        metrics = [metric(), metric('b', 'b', clicks=200), metric('c', 'c', clicks=900)]
        self.assertEqual(self.summarize(catalog, metrics)['totals']['clicks'], 100)
        self.assertEqual(self.summarize(catalog, metrics, actor=ADMIN)['totals']['clicks'], 300)
        self.assertEqual([r['value'] for r in report.options(catalog, OWNER)['options']['owner']], ['alice'])
        self.assertEqual({r['value'] for r in report.options(catalog, ADMIN)['options']['owner']}, {'alice', 'bob'})
        self.assertEqual(self.summarize(catalog, metrics, owner='bob')['totals']['clicks'], 0)
        self.assertEqual(report.options(catalog, {})['options']['owner'], [])

    def test_multiowner_conflict_excluded_from_personal_and_admin_groups(self):
        catalog = [catalog_row(), catalog_row(link_id=2, owner='bob', owner_label='乙')]
        own = self.summarize(catalog)
        admin = self.summarize(catalog, actor=ADMIN)
        self.assertEqual(own['rows'], [])
        self.assertEqual(own['quality']['unmatched_campaigns'], 0)
        self.assertEqual(own['quality']['unmatched_totals']['clicks'], 0)
        self.assertEqual(admin['totals']['clicks'], 0)
        self.assertEqual(admin['quality']['unmatched_campaigns'], 1)
        self.assertEqual(admin['quality']['unmatched_totals']['clicks'], 100)

    def test_crosstenant_conflict_does_not_leak_unmatched_metrics(self):
        catalog = [catalog_row(), catalog_row(link_id=2, tenant='tenant-b', owner='eve')]
        value = self.summarize(catalog, actor=ADMIN)
        self.assertEqual(value['rows'], [])
        self.assertEqual(value['quality']['unmatched_campaigns'], 0)
        self.assertEqual(value['quality']['unmatched_totals']['clicks'], 0)

    def test_filters_cannot_resolve_global_ownership_conflict(self):
        catalog = [catalog_row(), catalog_row(link_id=2, owner='bob', channel='channel-b')]
        value = self.summarize(catalog, channel='channel-a')
        self.assertEqual(value['rows'], [])
        self.assertEqual(value['totals']['clicks'], 0)
        self.assertEqual(value['quality']['unmatched_campaigns'], 0)

    def test_duplicate_contexts_and_distinct_duplicate_links_do_not_multiply_metrics(self):
        catalog = [catalog_row(), catalog_row(), catalog_row(link_id=2)]
        value = self.summarize(catalog)
        self.assertEqual(value['totals']['clicks'], 100)
        self.assertEqual(value['totals']['revenue'], 12.5)
        self.assertEqual(value['totals']['links'], 2)

    def test_old_link_creation_does_not_remove_recent_effect_metrics(self):
        value = self.summarize([catalog_row(date='2025-01-01')], group_by='date,drama')
        self.assertEqual(value['totals']['clicks'], 100)
        self.assertEqual(value['totals']['links'], 0)
        self.assertEqual(value['rows'][0]['date'], START)

    def test_combined_dimensions_daily_totals_and_rates_conserve_metrics(self):
        catalog = [catalog_row(), catalog_row(('b', 'b'), 2, channel='channel-b', channel_label='乙频道')]
        metrics = [metric(clicks=10, installs=5, conversions=2),
                   metric('b', 'b', clicks=90, installs=9, conversions=3),
                   metric(day=END, clicks=100, installs=6, conversions=1)]
        value = self.summarize(catalog, metrics, group_by='date,owner,channel')
        for key in ('clicks', 'views', 'installs', 'conversions', 'revenue', 'refunds', 'links'):
            self.assertAlmostEqual(sum(r[key] or 0 for r in value['rows']), value['totals'][key])
            self.assertAlmostEqual(sum(r[key] or 0 for r in value['trend']), value['totals'][key])
        self.assertEqual(value['totals']['install_rate'], 10.0)
        self.assertEqual(value['totals']['pay_rate'], 30.0)

    def test_empty_source_is_unknown_everywhere_not_zero(self):
        value = self.summarize(metrics=[])
        for row in [value['totals'], *value['rows'], *value['trend'], *value['ranking']]:
            for field in report.METRICS:
                if field != 'links':
                    self.assertIsNone(row[field], (field, row))
        self.assertEqual(value['totals']['links'], 1)
        self.assertEqual(value['quality']['missing_dates'], [START, '2026-09-21', END])

    def test_partial_missing_day_is_null_in_trend_table_and_csv(self):
        catalog = [catalog_row(date='2026-09-21')]
        value = self.summarize(catalog, group_by='date')
        trend_missing = next(r for r in value['trend'] if r['date'] == '2026-09-21')
        table_missing = next(r for r in value['rows'] if r['date'] == '2026-09-21')
        self.assertIsNone(trend_missing['clicks'])
        self.assertIsNone(table_missing['clicks'])
        self.assertEqual(table_missing['links'], 1)
        data = list(csv.DictReader(io.StringIO(report.csv_export(value).decode('utf-8-sig'))))
        missing = next(r for r in data if r['统计日期（UTC）'] == '2026-09-21')
        self.assertEqual(missing['落地页点击'], '未同步')

    def test_zero_denominator_rates_are_null_and_real_zero_stays_zero(self):
        value = self.summarize(metrics=[metric(clicks=0, installs=0, conversions=0)])
        self.assertEqual(value['totals']['clicks'], 0)
        self.assertIsNone(value['totals']['install_rate'])
        self.assertIsNone(value['totals']['pay_rate'])

    def test_duplicate_metric_or_out_of_range_day_fails_closed(self):
        for metrics in ([metric(), metric()], [metric(day='2026-09-19')]):
            with self.subTest(metrics=metrics), self.assertRaises(report.ReportError) as error:
                self.summarize(metrics=metrics)
            self.assertEqual(error.exception.status, 503)

    def test_invalid_numeric_metrics_fail_closed(self):
        for field, value in [('clicks', -1), ('installs', 'NaN'), ('conversions', 'Infinity'), ('views', 0.5)]:
            with self.subTest(field=field), self.assertRaises(report.ReportError) as error:
                self.summarize(metrics=[metric(**{field: value})])
            self.assertEqual(error.exception.status, 503)

    def test_filters_intersect_search_matches_drama_id_and_unicode_name(self):
        catalog = [catalog_row(), catalog_row(('b', 'b'), 2, drama='other', drama_label='另一剧', language='en')]
        metrics = [metric(), metric('b', 'b', clicks=900)]
        value = self.summarize(catalog, metrics, drama='drama-a', channel='channel-a', language='zh', link_type='auto', search='短剧')
        self.assertEqual(value['totals']['clicks'], 100)
        self.assertEqual(self.summarize(catalog, metrics, search='DRAMA-A')['totals']['clicks'], 100)
        self.assertEqual(self.summarize(catalog, metrics, language='en', drama='drama-a')['totals']['clicks'], 0)

    def test_sort_and_pagination_are_stable_ranking_covers_all_rows(self):
        catalog = [catalog_row((str(i), str(i)), i + 1, drama=f'drama-{i:02}', drama_label=f'剧{i:02}') for i in range(25)]
        metrics = [metric(str(i), str(i), clicks=i // 2) for i in range(25)]
        value = self.summarize(catalog, metrics, group_by='drama', page=2, page_size=20, sort='clicks', direction='asc')
        self.assertEqual(value['pagination'], dict(page=2, page_size=20, total=25, pages=2))
        self.assertEqual([r['drama'] for r in value['rows']], [f'drama-{i:02}' for i in range(20, 25)])
        self.assertEqual(value['ranking'][0]['clicks'], 12)
        self.assertEqual(value['totals']['clicks'], sum(i // 2 for i in range(25)))
        self.assertEqual(self.summarize(catalog, metrics, group_by='drama', page=100)['pagination']['page'], 1)

    def test_csv_has_bom_all_pages_and_escapes_formula_values(self):
        catalog = [catalog_row((str(i), str(i)), i + 1, drama=f'drama-{i:02}',
                               drama_label=['=SUM(1,2)', '+1', '-2', '@cmd', '  =1'][i % 5]) for i in range(25)]
        metrics = [metric(str(i), str(i)) for i in range(25)]
        value = self.summarize(catalog, metrics, group_by='drama', page=2, page_size=20)
        data = report.csv_export(value)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 25)
        self.assertTrue(all(r['剧名'].startswith("'") for r in rows))
        self.assertEqual({r['drama ID'] for r in rows}, {f'drama-{i:02}' for i in range(25)})

    def test_csv_carries_partial_period_and_conversion_caveat(self):
        value = self.summarize()
        row = next(csv.DictReader(io.StringIO(report.csv_export(value).decode('utf-8-sig'))))
        self.assertEqual(row['统计范围（UTC）'], START + ' 至 ' + END)
        self.assertIn('2026-09-21', row['数据完整性'])
        self.assertIn('未跨日去重', row['转化口径'])


class ParameterTests(unittest.TestCase):
    def test_invalid_dates_pagination_dimensions_filters_and_sort_are_rejected(self):
        invalid = [dict(start='2026-02-30'), dict(start=END, end=START), dict(end='2999-01-01'),
                   dict(start='2026-01-01', end='2026-04-04'), dict(page=0), dict(page_size=19),
                   dict(page=['1', '2']), dict(group_by='owner,owner'), dict(group_by='owner,drama,channel,date'),
                   dict(group_by='unknown'), dict(link_type='external'), dict(search='x\ny'),
                   dict(owner=''), dict(sort='owner', group_by='drama'), dict(direction='sideways'),
                   dict(ranking_metric='unsupported'), dict(injected='value')]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(report.ReportError):
                params(**kwargs)

    def test_date_filter_cannot_be_silently_ignored(self):
        with self.assertRaises(report.ReportError):
            params(date='2026-09-21')

    def test_exact_93_day_range_and_repeated_multi_filters_are_valid(self):
        value = params(start='2026-01-01', end='2026-04-03', owner=['alice', 'bob'], group_by='drama,channel,date')
        self.assertEqual(value['filters']['owner'], {'alice', 'bob'})
        self.assertEqual(value['groups'], ['drama', 'channel', 'date'])


class FakeHandler:
    def __init__(self, actor):
        self.actor, self.command, self.permission = actor, 'GET', None
        self.status, self.value, self.headers, self.wfile = None, None, {}, io.BytesIO()

    def _youtube_auto_actor(self, permission):
        self.permission = permission
        return self.actor

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


class RouteTests(CatalogFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        routes._cache.clear()
        self.addCleanup(routes._cache.clear)
        self.add_link(1)
        self.add_link(2, owner='bob')
        self.add_link(3, owner='eve', tenant='tenant-b')
        self.metrics = [metric('task-1', 'campaign-1'), metric('task-2', 'campaign-2', clicks=200),
                        metric('task-3', 'campaign-3', clicks=900)]

    def request(self, endpoint='report', actor=OWNER, query=None, method='GET'):
        handler = FakeHandler(actor)
        handler.command = method
        def reply(h, status, value, **kwargs):
            h.status, h.value = status, value
            h.no_store = kwargs.get('no_store')
        app = dict(JOB_DB_PATH=self.db_path, json_response=reply)
        suffix = '' if endpoint == 'options' else '?' + (query or urlencode(dict(start=START, end=END)))
        routes.dispatch(handler, urlsplit('/api/youtube-analytics/' + endpoint + suffix), app)
        return handler

    def test_route_permission_guard_runs_before_any_storage_or_source_access(self):
        with patch.object(routes, 'read_catalog') as catalog, patch.object(routes, 'query_metrics') as source:
            for endpoint in ('options', 'report', 'export.csv'):
                handler = self.request(endpoint, actor=None)
                self.assertEqual(handler.permission, 'youtubeAnalytics')
        catalog.assert_not_called()
        source.assert_not_called()

    def test_report_and_export_scopes_survive_shared_source_cache(self):
        with patch.object(routes, 'query_metrics', return_value=self.metrics) as query:
            own = self.request()
            admin = self.request(actor=ADMIN)
            other = self.request(actor=dict(tenant_key='tenant-b', user_id='eve', role='admin'))
            own_csv = self.request('export.csv')
            admin_csv = self.request('export.csv', actor=ADMIN)
        self.assertEqual(query.call_count, 1)
        self.assertEqual([h.status for h in (own, admin, other, own_csv, admin_csv)], [200] * 5)
        self.assertEqual([h.value['totals']['clicks'] for h in (own, admin, other)], [100, 300, 900])
        self.assertNotIn('all_rows', own.value)
        for handler, owners in ((own_csv, {'alice'}), (admin_csv, {'alice', 'bob'})):
            rows = list(csv.DictReader(io.StringIO(handler.wfile.getvalue().decode('utf-8-sig'))))
            self.assertEqual({r['owner ID'] for r in rows}, owners)
            self.assertEqual(handler.headers['Cache-Control'], 'no-store')
            self.assertEqual(handler.headers['Vary'], 'Cookie')
        self.assertTrue(own.no_store)

    def test_options_never_queries_source_and_has_exact_scope(self):
        with patch.object(routes, 'query_metrics', side_effect=AssertionError('Options must be local')):
            own, admin = self.request('options'), self.request('options', actor=ADMIN)
        self.assertEqual({r['value'] for r in own.value['options']['owner']}, {'alice'})
        self.assertEqual({r['value'] for r in admin.value['options']['owner']}, {'alice', 'bob'})

    def test_query_failure_is_503_not_zero_and_not_cached(self):
        with patch.object(routes, 'query_metrics', side_effect=[report.ReportError('synthetic source unavailable', 503), self.metrics]) as query:
            failed, recovered = self.request(), self.request()
        self.assertEqual(failed.status, 503)
        self.assertNotIn('totals', failed.value)
        self.assertEqual(recovered.status, 200)
        self.assertEqual(query.call_count, 2)

    def test_expired_cache_does_not_mask_source_refresh_failure(self):
        routes._cache[(START, END)] = (-1000000000, self.metrics, 'expired-snapshot')
        with patch.object(routes, 'query_metrics', side_effect=report.ReportError('source unavailable', 503)):
            failed = self.request()
        self.assertEqual(failed.status, 503)
        self.assertNotIn('totals', failed.value)

    def test_malformed_and_non_get_requests_do_not_query_source(self):
        with patch.object(routes, 'query_metrics') as source:
            self.assertEqual(self.request(query='start=invalid').status, 400)
            self.assertEqual(self.request(method='POST').status, 404)
            self.assertEqual(self.request('no-such-route').status, 404)
        source.assert_not_called()


class SourceQueryTests(unittest.TestCase):
    def test_sql_aggregates_all_source_channels_and_keeps_case_sensitive_keys(self):
        sql = routes.metric_sql(START, END)
        # SQLite executes the generated aggregation after only dialect substitutions.
        sql = sql.replace('kunlunads_dev.', '').replace(' FORCE INDEX(sd)', '')
        sql = sql.replace('BINARY campaign_id', 'campaign_id COLLATE BINARY').replace('BINARY campaign', 'campaign COLLATE BINARY')
        with database(':memory:') as c:
            c.execute('''CREATE TABLE ads_facebook_page_insight(
                dt TEXT,site_id TEXT,campaign_id TEXT,campaign TEXT,channel TEXT,
                clicks INTEGER,views INTEGER,installs INTEGER,recharge INTEGER,
                revenue REAL,refund_revenue REAL,updated_at TEXT)''')
            rows = [(START, '2284', 'task-a', 'campaign-a', 'youtube', 3, 2, 1, 1, 1.25, .25, START),
                    (START, '2284', 'task-a', 'campaign-a', 'none', 4, 3, 2, 1, 2.00, .00, START),
                    (START, '2284', 'task-a', 'campaign-a', 'organic', 5, 4, 1, 0, 3.50, .50, START),
                    (START, '2284', 'Task-A', 'campaign-a', 'youtube', 999, 0, 0, 0, 0, 0, START),
                    (START, '2284', 'task-a', 'Campaign-a', 'youtube', 999, 0, 0, 0, 0, 0, START),
                    (START, 'other-site', 'task-a', 'campaign-a', 'youtube', 999, 0, 0, 0, 0, 0, START)]
            c.executemany('INSERT INTO ads_facebook_page_insight VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            result = [json.loads(bytes.fromhex(row[0]).decode('utf-8')) for row in c.execute(sql)]
        self.assertEqual(len(result), 3)
        exact = next(r for r in result if r['campaign_id'] == 'task-a' and r['campaign'] == 'campaign-a')
        self.assertEqual((exact['clicks'], exact['installs'], exact['conversions'], exact['revenue_cents']), (12, 4, 2, 675))

    def app(self):
        return dict(ADMIN_MAPPING_MYSQL_HOST='101.32.56.53', ADMIN_MAPPING_MYSQL_PORT='63350',
                    ADMIN_MAPPING_MYSQL_USER='synthetic-test-user', ADMIN_MAPPING_MYSQL_PASSWORD='synthetic-placeholder')

    def test_mysql_call_is_read_only_bounded_and_contains_no_password_in_argv(self):
        encoded = json.dumps(metric()).encode('utf-8').hex()
        with patch.object(routes.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='1\n' + encoded + '\n')) as run:
            result = routes.query_metrics(self.app(), START, END)
        self.assertEqual(result, [metric()])
        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertNotIn('synthetic-placeholder', ' '.join(argv))
        self.assertIn('START TRANSACTION READ ONLY', kwargs['input'])
        self.assertIn('ROLLBACK;', kwargs['input'])
        self.assertNotIn('SQL_GATE_BYPASS', kwargs['env'])
        self.assertEqual(kwargs['timeout'], 25)
        self.assertFalse(any(word in kwargs['input'].upper() for word in ('INSERT ', 'UPDATE ', 'DELETE ', 'REPLACE ')))

    def test_source_errors_writable_replica_and_malformed_data_fail_safely(self):
        failures = [SimpleNamespace(returncode=1, stdout='password=should-never-be-shown'),
                    SimpleNamespace(returncode=0, stdout='0\n'), SimpleNamespace(returncode=0, stdout='1\nnot-hex'),
                    subprocess.TimeoutExpired('synthetic', 1)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                kwargs = dict(side_effect=failure) if isinstance(failure, Exception) else dict(return_value=failure)
                with patch.object(routes.subprocess, 'run', **kwargs), self.assertRaises(report.ReportError) as error:
                    routes.query_metrics(self.app(), START, END)
                self.assertEqual(error.exception.status, 503)
                self.assertNotIn('should-never-be-shown', str(error.exception))

    def test_wrong_source_configuration_is_rejected_before_query(self):
        app = self.app()
        app['ADMIN_MAPPING_MYSQL_HOST'] = 'wrong-host'
        with patch.object(routes.subprocess, 'run') as run, self.assertRaises(report.ReportError):
            routes.query_metrics(app, START, END)
        run.assert_not_called()

    def test_source_row_cap_is_not_silently_truncated(self):
        encoded = json.dumps(metric()).encode('utf-8').hex()
        with patch.object(routes, 'MAX_ROWS', 1), patch.object(routes.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0, stdout='1\n' + encoded + '\n' + encoded)), \
                self.assertRaises(report.ReportError) as error:
            routes.query_metrics(self.app(), START, END)
        self.assertEqual(error.exception.status, 400)


if __name__ == '__main__':
    unittest.main()
