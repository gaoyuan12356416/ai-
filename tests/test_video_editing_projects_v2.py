import copy
from contextlib import closing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from features.video_editing import VideoEditingError
from features.video_editing.service import Service
from features.video_editing.store import Store,owner
from features.video_editing.uploads import Uploads

ACTOR={'tenant_key':'tenant','user_id':'one','is_admin':False}
OTHER={'tenant_key':'tenant','user_id':'two','is_admin':False}
A='va_'+'a'*32
B='va_'+'b'*32


class ProjectsV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db.sqlite3')
        self.service=Service(self.store,Uploads(self.store,None,'bucket','ap-hongkong'))
        for sid,code,category in ((A,'21481','free_episode'),(B,'21482','material')):
            with self.store.transaction() as db:
                self.store.insert('source',{'id':sid,'kind':'asset','owner':owner(ACTOR),'name':'Video','status':'ready',
                    'data':{'series_code':code,'english_title':'English Title','library_kind':category,
                        'url':'https://cdn.example.com/'+sid+'.mp4',
                        'identity':{'size_bytes':1234,'etag':'abcd','bucket':'bucket','key':'video-editing/v1/'+sid},
                        'metadata':{'duration_ms':10000,'width':720,'height':1280,'has_audio':True,'fps':30}}},db)

    def unit(self,uid='unit_a',sid=A,code='21481'):
        return {'id':uid,'series_code':code,'title':'Title','clips':[
            {'id':'clip_a','source_id':sid,'start_ms':0,'end_ms':2000,'fit':'contain','mute':False,'transition':'cut'}]}

    def save(self,units=None):
        return self.service.save_project({'schema_version':2,'name':'Draft','units':units if units is not None else [self.unit()]},ACTOR)

    def render(self,project,op='render_op_1',ids=None,actor=ACTOR):
        data={'expected_version':project['version'],'operation_id':op}
        if ids is not None:data['unit_ids']=ids
        return self.service.render(project['id'],data,actor)

    def test_incomplete_and_incorrect_ranges_save_as_issues(self):
        unit=self.unit();unit['series_code']='';unit['clips'][0].update(start_ms=5000,end_ms=2)
        draft=self.save([unit,{'id':'unit_empty','series_code':'','title':'','clips':[]}])
        self.assertEqual(draft['schema_version'],2)
        self.assertTrue({'resource_required','clip_range_invalid','timeline_empty'} <= {x['code'] for x in draft['issues']})
        self.assertEqual(self.render(draft)['items'],[])

    def test_missing_ids_normalize_stably_for_creation_replay(self):
        unit=self.unit();unit.pop('id');unit['clips'][0].pop('id')
        payload={'schema_version':2,'name':'Draft','units':[unit],'operation_id':'create_without_ids'}
        first=self.service.save_project(payload,ACTOR)
        second=self.service.save_project(payload,ACTOR)
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(first['units'],second['units'])

    def test_free_episode_only_and_material_only_render_separately(self):
        draft=self.save([self.unit(),self.unit('unit_b',B,'21482')])
        response=self.render(draft)
        self.assertEqual(len(response['items']),2)
        self.assertEqual({x['unit_id'] for x in response['items']},{'unit_a','unit_b'})
        self.assertEqual(response['issues'],[])
        task=self.store.get('task',response['items'][0]['id'])
        self.assertEqual(task['data']['payload']['recipe']['version'],1)
        self.assertEqual(task['data']['source_attribution'][0]['library_kind'],'free_episode')

    def test_invalid_unit_does_not_block_valid_unit_and_retries_locally(self):
        with self.store.transaction() as db:self.store.update('source',B,{'status':'probing'},db)
        draft=self.save([self.unit(),self.unit('unit_b',B,'21482')])
        first=self.render(draft)
        self.assertEqual(len(first['items']),1)
        self.assertEqual(first['issues'][0]['unit_id'],'unit_b')
        with self.store.transaction() as db:self.store.update('source',B,{'status':'ready'},db)
        self.assertEqual(self.render(draft)['results'],first['results'])
        second=self.render(draft,'render_retry_2',['unit_b'])
        self.assertEqual(len(second['items']),1)
        all_again=self.render(draft,'render_all_again')
        self.assertEqual(len(all_again['items']),2)
        self.assertTrue(all(x['status']=='reused' for x in all_again['results']))
        self.assertEqual(self.store.list('task')[1],2)

    def test_resource_mismatch_is_render_local_issue_not_save_gate(self):
        draft=self.save([self.unit('unit_bad',B),self.unit()])
        self.assertIn('clip_resource_mismatch',{x['code'] for x in draft['issues']})
        response=self.render(draft)
        self.assertEqual(len(response['items']),1)
        self.assertEqual(response['results'][0]['status'],'invalid')

    def test_recipe_error_does_not_stop_other_unit(self):
        bad=self.unit('unit_bad');bad['clips'][0]['end_ms']=999999
        draft=self.save([bad,self.unit('unit_b',B,'21482')])
        response=self.render(draft)
        self.assertEqual([x['unit_id'] for x in response['items']],['unit_b'])
        self.assertEqual(response['issues'][0]['unit_id'],'unit_bad')

    def test_missing_source_reference_saved_without_disclosing_private_source(self):
        with self.store.transaction() as db:self.store.update('source',B,{'status':'uploading'},db)
        draft=self.service.save_project({'schema_version':2,'name':'Other','units':[self.unit('unit_b',B,'21482')]},OTHER)
        self.assertEqual(draft['issues'][0]['code'],'source_unavailable')
        response=self.render(draft,actor=OTHER)
        self.assertEqual(response['items'],[])

    def test_tags_and_later_edits_do_not_rewrite_frozen_or_recreate(self):
        draft=self.save();first=self.render(draft)['items'][0]
        frozen=copy.deepcopy(self.store.get('task',first['id'])['data'])
        source=self.store.get('source',A);source['data']['series_code']='changed'
        with self.store.transaction() as db:self.store.update('source',A,{'data':source['data']},db)
        self.assertEqual(self.render(draft,'another_op')['items'][0]['id'],first['id'])
        self.service.save_project({'schema_version':2,'name':'Later','units':[],'expected_version':1},ACTOR,draft['id'])
        self.assertEqual(self.render(draft,'old_version_op')['items'][0]['id'],first['id'])
        self.assertEqual(self.store.get('task',first['id'])['data'],frozen)

    def test_same_operation_different_selection_conflicts(self):
        draft=self.save([self.unit(),self.unit('unit_b',B,'21482')])
        self.render(draft,ids=['unit_a'])
        with self.assertRaises(VideoEditingError) as caught:self.render(draft,ids=['unit_b'])
        self.assertEqual(caught.exception.code,'operation_conflict')

    def test_concurrent_requests_share_one_unit_task(self):
        draft=self.save();barrier=Barrier(2)
        def submit(index):
            barrier.wait();return self.render(draft,'parallel_'+str(index))['items'][0]['id']
        with ThreadPoolExecutor(max_workers=2) as executor:ids=list(executor.map(submit,[1,2]))
        self.assertEqual(ids[0],ids[1]);self.assertEqual(self.store.list('task')[1],1)

    def test_source_change_during_preflight_affects_only_its_unit(self):
        from features.video_editing import projects_v2
        from unittest.mock import patch
        original=projects_v2.compile_unit
        def compile_changed(service,unit,actor):
            result=original(service,unit,actor)
            if unit['id']=='unit_a':
                row=self.store.get('source',A);row['data']['series_code']='other'
                with self.store.transaction() as db:self.store.update('source',A,{'data':row['data']},db)
            return result
        draft=self.save([self.unit(),self.unit('unit_b',B,'21482')])
        with patch.object(projects_v2,'compile_unit',side_effect=compile_changed):response=self.render(draft)
        self.assertEqual(response['issues'][0]['code'],'source_changed')
        self.assertEqual([x['unit_id'] for x in response['items']],['unit_b'])

    def test_permission_and_version_remain_enforced(self):
        draft=self.save()
        with self.assertRaises(VideoEditingError):self.render(draft,actor=OTHER)
        with self.assertRaises(VideoEditingError):self.service.save_project({'schema_version':2,'name':'Edit','units':[],'expected_version':99},ACTOR,draft['id'])

    def test_source_classification_indexes_follow_updates(self):
        self.assertEqual(self.service.assets(ACTOR,library_kind='free_episode',series_code='21481')['total'],1)
        self.assertEqual(self.service.assets(ACTOR,library_kind='material',series_code='21481')['total'],0)
        row=self.store.get('source',A);row['data'].update(library_kind='material',series_code='21482')
        with self.store.transaction() as db:self.store.update('source',A,{'data':row['data']},db)
        self.assertEqual(self.service.assets(ACTOR,library_kind='material',series_code='21482')['total'],2)

    def test_legacy_source_migration_preserves_original_data_and_timestamps(self):
        import sqlite3,json
        path=Path(self.tmp.name)/'legacy.sqlite3'
        value=json.dumps({'identity':{'key':'old'},'metadata':{}},sort_keys=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE ve_source(id TEXT PRIMARY KEY,kind TEXT,owner TEXT,name TEXT,status TEXT,data TEXT,created_at INTEGER,updated_at INTEGER)')
            db.execute('INSERT INTO ve_source VALUES(?,?,?,?,?,?,?,?)',(A,'asset',owner(ACTOR),'Original','ready',value,1,2))
            db.commit()
        migrated=Store(path);row=migrated.get('source',A)
        self.assertEqual(row['library_kind'],'material');self.assertEqual(row['series_code'],'')
        with closing(migrated.connect()) as db:
            self.assertEqual(db.execute('SELECT data,created_at,updated_at FROM ve_source').fetchone()[:],(value,1,2))
        Store(path)
        self.assertEqual(migrated.get('source',A)['updated_at'],2)


if __name__=='__main__':unittest.main()
