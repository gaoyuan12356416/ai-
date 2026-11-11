import copy
import hashlib
import tempfile
import unittest
from features.fb_page_portrait.build import build
from features.fb_page_portrait.runtime import encoded
from features.fb_page_portrait.storage import publish,Store,projections
from features.fb_page_portrait.server import select,PREFIX


def fixture():
    return dict(page_ids=['11','22','33'],start='2026-11-04',end='2026-11-10',social_version='fixture',
                resources=[dict(content_id='A',language='en',app_id=1479,series_code='A',labels=['female','ceo','sweet','sweet_love'])],
                infos=[dict(content_id='A',language='en',app_id=1479,series_code='A',resource_type=1,name='A title')],
                paid=[dict(page_id='11',date='2026-11-04',content_id='A',language='en',revenue='0.05',spend='1.00')],
                social=[dict(page_id='11',date='2026-11-10',content_id='A',language='en',revenue_cents=6,posts=['post1']),
                        dict(page_id='22',date='2026-11-10',content_id='B',language='en',revenue_cents=7,posts=['post2'])])


class PortraitTests(unittest.TestCase):
    def test_income_conservation_and_distinct_theme_allocation(self):
        data=build(fixture());p=data['pages']['11']
        self.assertEqual((p['paid_cents'],p['social_cents'],p['revenue_cents']),(5,6,11))
        nodes=p['audiences'][0]['children']
        self.assertEqual(len(nodes),2) # sweet and sweet_love map to ONE theme
        self.assertEqual(sum(n['revenue_cents'] for n in nodes),11)
        self.assertEqual(sum(n['share_bp'] for n in nodes),10000)
        self.assertEqual(p['focus']['audience']['key'],'female')
        for n in nodes:self.assertEqual(sum(d['allocated_cents'] for d in n['dramas']),n['revenue_cents'])

    def test_social_only_unknown_and_zero_income(self):
        data=build(fixture());p=data['pages']['22']
        self.assertEqual(p['status'],'ready');self.assertEqual(p['spend_cents'],0)
        self.assertEqual(p['focus']['audience']['key'],'__unknown__')
        self.assertEqual(p['audiences'][0]['children'][0]['key'],'__unknown__')
        self.assertEqual(data['pages']['33']['status'],'no_income')

    def test_no_old_data_or_out_of_scope(self):
        for field,value in [('date','2026-11-03'),('page_id','999')]:
            raw=fixture();raw['paid'][0][field]=value
            with self.assertRaises(AssertionError):build(raw)

    def test_signed_adjustment_is_not_positive_percentage(self):
        raw=fixture();raw['social'][1]['revenue_cents']=-7
        p=build(raw)['pages']['22']
        self.assertEqual(p['status'],'signed_adjustment')
        self.assertIsNone(p['audiences'][0]['share_bp'])

    def test_language_exact_match(self):
        raw=fixture();raw['paid'][0]['language']='ja';raw['social']=[]
        self.assertEqual(build(raw)['pages']['11']['focus']['audience']['key'],'__unknown__')

    def test_serving_acl_projection_and_version_pagination(self):
        data=build(fixture());hashes={pid:hashlib.sha256(encoded(p).encode()).hexdigest() for pid,p in data['pages'].items()}
        with tempfile.TemporaryDirectory() as root:
            publish(data,dict(version=data['meta']['version'],verified_rows=3,payload_hashes=hashes),root)
            store=Store(root)
            status,result=select(PREFIX,{'ids':['11,22']},{'11','22'},store)
            self.assertEqual(status,200);self.assertNotIn('children',result['items']['11']['audiences'][0])
            self.assertNotIn('audit',result['meta'])
            self.assertEqual(select(PREFIX,{'ids':['11,33']},{'11','22'},store)[0],404)
            self.assertEqual(select(PREFIX+'/33',{}, {'11'},store)[0],404)
            p=data['pages']['11'];a=p['audiences'][0];t=a['children'][0]
            status,details=select(PREFIX+'/11/dramas',{'path':[a['key']+'/'+t['key']], 'version':[data['meta']['version']],'page':['1']},{'11'},store)
            self.assertEqual(status,200);self.assertEqual(details['total'],1)
            self.assertEqual(details['items'][0]['paid_cents'],5)
            with self.assertRaises(ValueError):select(PREFIX,{'ids':[','.join(str(i) for i in range(1,22))]},set(),store)


if __name__=='__main__':unittest.main()
