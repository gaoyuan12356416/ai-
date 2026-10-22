import copy
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from features.video_editing import VideoEditingError
from features.video_editing.recipe import recipe_fingerprint
from features.video_editing.store import Store,owner
from features.video_editing.service import Service
from features.video_editing.uploads import Uploads
from features.video_editing.worker import Dispatcher

ACTOR={'tenant_key':'tenant','user_id':'user1','is_admin':False}
OTHER={'tenant_key':'tenant','user_id':'user2','is_admin':False}
ADMIN={'tenant_key':'tenant','user_id':'admin','is_admin':True}
A='va_'+'a'*32
E='vep_'+'e'*32
E2='vep_'+'f'*32


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db.sqlite3')
        self.service=Service(self.store,Uploads(self.store,None,'bucket','ap-hongkong'))
        self.add_source(A,'asset');self.add_source(E,'episode');self.add_source(E2,'episode',content='other')
    def add_source(self,sid,kind,status='ready',content='drama'):
        identity={'size_bytes':1234,'etag':'abcd','sha256':'f'*64}
        if kind=='asset':identity.update(bucket='bucket',key='video-editing/v1/sources/'+sid+'/source')
        with self.store.transaction() as db:
            self.store.insert('source',{'id':sid,'owner':owner(ACTOR),'kind':kind,'name':sid,'status':status,
                'data':{'owner_user_id':ACTOR['user_id'],'url':'https://cdn.example.com/'+sid+'.mp4','identity':identity,
                        'metadata':{'duration_ms':10000,'width':720,'height':1280,'fps':30,'has_audio':True},
                        'app_id':'app','content_id':content,'episode_number':1}},db)
    def draft(self):
        return {'name':'剪辑测试','asset_id':A,'dramas':[{'app_id':'app','content_id':'drama','title':'测试剧','clips':[
            {'id':'c1','source_id':A,'start_ms':0,'end_ms':1000,'fit':'contain','mute':False,'transition':'cut'},
            {'id':'c2','source_id':E,'start_ms':2000,'end_ms':5000,'fit':'cover','mute':False,'transition':'cut'}]}]}
    def project(self):return self.service.save_project(self.draft(),ACTOR)
    def render(self,p):return self.service.render(p['id'],{'operation_id':'render_op_1','expected_version':p['version']},ACTOR)['items'][0]
    def test_ready_assets_shared_management_only_owner_or_admin(self):
        self.assertEqual(self.service.assets(OTHER)['total'],1)
        self.assertEqual(self.service.source(A,OTHER)['id'],A)
        with self.assertRaises(VideoEditingError):self.service.mutate_asset(A,OTHER,name='stolen')
        self.assertEqual(self.service.mutate_asset(A,ADMIN,name='rename')['name'],'rename')
    def test_unready_assets_not_shared(self):
        with self.store.transaction() as db:self.store.update('source',A,{'status':'uploaded'},db)
        self.assertEqual(self.service.assets(OTHER,status='all')['total'],0)
        with self.assertRaises(VideoEditingError):self.service.source(A,OTHER)
    def test_personal_project_and_tasks_admin_visibility(self):
        p=self.project();task=self.render(p)
        with self.assertRaises(VideoEditingError):self.service.project(p['id'],OTHER)
        with self.assertRaises(VideoEditingError):self.service.task(task['id'],OTHER)
        self.assertEqual(self.service.project(p['id'],ADMIN)['id'],p['id'])
    def test_project_create_idempotent_and_compare_swap(self):
        draft={**self.draft(),'operation_id':'create_operation_1'}
        p=self.service.save_project(draft,ACTOR)
        self.assertEqual(self.service.save_project(draft,ACTOR)['id'],p['id'])
        with self.assertRaises(VideoEditingError):self.service.save_project({**draft,'name':'changed'},ACTOR)
        changed=self.service.save_project({**self.draft(),'expected_version':1},ACTOR,p['id'])
        self.assertEqual(changed['version'],2)
        with self.assertRaises(VideoEditingError):self.service.save_project({**self.draft(),'expected_version':1},ACTOR,p['id'])
    def test_rejects_external_url_and_cross_drama_source(self):
        draft=self.draft();draft['dramas'][0]['clips'][0]['url']='https://evil.test'
        with self.assertRaises(VideoEditingError):self.service.save_project(draft,ACTOR)
        draft=self.draft();draft['dramas'][0]['clips'][1]['source_id']=E2
        with self.assertRaises(VideoEditingError):self.service.save_project(draft,ACTOR)
    def test_frozen_render_independent_of_future_project_and_asset_edits(self):
        p=self.project();task=self.render(p);frozen=self.service.task(task['id'],ACTOR)['data']['payload']
        changed=self.draft();changed['dramas'][0]['clips'][1]['end_ms']=7000;changed['expected_version']=1
        self.service.save_project(changed,ACTOR,p['id']);self.service.mutate_asset(A,ACTOR,delete=True)
        self.assertEqual(self.service.task(task['id'],ACTOR)['data']['payload'],frozen)
    def test_render_idempotence_and_one_task_per_drama(self):
        p=self.project();first=self.render(p);second=self.render(p)
        self.assertEqual(first['id'],second['id']);self.assertEqual(self.store.list('task')[1],1)
    def test_repeat_selected_asset_supported(self):
        draft=self.draft();extra=copy.deepcopy(draft['dramas'][0]['clips'][0]);extra['id']='again';draft['dramas'][0]['clips'].append(extra)
        p=self.service.save_project(draft,ACTOR);self.assertEqual(len(self.service.task(self.render(p)['id'],ACTOR)['data']['payload']['recipe']['clips']),3)
    def test_all_drama_submissions_atomic_on_bad_timeline(self):
        draft=self.draft();draft['dramas'].append({'app_id':'app','content_id':'other','title':'other','clips':[]})
        p=self.service.save_project(draft,ACTOR)
        with self.assertRaises(VideoEditingError):self.render(p)
        self.assertEqual(self.store.list('task')[1],0)
    def test_worker_unknown_does_not_create_second_job(self):
        p=self.project();task=self.render(p)
        self.service.render(p['id'],{'operation_id':'render_op_2','expected_version':1},ACTOR)
        calls=[]
        def transport(method,path,payload):
            calls.append((method,path))
            if method=='POST':raise TimeoutError()
            return None
        worker=Dispatcher(self.service,transport);worker.tick();worker.tick();worker.tick()
        self.assertEqual(sum(m=='POST' for m,p in calls),1)
        self.assertEqual(self.service.task(task['id'],ACTOR)['status'],'unknown')
        self.assertEqual([r['status'] for r in self.store.list('task')[0]].count('queued'),1)
    def test_worker_checks_same_id_and_never_requeues_completed(self):
        p=self.project();task=self.render(p);record=self.service.task(task['id'],ACTOR)
        result={'job_id':task['id'],'task_type':'video_editing_v1','operation':'render',
            'recipe_sha256':recipe_fingerprint(record['data']['payload']['recipe']),
            'artifact':{'bucket':'bucket','key':'video-editing/v1/outputs/o.mp4','sha256':'f'*64,'size_bytes':100,'duration_ms':4000,'width':720,'height':1280}}
        calls=[]
        def transport(method,path,payload):
            calls.append((method,path));return {'job_id':task['id'],'status':'completed','result':result}
        worker=Dispatcher(self.service,transport)
        with patch.object(worker,'verify_artifact'):
            worker.tick();worker.tick()
        self.assertEqual(len(calls),1);self.assertEqual(self.service.task(task['id'],ACTOR)['status'],'completed')
        with self.assertRaises(VideoEditingError):self.service.resume(task['id'],ACTOR)
    def test_wrong_gpu_recipe_cannot_complete(self):
        p=self.project();task=self.render(p)
        worker=Dispatcher(self.service,lambda *_:{'job_id':task['id'],'status':'completed','result':{
            'job_id':task['id'],'task_type':'video_editing_v1','operation':'render','recipe_sha256':'0'*64}})
        worker.tick();self.assertEqual(self.service.task(task['id'],ACTOR)['status'],'unknown')
    def test_probe_reuses_active_and_completed_metadata(self):
        with self.store.transaction() as db:self.store.update('source',A,{'status':'uploaded'},db)
        a=self.service.enqueue_probe(A,ACTOR);b=self.service.enqueue_probe(A,ACTOR)
        self.assertEqual(a['data']['probe_task_id'],b['data']['probe_task_id']);self.assertEqual(self.store.list('task')[1],1)
    def test_runtime_intake_file_blocks_new_work_preserves_reads(self):
        project=self.project();task=self.render(project)
        marker=Path(self.tmp.name)/'intake.disabled'
        with patch.dict(os.environ,{'VIDEO_EDITING_DISABLED_FILE':str(marker)}):
            self.assertTrue(self.service.config()['enabled']);marker.write_text('paused')
            self.assertFalse(self.service.config()['enabled'])
            with self.assertRaises(VideoEditingError):self.service.render(project['id'],{'operation_id':'new_render_2','expected_version':1},ACTOR)
            self.assertEqual(self.render(project)['id'],task['id'])
            self.assertEqual(self.service.task(task['id'],ACTOR)['id'],task['id'])
            marker.unlink();self.assertTrue(self.service.config()['enabled'])
    def test_resume_uses_same_id_and_generation_compare_swap(self):
        p=self.project();task=self.render(p)
        with self.store.transaction() as db:self.store.update('task',task['id'],{'status':'failed'},db)
        self.service.resume(task['id'],ACTOR);calls=[]
        def transport(method,path,payload):
            calls.append((method,path,payload))
            return {'job_id':task['id'],'status':'failed','generation':7} if method=='GET' else {'job_id':task['id'],'status':'queued','generation':8}
        Dispatcher(self.service,transport).tick()
        self.assertEqual(calls[1][1],'/api/gpu-video/video-editing/jobs/'+task['id']+'/resume')
        self.assertEqual(calls[1][2]['expected_generation'],7)
        self.assertEqual(calls[1][2]['job_id'],task['id'])
    def test_explicit_gpu_preacceptance_rejection_waits_without_unknown(self):
        p=self.project();task=self.render(p);calls=[]
        def transport(method,path,payload):
            calls.append(method);raise VideoEditingError('video_editing_disabled','paused',503)
        worker=Dispatcher(self.service,transport);worker.tick();worker.tick()
        self.assertEqual(calls,['POST']);self.assertEqual(self.service.task(task['id'],ACTOR)['status'],'queued')
        self.assertEqual(self.service.task(task['id'],ACTOR)['data']['stage'],'waiting_gpu_capacity')


if __name__=='__main__':unittest.main()
