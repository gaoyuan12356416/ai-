from contextlib import closing
from decimal import Decimal
from datetime import datetime,timezone
import os
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.social_material_effects import data, facebook, report, selection
from features.social_material_effects.graph import extract
from features.fb_auto_posts.validation import normalize_template_payload, ValidationError
from scripts.test_fb_auto_validation import payload


class ChannelEffectsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ,SOCIAL_MATERIAL_EFFECT_ROOT=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.admin = {'role':'admin'}
        now = data.utc()
        meta = dict(version='fixture',generated_at=now,revenue_source_generated_at=now,revenue_through='2026-11-25')
        post = dict(post_id='1',material_id='7',namespace='ads_custom_source',material_name='M',catalog='dramawave',
                    content_id='D1',drama_name='D',product='Dramawave',language='en',account_id='2',
                    published_at='2026-11-19T16:00:00Z',metrics_at='2026-11-25T15:00:00Z',
                    _window_income=dict(start='2026-11-19',end='2026-11-25',missing=[],daily={'2026-11-25':50}),
                    views=0,revenue_cents=50,owner='u',tenant='t')
        data.publish(self.root,'x',[dict(post,account_ids=['2','4'],views=3)],meta)
        data.publish(self.root,'facebook',[dict(post,views=110),dict(post,post_id='3',views=None,owner='v'),
                     dict(post,post_id='2',material_id='8',content_id='D2',views=1),
                     dict(post,post_id='4',material_id='11',product='Other',views=1000)],meta)

    def test_report_separates_same_material_and_post_ids(self):
        result = report.query({'q':['7']},self.admin,self.root)
        self.assertEqual((result['post_count'],result['total'],result['summary']['views']),(3,2,113))
        fb,x = result['items']
        self.assertEqual((fb['channel'],fb['views'],fb['known']['views'],fb['coverage']['views']),('facebook',None,110,1))
        self.assertEqual((x['channel'],x['account_count']),('x',2))
        self.assertEqual(report.query({'channel':['x'],'account_id':['4']},self.admin,self.root)['post_count'],1)
        self.assertEqual(report.query({'channel':['facebook'],'q':['7'],'end':['2026-11-19']},self.admin,self.root)['total'],0)

    def test_facebook_details_owner_tenant_paging(self):
        params={'view':['posts'],'channel':['facebook'],'material_id':['7'],'page_size':['1']}
        user={'role':'user','user_id':'u','tenant_key':'t'}
        rows=report.query(params,user,self.root)
        self.assertEqual(rows['total'],1)
        self.assertNotIn('owner',rows['items'][0])
        self.assertEqual(report.query(params,dict(user,tenant_key='other'),self.root)['total'],0)
        self.assertEqual(report.query(dict(params,page=['2']),self.admin,self.root)['items'][0]['post_id'],'3')

    def test_filters_and_unknown_upper_bounds(self):
        cfg=payload()
        cfg['drama_rule'].update(effect_channel='x',x_views_min='3')
        cfg['material_rule'].update(effect_channel='all',x_views_min='113',sort_by='x_revenue')
        config=normalize_template_payload(cfg)
        rules=selection.facebook_rules(config,'en',now=datetime(2026,11,26,tzinfo=timezone.utc))
        self.assertTrue(rules.matches('drama','D1'))
        self.assertFalse(rules.matches('drama','D2'))
        self.assertTrue(rules.matches('material','D1','7'))
        self.assertEqual(rules.rank('material','D1','7'),(False,-150))
        self.assertFalse(rules.matches('material','D2','7'))
        self.assertEqual(rules.audit('D1','7')['material']['facts']['coverage']['views'],2)
        cfg['material_rule']['x_views_max']='120'
        self.assertFalse(selection.facebook_rules(normalize_template_payload(cfg),'en',now=datetime(2026,11,26,tzinfo=timezone.utc)).matches('material','D1','7'))
        cfg['material_rule']['effect_channel']='tiktok'
        with self.assertRaises(ValidationError):
            normalize_template_payload(cfg)

    def test_x_runtime_accepts_independent_sources_and_freezes_them(self):
        from features.x_auto_posts.selector import DramaFilterRule,MaterialFilterRule,SortRule,DecimalRange
        from features.x_material_effects import selection as xs
        rules=SimpleNamespace(drama=DramaFilterRule(effect_channel='facebook',sort=SortRule('x_views','desc')),
              material=MaterialFilterRule(effect_channel='all',x_effect={'revenue':DecimalRange(Decimal('1.50'))}),
              material_products=('Dramawave',))
        drama=SimpleNamespace(catalog='dramawave',content_id='D1',language='en')
        material=SimpleNamespace(source=SimpleNamespace(product='Dramawave',content_id='D1',material_id='7'))
        with xs.scope(SimpleNamespace(rules=rules,language='en')):
            self.assertEqual(xs.apply([material],rules.material,'material',{}),[material])
            evidence=xs.audit(drama,material)
            self.assertEqual(evidence['channels'],{'drama':'facebook','material':'all'})
            self.assertEqual(evidence['drama']['known']['views'],110)
            self.assertEqual(evidence['material']['revenue_cents'],150)

    def test_facebook_shared_catalog_filters_before_limit(self):
        from datetime import datetime,timezone
        from features.fb_auto_posts import catalog_plan
        from features.fb_auto_posts.metrics import MetricWindow
        cfg = normalize_template_payload(payload())
        cfg.update(metric_product='Dramawave', metric_platform=0, _candidate_limit=1)
        cfg['drama_rule'].update(effect_channel='facebook',x_views_min='100',sort_by='x_views')
        cfg['material_rule'].update(effect_channel='all',x_views_min='110',sort_by='x_views')
        window=MetricWindow((3,),('2026-11-25',),{}, {})
        repo=SimpleNamespace(now_fn=lambda:datetime(2026,11,26,tzinfo=timezone.utc),candidate_limit=1,
            metric_store=SimpleNamespace(load_metric_window=lambda **kw:window))
        facts={cid:dict(name=cid,resource_type_v2='2',deploy_time=1,language='en') for cid in ('D1','D2')}
        def hydrate(_,ids):
            return {mid:(dict(cid='D1',lang='en',product='Dramawave',name='name',tag_name='',video_duration=10),
                         'https://example.org/video') for mid in ids}
        with patch.object(catalog_plan,'BrokerReadOnlyMySQL',return_value=SimpleNamespace(schema='s',blacklist_schema='b')), \
             patch('features.fb_auto_posts.planning_catalog.open_for_plan',return_value=None), \
             patch.object(catalog_plan.Catalog,'index',return_value={('D2','en'):[8],('D1','en'):[6,7]}), \
             patch.object(catalog_plan.Catalog,'drama_facts',return_value=(facts,{}, {},'')), \
             patch.object(catalog_plan.Catalog,'hydrate',hydrate):
            chosen=catalog_plan.snapshots(repo,cfg,{('en',False)})['en',False].candidates
        self.assertEqual([m.material_id for m in chosen],['7'])
        self.assertEqual(chosen[0].social_effect['material']['channel'],'all')

    def test_one_missing_channel_leaves_other_report_available(self):
        (self.root/'facebook.sqlite3').unlink()
        result=report.query({},self.admin,self.root)
        self.assertEqual(result['meta']['unavailable_channels'],['facebook'])
        self.assertEqual(result['post_count'],1)
        with self.assertRaises(OSError):
            report.query({'channel':['facebook']},self.admin,self.root)

    def test_native_identity_and_observed_zero(self):
        self.assertEqual(facebook.identity('12','13'),facebook.identity('12','12_13'))
        self.assertIsNone(facebook.identity('12','14_13'))
        self.assertIsNone(facebook.identity('12','12_x_13'))
        self.assertEqual(extract({'reactions':{'summary':{'total_count':0}},'shares':{}}),{'likes':0})

    def test_exact_fb_revenue_with_different_site_start_dates(self):
        sites = (('2049','2026-11-19','7','101'),('2310','2026-11-20','8','102'))
        posts = {}
        warehouse = self.root/'warehouse.sqlite3'
        with closing(sqlite3.connect(warehouse)) as db:
            db.executescript('CREATE TABLE catalog(payload);CREATE TABLE effects(platform,dt,payload)')
            for site,day,material,post in sites:
                row=dict(platform='facebook',site_id=site,key=[post,'c'],date=day,owner='u',tenant='t',
                         drama='D',language='en',channel='facebook:2',link_type='auto_template' if site=='2049' else 'manual',
                         effect_source=dict(ad_id=material,adset_id='2'),
                         publications=[dict(id='facebook:post:2:'+post,date=day,kind='post')])
                posts['2_'+post]=dict(content_id='D',material_id=material,account_id='2',published_at=day,drama_name='',link_site=site)
                db.execute('INSERT INTO catalog VALUES(?)',(json.dumps(row),))
                for mid in [material,'999']:
                    fact=dict(site_id=site,date=day,campaign_id=post,revenue_cents=120,refund_cents=20,ad_id=mid,adset_id='2')
                    db.execute('INSERT INTO effects VALUES(?,?,?)',('facebook',day,json.dumps(fact)))
            db.commit()
        meta=dict(start='2026-11-17',end='2026-11-20',version='fixture',generated_at=data.utc(),
                  coverage={'facebook:2049':['2026-11-19','2026-11-20'],'facebook:2310':['2026-11-20']})
        reader=SimpleNamespace(current=lambda root:(meta,warehouse),connect=sqlite3.connect)
        with patch.dict(sys.modules,{'features.social_analytics.warehouse_reader':reader}):
            result=facebook.revenue(posts)
        self.assertEqual(result['assigned_revenue_cents'],240)
        self.assertEqual(result['unassigned_revenue_cents'],240)
        self.assertEqual(result['revenue_incomplete_sites'],[])
        self.assertEqual(posts['2_101']['_window_income']['daily'],{'2026-11-19':120})
        self.assertEqual(posts['2_102']['_window_income']['daily'],{'2026-11-20':120})


if __name__=='__main__':
    unittest.main()
