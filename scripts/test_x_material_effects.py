"""Offline acceptance for cumulative effects and explicitly scoped material reuse."""
import json
from contextlib import closing
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_material_effects import model,store,selection,migrations,reuse,collector,api
from features.x_auto_posts.selector import DramaFilterRule,SortRule,DecimalRange,MaterialFilterRule
from features.x_auto_posts.validation import normalize_template_payload,ValidationError
from features.x_auto_posts.publisher import selector_rules
from scripts.test_x_auto_post_validation import valid_payload

def post(pid='100',material='7',account='1',**changes):
    data=dict(post_id=pid,namespace='ads_custom_source',material_id=material,account_id=account,
        product='Dramawave',catalog='dramawave',content_id='42',language='en',material_name='Clip',drama_name='Drama',
        published_at='2026-11-18T16:30:00Z',metrics_at='2026-11-20T00:00:00Z',revenue_through='2026-11-19',
        error_code='',campaign_ids=['900'],source='ordinary',post_url='https://x.com/i/status/'+pid)
    data.update({k:0 for k in model.METRICS});data.update(views=100,likes=3,revenue_cents=125);data.update(changes);return data

class EffectsTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def snapshot(self,posts):
        return store.publish(self.root,'20261120T010000Z',posts,dict(generated_at='2026-11-20T01:00:00Z',revenue_source_generated_at='2026-11-20T00:30:00Z',revenue_through='2026-11-19'))
    def test_distinct_posts_sum_across_accounts(self):
        row=model.aggregate([post(),post('101',account='2',views=200,revenue_cents=201)])[0]
        self.assertEqual((row['views'],row['revenue_cents'],row['account_count'],row['post_count']),(300,326,2,2))
    def test_missing_is_not_zero_and_has_known_subtotal(self):
        row=model.aggregate([post(),post('101',views=None,likes=0,revenue_cents=None)])[0]
        self.assertIsNone(row['views']);self.assertEqual(row['known']['views'],100)
        self.assertEqual(row['coverage']['views'],1);self.assertEqual(row['likes'],3);self.assertIsNone(row['revenue_cents'])
    def test_duplicate_post_rejected(self):
        with self.assertRaises(ValueError):model.aggregate([post(),post()])
    def test_product_catalogs_do_not_mix_in_drama(self):
        rows=model.aggregate([post(),post('101','8',catalog='moboreels',product='Moboreels')],level='drama')
        self.assertEqual(len(rows),2)
    def test_public_metric_definitions_and_missing(self):
        value=model.extract({'public_metrics':dict(impression_count=900,like_count=0,reply_count=2,retweet_count=3,quote_count=4)})
        self.assertEqual((value['views'],value['shares'],value['likes']),(900,7,0));self.assertIsNone(value['bookmarks'])
    def test_collection_intervals(self):
        now=datetime(2026,11,20,tzinfo=timezone.utc)
        self.assertEqual([model.interval((now-timedelta(days=d)).isoformat(),now) for d in (1,7,8,30,31)],[3600,3600,21600,21600,86400])
    def test_page_filter_uses_beijing_publication_date(self):
        self.snapshot([post(),post('101','8',published_at='2026-11-18T15:59:00Z')])
        value=api.query({'start':['2026-11-19'],'end':['2026-11-19']},{'role':'admin'},self.root)
        self.assertEqual(value['total'],1);self.assertEqual(value['items'][0]['material_id'],'7')
    def test_page_missing_sorts_last_in_both_directions(self):
        self.snapshot([post(),post('101','8',views=None),post('102','9',views=10)])
        for direction,ids in [('asc',['9','7','8']),('desc',['7','9','8'])]:
            value=api.query({'direction':[direction]},{'role':'admin'},self.root)
            self.assertEqual([r['material_id'] for r in value['items']],ids)
    def test_threshold_sort_and_exact_decimal_revenue(self):
        a=SimpleNamespace(catalog='dramawave',content_id='a',language='en')
        b=SimpleNamespace(catalog='dramawave',content_id='b',language='en')
        c=SimpleNamespace(catalog='dramawave',content_id='c',language='en')
        token=selection.ACTIVE.set(dict(drama={('dramawave','a','en'):{'views':200,'revenue_cents':101},('dramawave','b','en'):{'views':10,'revenue_cents':100}},material={}))
        try:
            rule=DramaFilterRule(sort=SortRule('x_views','desc'),x_effect={'revenue':DecimalRange(Decimal('1.01'),None)})
            rejected={};self.assertEqual(selection.apply([b,c,a],rule,'drama',rejected),[a]);self.assertEqual(rejected['drama_x_revenue'],2)
        finally:selection.ACTIVE.reset(token)
    def test_template_validation_and_mapping_both_levels(self):
        raw=valid_payload();raw['profile_model']='growth'
        raw['drama_rule'].update(x_views_min='100',sort_by='x_comments')
        raw['material_rule'].update(x_revenue_min='1.01',x_likes_max='20',sort_by='x_revenue')
        normalized=normalize_template_payload(raw);rules=selector_rules(normalized)
        self.assertEqual(normalized['profile_model'],'revenue');self.assertTrue(selection.enabled(rules))
        self.assertEqual(rules.material.x_effect['revenue'].minimum,Decimal('1.01'))
        self.assertEqual(rules.drama.sort.field,'x_comments')
        raw['drama_rule']['x_views_min']='1.5'
        with self.assertRaises(ValidationError):normalize_template_payload(raw)
    def test_complete_selector_filters_ranks_and_freezes_effect_version(self):
        from scripts.test_x_auto_post_selector import FakeSource,FakeMetricStore,FakeStore,drama,material,selector,request,rules
        source=FakeSource([drama('C1'),drama('C2')],{'C1':[material('101','C1')],'C2':[material('201','C2'),material('202','C2')]})
        ledger=FakeStore()
        self.snapshot([post('100','101',content_id='C1',views=100),post('101','201',content_id='C2',views=600),post('102','202',content_id='C2',views=900)])
        chosen=rules(drama={'sort':{'field':'x_views','direction':'desc'}},material={'x_effect':{'views':{'min':500}},'sort':{'field':'x_views','direction':'asc'}})
        with patch.dict('os.environ',{'X_MATERIAL_EFFECT_ROOT':str(self.root)}),patch.object(reuse,'client',return_value={'ok':True,'blocked':[]}):
            selected=selector(source,FakeMetricStore([]),store=ledger).select_and_reserve(request(chosen))
        self.assertEqual(selected.material.source.material_id,'201')
        frozen=ledger.reserve_calls[0]['selection_snapshot']['x_effect']
        self.assertEqual(frozen['version'],'20261120T010000Z');self.assertEqual(frozen['material']['views'],600);self.assertEqual(frozen['drama']['views'],1500)
    def test_legacy_templates_do_not_enable_reuse(self):
        config=normalize_template_payload(valid_payload());self.assertFalse(selection.config_enabled(config));self.assertFalse(selection.enabled(selector_rules(config)))
    def test_ledger_migration_preserves_history_and_is_idempotent(self):
        db=sqlite3.connect(':memory:')
        db.executescript('CREATE TABLE x_auto_material_ledger(material_id TEXT PRIMARY KEY COLLATE BINARY,task_id INTEGER NOT NULL UNIQUE,proof TEXT);INSERT INTO x_auto_material_ledger VALUES(\'7\',1,\'receipt\');')
        self.assertTrue(migrations.auto_ledger(db));self.assertFalse(migrations.auto_ledger(db))
        self.assertEqual(db.execute('SELECT * FROM x_auto_material_ledger').fetchone(),('7',1,'receipt'))
        db.execute("INSERT INTO x_auto_material_ledger VALUES('7',2,'second-account')")
        with self.assertRaises(sqlite3.IntegrityError):db.execute("INSERT INTO x_auto_material_ledger VALUES('8',2,'conflict')")
        db.close()
    def test_revenue_campaign_exact_and_conserved(self):
        path=self.root/'source.sqlite3'
        catalog=dict(platform='x',site_id='2116',key=['900'],tenant='t',owner='u',channel='1',drama='42',language='en',
            link_type='manual',excluded=False,publications=[dict(id='x:post:100',date='2026-11-18',kind='post')])
        with closing(sqlite3.connect(path)) as db:
            db.executescript('CREATE TABLE catalog(payload TEXT);CREATE TABLE effects(platform TEXT,dt TEXT,payload TEXT);')
            db.execute('INSERT INTO catalog VALUES(?)',(json.dumps(catalog),))
            for campaign,cents in [('900',123),('901',400)]:db.execute('INSERT INTO effects VALUES(?,?,?)',('x','2026-11-19',json.dumps(dict(date='2026-11-19',campaign_id=campaign,site_id='2116',revenue_cents=cents,refund_cents=1))))
            db.commit()
        source=dict(start='2026-11-19',end='2026-11-19',version='v',generated_at='2026-11-20T00:00:00Z',coverage={'x:2116':['2026-11-19']})
        posts=[post()]
        with patch('features.social_analytics.warehouse_reader.current',return_value=(source,path)):
            meta=collector.revenue(posts,'unused')
        self.assertEqual(posts[0]['revenue_cents'],123);self.assertEqual(meta['unassigned_revenue_cents'],400)
        posts=[post(),post('101')]
        with patch('features.social_analytics.warehouse_reader.current',return_value=(source,path)):
            meta=collector.revenue(posts,'unused')
        self.assertTrue(all(p['revenue_cents'] is None for p in posts));self.assertEqual(meta['unassigned_revenue_cents'],523)

class ReuseQueueTests(unittest.TestCase):
    def test_other_account_reuse_and_same_account_hold_at_queue_boundary(self):
        from features.x_posts.service import XPostStore,XPostError
        from scripts.test_x_post_priority_manual_store import material_candidate
        actor=dict(user_id='admin',name='Admin')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'posts.sqlite3';publisher=XPostStore(path)
            first=publisher.create_manual_run(['701'],[2],'first',actor)
            plan=publisher.create_manual_plan(first['id'],[material_candidate(first,2,'701')])
            with closing(sqlite3.connect(path)) as db:
                db.execute("UPDATE x_post_queue SET status='published' WHERE id=?",(plan['queues'][0]['id'],));db.commit()
            second=publisher.create_auto_template_run('701',3,'task-two','template',1,'{{drama_name}}\n{{desc}}',actor,reuse_context=dict(lane='tag',task_id=2))
            with patch.object(reuse,'client',return_value={'ok':True}):
                result=publisher.create_manual_plan(second['id'],[material_candidate(second,3,'701')],'auto_template')
            self.assertEqual(len(result['queues']),1)
            with self.assertRaises(XPostError):publisher.create_auto_template_run('701',2,'task-three','template',1,'{{drama_name}}\n{{desc}}',actor,reuse_context=dict(lane='tag',task_id=3))

class BrokerTests(unittest.TestCase):
    def test_serial_grants_block_other_task_then_release_after_terminal_receipt(self):
        sources={1:dict(account_id='2',material_id='',status='selecting'),2:dict(account_id='3',material_id='',status='selecting')}
        with tempfile.TemporaryDirectory() as directory, patch.object(reuse,'task',side_effect=lambda lane,tid:sources[tid]), patch.object(reuse,'conflicts',return_value=set()):
            one=dict(action='reserve',lane='tag',task_id=1,account_id='2',material_id='701')
            two=dict(action='reserve',lane='ordinary',task_id=2,account_id='3',material_id='701')
            self.assertTrue(reuse.handle(one,directory)['ok'])
            self.assertTrue(reuse.handle(one,directory)['ok'])
            self.assertFalse(reuse.handle(two,directory)['ok'])
            sources[1].update(status='published',material_id='701')
            self.assertTrue(reuse.handle(two,directory)['ok'])
            sources[2].update(status='published',material_id='701')
            sources[3]=dict(account_id='2',material_id='',status='selecting')
            self.assertFalse(reuse.handle(dict(one,task_id=3),directory)['ok'])
    def test_canceled_selection_without_material_releases_its_grant(self):
        sources={1:dict(account_id='2',material_id='',status='selecting'),2:dict(account_id='3',material_id='',status='selecting')}
        with tempfile.TemporaryDirectory() as directory,patch.object(reuse,'task',side_effect=lambda lane,tid:sources[tid]),patch.object(reuse,'conflicts',return_value=set()):
            one=dict(action='reserve',lane='tag',task_id=1,account_id='2',material_id='701')
            self.assertTrue(reuse.handle(one,directory)['ok']);sources[1]['status']='canceled'
            self.assertTrue(reuse.handle(dict(one,task_id=2,account_id='3'),directory)['ok'])
    def test_unknown_or_active_canonical_conflict_never_grants(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(reuse,'task',return_value=dict(account_id='2',material_id='',status='selecting')), patch.object(reuse,'conflicts',return_value={'701'}):
            self.assertFalse(reuse.handle(dict(action='reserve',lane='tag',task_id=1,account_id='2',material_id='701'),directory)['ok'])

if __name__=='__main__':unittest.main()
