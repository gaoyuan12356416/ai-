import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from features.drama_audience import store
from features.drama_audience.client import resolve
from features.drama_audience.sync import fill, discover
from features.drama_pay_rank import core, integration
from features.x_tag_revenue.taxonomy import classify, classification_index
from features.x_tag_revenue.hierarchy import build_tree


def response(cid='AbC', audience=None, lang='en', app='com.dramawave.app'):
    return {'code':200,'data':{'list':[{'id':cid,'app_id':app,'language':[lang],
           'audience_labels':[] if audience is None else audience,'name':'A <drama>'}]}}


class AudienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=str(Path(self.tmp.name)/'catalog.sqlite3')
        store.initialize(self.db)
        self.env=patch.dict(os.environ,DRAMA_AUDIENCE_DB=self.db);self.env.start()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_all_seven(self):
        for label,key in store.BY_NAME.items():
            row=resolve('AbC',response(audience=[label]));self.assertEqual(row['audience'],key)
            self.assertEqual(classify(['War God','sweet'],audience_record=row,expected_language='en')['audience'],key)
    def test_never_infer(self):
        self.assertEqual(classify(['BL','女性','War God'])['audience'],store.UNKNOWN)
    def test_empty_is_not_general(self):
        self.assertEqual(resolve('AbC',response())['audience'],store.UNKNOWN)
        self.assertEqual(resolve('AbC',response(audience=['通用']))['audience'],'general')
    def test_exact_id_and_product(self):
        for d in (response(cid='abc'),response(app='other.app')):
            with self.assertRaises(ValueError):resolve('AbC',d)
        d=response(audience=['女性']);d['data']['list'].append(response(audience=['男性'],app='other.app')['data']['list'][0])
        self.assertEqual(resolve('AbC',d)['audience'],'female')
    def test_conflict_and_unsupported(self):
        self.assertEqual(resolve('AbC',response(audience=['女性','男性']))['status'],'conflict')
        self.assertEqual(resolve('AbC',response(audience=['未知类型']))['status'],'unsupported')
    def test_missing_field_business_error(self):
        d=response();del d['data']['list'][0]['audience_labels']
        with self.assertRaises(ValueError):resolve('AbC',d)
        d['code']=401
        with self.assertRaises(ValueError):resolve('AbC',d)
    def test_language_guard(self):
        r=resolve('AbC',response(audience=['女性'],lang='pt'))
        self.assertEqual(store.direction(r,'pt-br')['key'],'female')
        self.assertEqual(store.direction(r,'en')['status'],'language_mismatch')
    def test_known_rows_reused_and_manual_replace(self):
        store.save(resolve('AbC',response(audience=['女性'])),self.db)
        store.enqueue(['AbC','New'],self.db)
        self.assertEqual(store.counts(self.db)['pending'],1)
        store.save(resolve('AbC',response(audience=['男性'])),self.db)
        self.assertEqual(store.records(['AbC'],self.db)['AbC']['audience'],'female')
        store.save(resolve('AbC',response(audience=['男性'])),self.db,replace=True)
        self.assertEqual(store.records(['AbC'],self.db)['AbC']['audience'],'male')
    def test_failed_query_remains_pending(self):
        store.enqueue(['AbC'],self.db)
        def fail(_):raise ValueError('exact_drama_not_found')
        r=fill(self.db,fetcher=fail);self.assertEqual((r['stored'],r['pending']),(0,1))
        r=fill(self.db,fetcher=lambda cid:resolve(cid,response(cid=cid)))
        self.assertEqual((r['stored'],r['pending']),(1,0))
    def test_discovery_cursor_and_dedup(self):
        class Reader:
            arrived=False
            def query(self,queries,timeout):
                q=queries[0]
                if 'DESC LIMIT 1' in q and 'id<' not in q:return [{'id':10}]
                if 'id<11 ' in q:return [{'id':10,'content_id':'AbC'}]
                if self.arrived and 'id>10 ' in q:return [{'id':11,'content_id':'New'}]
                return []
        reader=Reader()
        self.assertEqual(discover(self.db,reader),1)
        self.assertEqual(discover(self.db,reader),0)
        reader.arrived=True
        self.assertEqual(discover(self.db,reader),1)
        self.assertEqual(store.counts(self.db)['pending'],2)
        with closing(store.connect(self.db)) as db:
            self.assertEqual(dict(db.execute('SELECT content_id,priority FROM pending')),{'AbC':0,'New':10})
    def test_income_conservation_and_theme_preserved(self):
        store.save(resolve('AbC',response(audience=['合家欢'])),self.db)
        resources=[dict(app_id=1479,content_id='AbC',language='en',series_code='s',labels='sweet,revenge')]
        index=classification_index(resources,[])
        self.assertEqual(index[('AbC','en')]['audience'],'family_all')
        dramas={('AbC','en'):dict(content_id='AbC',language='en',name='n',revenue_cents=101,posts={'p'})}
        tree=build_tree(dramas,index)
        self.assertEqual(sum(a['revenue_cents'] for a in tree['audiences']),101)
        self.assertEqual(sum(t['revenue_cents'] for t in tree['audiences'][0]['children']),101)

    def test_authoritative_audience_without_theme_metadata(self):
        store.save(resolve('AbC',response(audience=['男性'])),self.db)
        self.assertEqual(classification_index([],[],['AbC'])[('AbC','en')]['audience'],'male')
        dramas={('AbC','en'):dict(content_id='AbC',language='en',name='n',revenue_cents=101,posts={'p'})}
        tree=build_tree(dramas,{})
        self.assertEqual(tree['audiences'][0]['key'],'male')
        self.assertEqual(tree['audiences'][0]['children'][0]['key'],'__unknown__')

    def test_reprojection_preserves_exact_facts(self):
        from features.drama_audience.reproject import rebuild,facts
        old=classify(['sweet','revenge'],audience_record=resolve('AbC',response(audience=['女性'])))
        dramas={('AbC','en'):dict(content_id='AbC',language='en',name='n',revenue_cents=101,posts={'p1','p2'})}
        profile=dict(revenue_cents=101,drama_count=1,published_count=2,status='ready',**build_tree(dramas,{('AbC','en'):old}))
        updated=rebuild(profile,{'AbC':resolve('AbC',response(audience=['男性']))})
        self.assertEqual(updated['audiences'][0]['key'],'male')
        self.assertEqual(facts(profile),facts(updated))
        self.assertEqual(updated['published_count'],2)


class RankTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=self.tmp.name
    def tearDown(self):self.tmp.cleanup()
    def data(self,day='2026-11-13'):
        return dict(code=200,data=dict(date=day,list=[dict(date=day,language_code='en',series_key='AbC',rank=1)]))
    def test_snapshot_filters_and_window(self):
        core.update('secret',self.root,fetcher=lambda _:self.data(),today='2026-11-13')
        d=core.read(root=self.root);self.assertEqual(d['window_start'],'2026-11-10');self.assertEqual(len(d['list']),1)
        self.assertEqual(core.read(lang='es',root=self.root)['availability'],'language_empty')
        self.assertEqual(core.read(day='2026-11-12',root=self.root)['availability'],'not_generated')
    def test_failure_preserves_good(self):
        core.update('secret',self.root,fetcher=lambda _:self.data(),today='2026-11-13')
        core.update('secret',self.root,fetcher=lambda _:dict(code=401),today='2026-11-13')
        d=core.read(root=self.root);self.assertEqual(d['status']['status'],'error');self.assertEqual(len(d['list']),1)
    def test_stale_date_not_current_success(self):
        self.assertEqual(core.update('s',self.root,fetcher=lambda _:self.data('2026-11-12'),today='2026-11-13')['status'],'not_generated')
        self.assertEqual(core.update('s',self.root,fetcher=lambda _:dict(code=200,data=dict(date='',list=[])),today='2026-11-13')['status'],'not_generated')
    def test_duplicates_bad_rank_empty(self):
        d=self.data();d['data']['list']*=2
        with self.assertRaises(ValueError):core.validate(d)
        d=self.data();d['data']['list'][0]['rank']=101
        with self.assertRaises(ValueError):core.validate(d)
        with self.assertRaises(ValueError):core.validate(dict(code=200,data=dict(date='',list=[])))
    def test_invalid_date(self):
        with self.assertRaises(ValueError):core.read(day='invalid',root=self.root)


class RouteTests(unittest.TestCase):
    def test_cookie_and_navigation_gate(self):
        from features.drama_pay_rank.routes import dispatch
        from urllib.parse import urlparse
        calls=[]
        class Handler:
            def _require_cookie_module(self,key):calls.append(key);return False
        dispatch(Handler(),urlparse('/api/drama-pay-rank/list'),{})
        self.assertEqual(calls,['drama_pay_rank'])

    def test_navigation_idempotent(self):
        from features.drama_pay_rank.routes import ensure_navigation
        a=ensure_navigation([]);self.assertEqual(ensure_navigation(a),a)


class RankMetadataTests(unittest.TestCase):
    def resource(self, **changes):
        return dict(app_id=1479, content_id='AbC', language='pt', series_code='30702',
                    labels='Sweet,Hidden Identity', **changes)

    def test_resource_id_and_same_portrait_themes(self):
        from features.drama_pay_rank.metadata import index
        row=self.resource()
        result=index([row,row],[])[('AbC','pt-br')]
        self.assertEqual(result['resource_id'],'30702')
        self.assertEqual(result['labels'],['Sweet','Hidden Identity'])
        self.assertEqual(result['secondary_labels'],classify(row['labels'])['theme_names'])

    def test_product_id_language_and_conflict(self):
        from features.drama_pay_rank.metadata import index
        row=self.resource(); other={**row,'app_id':3360,'series_code':'wrong'}
        result=index([row,other],[])
        self.assertEqual(len(result),1)
        self.assertNotIn(('abc','pt-br'),result)
        conflict=index([row],[{**row,'series_code':'999'}])[('AbC','pt-br')]
        self.assertEqual((conflict['status'],conflict['resource_id'],conflict['labels']),('conflict','',[]))

    def test_cached_metadata_and_failed_retry(self):
        from features.drama_pay_rank import metadata
        with tempfile.TemporaryDirectory() as root:
            payload=dict(code=200,data=dict(date='2026-11-13',list=[
                dict(date='2026-11-13',language_code='pt',series_key='AbC',rank=7)]))
            core.update('s',root,fetcher=lambda _:payload,today='2026-11-13')
            class Reader:
                failed=True
                calls=0
                def query(inner,statements,timeout):
                    inner.calls+=1
                    if inner.failed:raise RuntimeError('source_unavailable')
                    return [dict(self.resource(),kind='ads_drama_resource')]
            reader=Reader()
            with self.assertRaises(RuntimeError):metadata.refresh(reader,root)
            self.assertEqual(metadata.read(root),{})
            reader.failed=False
            self.assertEqual(metadata.refresh(reader,root)['saved'],1)
            self.assertEqual(metadata.refresh(reader,root)['requested'],0)
            self.assertEqual(reader.calls,2)
            result=core.read(lang='pt',root=root)['list'][0]
            self.assertEqual((result['rank'],result['resource_id']),(7,'30702'))
            self.assertTrue(result['secondary_labels'])
            self.assertEqual(core.read(lang='en',root=root)['list'],[])

    def test_wrong_case_source_row_not_saved(self):
        from features.drama_pay_rank import metadata
        with tempfile.TemporaryDirectory() as root:
            payload=dict(code=200,data=dict(date='2026-11-13',list=[dict(date='2026-11-13',language_code='pt',series_key='AbC',rank=1)]))
            core.update('s',root,fetcher=lambda _:payload,today='2026-11-13')
            class Reader:
                def query(inner,statements,timeout):
                    return [{**self.resource(),'content_id':'abc','kind':'ads_drama_resource'}]
            self.assertEqual(metadata.refresh(Reader(),root)['saved'],0)
            self.assertEqual(metadata.read(root),{})


if __name__=='__main__':unittest.main()
