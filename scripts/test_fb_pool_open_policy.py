"""Explicit Page-pool repeats, optional metadata, and task-owned write safety."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
from contextlib import closing
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from features.fb_video_publish.core import Store, normalize_batch
from features.fb_video_publish.service import Service
from features.fb_video_publish.sources import SourceRepository
from features.fb_video_publish.guards import install_backstops, manual_block, automatic_block
from features.pipeline_dispatch.fb_isolation import identity_resources
from features.fb_gpu.prepare_worker import validate_request, PrepareWorkerError

NOW = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
ACTOR = dict(tenant_key='test', user_id='user', user_name='Tester', is_admin=True,
             business_owner_id='1', sub_user_id='1')


class Sources:
    def __init__(self): self.last_policy = {}
    def get_materials(self, ids, **kwargs):
        self.last_policy = kwargs
        return {mid: dict(material_id=mid, material_name='Video', content_id='content', language='en',
                         media_url='http://media.example.test/stream?id=1', duration_seconds=0,
                         material_tag='', drama_name='', drama_description='', thumbnail_url='') for mid in ids}
    def resolve_materials(self, ids, **kwargs): return {'items':self.get_materials(ids, **kwargs), 'errors':{}}
    def get_pages(self, actor, ids, **kwargs): return {p:dict(page_id=p,page_name='Page',language='en') for p in ids}
    def list_pages(self, actor, **kwargs): return list(self.get_pages(actor, ['1']).values())


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'test.sqlite3', now_fn=lambda:NOW)
        self.sources=Sources()
        self.service=Service(self.store,self.sources,'k'*40,live_enabled=True,pool_enabled=True)
        self.store.save_configs(ACTOR,['1'],'en','')
        with self.store.connect() as c:
            self.config=dict(c.execute('SELECT * FROM fb_video_page').fetchone())
        self.version=0
    def admit(self, ids):
        request=dict(config_instance_id=self.config['config_instance_id'], expected_version=self.version, material_ids=ids)
        preview=self.service.pool.resolve(ACTOR,'1',request)
        self.assertEqual(preview['material_ids'],ids)
        self.assertFalse(preview['repeat_required'])
        operation={**request,'operation_id':uuid.uuid4().hex,'preview_token':preview['preview_token'],'allow_repeat':False}
        result=self.service.pool.add(ACTOR,'1',operation)
        self.version=result['pool']['version']
        return result,operation
    def freeze(self, caption='', slot=1):
        pair=dict(material_id='101',page_id='1',config_instance_id=self.config['config_instance_id'],
                  config_version=self.config['version'],caption_template=caption)
        req=normalize_batch(dict(schema_version=2,operation_id=uuid.uuid4().hex,material_ids=['101'],page_ids=['1'],
                  processing_mode='original',material_schedules={'101':None},pairs=[pair]),manual=True)
        resolved=dict(materials=self.sources.get_materials(['101']),pages=self.sources.get_pages(ACTOR,['1']))
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            configs,repeats=self.store.check_plan(c,ACTOR,req,resolved,page_pool=True)
            result=self.store.freeze(c,ACTOR,req,resolved,configs,repeats,[(1,pair,1,NOW,'2026-11-06')],
                pool=dict(config_instance_id=self.config['config_instance_id'],slot_id=slot,item_id=slot,slot_at_utc=NOW.isoformat()))
            c.commit()
        return result['batch']['items'][0]['task_id']
    def test_over_200_repeated_rows_and_operation_idempotency(self):
        first,operation=self.admit(['101']*205)
        self.assertEqual(len(first['items']),205)
        self.assertTrue(self.service.pool.add(ACTOR,'1',operation)['idempotent'])
        self.admit(['101','101'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM fb_video_pool_item').fetchone()[0],207)
    def test_empty_long_caption_missing_macros_and_missing_drama_name(self):
        for i,caption in enumerate(['','{name}{desc}{unknown}', 'text'*6000],1):
            tid=self.freeze(caption,i)
            with self.store.connect() as c:
                row=dict(c.execute('SELECT * FROM fb_video_task WHERE task_id=?',(tid,)).fetchone())
            self.assertEqual(row['caption'],caption.replace('{name}','').replace('{desc}',''))
            self.assertIn('*none*',row['long_url'])
    def test_unknown_task_does_not_block_new_item_but_cannot_replay_itself(self):
        old=self.freeze()
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET status='unknown',unknown_outcome=1,graph_post_id='original_receipt' WHERE task_id=?",(old,))
        self.admit(['101'])
        new=self.freeze(slot=2)
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET link_state='ready' WHERE task_id=?",(new,))
        claimed=self.store.claim('execute','test',task_id=new)
        self.assertEqual(claimed['task_id'],new)
        self.assertIsNone(self.store.claim('execute','test',task_id=old))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT graph_post_id FROM fb_video_task WHERE task_id=?',(old,)).fetchone()[0],'original_receipt')
    def test_same_page_write_is_still_serial(self):
        one,two=self.freeze(slot=1),self.freeze(slot=2)
        with self.store.connect() as c: c.execute("UPDATE fb_video_task SET link_state='ready'")
        self.assertEqual(self.store.claim('execute','test',task_id=one)['task_id'],one)
        self.assertIsNone(self.store.claim('execute','test',task_id=two))
    def test_native_execution_reuses_page_pool_source_policy(self):
        from features.pipeline_dispatch.fb_native import FBNativeBridge
        task_id=self.freeze()
        with self.store.connect() as c:
            row=dict(c.execute('SELECT * FROM fb_video_task WHERE task_id=?',(task_id,)).fetchone())
        row['prepared_json']=json.dumps({'media_url':self.sources.get_materials(['101'])['101']['media_url']})
        bridge=object.__new__(FBNativeBridge)
        bridge.runtime=SimpleNamespace(manual_worker=SimpleNamespace(sources=self.sources))
        request=bridge._video_preflight(row)
        self.assertTrue(self.sources.last_policy['page_pool'])
        self.assertEqual(request['message'],'')
    def test_cross_family_unknown_allows_pool_but_not_other_policies(self):
        with self.store.connect() as c:
            c.execute('CREATE TABLE fb_auto_task(id TEXT PRIMARY KEY,page_id TEXT,material_id TEXT,status TEXT,unknown_outcome INTEGER,graph_post_id TEXT)')
            c.execute("INSERT INTO fb_auto_task VALUES('old','1','101','unknown',1,'receipt')")
            install_backstops(c)
            self.assertEqual(automatic_block(c,'1','101'),'fb_video_material_unknown')
            self.assertEqual(automatic_block(c,'1','101',allow_repeat=True),'')
        self.admit(['101'])
        self.freeze()


class SourceTests(unittest.TestCase):
    def test_catalog_duration_and_address_are_not_admission_gates(self):
        for duration in ('', '0', '999999','broken','NaN'):
            row=dict(material_id='1',content_id='content',language='en',material_name='Video',
                     video_duration=duration,media_url='http://arbitrary.example/stream?id=1')
            self.assertEqual(SourceRepository._material(row,page_pool=True)['media_url'],row['media_url'])
        repo=SourceRepository(None,SimpleNamespace(mysql=SimpleNamespace(schema='ads',blacklist_schema='setting')))
        sql,_=repo._conditions(enforce_blacklist=False,page_pool=True)
        self.assertNotIn('is_delete',sql);self.assertNotIn('video_duration',sql)
    def test_render_accepts_external_http_url_only_for_pool_policy(self):
        config=SimpleNamespace(profile='test',allowed_source_hosts=('old.example',))
        req=dict(job_id='fb-page-'+'a'*48,content_id='content',source_url='http://arbitrary.example/stream',
                 source_trim_tail_seconds=0,video_template='random_overlay',expected_profile='test')
        with self.assertRaises(PrepareWorkerError):validate_request(req,config)
        self.assertTrue(validate_request(dict(req,page_pool=True),config)['page_pool'])
    def test_unknown_and_material_fences_are_task_scoped_for_pool(self):
        one=identity_resources('1','101','publish',pool_task_id='a'*32)[0]
        two=identity_resources('1','101','publish',pool_task_id='b'*32)[0]
        old=identity_resources('1','101','publish')[0]
        self.assertEqual(set(one)&set(two),{'fb:page:1'})
        self.assertNotEqual(one[1],old[1])


class DispatchPoolTests(unittest.TestCase):
    def test_old_unknown_does_not_block_independent_same_material_pool_task(self):
        from scripts.test_pipeline_fb_source_locks import FBSourceLockTests
        from features.pipeline_dispatch.core import DispatchStore
        from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
        FBSourceLockTests.setUp(self)
        adapter=LedgerAdapter(LedgerSpec('fb','video',self.video.path,'fb_video_task',identity='task_id'),driver=object())
        dispatch=DispatchStore(Path(self.temp.name)/'dispatch.db',lambda:self.now)
        dispatch.register_node('cpu-main',['publish:fb','prepare:fb','backend:cpu'])
        dispatch.channel_enabled('fb',True)
        dispatch.worker_heartbeat('cpu-main','one','publish')
        dispatch.worker_heartbeat('cpu-main','two','publish')
        old,new,third=[FBSourceLockTests.video_job(self,i) for i in (1,2,3)]
        with self.video.connect() as c:
            for job in (new,third):
                c.execute("INSERT INTO fb_video_batch SELECT ?,tenant_key,owner_user_id,owner_name,?,request_hash,request_json,created_at_utc FROM fb_video_batch WHERE batch_id='batch'",(job['source_id'],job['source_id']))
                c.execute('UPDATE fb_video_task SET batch_id=? WHERE task_id=?',(job['source_id'],job['source_id']))
            c.execute("UPDATE fb_video_task SET source_kind='pool',material_id='101'")
            c.execute("UPDATE fb_video_task SET status='unknown',unknown_outcome=1 WHERE task_id=?",(old['source_id'],))
        for job in (old,new,third):
            offer=adapter.project(adapter.read(job['source_id']),now=self.now)
            self.assertEqual(offer['fb_pool_task_id'],job['source_id'])
            dispatch.offer(offer)
        claimed=dispatch.claim('cpu-main','one','publish')['job']
        self.assertEqual(claimed['source_id'],new['source_id'])
        self.assertIsNone(dispatch.claim('cpu-main','two','publish')['job'])
        with closing(dispatch.connect()) as c:
            self.assertEqual(c.execute("SELECT status FROM pipeline_job WHERE source_id=?",(old['source_id'],)).fetchone()[0],'unknown')


if __name__=='__main__':unittest.main()
