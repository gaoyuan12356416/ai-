import copy
import unittest
from features.fb_page_profile.build import build

def fixture():
    ids=[str(i) for i in range(1,237)]
    raw=dict(scope=ids,start='2026-11-03',end='2026-11-09',history_first='2024-10-01',
      membership=[dict(page_id=i,language='en',deleted_at=None) for i in ids],
      pages=[dict(page_id=i,page_name='Page '+i,status=0,forbidden_status=0,is_permission=1,fan_ccount=0) for i in ids],
      windows={i:dict(start='2026-11-03',end='2026-11-09',kind='none') for i in ids},
      resources=[dict(content_id='d1',app_id=1479,language='en',series_code='1',labels='Romance,Revenge,Romance')],
      drama_info=[dict(content_id='d1',app_id=1479,language='en',series_code='1',name='Drama',resource_type=1)],
      effects=[dict(page_id='1',dt='2026-11-04',series_code='1',data_source_id='d1',language='en',spend='0.03',af_revenue0='0.01',af_revenue='0.02',revenue='0.01')])
    raw['windows']['1']=dict(start='2026-11-03',end='2026-11-09',kind='recent')
    return raw

class ProfileTests(unittest.TestCase):
    def test_equal_distinct_theme_allocation_and_money_conservation(self):
        r=build(fixture());p=next(p for p in r['pages'] if p['page_id']=='1')
        self.assertEqual(len(p['combinations']),2)
        for key,total in [('spend',3),('af_revenue0',1),('af_revenue',2),('revenue',1)]:
            self.assertEqual(sum(x[key] for x in p['combinations']),total)
            self.assertEqual(sum(x[key] for x in p['tags']),total)
        self.assertEqual(p['aligned_spend'],3)
    def test_empty_backup_does_not_fabricate_portrait(self):
        raw=fixture();raw['membership'][1]['language']='';r=build(raw)
        p=next(p for p in r['pages'] if p['page_id']=='2')
        self.assertTrue(p['backup']);self.assertEqual(p['combinations'],[]);self.assertEqual(p['evidence'],'none')
    def test_outside_page_window_excluded(self):
        raw=fixture();raw['effects'].append(dict(raw['effects'][0],dt='2026-11-02',spend='999'))
        self.assertEqual(build(raw)['meta']['audit']['spend'],3)
    def test_scope_and_membership_are_exact(self):
        raw=fixture();raw['effects'][0]['page_id']='999'
        with self.assertRaises(AssertionError):build(raw)
        raw=fixture();raw['membership'].pop()
        with self.assertRaises(AssertionError):build(raw)
    def test_no_matching_metadata_remains_unknown(self):
        raw=fixture();raw['resources']=[];raw['drama_info']=[];p=build(raw)['pages'][0]
        self.assertEqual(p['known_spend'],0);self.assertEqual(p['combinations'][0]['theme'],'__unknown__')
    def test_cross_language_history_flag_basis(self):
        raw=fixture();raw['membership'][0]['language']='fr';p=build(raw)['pages'][0]
        self.assertEqual(p['aligned_spend'],0);self.assertEqual(p['language'],'fr')
    def test_conditional_percentages_sum_to_100(self):
        p=build(fixture())['pages'][0]
        self.assertEqual(sum(a['share_bp'] for a in p['audiences']),10000)
        for a in p['audiences']:
            if a['revenue_cents']>0:self.assertEqual(sum(t['share_bp'] for t in a['children']),10000)
    def test_historical_windows_are_rejected(self):
        raw=fixture();raw['windows']['1']['start']='2026-10-20'
        with self.assertRaises(AssertionError):build(raw)
    def test_zero_spend_attributed_income_is_retained_without_portrait(self):
        raw=fixture();raw['effects'][0]['spend']='0';p=build(raw)['pages'][0]
        self.assertEqual(p['af_revenue0'],1);self.assertFalse(p['profile_eligible']);self.assertIsNone(p['focus'])

if __name__=='__main__':unittest.main()
