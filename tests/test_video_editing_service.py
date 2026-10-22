import copy
import tempfile
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from features.video_editing import VideoEditingError
from features.video_editing.recipe import recipe_fingerprint
from features.video_editing.catalog import Catalog
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
    def test_removed_product_source_cannot_create_new_work_but_old_batch_survives(self):
        p=self.project();task=self.render(p)
        changed=self.service.save_project({**self.draft(),'expected_version':1},ACTOR,p['id'])
        self.service.catalog=Catalog(self.store,SimpleNamespace(list_products=lambda:[{'app_id':'other_app'}]))
        with self.assertRaises(VideoEditingError):self.service.source(E,ACTOR)
        with self.assertRaises(VideoEditingError):self.service.save_project(self.draft(),ACTOR)
        with self.assertRaises(VideoEditingError):
            self.service.render(p['id'],{'operation_id':'new_render_2','expected_version':changed['version']},ACTOR)
        self.assertEqual(self.render(p)['id'],task['id'])
        self.assertEqual(self.service.render(p['id'],{'operation_id':'reload_old_version','expected_version':1},ACTOR)['items'][0]['id'],task['id'])
        self.assertEqual(self.service.task(task['id'],ACTOR)['id'],task['id'])
    def test_frozen_render_independent_of_future_project_and_asset_edits(self):
        p=self.project();task=self.render(p);frozen=self.service.task(task['id'],ACTOR)['data']['payload']
        changed=self.draft();changed['dramas'][0]['clips'][1]['end_ms']=7000;changed['expected_version']=1
        self.service.save_project(changed,ACTOR,p['id']);self.service.mutate_asset(A,ACTOR,delete=True)
        self.assertEqual(self.service.task(task['id'],ACTOR)['data']['payload'],frozen)
    def test_render_idempotence_and_one_task_per_drama(self):
        p=self.project();first=self.render(p);second=self.render(p)
        self.assertEqual(first['id'],second['id']);self.assertEqual(self.store.list('task')[1],1)
    def test_batch_version_and_output_ledgers_survive_edit_and_retry(self):
        p=self.project();task=self.render(p)
        self.assertTrue(task['batch_id'].startswith('vb_'));self.assertTrue(task['output_id'].startswith('vo_'))
        batch=self.store.get('batch',task['batch_id']);snapshot=self.store.get('project_version',task['project_version_id'])
        output=self.store.get('output',task['output_id'])
        self.assertEqual(batch['data']['task_ids'],[task['id']]);self.assertEqual(snapshot['data'],self.draft())
        self.assertEqual(output['task_id'],task['id']);self.assertEqual(output['status'],'pending')
        changed=self.draft();changed['dramas'][0]['clips'][1]['end_ms']=7000;changed['expected_version']=1
        self.service.save_project(changed,ACTOR,p['id']);self.service.mutate_asset(A,ACTOR,delete=True)
        retry=self.render(p)
        self.assertEqual((retry['batch_id'],retry['output_id'],retry['id']),(task['batch_id'],task['output_id'],task['id']))
        self.assertEqual(self.store.get('project_version',task['project_version_id'])['data'],self.draft())
        with self.assertRaises(VideoEditingError):
            self.service.render(p['id'],{'operation_id':'render_op_1','expected_version':2},ACTOR)
    def test_repeat_selected_asset_supported(self):
        draft=self.draft();extra=copy.deepcopy(draft['dramas'][0]['clips'][0]);extra['id']='again';draft['dramas'][0]['clips'].append(extra)
        p=self.service.save_project(draft,ACTOR);self.assertEqual(len(self.service.task(self.render(p)['id'],ACTOR)['data']['payload']['recipe']['clips']),3)
    def test_all_drama_submissions_atomic_on_bad_timeline(self):
        draft=self.draft();draft['dramas'].append({'app_id':'app','content_id':'other','title':'other','clips':[]})
        p=self.service.save_project(draft,ACTOR)
        with self.assertRaises(VideoEditingError):self.render(p)
        self.assertEqual(self.store.list('task')[1],0)
        for kind in ('batch','project_version','output'):self.assertEqual(self.store.list(kind)[1],0)
    def test_worker_unknown_does_not_create_second_job(self):
        p=self.project();task=self.render(p)
        another=self.project()
        self.service.render(another['id'],{'operation_id':'render_op_2','expected_version':1},ACTOR)
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
        output=self.store.get('output',task['output_id'])
        self.assertEqual(output['status'],'ready');self.assertEqual(output['data'],result)
        self.assertEqual(self.service.task_dto(self.service.task(task['id'],ACTOR))['output']['id'],output['id'])
        with self.assertRaises(VideoEditingError):self.service.resume(task['id'],ACTOR)
    def test_wrong_gpu_recipe_cannot_complete(self):
        p=self.project();task=self.render(p)
        worker=Dispatcher(self.service,lambda *_:{'job_id':task['id'],'status':'completed','result':{
            'job_id':task['id'],'task_type':'video_editing_v1','operation':'render','recipe_sha256':'0'*64}})
        worker.tick();self.assertEqual(self.service.task(task['id'],ACTOR)['status'],'unknown')
    def test_gpu_runtime_nested_error_retains_safe_code_and_message(self):
        p=self.project();task=self.render(p)
        worker=Dispatcher(self.service,lambda *_:{'job_id':task['id'],'status':'failed',
                          'error':{'code':'video_editing_source_changed','message':'SECRET_SIGNED_URL'}})
        worker.tick();result=self.service.task_dto(self.service.task(task['id'],ACTOR))
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error_code'],'video_editing_source_changed')
        self.assertNotIn('SECRET',result['error_message']);self.assertIn('原始视频',result['error_message'])
    def test_probe_reuses_active_and_completed_metadata(self):
        with self.store.transaction() as db:self.store.update('source',A,{'status':'uploaded'},db)
        a=self.service.enqueue_probe(A,ACTOR);b=self.service.enqueue_probe(A,ACTOR)
        self.assertEqual(a['data']['probe_task_id'],b['data']['probe_task_id']);self.assertEqual(self.store.list('task')[1],1)
    def test_runtime_intake_file_blocks_new_work_preserves_reads(self):
        project=self.project();task=self.render(project)
        changed=self.service.save_project({**self.draft(),'expected_version':1},ACTOR,project['id'])
        marker=Path(self.tmp.name)/'intake.disabled'
        with patch.dict(os.environ,{'VIDEO_EDITING_DISABLED_FILE':str(marker)}):
            self.assertTrue(self.service.config()['enabled']);marker.write_text('paused')
            self.assertFalse(self.service.config()['enabled'])
            with self.assertRaises(VideoEditingError):self.service.render(project['id'],{'operation_id':'new_render_2','expected_version':changed['version']},ACTOR)
            self.assertEqual(self.render(project)['id'],task['id'])
            self.assertEqual(self.service.render(project['id'],{'operation_id':'reload_while_closed','expected_version':1},ACTOR)['items'][0]['id'],task['id'])
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
    def test_user_resume_during_stale_poll_is_not_lost(self):
        for reply in ('failed','running','missing','transport_error'):
            with self.subTest(reply=reply):
                p=self.project()
                task=self.service.render(p['id'],{'operation_id':'race_'+reply,'expected_version':1},ACTOR)['items'][0]
                with self.store.transaction() as db:self.store.update('task',task['id'],{'status':'unknown'},db)
                def transport(method,path,payload):
                    self.service.resume(task['id'],ACTOR)
                    if reply=='transport_error':raise TimeoutError('must not log signed URL')
                    return None if reply=='missing' else {'job_id':task['id'],'status':reply}
                Dispatcher(self.service,transport).tick()
                self.assertEqual(self.service.task(task['id'],ACTOR)['status'],'resume_queued')
                with self.store.transaction() as db:
                    self.store.update('task',task['id'],{'status':'cancelled'},db)
                    db.execute('DELETE FROM ve_lease')

    def test_new_operations_reuse_every_state_of_the_same_saved_version(self):
        for status in ('queued','running','unknown','recovery_required','failed','completed'):
            with self.subTest(status=status):
                p=self.project();payload={'operation_id':'initial_'+status,'expected_version':1}
                first=self.service.render(p['id'],payload,ACTOR);tid=first['items'][0]['id']
                with self.store.transaction() as db:self.store.update('task',tid,{'status':status},db)
                again=self.service.render(p['id'],{'operation_id':'reload_'+status,'expected_version':1},ACTOR)
                self.assertEqual(again['batch_id'],first['batch_id'])
                self.assertEqual(again['items'][0]['id'],tid)
                self.assertEqual(again['items'][0]['output_id'],first['items'][0]['output_id'])
                self.assertEqual(again['items'][0]['status'],status)
        self.assertEqual(self.store.list('batch')[1],6)
        self.assertEqual(self.store.list('task')[1],6)
        self.assertEqual(self.store.list('output')[1],6)

    def test_concurrent_tabs_with_different_operations_create_one_batch(self):
        p=self.project();barrier=Barrier(2)
        def submit(index):
            barrier.wait(timeout=5)
            return self.service.render(p['id'],{'operation_id':'concurrent_tab_'+str(index),'expected_version':1},ACTOR)
        with ThreadPoolExecutor(max_workers=2) as pool:answers=list(pool.map(submit,(1,2)))
        self.assertEqual(answers[0]['batch_id'],answers[1]['batch_id'])
        for kind in ('task','batch','output','project_version'):self.assertEqual(self.store.list(kind)[1],1)
        with closing(self.store.connect()) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ve_render_operation').fetchone()[0],2)

    def test_editing_creates_new_version_but_old_operation_conflicts_remain(self):
        p=self.project();first=self.service.render(p['id'],{'operation_id':'original_submit','expected_version':1},ACTOR)
        changed=self.draft();changed['dramas'][0]['clips'][1]['end_ms']=7000
        self.service.save_project({**changed,'expected_version':1},ACTOR,p['id'])
        second=self.service.render(p['id'],{'operation_id':'edited_submit','expected_version':2},ACTOR)
        self.assertNotEqual(first['batch_id'],second['batch_id'])
        self.assertEqual(self.service.render(p['id'],{'operation_id':'reload_original','expected_version':1},ACTOR)['batch_id'],first['batch_id'])
        for op,version in (('original_submit',2),('reload_original',2)):
            with self.assertRaises(VideoEditingError) as caught:self.service.render(p['id'],{'operation_id':op,'expected_version':version},ACTOR)
            self.assertEqual(caught.exception.code,'operation_conflict')
        another=self.project()
        with self.assertRaises(VideoEditingError) as caught:self.service.render(another['id'],{'operation_id':'reload_original','expected_version':1},ACTOR)
        self.assertEqual(caught.exception.code,'operation_conflict')

    def test_mixed_batch_reuse_does_not_retry_failed_or_completed_tasks(self):
        draft=self.draft();other=copy.deepcopy(draft['dramas'][0]);other['content_id']='other';other['clips'][1]['source_id']=E2;draft['dramas'].append(other)
        p=self.service.save_project(draft,ACTOR)
        first=self.service.render(p['id'],{'operation_id':'mixed_initial','expected_version':1},ACTOR)
        complete,failed=[t['id'] for t in first['items']]
        with self.store.transaction() as db:
            self.store.update('task',complete,{'status':'completed'},db);self.store.update('task',failed,{'status':'failed'},db)
        again=self.service.render(p['id'],{'operation_id':'mixed_reload','expected_version':1},ACTOR)
        self.assertEqual([t['status'] for t in again['items']],['completed','failed'])
        self.service.resume(failed,ACTOR)
        self.assertEqual(self.service.task(complete,ACTOR)['status'],'completed')
        self.assertEqual(self.service.task(failed,ACTOR)['status'],'resume_queued')
        self.assertEqual(self.store.list('task')[1],2)
        with self.assertRaises(VideoEditingError):self.service.resume(complete,ACTOR)

    def test_admin_submission_records_project_owner_and_actual_submitter(self):
        p=self.project();first=self.service.render(p['id'],{'operation_id':'admin_submission','expected_version':1},ADMIN)
        task=self.service.task(first['items'][0]['id'],ACTOR)
        for kind,rid in (('task',task['id']),('batch',first['batch_id']),('output',task['data']['output_id'])):
            self.assertEqual(self.store.get(kind,rid)['owner'],owner(ACTOR))
        self.assertEqual(task['data']['submitted_by'],owner(ADMIN))
        self.assertEqual(self.store.get('batch',first['batch_id'])['data']['submitted_by'],owner(ADMIN))
        self.assertEqual(self.service.render(p['id'],{'operation_id':'owner_reload','expected_version':1},ACTOR)['batch_id'],first['batch_id'])
        with self.assertRaises(VideoEditingError):self.service.render(p['id'],{'operation_id':'unrelated_user','expected_version':1},OTHER)

    def test_same_operation_from_distinct_admins_does_not_collide_under_project_owner(self):
        one=self.project();two=self.project();admin2={**ADMIN,'user_id':'admin2'}
        first=self.service.render(one['id'],{'operation_id':'same_external_operation','expected_version':1},ADMIN)
        second=self.service.render(two['id'],{'operation_id':'same_external_operation','expected_version':1},admin2)
        self.assertNotEqual(first['batch_id'],second['batch_id'])
        self.assertNotEqual(self.store.get('batch',first['batch_id'])['operation_id'],self.store.get('batch',second['batch_id'])['operation_id'])

    def _legacy_batch(self,p,op,legacy_actor=ACTOR):
        response=self.service.render(p['id'],{'operation_id':op,'expected_version':1},legacy_actor)
        with self.store.transaction() as db:
            batch=self.store.get('batch',response['batch_id'],db)
            batch['data'].pop('request_operation_id',None);batch['data'].pop('submitted_by',None)
            db.execute('UPDATE ve_batch SET owner=?,operation_id=? WHERE id=?',(owner(legacy_actor),op,batch['id']))
            self.store.update('batch',batch['id'],{'data':batch['data']},db)
            for task in response['items']:
                db.execute('UPDATE ve_task SET owner=? WHERE id=?',(owner(legacy_actor),task['id']))
                db.execute('UPDATE ve_output SET owner=? WHERE id=?',(owner(legacy_actor),task['output_id']))
            db.execute('DELETE FROM ve_render_operation')
        return response

    def test_legacy_request_maps_once_and_preserves_its_batch_operation(self):
        p=self.project();first=self._legacy_batch(p,'legacy_submit')
        another=self.project()
        with self.assertRaises(VideoEditingError) as caught:self.service.render(another['id'],{'operation_id':'legacy_submit','expected_version':1},ACTOR)
        self.assertEqual(caught.exception.code,'operation_conflict')
        again=self.service.render(p['id'],{'operation_id':'legacy_submit','expected_version':1},ACTOR)
        alias=self.service.render(p['id'],{'operation_id':'new_browser_operation','expected_version':1},ACTOR)
        self.assertEqual(again['batch_id'],first['batch_id']);self.assertEqual(alias['batch_id'],first['batch_id'])
        self.assertEqual(self.store.get('batch',first['batch_id'])['operation_id'],'legacy_submit')
        with self.assertRaises(VideoEditingError):self.service.render(another['id'],{'operation_id':'legacy_submit','expected_version':1},ACTOR)

    def test_legacy_admin_owned_batch_cannot_leak_tasks_to_project_owner(self):
        p=self.project();first=self._legacy_batch(p,'legacy_admin_submit',ADMIN)
        with self.assertRaises(VideoEditingError) as caught:
            self.service.render(p['id'],{'operation_id':'owner_new_operation','expected_version':1},ACTOR)
        self.assertEqual(caught.exception.code,'batch_task_permission_conflict')
        self.assertEqual(caught.exception.status,409)
        with closing(self.store.connect()) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ve_render_operation').fetchone()[0],0)
        with self.assertRaises(VideoEditingError):self.service.task(first['items'][0]['id'],ACTOR)
        self.assertEqual(self.service.render(p['id'],{'operation_id':'admin_reload','expected_version':1},ADMIN)['batch_id'],first['batch_id'])
        self.assertEqual(self.store.get('task',first['items'][0]['id'])['owner'],owner(ADMIN))

    def test_legacy_duplicate_versions_keep_original_operations_and_new_ops_use_earliest(self):
        p=self.project();first=self._legacy_batch(p,'legacy_first')
        batch=self.store.get('batch',first['batch_id'])
        # Model an older release that accepted two operations for one version.
        later=copy.deepcopy(batch);later['id']='vb_legacy_later';later['operation_id']='legacy_later'
        task=copy.deepcopy(self.store.get('task',first['items'][0]['id']));task['id']='ve_'+'1'*32;task['operation_id']='legacy_later_0'
        task['data'].update(batch_id=later['id'],output_id='vo_'+'1'*32)
        output=copy.deepcopy(self.store.get('output',first['items'][0]['output_id']))
        output.update(id=task['data']['output_id'],task_id=task['id'],batch_id=later['id'])
        later['data']['task_ids']=[task['id']]
        with self.store.transaction() as db:
            self.store.insert('batch',later,db);self.store.insert('task',task,db);self.store.insert('output',output,db)
        alias=self.service.render(p['id'],{'operation_id':'fresh_browser','expected_version':1},ACTOR)
        original=self.service.render(p['id'],{'operation_id':'legacy_later','expected_version':1},ACTOR)
        self.assertEqual(alias['batch_id'],first['batch_id']);self.assertEqual(original['batch_id'],later['id'])
        self.assertEqual(original['items'][0]['id'],task['id'])
        self.assertEqual(self.store.list('batch')[1],2)
        self.assertEqual(self.store.get('batch',later['id'])['operation_id'],'legacy_later')


if __name__=='__main__':unittest.main()
