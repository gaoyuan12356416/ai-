import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import meta_asset_permission_sync as m
import meta_asset_permission_followup as f


class BasicAdsTests(unittest.TestCase):
    def row(self, kind='page'):
        return {'kind':kind, 'asset_id':'123', 'business_id':'bm', 'permission_profile':'basic_ads',
                'source_live':{'is_published':True,'business':{'id':'other'},'account_status':1},
                'backend':({'status':0,'forbidden_status':0} if kind=='page' else {'account_status':1,'is_inactive':0})}

    def test_basic_does_not_require_management_or_draft(self):
        for kind in ('account','page'):
            self.assertTrue(m.meets_requirements(kind, ['ADVERTISE','ANALYZE'], 'basic_ads'))
            self.assertFalse(m.meets_requirements(kind, ['ANALYZE'], 'basic_ads'))
            self.assertFalse(m.meets_requirements(kind, ['ADVERTISE'], 'basic_ads'))

    def test_page_models_and_preservation(self):
        self.assertTrue(m.meets_requirements('page',['PROFILE_PLUS_FULL_CONTROL'],'basic_ads'))
        self.assertTrue(m.meets_requirements('page',['PROFILE_PLUS_ADVERTISE','PROFILE_PLUS_ANALYZE'],'basic_ads'))
        self.assertEqual(m.required_tasks('page', [], 'basic_ads', m.PAGE_PROFILE_TASKS), {'PROFILE_PLUS_ADVERTISE','PROFILE_PLUS_ANALYZE'})
        source={'tasks':['ADVERTISE','ANALYZE']}
        before={'tasks':['CREATE_CONTENT']}
        r=f.classify(self.row(),source,source,before,before,True,True,{'permitted_tasks':['ADVERTISE','ANALYZE']},False)
        self.assertEqual(r['desired_tasks'],['ADVERTISE','ANALYZE','CREATE_CONTENT'])
        self.assertEqual(r['category'],'insufficient')

    def test_existing_basic_access_excluded_without_delegation(self):
        value={'tasks':['ADVERTISE','ANALYZE']}
        r=f.classify(self.row(),value,None,value,None,True,True,None,False)
        self.assertEqual(r['category'],'already_sufficient')

    def test_shared_account_basic_tasks_suffice(self):
        value={'tasks':['ADVERTISE','ANALYZE','DRAFT']}
        r=f.classify(self.row('account'),value,value,None,None,True,False,{'permitted_tasks':value['tasks']},False)
        self.assertEqual(r['category'],'missing')
        self.assertTrue(r['delegable'])
        self.assertEqual(r['desired_tasks'],['ADVERTISE','ANALYZE'])

    def test_share_below_requested_tasks_stays_blocked(self):
        value={'tasks':['ADVERTISE','ANALYZE']}
        r=f.classify(self.row(),value,value,None,None,True,False,{'permitted_tasks':['ANALYZE']},False)
        self.assertEqual(r['category'],'not_delegable')

    def test_unknown_read_and_abnormal_never_granted(self):
        value={'tasks':['ADVERTISE','ANALYZE']}
        r=f.classify(self.row(),value,value,value,value,True,False,None,False)
        self.assertEqual(r['category'],'unknown')
        row=self.row();row['backend']['status']=1
        r=f.classify(row,value,value,None,None,True,False,{'permitted_tasks':value['tasks']},False)
        self.assertEqual(r['category'],'abnormal')

    def test_csv_exact_scope_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'scope.csv'
            p.write_text('类型,资产ID,名称\naccount,act_123,A\npage,456,P\n',encoding='utf-8-sig')
            scope, proof=f.load_scope_csv(p)
            self.assertEqual(set(scope['account']),{'123'})
            self.assertEqual(proof['counts'],{'account':1,'page':1})
            p.write_text('类型,资产ID,名称\npage,123,P\npage,123,P\n',encoding='utf-8-sig')
            with self.assertRaises(ValueError): f.load_scope_csv(p)

    def test_missing_target_uses_complete_own_edge_and_expiring_cache(self):
        calls=[]
        g=SimpleNamespace(rows=lambda *a: calls.append(a) or {'act_2':{'id':'act_2','tasks':['ANALYZE']}})
        target={'id':1,'user_id':916,'accessToken':'fake'}
        asset={'kind':'account','asset_id':'123','permission_profile':'basic_ads'}
        with patch.object(m.time,'monotonic',return_value=100):
            self.assertIsNone(m.read_target_before(g,target,asset,'target','bm',None))
            self.assertIsNone(m.read_target_before(g,target,asset,'target','bm',None))
        self.assertEqual(len(calls),1)
        with patch.object(m.time,'monotonic',return_value=161):
            m.read_target_before(g,target,asset,'target','bm',None)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][0],'target/assigned_ad_accounts')

    def test_missing_target_read_failure_is_not_absence(self):
        def fail(*args): raise m.GraphError({'http':400,'error':{'code':190}})
        g=SimpleNamespace(rows=fail)
        with self.assertRaises(m.GraphError):
            m.read_target_before(g,{'id':1,'user_id':916,'accessToken':'fake'},
                {'kind':'account','asset_id':'123','permission_profile':'basic_ads'},'target','bm',None)


if __name__=='__main__': unittest.main()
