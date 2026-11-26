from contextlib import closing
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
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
                    published_at='2026-11-19T16:00:00Z',views=0,revenue_cents=50,owner='u',tenant='t')
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
        rules=selection.facebook_rules(config,'en')
        self.assertTrue(rules.matches('drama','D1'))
        self.assertFalse(rules.matches('drama','D2'))
        self.assertTrue(rules.matches('material','D1','7'))
        self.assertEqual(rules.rank('material','D1','7'),(False,-150))
        self.assertFalse(rules.matches('material','D2','7'))
        self.assertEqual(rules.audit('D1','7')['material']['facts']['coverage']['views'],2)
        cfg['material_rule']['x_views_max']='120'
        self.assertFalse(selection.facebook_rules(normalize_template_payload(cfg),'en').matches('material','D1','7'))
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

    def test_native_identity_and_observed_zero(self):
        self.assertEqual(facebook.identity('12','13'),facebook.identity('12','12_13'))
        self.assertIsNone(facebook.identity('12','14_13'))
        self.assertIsNone(facebook.identity('12','12_x_13'))
        self.assertEqual(extract({'reactions':{'summary':{'total_count':0}},'shares':{}}),{'likes':0})


if __name__=='__main__':
    unittest.main()
