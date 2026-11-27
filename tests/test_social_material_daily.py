from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from features.social_material_effects import data, daily, report

NOW = datetime(2026, 11, 27, 4, tzinfo=timezone.utc)
ADMIN = dict(role='admin')


def post(pid='1', **changes):
    row = dict(post_id=pid,namespace='ads_custom_source',material_id='7',catalog='dramawave',
               material_name='Old material',content_id='drama1',drama_name='Drama',product='Dramawave',language='en',
               account_id='a',account_ids=['a'],published_at='2026-10-01T00:00:00Z',owner='u',tenant='t')
    row.update(changes)
    return row


class DailyReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def publish(self, day, posts, channel='facebook'):
        stamp = day+'T15:00:00+00:00'
        data.publish(self.root,channel,[dict(p,metrics_at=p.get('metrics_at',stamp)) for p in posts],
                     dict(version=stamp,generated_at=stamp,revenue_source_generated_at=stamp,revenue_through=day))

    def query(self, start='2026-11-25', end='2026-11-26', actor=None, **params):
        return report.query(dict(start=[start],end=[end],**{k:[str(v)] for k,v in params.items()}),
                            actor or ADMIN,self.root,now=NOW)

    def seed(self, channel='facebook'):
        self.publish('2026-11-24',[post(views=100,likes=10,comments=2,shares=1,reposts=1,quotes=0,bookmarks=3),
                                   post('2',views=20)],channel)
        self.publish('2026-11-25',[post(views=150,likes=15,comments=3,shares=3,reposts=2,quotes=1,bookmarks=4),
                                   post('2',views=None)],channel)
        income = dict(start='2026-11-24',end='2026-11-26',missing=[],daily={'2026-11-24':9999,'2026-11-25':100,'2026-11-26':250})
        self.publish('2026-11-26',[post(views=170,likes=18,comments=5,shares=4,reposts=3,quotes=1,bookmarks=6,
                                        revenue_cents=10349,_window_income=income),
                                   post('2',views=40,owner='v',_window_income=dict(income,missing=['2026-11-26'],daily={}))],channel)

    def test_group_and_filter_by_data_day_for_older_material(self):
        self.seed()
        result = self.query()
        self.assertEqual((result['total'],result['material_count'],result['data_days']),(2,1,2))
        self.assertEqual([p['data_date'] for p in result['items']],['2026-11-26','2026-11-25'])
        self.assertEqual([p['known']['views'] for p in result['items']],[20,50])
        self.assertEqual([p['known']['revenue_cents'] for p in result['items']],[250,100])
        self.assertEqual(result['summary']['views'],70)
        self.assertEqual(result['summary']['revenue_cents'],350)
        self.assertEqual(self.query(start='2026-11-25',end='2026-11-25')['summary']['views'],50)
        self.assertEqual(self.query(start='2026-11-26',end='2026-11-26')['summary']['views'],20)
        self.assertEqual(result['meta']['dates']['mode'],'daily_increment')

    def test_unknowns_are_not_zero_and_detail_matches_row(self):
        self.seed()
        row = self.query()['items'][0]
        self.assertIsNone(row['views'])
        self.assertEqual(row['coverage']['views'],1)
        self.assertEqual(row['post_count'],2)
        result = self.query(start='2026-11-26',view='posts',material_id='7')
        self.assertEqual(result['total'],2)
        self.assertEqual(result['summary']['views'],row['known']['views'])
        self.assertEqual(result['summary']['revenue_cents'],row['known']['revenue_cents'])
        self.assertIsNone(result['items'][1]['views'])
        self.assertIsNone(result['items'][1]['revenue_cents'])
        self.assertEqual(result['items'][0]['bookmarks'],2)
        self.assertNotIn('owner',result['items'][0])
        self.assertNotIn('account_ids',result['items'][0])

    def test_source_and_namespace_identity_and_non_date_filters(self):
        for channel in ('x','facebook'):
            self.seed(channel)
        result = self.query(start='2026-11-26')
        self.assertEqual((result['total'],result['material_count'],result['summary']['views']),(2,2,40))
        self.assertEqual(self.query(channel='x',q='old',language='en',product='Dramawave',account_id='a')['total'],2)
        self.assertEqual(self.query(channel='x',product='Other')['total'],0)
        self.publish('2026-11-26',[post(views=180),post('3',namespace='other',views=10,
                    published_at='2026-11-26T01:00:00Z')])
        self.assertEqual(self.query(start='2026-11-26',channel='facebook')['total'],2)

    def test_beijing_midnight_new_posts_and_utc_income(self):
        income = dict(start='2026-11-25',end='2026-11-26',missing=[],daily={'2026-11-25':30,'2026-11-26':40})
        self.publish('2026-11-26',[post(published_at='2026-11-25T16:00:00Z',views=5,_window_income=income)])
        rows = self.query()['items']
        self.assertEqual((rows[0]['data_date'],rows[0]['views'],rows[0]['revenue_cents']),('2026-11-26',5,40))
        self.assertEqual((rows[1]['data_date'],rows[1]['views'],rows[1]['revenue_cents']),('2026-11-25',None,30))
        self.assertEqual(self.query()['meta']['dates']['revenue_timezone'],'UTC')

    def test_missing_endpoint_reset_and_stale_field_never_use_lifetime(self):
        self.publish('2026-11-25',[post(views=200,likes=30),post('2',material_id='8',views=40)])
        self.publish('2026-11-26',[post(views=100,likes=30,_metric_times={'likes':'2026-11-25T15:00:00Z'}),
                                   post('2',material_id='8',views=40,metrics_at='2026-11-25T15:00:00Z'),
                                   post('3',material_id='9',views=900)])
        rows = {r['material_id']:r for r in self.query(start='2026-11-26')['items']}
        self.assertIsNone(rows['7']['views'])
        self.assertIsNone(rows['7']['likes'])
        self.assertIsNone(rows['9']['views'])
        self.assertNotIn('8',rows)  # No evidence on the selected data date.

    def test_permissions_are_preserved_for_post_details(self):
        self.seed()
        self.seed('x')
        actor = dict(role='user',user_id='u',tenant_key='t')
        self.assertEqual(self.query(start='2026-11-26',actor=actor,channel='facebook',view='posts')['total'],1)
        self.assertEqual(self.query(start='2026-11-26',actor=dict(actor,tenant_key='other'),channel='facebook',view='posts')['total'],0)
        with patch.object(report,'visible_x',return_value=['a']):
            self.assertEqual(self.query(start='2026-11-26',actor=actor,channel='x',view='posts')['total'],2)
        with patch.object(report,'visible_x',return_value=[]):
            self.assertEqual(self.query(start='2026-11-26',actor=actor,channel='x',view='posts')['total'],0)

    def test_sort_pagination_empty_range_and_default_dates(self):
        self.seed()
        asc = self.query(sort='data_date',direction='asc',page_size=1)
        self.assertEqual((asc['total'],asc['items'][0]['data_date']),(2,'2026-11-25'))
        self.assertEqual(self.query(sort='views',page_size=1)['items'][0]['data_date'],'2026-11-25')
        beyond = self.query(page=3,page_size=1)
        self.assertEqual((beyond['total'],beyond['items'],beyond['summary']['views']),(2,[],70))
        empty = self.query(start='2026-10-01',end='2026-10-02')
        self.assertEqual((empty['total'],empty['items'],empty['summary']['views']),(0,[],None))
        self.assertEqual(daily.date_range(now=NOW),('2026-11-20','2026-11-26','2026-11-27'))
        self.assertEqual(daily.date_range(now=datetime(2026,11,26,16,tzinfo=timezone.utc))[1],'2026-11-26')
        self.assertTrue(self.query(start='2026-11-27',end='2026-11-27')['meta']['dates']['includes_today'])
        for params in (dict(start='bad'),dict(start='2026-11-27',end='2026-11-26'),dict(sort='published_at')):
            with self.assertRaises(ValueError): self.query(**params)

    def test_old_snapshot_is_unavailable_and_other_channel_continues(self):
        self.seed()
        self.seed('x')
        with closing(sqlite3.connect(self.root/'x.sqlite3')) as db:
            for name in ('counter_day','income_day','income_scope'):
                db.execute('DROP TABLE '+name)
            db.commit()
        result = self.query(start='2026-11-26')
        self.assertFalse(result['meta']['dates']['availability']['x']['available'])
        self.assertEqual((result['total'],result['summary']['views']),(1,20))

    def test_retention_does_not_turn_pruned_income_into_zero(self):
        self.publish('2026-11-26',[post(_window_income=dict(start='2026-10-01',end='2026-11-26',missing=[],
                                    daily={'2026-10-01':9000,'2026-11-26':100}))])
        old = self.query(start='2026-10-01',end='2026-10-01')
        self.assertEqual(old['total'],0)
        self.assertIsNone(old['summary']['revenue_cents'])
        current = self.query(start='2026-11-26')
        self.assertEqual(current['summary']['revenue_cents'],100)


if __name__ == '__main__':
    unittest.main()
