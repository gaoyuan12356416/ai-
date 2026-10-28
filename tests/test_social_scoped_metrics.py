import copy
import csv
import io
import unittest
import re
from datetime import date, timedelta
from unittest.mock import patch

from features.social_analytics import report, routes, scoped_metrics as scoped
from test_social_analytics import ACTOR, DAY, SYSTEM, catalog, metric as raw_metric, params


def metric(**changes):
    changes.setdefault('site_id', '2284')
    return raw_metric(**changes)


def published(**changes):
    return catalog(publications=[dict(id='video-' + changes.get('campaign', 'campaign'), date=DAY, kind='video')], **changes)


class ScopedMetricTests(unittest.TestCase):
    def setUp(self):
        routes._cache.clear()

    def tearDown(self):
        routes._cache.clear()

    def test_scope_uses_acl_all_filters_and_known_aliases(self):
        cid = 'a' * 32 + '-suffix'
        rows = [published(campaign=cid, language='ja', drama_label='Wanted drama'),
                published(campaign='other-owner', owner='bob'),
                published(campaign='other-tenant', tenant='elsewhere'),
                published(campaign='different-drama'), catalog(campaign='failed')]
        p = params(platform=['youtube'], owner=['alice'], language=['ja'], search=['wanted'])
        self.assertEqual(scoped.campaign_scope(rows, ACTOR, p, 'youtube', '2284'),
                         (cid[:32], cid))
        self.assertEqual(scoped.campaign_scope(rows, dict(ACTOR, role='user', user_id='bob'),
                                               p, 'youtube', '2284'), ())

    def test_no_filter_can_hide_global_conflict(self):
        rows = [published(owner='alice'), published(owner='bob')]
        p = params(platform=['youtube'], owner=['alice'])
        self.assertEqual(scoped.campaign_scope(rows, ACTOR, p, 'youtube', '2284'), ('campaign',))
        with patch.object(routes, 'read_catalogs', return_value=(rows, {})), \
                patch.object(scoped, 'source', return_value=([metric()], 'now', {DAY})):
            with self.assertRaisesRegex(report.ReportError, '冲突'):
                routes.build_report({}, ACTOR, {'start': [DAY], 'end': [DAY],
                                               'platform': ['youtube'], 'owner': ['alice']},
                                    ai_backend_only=True)

    def test_sql_keeps_index_and_exact_case_without_interpolating_id(self):
        value = "a' OR 1=1 --"
        sql = scoped.metric_sql(DAY, DAY, 'youtube', '2284', [value])
        self.assertIn('FORCE INDEX(c_id_dt)', sql)
        self.assertIn('AND campaign_id IN (CONVERT(0x', sql)
        self.assertIn('AND BINARY campaign_id IN (0x', sql)
        self.assertNotIn(value, sql)
        self.assertIn("site_id='2284'", sql)

    def test_56_days_split_exactly_without_overlap(self):
        self.assertEqual(list(scoped.windows('2026-09-01', '2026-10-26')), [
            ('2026-09-01', '2026-09-14'), ('2026-09-15', '2026-09-28'),
            ('2026-09-29', '2026-10-12'), ('2026-10-13', '2026-10-26')])

    def test_single_post_long_range_avoids_empty_window_round_trips(self):
        with patch.object(routes, 'query_source_rows', side_effect=[[dict(date=DAY)], [metric()]]) as read:
            scoped.source({}, '2026-09-01', '2026-10-26', 'youtube', '2284', ['campaign'])
        self.assertEqual(read.call_count, 2)
        self.assertIn("dt BETWEEN '2026-09-01' AND '2026-10-26'", read.call_args.args[1])
        self.assertIn('FORCE INDEX(c_id_dt)', read.call_args.args[1])

    def test_success_cache_is_scoped_to_ids_and_source_site(self):
        def query(app, sql, limit):
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date=DAY)]
            cid = 'new' if '0x6e6577' in sql else 'campaign'
            return [metric(campaign=cid, site_id='2284')]
        with patch.object(routes, 'query_source_rows', side_effect=query) as read:
            a = scoped.source({}, DAY, DAY, 'youtube', '2284', ['campaign'])
            b = scoped.source({}, DAY, DAY, 'youtube', '2284', ['campaign'])
            scoped.source({}, DAY, DAY, 'youtube', '2284', ['new'])
        self.assertEqual(a, b)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(a[2], {DAY})

    def test_failed_batch_keeps_nothing_and_can_retry_fresh(self):
        with patch.object(routes, 'query_source_rows', side_effect=[
                [dict(date=DAY)], report.ReportError('效果查询超时（MySQL 3024）', 503)]):
            with self.assertRaises(report.ReportError):
                scoped.source({}, DAY, DAY, 'youtube', '2284', ['campaign'])
        self.assertFalse(routes._cache)
        with patch.object(routes, 'query_source_rows', side_effect=[[dict(date=DAY)], [metric()]]):
            self.assertEqual(len(scoped.source({}, DAY, DAY, 'youtube', '2284', ['campaign'])[0]), 1)

    def test_empty_selection_queries_only_coverage(self):
        with patch.object(routes, 'query_source_rows', return_value=[dict(date=DAY)]) as read:
            rows, _, coverage = scoped.source({}, DAY, DAY, 'youtube', '2284', [])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(rows, [])
        self.assertEqual(coverage, {DAY})

    def test_missing_date_stays_missing_not_zero(self):
        rows = [published()]
        query = {'start': ['2026-10-18'], 'end': [DAY], 'platform': ['youtube']}
        with patch.object(routes, 'read_catalogs', return_value=(rows, {})), \
                patch.object(scoped, 'source', return_value=([metric()], 'now', {DAY})):
            value = routes.build_report({}, ACTOR, query, ai_backend_only=True)
        self.assertEqual(value['totals']['clicks'], 4)
        self.assertIsNone(value['trend'][0]['clicks'])
        self.assertEqual(value['quality']['missing_dates'], ['2026-10-18'])
        self.assertEqual(value['rows'][0]['data_status'], 'partial')

    def test_source_failure_does_not_turn_coverage_into_false_zero(self):
        with patch.object(routes, 'read_catalogs', return_value=([published()], {})), \
                patch.object(scoped, 'source', side_effect=report.ReportError('timeout', 503)):
            value = routes.build_report({}, ACTOR, {'start': [DAY], 'end': [DAY], 'platform': ['youtube']},
                                        ai_backend_only=True)
        self.assertIsNone(value['totals']['clicks'])
        self.assertEqual(value['totals']['published_count'], 1)

    def test_date_case_site_and_duplicate_validation_are_fail_closed(self):
        for bad in [metric(campaign='CAMPAIGN'), metric(date='2026-10-18'),
                    metric(site_id='2116'), metric(clicks=None)]:
            routes._cache.clear()
            with patch.object(routes, 'query_source_rows', side_effect=[[dict(date=DAY)], [bad]]):
                with self.assertRaises(report.ReportError):
                    scoped.source({}, DAY, DAY, 'youtube', '2284', ['campaign'])
        with patch.object(routes, 'query_source_rows', side_effect=[[dict(date=DAY)], [metric(), metric()]]):
            with self.assertRaises(report.ReportError):
                scoped.source({}, DAY, DAY, 'youtube', '2284', ['campaign'])

    def test_row_budget_applies_across_batches(self):
        values = ['campaign', 'second']
        with patch.object(scoped, 'SINGLE_DAY_BATCH_SIZE', 1), patch.object(routes, 'MAX_ROWS', 1), \
                patch.object(routes, 'query_source_rows', side_effect=[
                    [dict(date=DAY)], [metric()], [metric(campaign='second')]]):
            with self.assertRaisesRegex(report.ReportError, '上限'):
                scoped.source({}, DAY, DAY, 'youtube', '2284', values)
        self.assertFalse(routes._cache)

    def test_single_day_large_history_keeps_all_batches_and_exact_ids(self):
        ids = ['p%04d' % i for i in range(2100)]
        def query(app, sql, limit):
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date=DAY)]
            present = [cid for cid in ids if 'CONVERT(0x' + cid.encode().hex() + ' USING' in sql]
            return [metric(campaign=cid) for cid in present]
        with patch.object(routes, 'query_source_rows', side_effect=query) as read:
            rows, _, coverage = scoped.source({}, DAY, DAY, 'youtube', '2284', ids)
        self.assertEqual(read.call_count, 4)
        self.assertEqual({r['campaign_id'] for r in rows}, set(ids))
        self.assertEqual(len(rows), 2100)
        self.assertEqual(coverage, {DAY})
        with self.assertRaises(report.ReportError):
            scoped.metric_sql('2026-09-01', DAY, 'youtube', '2284', ids[:101])

    def test_last_single_day_batch_failure_never_caches_partial_success(self):
        ids = ['p%04d' % i for i in range(1001)]
        with patch.object(routes, 'query_source_rows', side_effect=[
                [dict(date=DAY)], [metric(campaign=ids[0])], report.ReportError('timeout', 503)]):
            with self.assertRaises(report.ReportError):
                scoped.source({}, DAY, DAY, 'youtube', '2284', ids)
        self.assertFalse(routes._cache)

    def test_broad_youtube_month_uses_weekly_site_ranges_with_exact_ids(self):
        ids = ['p%04d' % i for i in range(2038)]
        days = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(27)]
        def query(app, sql, limit):
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date=day) for day in days]
            self.assertIn('FORCE INDEX(sd)', sql)
            self.assertIn('AND BINARY campaign_id IN (', sql)
            self.assertIn("site_id='2284'", sql)
            first, last = re.search(r"dt BETWEEN '([^']+)' AND '([^']+)'", sql).groups()
            self.assertLessEqual((date.fromisoformat(last) - date.fromisoformat(first)).days, 6)
            return [metric(date=day, campaign=cid) for day in days if first <= day <= last
                    for cid in (ids[0], ids[-1])]
        with patch.object(routes, 'query_source_rows', side_effect=query) as read:
            rows, _, coverage = scoped.source({}, days[0], days[-1], 'youtube', '2284', ids)
        self.assertEqual(read.call_count, 5)
        self.assertEqual(len(rows), 54)
        self.assertEqual(coverage, set(days))
        self.assertEqual({(r['date'], r['campaign_id']) for r in rows},
                         {(day, cid) for day in days for cid in (ids[0], ids[-1])})

    def test_broad_range_batches_are_disjoint_and_missing_days_stay_missing(self):
        ids = ['p%04d' % i for i in range(3001)]
        def query(app, sql, limit):
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date=DAY)]
            included = [cid for cid in ids if 'CONVERT(0x' + cid.encode().hex() + ' USING' in sql]
            return [metric(campaign=cid) for cid in included]
        with patch.object(routes, 'query_source_rows', side_effect=query) as read:
            rows, _, coverage = scoped.source({}, '2026-10-18', DAY, 'youtube', '2284', ids)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(len(rows), len(ids))
        self.assertEqual({r['campaign_id'] for r in rows}, set(ids))
        self.assertEqual(coverage, {DAY})

    def test_broad_range_last_window_failure_does_not_cache_partial_metrics(self):
        ids = ['p%04d' % i for i in range(101)]
        with patch.object(routes, 'query_source_rows', side_effect=[
                [dict(date='2026-10-01'), dict(date='2026-10-08')],
                [metric(date='2026-10-01', campaign=ids[0])], report.ReportError('timeout', 503)]):
            with self.assertRaises(report.ReportError):
                scoped.source({}, '2026-10-01', '2026-10-14', 'youtube', '2284', ids)
        self.assertFalse(routes._cache)

    def test_site_scan_is_bounded_and_does_not_change_x_or_narrow_queries(self):
        for platform, start, end, ids in [
                ('x', DAY, DAY, ['id']),
                ('youtube', '2026-10-01', '2026-10-08', ['id']),
                ('youtube', DAY, DAY, ['id'] * 3001)]:
            with self.assertRaises(report.ReportError):
                scoped.metric_sql(start, end, platform, '2116' if platform == 'x' else '2284',
                                  ids, site_scan=True)
        self.assertIn('FORCE INDEX(c_id_dt)', scoped.metric_sql(DAY, DAY, 'youtube', '2284', ['id']))
        self.assertIn('FORCE INDEX(c_id_dt)', scoped.metric_sql('2026-10-01', DAY, 'x', '2116', ['id']))

    def test_filters_and_csv_keep_same_values_as_full_source(self):
        rows = [published(), published(campaign='other', owner='bob')]
        raw = [metric(), metric(campaign='other', cents=999)]
        query = {'start': [DAY], 'end': [DAY], 'platform': ['youtube'], 'owner': ['alice']}
        with patch.object(routes, 'read_catalogs', return_value=(copy.deepcopy(rows), {})), \
                patch.object(scoped, 'source', return_value=([metric()], 'now', {DAY})):
            value = routes.build_report({}, ACTOR, query, ai_backend_only=True)
        expected = report.summarize(rows, raw, ACTOR, report.parameters(query), 'now')
        for key in report.FIELDS + report.COUNTS:
            self.assertEqual(value['totals'][key], expected['totals'][key])
        exported = list(csv.DictReader(io.StringIO(report.csv_export(value).decode('utf-8-sig'))))
        self.assertEqual(exported[0]['落地页点击'], '4')
        self.assertEqual(exported[0]['收入（USD）'], '1.0')
        self.assertEqual(len(exported), 1)

    def test_scheduled_collector_retains_full_source_contract(self):
        with patch.object(routes, 'read_catalogs', return_value=([published()], {})), \
                patch.object(routes, 'source', return_value=([metric()], 'now')) as old, \
                patch.object(scoped, 'source') as new:
            value = routes.build_report({}, SYSTEM, {'start': [DAY], 'end': [DAY], 'platform': ['youtube']},
                                        ai_backend_only=True)
        old.assert_called_once()
        new.assert_not_called()
        self.assertEqual(value['totals']['revenue_cents'], 100)


if __name__ == '__main__':
    unittest.main()
