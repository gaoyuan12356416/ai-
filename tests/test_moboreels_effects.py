import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from decimal import Decimal

from features.moboreels_library.effects import Effects
from features.moboreels_library.clicks import ClickStats
from features.moboreels_library.orders import ReportStore, _query_report
from features.moboreels_library.promotions import identity
from features.moboreels_library.settings import Settings
import test_moboreels_orders as fixtures
from test_moboreels_orders import SOURCE_DAY, PERIOD


class EffectsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.actor = {'tenant_key': 'tenant', 'user_id': 'user', 'name': 'Owner'}
        self.settings = Settings(Path(temp.name), 'institution', 'test', 'test', 'test', 'test')
        self.star = identity('institution', self.actor)[0]
        self.fixture = fixtures.ReportStoreTest(); reader = self.fixture.report_reader()
        self.fixture.mapping['star_user_id'] = self.star
        for r in self.fixture.source:
            if r.get('_kind') == 'order':
                r['real_star_user_id'] = self.star
        def read(sql):
            result = reader._read(sql)
            if "'_kind','ad_day'" in sql:
                result = [r for r in result if r['_kind'] != 'coverage']
                result.append({'_kind': 'ad_day', 'date': SOURCE_DAY})
            return result
        self.store = ReportStore(self.settings, SimpleNamespace(_read=read))
        self.jobs = [{'date_from': SOURCE_DAY, 'date_to': SOURCE_DAY, 'status': 'succeeded',
                      'finished_at': '2026-11-11T02:00:00Z', 'parts': [{'kind':'orders','status':'succeeded'}]}]
        self.reports = SimpleNamespace(store=self.store, _jobs=lambda:self.jobs)
        self.clicks = ClickStats(self.settings)
        with self.clicks.connect() as c:
            c.execute("UPDATE metadata SET value=? WHERE key='coverage_from'", (SOURCE_DAY,))
            c.executemany('INSERT INTO daily VALUES(?,?,?,?,?)', [(SOURCE_DAY,'x1','x',4,2),(SOURCE_DAY,'x2','x',5,0),(SOURCE_DAY,'fb1','fb',3,1)])
        self.base = dict(institution_user_id='institution',project_type=2,status='succeeded',
                         tenant_key='tenant',user_id='user',star_user_id=self.star,star_name='Owner',story_id='42',koc_code='25FA')
        self.write('promotions.sqlite3','promotions',[self.base])
        self.links = [dict(self.base,short_code='x1',platform='x',account_id='one'),dict(self.base,short_code='x2',platform='x',account_id='two'),dict(self.base,short_code='fb1',platform='fb',account_id='three')]
        self.write('short-links.sqlite3','links',self.links)
        self.effects=Effects(self.settings,self.reports,self.clicks)

    def write(self,path,table,rows):
        with closing(sqlite3.connect(self.settings.data_dir/path)) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS '+table+'(record_json TEXT)')
            c.execute('DELETE FROM '+table)
            c.executemany('INSERT INTO '+table+' VALUES(?)', [(json.dumps(r),) for r in rows])

    def query(self,**params):
        return self.effects.query(dict(PERIOD,**params),dict(self.actor,is_admin=True))

    def test_shared_promotion_conserves_money_and_clicks(self):
        result=self.query()
        self.assertEqual(result['summary']['clicks'],12)
        self.assertEqual(result['summary']['links_clicked'],3)
        self.assertEqual(Decimal(result['summary']['total_revenue_usd']),Decimal('9.00125'))
        groups={r['dimensions'][0]['value']:r for r in result['list']}
        self.assertEqual(Decimal(groups['shared']['total_revenue_usd']),Decimal('7.00125'))
        self.assertEqual(Decimal(groups['unknown']['total_revenue_usd']),2)
        self.assertEqual(Decimal(groups['x']['total_revenue_usd']),0)
        self.assertEqual(result['summary']['order_refund_count'],1)
        self.assertEqual(Decimal(result['summary']['unclassified_order_usd']),Decimal('9.125'))

    def test_channel_filter_reports_excluded_shared_income(self):
        r=self.query(platform='x')
        self.assertEqual(r['summary']['clicks'],9)
        self.assertEqual(Decimal(r['summary']['total_revenue_usd']),0)
        self.assertEqual(Decimal(r['excluded_shared']['total_revenue_usd']),Decimal('9.00125'))

    def test_owner_and_publisher_filter_do_not_leak_unmatched_ads(self):
        r=self.effects.query(PERIOD,self.actor)
        self.assertEqual(Decimal(r['summary']['total_revenue_usd']),Decimal('7.00125'))
        self.assertEqual(r['scope']['kind'],'owner')
        r=self.query(publisher=self.star)
        self.assertEqual(Decimal(r['summary']['total_revenue_usd']),Decimal('7.00125'))
        with patch('features.moboreels_library.effects._query_report',return_value={'list':[],'ad_data_as_of':SOURCE_DAY}) as read:
            r=self.effects.query(dict(PERIOD,is_admin=True,all_users=True),dict(self.actor,user_id='foreign'))
            self.assertEqual(r['summary']['clicks'],0)
            self.assertNotEqual(read.call_args.args[2],None)

    def test_story_search_and_dimensions_keep_full_summary(self):
        for group in ['publisher','story','promotion','day','platform,publisher,story']:
            r=self.query(group_by=group,q='title')
            self.assertEqual(Decimal(r['summary']['total_revenue_usd']),Decimal('7.00125'))
            self.assertEqual(r['summary']['clicks'],12)
            self.assertEqual(sum(Decimal(x['total_revenue_usd']) for x in r['list']),Decimal('7.00125'))
        with self.assertRaises(ValueError):self.query(group_by='platform,platform')

    def test_validation_links_excluded_and_single_platform_shared_accounts(self):
        self.links[2]['purpose']='validation';self.write('short-links.sqlite3','links',self.links)
        r=self.query(group_by='platform,account')
        self.assertEqual(r['summary']['clicks'],9)
        matched=[x for x in r['list'] if Decimal(x['total_revenue_usd'])==Decimal('7.00125')][0]
        self.assertEqual([d['value'] for d in matched['dimensions']],['x','x:shared'])

    def test_revenue_without_clicks_and_unknown_coverage(self):
        self.write('short-links.sqlite3','links',[])
        self.jobs.clear()
        r=self.query()
        self.assertEqual(r['summary']['clicks'],0)
        self.assertEqual(Decimal(r['summary']['total_revenue_usd']),Decimal('9.00125'))
        self.assertEqual(r['complete_order_days'],0)
        self.assertFalse(r['trend'][0]['orders_synced'])
        self.assertTrue(r['trend'][0]['ads_synced'])

    def test_internal_facts_keep_days_and_do_not_paginate(self):
        row=dict(self.fixture.source[0],date='2026-11-11');self.fixture.source.append(row)
        r=_query_report(self.store,dict(date_from=SOURCE_DAY,date_to='2026-11-11',page_size=1),None,facts=True)
        self.assertEqual(len(r['list']),3)
        self.assertEqual({x['date'] for x in r['list']},{SOURCE_DAY,'2026-11-11'})
        legacy=self.store.query(dict(date_from=SOURCE_DAY,date_to='2026-11-11',page_size=1))
        self.assertEqual(len(legacy['list']),1)

    def test_income_reused_across_filters_but_clicks_are_live(self):
        first = self.query()
        self.assertEqual(len(self.fixture.queries), 2)
        with self.clicks.connect() as conn:
            conn.execute("UPDATE daily SET clicks=clicks+1 WHERE short_code='x1'")
        second = self.query(group_by='story')
        self.assertEqual(len(self.fixture.queries), 2)
        self.assertEqual(second['summary']['clicks'], first['summary']['clicks']+1)
        self.assertEqual(second['summary']['total_revenue_usd'], first['summary']['total_revenue_usd'])
        self.query(platform='x', q='Title')
        self.assertEqual(len(self.fixture.queries), 2)

    def test_cache_never_crosses_owner_or_admin_scope(self):
        self.query()
        owner = self.effects.query(PERIOD, self.actor)
        self.assertEqual(len(self.fixture.queries), 4)
        self.assertEqual(Decimal(owner['summary']['total_revenue_usd']), Decimal('7.00125'))
        with patch('features.moboreels_library.effects._query_report', return_value={'list': [], 'ad_dates': [], 'ad_data_as_of': None}) as read:
            foreign = self.effects.query(PERIOD, dict(self.actor, user_id='foreign'))
            self.assertEqual(read.call_count, 1)
            self.assertEqual(foreign['summary']['clicks'], 0)
            self.assertEqual(Decimal(foreign['summary']['total_revenue_usd']), 0)

    def test_sync_and_promotion_receipts_invalidate_income_cache(self):
        self.query()
        self.jobs[0]['updated_at'] = '2026-11-20T11:01:00Z'
        self.query()
        self.assertEqual(len(self.fixture.queries), 4)
        self.write('promotions.sqlite3', 'promotions', [dict(self.base, star_name='New name')])
        self.query()
        self.assertEqual(len(self.fixture.queries), 6)

    def test_ttl_expiry_failure_and_detached_responses(self):
        with patch('features.moboreels_library.effects.time.monotonic', return_value=100):
            first = self.query()
            first['summary']['total_revenue_usd'] = '999'
        with patch('features.moboreels_library.effects.time.monotonic', return_value=129.9):
            self.assertNotEqual(self.query()['summary']['total_revenue_usd'], '999')
        self.assertEqual(len(self.fixture.queries), 2)
        with patch('features.moboreels_library.effects.time.monotonic', return_value=130), \
                patch.object(self.store.reader, '_read', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError): self.query()
        self.query()
        self.assertEqual(len(self.fixture.queries), 4)

    def test_simultaneous_queries_share_only_income_read(self):
        entered, release = threading.Event(), threading.Event()
        original = _query_report
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise RuntimeError('test timed out')
            return original(*args, **kwargs)
        with patch('features.moboreels_library.effects._query_report', side_effect=blocked) as read:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = [pool.submit(self.query) for _ in range(4)]
                self.assertTrue(entered.wait(3))
                time.sleep(.05)
                release.set()
                results = [r.result(3) for r in results]
            self.assertEqual(read.call_count, 1)
        self.assertTrue(all(r['summary'] == results[0]['summary'] for r in results))

    def test_click_only_story_names_and_empty_ad_day_coverage_are_batched(self):
        self.links.append(dict(self.base, short_code='only', story_id='99', platform='youtube', account_id='four'))
        self.write('short-links.sqlite3', 'links', self.links)
        with self.clicks.connect() as conn:
            conn.execute('INSERT INTO daily VALUES(?,?,?,?,?)', (SOURCE_DAY, 'only', 'youtube', 2, 0))
        original = self.store.reader._read
        def read(sql):
            rows = original(sql)
            if "'_kind','story'" in sql:
                rows.append({'_kind': 'story', 'story_id': '99', 'story_name': 'Click only'})
            return rows
        self.store.reader._read = read
        result = self.query(group_by='story', q='Click only')
        self.assertEqual(result['summary']['clicks'], 2)
        self.assertEqual(result['list'][0]['dimensions'][0]['label'], 'Click only')
        self.assertTrue(result['trend'][0]['ads_synced'])
        self.assertEqual(len(self.fixture.queries), 2)

if __name__=='__main__':unittest.main()
