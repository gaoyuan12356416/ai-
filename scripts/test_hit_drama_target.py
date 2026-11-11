import unittest
from features.hit_drama_target import identifier, resolve_target, content_ids


class TargetTests(unittest.TestCase):
    def test_business_resource_returns_all_languages_with_product_and_index(self):
        calls=[]
        def query(sql,args):
            calls.append((sql,args))
            return [{'content_id':'EnAb9','language':'en'},{'content_id':'IdXy3','language':'id'},
                    {'content_id':'EnAb9','language':'en'},{'content_id':'JpZ1','language':'jp'}]
        target=resolve_target(query,'ads',1479,dict(id_type='resource_id',drama_id='30911'))
        self.assertEqual(target['by_language'],{'en':['EnAb9'],'id':['IdXy3'],'ja':['JpZ1']})
        self.assertEqual(content_ids(target,'id'),['IdXy3']);self.assertEqual(content_ids(target,'th'),[])
        self.assertIn('FORCE INDEX(scoo)',calls[0][0]);self.assertIn('series_code=%s',calls[0][0]);self.assertEqual(calls[0][1],(1479,'30911','30911'))

    def test_alphanumeric_drama_preserves_case_and_does_not_expand_to_series(self):
        self.assertEqual(identifier('content_id',' kOfK3rsZLN '),'kOfK3rsZLN')
        calls=[]
        def query(sql,args):calls.append(sql);return [{'content_id':'kOfK3rsZLN','language':'en'},{'content_id':'kofk3rszln','language':'en'}]
        target=resolve_target(query,'ads',1479,dict(id_type='content_id',drama_id='kOfK3rsZLN'))
        self.assertEqual(target['content_ids'],['kOfK3rsZLN']);self.assertIn('BINARY content_id',calls[0])

    def test_invalid_missing_and_incomplete_targets_never_become_unrestricted(self):
        for value in ('30911 OR 1=1',True,'-1','abc'):
            with self.assertRaises(ValueError):identifier('resource_id',value)
        for rows in ([],[{'content_id':'abc','language':''}],[{'content_id':'abc','language':'en'}]*1001):
            with self.assertRaises(ValueError):resolve_target(lambda *a:rows,'ads',1479,dict(id_type='resource_id',drama_id='30911'))

    def test_legacy_frozen_targets_remain_readable(self):
        self.assertEqual(content_ids({'content_id':'old'}),['old'])
        self.assertEqual(content_ids({'content_id':'old','languages':['en']},'id'),[])


if __name__=='__main__':unittest.main()
