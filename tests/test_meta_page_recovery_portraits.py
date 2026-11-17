"""Product identity and source partition regressions for the extended collector."""
import unittest
from unittest.mock import patch

from features.fb_page_portrait.classification import classifications
from features.fb_page_portrait.collect import page_scope,series_ranges,paid,catalog
from features.meta_page_recovery.sources import Sources


class PortraitRecoveryTests(unittest.TestCase):
    def test_catalog_timeout_splits_ids_without_losing_or_duplicating_records(self):
        class Reader:
            def __init__(self):self.sizes=[]
            def query(self,statements,timeout=60):
                import re
                sql=statements[0]
                values=re.search(r'content_id IN \(([^)]+)\)',sql).group(1).split(',')
                self.sizes.append(len(values))
                if len(values)>10:raise RuntimeError('source_query_failed_3024')
                return [{'content_id':bytes.fromhex(value[2:]).decode(),'app_id':1479} for value in values]
        reader=Reader();ids=[str(i) for i in range(32)]
        rows=catalog(reader,'ads_drama_resource','content_id',['content_id','app_id'],1479,ids)
        self.assertEqual({row['content_id'] for row in rows},set(ids));self.assertEqual(len(rows),32)
        self.assertEqual(max(reader.sizes),25)

    def test_retired_catalog_keeps_verified_material_language(self):
        class Reader:
            def __init__(self):self.calls=0
            def query(self,statements):
                self.calls+=1
                if self.calls==1:
                    self.sql=statements[0]
                    if 'WHERE s.id=123 LIMIT 2' not in self.sql:raise AssertionError('source_numeric_identity_was_reinterpreted')
                return [{'product':'Dramawave','content_id':'old','language':'fr','data_source':6}] if self.calls==1 else []
        with patch('features.fb_page_portrait.classification.classifications',return_value={}):
            value=Sources({},reader=Reader()).target({'source_id':'123','group_id':57},{'language':''})
        self.assertEqual(value['language'],'fr');self.assertEqual(value['source_app_id'],1479)
    def test_three_products_do_not_share_content_identity(self):
        rows=[dict(app_id=app,content_id='same',language='en',series_code=str(app),labels=[label])
              for app,label in [(1479,'ceo'),(979,'revenge'),(3302,'sweet_love')]]
        infos=[dict(app_id=app,content_id='same',language='en',series_code=str(app),resource_type=1) for app in (1479,979,3302)]
        with patch('features.x_tag_revenue.taxonomy.audience_records',return_value={}):
            result=classifications(rows,infos)
        self.assertEqual(set(result),{(1479,'same','en'),(979,'same','en'),(3302,'same','en')})
        self.assertEqual(result[(979,'same','en')]['themes'],['revenge'])
        self.assertEqual(result[(3302,'same','en')]['themes'],['sweet_romance'])
        self.assertEqual(result[(3302,'same','en')]['audience'],'__unknown__')

    def test_scope_is_union_not_managed_only(self):
        class Reader:
            def query(self,statements):return [{'page_id':'2'},{'page_id':'3'}]
        with patch('features.fb_page_portrait.collect.configured',return_value=['1','2']):
            self.assertEqual(page_scope(Reader()),['1','2','3'])

    def test_query_partition_budget_is_smaller(self):
        rows=[{'series_code':str(i),'rows':2000} for i in range(6)]
        self.assertGreater(len(series_ranges(rows)),len(series_ranges(rows,target=25000)))

    def test_timeout_splits_exact_page_sets_and_includes_null_series(self):
        class Reader:
            def __init__(self):self.queries=[];self.failed=False
            def query(self,statements,timeout=45):
                sql=statements[0];self.queries.append(sql)
                if "'rows',COUNT(*)" in sql:return [{'series_code':None,'rows':7000},{'series_code':'b','rows':1}]
                if 'page_id IN (0x31,0x32)' in sql and not self.failed:
                    self.failed=True;raise RuntimeError('source_query_failed_3024')
                return []
        reader=Reader();paid(reader,['1','2'],'2026-11-10','2026-11-16')
        self.assertTrue(reader.failed)
        self.assertTrue(any('series_code IS NULL' in q for q in reader.queries))
        self.assertTrue(any('page_id IN (0x31)' in q for q in reader.queries))
        self.assertTrue(any('page_id IN (0x32)' in q for q in reader.queries))


if __name__=='__main__':unittest.main()
