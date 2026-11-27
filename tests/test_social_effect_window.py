from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from features.social_material_effects import data, selection, window

NOW = datetime(2026,11,27,4,tzinfo=timezone.utc)


def post(pid='1', **changes):
    row = dict(post_id=pid,namespace='ads_custom_source',material_id=pid,catalog='dramawave',
               content_id='old',product='Dramawave',language='en',account_id='2',
               published_at='2026-10-01T00:00:00Z')
    row.update(changes)
    return row


class WindowTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        env=patch.dict(os.environ,SOCIAL_MATERIAL_EFFECT_ROOT=str(self.root))
        env.start();self.addCleanup(env.stop)

    def publish(self, day, rows, source='facebook'):
        stamp=day+'T15:30:00+00:00'
        data.publish(self.root,source,[dict(r,metrics_at=r.get('metrics_at',stamp)) for r in rows],
                     dict(version=stamp,generated_at=stamp))

    def load(self, days, source='facebook'):
        values,_,_=selection.load('material',source,'en',['Dramawave'],dates=window.bounds(days,NOW))
        return {k[-1]:selection.evidence(v) for k,v in values.items()}

    def test_old_post_increments_and_income_are_windowed(self):
        self.publish('2026-11-19',[post(views=100,likes=20,comments=3,shares=1)])
        self.publish('2026-11-25',[post(views=160,likes=27,comments=5,shares=4)])
        income=dict(start='2026-10-01',end='2026-11-26',missing=[],daily={'2026-11-19':10000,'2026-11-20':200,'2026-11-26':400})
        self.publish('2026-11-26',[post(views=180,likes=30,comments=7,shares=6,revenue_cents=10600,_window_income=income)])
        self.assertEqual([self.load(1)['1'][k] for k in selection.METRICS],[20,3,2,2,400])
        self.assertEqual(self.load(7)['1']['views'],80)
        self.assertEqual(self.load(7)['1']['revenue_cents'],600)
        self.assertIsNone(self.load(30)['1']['views'])
        # Existing lifetime readers retain their old behavior.
        lifetime,_,_=selection.load('material','facebook','en',['Dramawave'])
        self.assertEqual(selection.evidence(next(iter(lifetime.values())))['views'],180)

    def test_new_post_zero_unknown_stale_reset_and_today(self):
        self.publish('2026-11-25',[post('1',views=100),post('2',views=200),post('5',views=30)])
        rows=[post('1',views=110),post('2',views=150),post('3',views=900),
              post('4',views=0,published_at='2026-11-26T00:00:00Z'),
              post('5',views=30,metrics_at='2026-11-25T12:00:00Z'),
              post('6',views=999,published_at='2026-11-26T16:00:00Z')]
        self.publish('2026-11-26',rows)
        values=self.load(1)
        self.assertEqual(values['1']['views'],10)
        self.assertIsNone(values['2']['views'])
        self.assertIsNone(values['3']['views'])
        self.assertEqual(values['4']['views'],0)
        self.assertIsNone(values['5']['views'])
        self.assertNotIn('6',values)
        self.publish('2026-11-27',rows)
        self.assertEqual(self.load(1)['1']['views'],10)

    def test_channel_sum_coverage_and_missing_income_day(self):
        for ch,n in [('facebook',100),('x',10)]:
            self.publish('2026-11-25',[post(views=n)],ch)
            self.publish('2026-11-26',[post(views=n+5,_window_income=dict(start='2026-11-01',end='2026-11-26',missing=['2026-11-26'],daily={}))],ch)
        result=self.load(1,'all')['1']
        self.assertEqual((result['views'],result['post_count'],result['coverage']['views']),(10,2,2))
        self.assertIsNone(result['revenue_cents'])

    def test_fb_filters_sort_and_audit_change_with_days(self):
        self.publish('2026-11-19',[post(views=100)])
        self.publish('2026-11-25',[post(views=160)])
        self.publish('2026-11-26',[post(views=180)])
        cfg=dict(metric_window_days=1,material_product='Dramawave',
                 drama_rule=dict(effect_channel='facebook',x_views_min=50,sort_by='x_views',sort_direction='desc'),
                 material_rule=dict(sort_by='roas',sort_direction='desc'))
        rules=selection.facebook_rules(cfg,'en',now=NOW)
        self.assertFalse(rules.matches('drama','old'))
        cfg['metric_window_days']=7
        rules=selection.facebook_rules(cfg,'en',now=NOW)
        self.assertTrue(rules.matches('drama','old'))
        self.assertEqual(rules.rank('drama','old'),(False,-80))
        self.assertEqual(rules.audit('old','1')['window']['start'],'2026-11-20')

    def test_per_metric_time_and_beijing_boundary(self):
        self.assertEqual(window.bounds(1,datetime(2026,11,26,16,tzinfo=timezone.utc)),('2026-11-26','2026-11-26'))
        self.assertEqual(window.bounds(1,datetime(2026,11,26,15,59,tzinfo=timezone.utc)),('2026-11-25','2026-11-25'))
        self.publish('2026-11-25',[post(views=100,likes=10)])
        self.publish('2026-11-26',[post(views=110,likes=10,_metric_times={'likes':'2026-11-25T12:00:00Z'})])
        row=self.load(1)['1']
        self.assertEqual(row['views'],10)
        self.assertIsNone(row['likes'])


if __name__=='__main__':
    unittest.main()
