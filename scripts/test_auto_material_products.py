"""Offline checks for saved scopes and actual multi-product selection queries."""
import argparse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.material_products import selected

class SharedTests(unittest.TestCase):
    def test_legacy_and_canonical_products(self):
        self.assertEqual(selected({}), ('Dramawave',))
        self.assertEqual(selected({'material_product':'LegacySource'}), ('LegacySource',))
        self.assertEqual(selected({'material_products':['dramawave','MoboReels','moboshort','DRAMAWAVE']}),
                         ('Dramawave','Moboreels','Moboshort'))
        for value in ([], '', None, [True], ['bad\nvalue']):
            with self.assertRaises(ValueError): selected({'material_products':value})

class XTests(unittest.TestCase):
    def test_saved_products_survive_reopen(self):
        import tempfile
        from features.x_auto_posts.core import XAutoPostStore
        from features.x_auto_posts.validation import normalize_template_payload
        from scripts.test_x_auto_post_validation import valid_payload
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture.sqlite3';store=XAutoPostStore(path)
            config=normalize_template_payload(valid_payload(material_products=['Moboreels','Moboshort']))
            item=store.create_template(name=config['name'],config=config)
            self.assertEqual(XAutoPostStore(path).get_template(item.id).config['material_products'],['Moboreels','Moboshort'])

    def test_validation_and_rules_roundtrip(self):
        from scripts.test_x_auto_post_validation import valid_payload
        from features.x_auto_posts.validation import normalize_template_payload, ValidationError
        from features.x_auto_posts.publisher import selector_rules
        from features.x_auto_posts.selector import SelectionRules
        config=normalize_template_payload(valid_payload(material_products=['moboreels','moboshort']))
        self.assertEqual(config['material_products'],['Moboreels','Moboshort'])
        rule=selector_rules(config)
        self.assertEqual(SelectionRules.from_mapping(rule.as_dict()).material_products,('Moboreels','Moboshort'))
        self.assertEqual(normalize_template_payload(valid_payload())['material_products'],['Dramawave'])
        with self.assertRaises(ValidationError): normalize_template_payload(valid_payload(material_products=[]))

    def test_query_and_hard_filter_use_same_product_scope(self):
        from features.x_auto_posts.repositories import ReadOnlyMySQLRepository
        repo=ReadOnlyMySQLRepository.__new__(ReadOnlyMySQLRepository)
        repo.schema='fixture';repo.material_data_source=6
        calls=[]
        repo._stream_select=lambda sql,params: calls.append((sql,params)) or []
        repo.list_material_rows(content_id='a',language='en',products=('Moboreels','Moboshort'))
        sql,params=calls[0]
        self.assertIn('s.product IN (%s,%s)',sql)
        self.assertEqual(sql.count('%s'),len(params))
        self.assertEqual(params[2:4],('Moboreels','Moboshort'))
        from scripts.test_x_auto_post_selector import FakeSource,FakeMetricStore,drama,material,selector,request,rules
        class Source(FakeSource):
            def list_material_rows(self, *, content_id, language, products=('Dramawave',)):
                self.products=products
                return self.materials[content_id]  # Deliberately return unselected products too.
        source=Source([replace(drama('C0'),catalog='moboreels',app_id=0,deploy_time=0)],{'C0':[material('1','C0'),material('2','C0',product='MoboShort'),material('3','C0',product='Moboreels')]})
        result=selector(source,FakeMetricStore()).preview(request(rules(material_products=['Moboreels','Moboshort'],drama={'resource_types':[]})))
        self.assertEqual(source.products,('Moboreels','Moboshort'))
        self.assertEqual(result.material.source.material_id,'2')
        source.dramas=[drama('C0')]
        self.assertEqual(selector(source,FakeMetricStore()).preview(request()).material.source.material_id,'1')

class FBTests(unittest.TestCase):
    def test_saved_products_survive_reopen(self):
        import tempfile
        from features.fb_auto_posts.core import FBAutoPostStore,ActorScope
        from scripts.test_fb_auto_validation import payload
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture.sqlite3';store=FBAutoPostStore(path)
            actor=ActorScope('1','fixture',True,'1')
            raw=payload();raw['material_products']=['Moboreels','Moboshort']
            item=store.create_template(raw,actor)
            self.assertEqual(FBAutoPostStore(path).get_template(item['id'],actor)['config']['material_products'],['Moboreels','Moboshort'])

    def config(self, **overrides):
        from scripts.test_fb_auto_validation import payload
        from features.fb_auto_posts.validation import normalize_template_payload
        raw=payload();raw.update(overrides)
        return dict(normalize_template_payload(raw),language='en',app_id='1479',product='Dramawave',metric_product='Dramawave')

    def test_validation_and_legacy(self):
        self.assertEqual(self.config()['material_products'],['Dramawave'])
        self.assertEqual(self.config(material_product='DramawaveSource')['material_products'],['DramawaveSource'])
        self.assertEqual(self.config(material_products=['moboreels','moboshort'])['material_products'],['Moboreels','Moboshort'])
        with self.assertRaises(ValueError): selected({'material_products':[]})

    def test_normal_and_indexed_queries(self):
        from features.fb_auto_posts.repositories import MaterialRepository
        from features.fb_auto_posts.metrics import MetricWindow
        for indexed in (False,True):
            calls=[]
            def select(sql,params):
                calls.append((sql,params))
                return [{'content_id':'a'}] if 'SELECT /*+ MAX_EXECUTION_TIME(45000) */ DISTINCT' in sql else []
            mysql=SimpleNamespace(schema='fixture',blacklist_schema='blacklist',select=select)
            metrics=SimpleNamespace(load_metric_window=lambda **kw: MetricWindow((),(),{},{}))
            repo=MaterialRepository(mysql,metrics)
            products=['Moboreels','Moboshort']+(['DramawaveSource'] if indexed else [])
            repo.candidate_snapshot(self.config(material_products=products))
            queries=[(s,p) for s,p in calls if 'ads_custom_source s' in s]
            self.assertEqual(len(queries),1)
            sql,params=queries[0]
            self.assertIn('s.product IN ('+','.join('%s' for _ in products)+')',sql)
            self.assertEqual(sql.count('%s'),len(params))
            for p in products:self.assertIn(p,params)
            self.assertEqual('FORCE INDEX(idx_source_type_source_id)' in sql,indexed)

    def test_moboreels_queries_and_frozen_legacy_scope(self):
        from unittest.mock import patch
        from features.fb_auto_posts.moboreels import candidate_snapshot
        from features.fb_auto_posts.repositories import MaterialRepository
        calls=[]
        repo=MaterialRepository(SimpleNamespace(schema='fixture',select=lambda s,p: calls.append((s,p)) or []))
        config=self.config(material_products=['Moboreels','Moboshort'],message_template='{{m_url}}')
        with patch('features.fb_auto_posts.moboreels.moboreels.institution_id',return_value=1):
            candidate_snapshot(repo,config)
            sql,params=calls[-1]
            self.assertIn('s.product IN (%s,%s)',sql)
            self.assertEqual(sql.count('%s'),len(params))
            self.assertEqual(params[2:4],('Moboreels','Moboshort'))
            config.pop('material_products');candidate_snapshot(repo,config)
            self.assertNotIn('s.product IN',calls[-1][0])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--platform',choices=['x','fb'],required=True);args=p.parse_args()
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (SharedTests,XTests if args.platform=='x' else FBTests))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
