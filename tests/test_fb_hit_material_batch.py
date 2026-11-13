import json
import unittest
from datetime import timedelta
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from scripts.test_fb_hit_drama import HitDramaTests
from features.fb_auto_posts.hit_material_batch import publish_material
from features.fb_auto_posts.hit_material import task_policy
from features.fb_auto_posts.repositories import MaterialCandidate, PageTarget
from tests.test_fb_hit_profile import snapshot,portrait,classification


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture=HitDramaTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.runtime,self.store,self.actor,self.template=(self.fixture.runtime,self.fixture.store,self.fixture.actor,self.fixture.template)
        self.runtime.resolve_source=lambda *args:None
        self.pages=[PageTarget('6',('6',),pid,'1','UTC','en',1) for pid in ['11','12','13']]
        self.runtime.pages.list_pages=lambda *a,**k:self.pages
        self.future=self.store.now_fn()+timedelta(days=2)
        self.runtime.materials.mysql.select=lambda sql,args:[dict(content_id='123',language='en',deploy_time=int(self.future.timestamp()))]
        def exact(config,mid,*,allow_future=False):
            self.assertTrue(allow_future)
            if mid=='99':raise ValueError('missing')
            return MaterialCandidate(mid,'123','https://media.test/'+mid+'.mp4','m','drama','en',Decimal(60),Decimal(0),None,Decimal(0),None,'1')
        self.runtime.materials.exact_material=exact
        self.request=dict(expected_version=1,material_ids=['1','2'],operation_id='batch-request')
        self.profiles=snapshot(portrait('11',themes={'sweet_romance':900,'revenge':100}),portrait('12',themes={'sweet_romance':800,'revenge':200}),portrait('13',themes={'sweet_romance':100,'revenge':900}))

    def publish(self,request=None):
        with patch('features.fb_auto_posts.hit_profile.load_profiles',return_value=self.profiles),patch('features.fb_auto_posts.hit_profile.load_classifications',return_value={('123','en'):classification()}):
            return publish_material(self.runtime,self.template['id'],self.actor,request or self.request)

    def test_best_two_pages_are_unique_and_preparation_precedes_publish(self):
        result=self.publish()
        self.assertEqual(result['queued'],2)
        self.assertEqual({a['page_id'] for a in result['assignments']},{'11','12'})
        with self.store.connect() as conn:
            tasks=[dict(r) for r in conn.execute('SELECT * FROM fb_auto_task')]
            run=dict(conn.execute('SELECT * FROM fb_auto_run').fetchone())
        self.assertEqual({t['material_id'] for t in tasks},{'1','2'})
        self.assertTrue(all(t['planned_publish_at_utc']==(self.future-timedelta(minutes=30)).isoformat() for t in tasks))
        for task in tasks:self.assertTrue(task_policy(run,task,json.loads(run['config_json'])))
        self.assertIsNotNone(self.store.claim_prepare_next('worker',task_id=tasks[0]['id']))
        self.assertIsNone(self.store.claim_next('publisher',task_id=tasks[1]['id']))
        with patch.object(self.runtime.materials,'exact_material',side_effect=AssertionError('replay must not hydrate')):
            replay=self.publish()
        self.assertEqual(replay['run_id'],result['run_id']);self.assertTrue(replay['idempotent'])

    def test_bad_material_and_capacity_shortage_are_visible(self):
        self.pages=self.pages[:1]
        result=self.publish(dict(self.request,material_ids=['1','2','99']))
        self.assertEqual(result['queued'],1)
        self.assertEqual(len(result['skipped_materials']),2)

    def test_paused_page_does_not_take_a_material(self):
        with self.store.connect() as conn:
            row=conn.execute('SELECT config_json FROM fb_auto_template_version').fetchone()
            config=json.loads(row[0]);config['page_daily_limits']=[{'page_id':'11','daily_count':0}]
            conn.execute('UPDATE fb_auto_template_version SET config_json=?',(json.dumps(config),))
        result=self.publish()
        self.assertEqual({a['page_id'] for a in result['assignments']},{'12','13'})

    def test_tampered_timing_rejected_by_publisher(self):
        result=self.publish()
        with self.store.connect() as conn:
            task=dict(conn.execute('SELECT * FROM fb_auto_task LIMIT 1').fetchone());run=dict(conn.execute('SELECT * FROM fb_auto_run').fetchone())
        task['planned_publish_at_utc']=self.store.now_fn().isoformat()
        with self.assertRaises(Exception):task_policy(run,task,json.loads(run['config_json']))

    def test_same_operation_with_different_materials_is_rejected(self):
        self.publish()
        with self.assertRaises(Exception):self.publish(dict(self.request,material_ids=['3']))
