import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from features.social_analytics import query_cache, routes, scoped_metrics as scoped
from features.social_analytics.report import ReportError
from test_social_scoped_metrics import ACTOR, DAY, metric, published


class SharedQueryTests(unittest.TestCase):
    def setUp(self):
        routes._cache.clear()

    def tearDown(self):
        routes._cache.clear()

    def test_simultaneous_reads_share_work_and_return_independent_values(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def loader():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return [{'clicks': 4}]
        def read():
            return query_cache.get_or_load('same-visible-sql', loader, deadline=time.monotonic() + 2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(read)
            self.assertTrue(entered.wait(1))
            b = executor.submit(read)
            release.set()
            first, second = a.result(), b.result()
        first[0]['clicks'] = 999
        self.assertEqual(second[0]['clicks'], 4)
        self.assertEqual(read(), [{'clicks': 4}])
        self.assertEqual(len(calls), 1)

    def test_failure_is_not_cached_and_distinct_sql_stays_separate(self):
        with self.assertRaises(ReportError):
            query_cache.get_or_load('failed', lambda: (_ for _ in ()).throw(ReportError('busy', 503)),
                                    deadline=time.monotonic() + 1)
        self.assertEqual(query_cache.get_or_load('failed', lambda: [1], deadline=time.monotonic() + 1), [1])
        self.assertEqual(query_cache.get_or_load('different', lambda: [2], deadline=time.monotonic() + 1), [2])

    def test_waiter_deadline_does_not_cancel_owner_or_invent_zero(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            return [{'clicks': 7}]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(query_cache.get_or_load, 'waiting', slow, deadline=time.monotonic() + 2)
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaisesRegex(ReportError, '等待超时'):
                    query_cache.get_or_load('waiting', lambda: [], deadline=time.monotonic() + .02)
            finally:
                release.set()
            self.assertEqual(future.result(), [{'clicks': 7}])

    def test_payload_and_entry_caps_are_enforced(self):
        with patch.object(query_cache, 'MAX_BYTES', 8), patch.object(query_cache, 'MAX_ENTRIES', 1):
            query_cache.get_or_load('a', lambda: [1], deadline=time.monotonic() + 1)
            query_cache.get_or_load('b', lambda: [2], deadline=time.monotonic() + 1)
            self.assertEqual(len(query_cache._cache), 1)
            query_cache.get_or_load('large', lambda: ['x' * 20], deadline=time.monotonic() + 1)
            self.assertNotIn('large', query_cache._cache)
            self.assertLessEqual(query_cache._bytes, 8)


class RangeReliabilityTests(unittest.TestCase):
    def setUp(self):
        routes._cache.clear()

    def tearDown(self):
        routes._cache.clear()

    def test_broad_web_windows_really_overlap_and_keep_every_date(self):
        barrier = threading.Barrier(2)
        active, peak = 0, 0
        lock = threading.Lock()
        def query(app, sql, limit):
            nonlocal active, peak
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date='2026-10-01'), dict(date='2026-10-08')]
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(2)
                day = '2026-10-01' if "BETWEEN '2026-10-01'" in sql else '2026-10-08'
                return [metric(date=day, campaign='p000')]
            finally:
                with lock:
                    active -= 1
        with patch.object(routes, 'query_source_rows', side_effect=query):
            rows, _, coverage = scoped.source({'SOCIAL_ANALYTICS_RANGE_WORKERS': 2},
                '2026-10-01', '2026-10-14', 'youtube', '2284', ['p%03d' % i for i in range(101)])
        self.assertEqual(peak, 2)
        self.assertEqual({r['date'] for r in rows}, coverage)

    def test_parallel_failure_never_returns_partial_success(self):
        def query(app, sql, limit):
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date='2026-10-01'), dict(date='2026-10-08')]
            if "BETWEEN '2026-10-08'" in sql:
                raise ReportError('busy', 503)
            return [metric(date='2026-10-01', campaign='p000')]
        with patch.object(routes, 'query_source_rows', side_effect=query), self.assertRaises(ReportError):
            scoped.source({'SOCIAL_ANALYTICS_RANGE_WORKERS': 2}, '2026-10-01', '2026-10-14',
                          'youtube', '2284', ['p%03d' % i for i in range(101)])
        self.assertFalse(routes._cache)

    def test_failed_read_is_unverified_not_claimed_unsynced(self):
        with patch.object(routes, 'read_catalogs', return_value=([published()], {})), \
                patch.object(scoped, 'source', side_effect=ReportError('busy', 503)):
            value = routes.build_report({}, ACTOR, {'start': [DAY], 'end': [DAY], 'platform': ['youtube']},
                                        ai_backend_only=True)
        self.assertFalse(value['quality']['complete'])
        self.assertEqual(value['quality']['missing_dates'], [])
        self.assertEqual(value['quality']['unverified_dates'], [DAY])
        self.assertIsNone(value['totals']['revenue'])
        self.assertEqual(value['totals']['published_count'], 1)

    def test_broad_x_history_uses_two_bounded_site_reads_instead_of_48_connections(self):
        ids = ['x%04d' % i for i in range(4712)]
        def query(app, sql, limit):
            if 'GROUP BY dt LIMIT 94' in sql:
                return [dict(date=DAY)]
            self.assertIn('FORCE INDEX(sd)', sql)
            self.assertIn("site_id='2116'", sql)
            self.assertIn('AND BINARY campaign_id IN (', sql)
            included = [cid for cid in ids if 'CONVERT(0x' + cid.encode().hex() + ' USING' in sql]
            return [metric(campaign=cid, site_id='2116', platform='x') for cid in included]
        with patch.object(routes, 'query_source_rows', side_effect=query) as read:
            rows, _, coverage = scoped.source({}, '2026-10-18', DAY, 'x', '2116', ids)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(len(rows), len(ids))
        self.assertEqual({r['campaign_id'] for r in rows}, set(ids))
        self.assertEqual(coverage, {DAY})

    def test_cancelled_browser_request_does_not_send_a_second_error_response(self):
        from urllib.parse import urlsplit
        from unittest.mock import Mock
        class Handler:
            command = 'GET'
            close_connection = False
            def _social_analytics_actor(self):
                return dict(ACTOR)
        h=Handler();send=Mock(side_effect=BrokenPipeError())
        with patch.object(routes, 'build_report', return_value={'all_rows': []}):
            routes.dispatch(h, urlsplit('/api/social-analytics/report'), {'json_response': send})
        self.assertTrue(h.close_connection)
        self.assertEqual(send.call_count, 1)
