"""Sanitized fixtures: no production materials, credentials or audit files."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from features.video_editing import VideoEditingError
from features.video_editing.material_replication import (
    MATERIAL_CATEGORIES, MaterialReplicationWorker, ReplicationCatalog,
    b_key, canonical_request, digest, intent, receipt,
)
from features.video_editing.service import Service
from features.video_editing.store import Store, owner
from features.video_editing.uploads import Uploads

TASK = 've_'+'a'*32
OUTPUT = 'vo_'+'a'*32
CREATOR = {'tenant_key':'tenant','user_id':'creator','email':'creator@example.test','is_admin':False}
ADMIN = {'tenant_key':'tenant','user_id':'admin','email':'admin@example.test','is_admin':True}
OTHER = {'tenant_key':'tenant','user_id':'other','is_admin':False}
ARTIFACT = {'bucket':'private-test','key':'video-editing/v1/output.mp4','sha256':'a'*64,'size_bytes':128,'duration_ms':60000}


def accepted(request, unsupported=()):
    effective = [code for code in request['target_langs'] if code not in unsupported]
    return {'code':200,'message':'success','data':{'operator':request['operator'],'status':'waiting_download',
        'task_id':101,'task_material_ids':[202],'series_langs':request['series_langs'],
        'target_langs':effective,'unsupported_langs':list(unsupported),
        'items':[{'material_id':1001,'task_material_id':202,'status':'waiting_download','err_msg':''}]}}


class Http:
    def __init__(self):
        self.calls=[];self.response=None;self.exception=None;self.http_status=200

    def post(self,url,**kwargs):
        self.calls.append((url,copy.deepcopy(kwargs)))
        if self.exception:
            raise self.exception
        body = accepted(kwargs['json']) if self.response is None else self.response
        return SimpleNamespace(status_code=self.http_status,json=lambda:body)


class MaterialReplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.marker=Path(self.tmp.name)/'disabled'
        patch=mock.patch.dict(os.environ,{'VIDEO_EDITING_MATERIAL_REPLICATION_ENABLED':'1',
            'VIDEO_EDITING_MATERIAL_REPLICATION_DISABLED_FILE':str(self.marker),
            'A_GROUP_TASK_CREATE_BASE_URL':'https://marketing.mydramawave.com',
            'A_GROUP_TASK_CREATE_TOKEN':'private-unit-test-token'})
        patch.start();self.addCleanup(patch.stop)
        self.store=Store(Path(self.tmp.name)/'editing.sqlite3')
        self.users_path=Path(self.tmp.name)/'users.sqlite3'
        with closing(sqlite3.connect(self.users_path)) as db:
            db.execute('CREATE TABLE drama_admin_user(tenant_key TEXT,user_id TEXT,email TEXT,name TEXT)')
            db.executemany('INSERT INTO drama_admin_user VALUES(?,?,?,?)',[
                ('tenant','creator','creator@example.test','Original Creator'),
                ('other-tenant','creator','wrong@example.test','Different Tenant'),
                ('tenant','admin','admin@example.test','Admin')])
            db.commit()
        app=SimpleNamespace(JOB_DB_LOCK=threading.Lock(),get_job_db_connection=lambda:sqlite3.connect(self.users_path))
        self.service=Service(self.store,Uploads(self.store,None,'private-test','test','video-editing/v1'),app=app)
        self.service.catalog=mock.Mock()
        self.service.catalog.variant.return_value={'series_code':'55','language':'en'}
        self.service.catalog.resource_variants.return_value=[{'series_code':'55','language':'es'},
            {'series_code':'55','language':'ES'},{'series_code':'55','language':'zh-tw'},
            {'series_code':'99','language':'fr'}]
        self.material=self.service.material_replication
        self.material.catalog=mock.Mock()
        self.material.catalog.operator.return_value='original_editor'
        self.http=Http();self.worker=MaterialReplicationWorker(self.material,self.http)
        self.insert_task()

    def insert_task(self):
        data={'output_id':OUTPUT,'app_id':'1479','content_id':'TEST_EN','drama_title':'Test drama',
              'result':{'artifact':dict(ARTIFACT)}}
        with self.store.transaction() as db:
            self.store.insert('task',{'id':TASK,'owner':owner(CREATOR),'project_id':'vp_test',
                'operation_id':'render-test','kind':'render','status':'completed','data':data},db)
            self.store.insert('output',{'id':OUTPUT,'owner':owner(CREATOR),'task_id':TASK,'batch_id':'vb_test',
                'status':'ready','data':data['result']},db)
            self.store.insert('material_upload',{'id':OUTPUT,'output_id':OUTPUT,'task_id':TASK,'owner':owner(ADMIN),
                'status':'succeeded','data':{'material_id':'1001','artifact':dict(ARTIFACT),
                'request':{'app_id':3214,'content_id':'TEST_EN','language':'en','name':'Test material.mp4',
                           'url':'https://materials.example.test/test.mp4'}}},db)

    def payload(self, **changes):
        return dict({'operation_id':'operation-test-01','task_type':1,'audio_type':0,
                     'target_material_type':1,'target_langs':['es'],'no_bgm':False},**changes)

    def enqueue(self, **changes):
        return self.material.enqueue(TASK,CREATOR,self.payload(**changes))

    def row(self, item):
        return self.store.get('material_replication',item['id'])

    def change(self,kind,record_id,**values):
        with self.store.transaction() as db:
            self.store.update(kind,record_id,values,db)

    def test_context_uses_original_creator_and_source_product_never_upload_target(self):
        item=self.material.context(TASK,ADMIN)
        self.assertTrue(item['enabled']);self.assertTrue(item['can_change_operator'])
        self.assertEqual(item['default_operator'],'original_editor')
        self.material.catalog.operator.assert_called_once_with('creator@example.test')
        self.service.catalog.variant.assert_called_once_with('1479','TEST_EN',series_code=None)
        self.assertEqual(item['source'],{'material_id':1001,'name':'Test material.mp4','resource_id':55,
                                        'source_language':'en','duration_ms':60000})
        self.assertEqual([v['code'] for v in item['target_languages']],['en','es','zh-TW'])

    def test_frozen_v2_variant_uses_catalog_resource_and_upload_language(self):
        task=self.store.get('task',TASK);task['data'].update(schema_version=2,series_code='55')
        task['data'].pop('app_id');task['data'].pop('content_id')
        upload=self.store.get('material_upload',OUTPUT)
        upload['data']['source_variant']={'app_id':'979','content_id':'SELECTED_EN','series_code':'55','language':'en'}
        self.change('task',TASK,data=task['data']);self.change('material_upload',OUTPUT,data=upload['data'])
        item=self.material.context(TASK,CREATOR)
        self.assertTrue(item['enabled'])
        self.service.catalog.variant.assert_called_once_with('979','SELECTED_EN',series_code='55')

    def test_missing_or_ambiguous_owner_never_falls_back(self):
        with closing(sqlite3.connect(self.users_path)) as db:
            db.execute("DELETE FROM drama_admin_user WHERE tenant_key='tenant' AND user_id='creator'");db.commit()
        item=self.material.context(TASK,CREATOR)
        self.assertFalse(item['enabled']);self.assertEqual(item['default_operator'],'')
        self.material.catalog.operator.assert_not_called()
        with self.assertRaises(VideoEditingError):self.enqueue()
        # An admin can explicitly correct the account even when owner metadata is absent.
        item=self.material.context(TASK,ADMIN)
        self.assertTrue(item['enabled']);self.assertTrue(item['operator_error'])
        created=self.material.enqueue(TASK,ADMIN,self.payload(operator='reviewed_editor'))
        self.assertEqual(created['operator'],'reviewed_editor')

    def test_non_admin_cannot_change_operator_or_access_other_task(self):
        with self.assertRaises(VideoEditingError) as caught:self.enqueue(operator='other_editor')
        self.assertEqual(caught.exception.status,403)
        for action in (lambda:self.material.context(TASK,OTHER),lambda:self.material.enqueue(TASK,OTHER,self.payload())):
            with self.assertRaises(VideoEditingError) as caught:action()
            self.assertEqual(caught.exception.status,404)

    def test_public_dto_and_task_list_never_query_remote_catalog_or_credentials(self):
        item=self.enqueue()
        with mock.patch.object(self.service.catalog,'variant',side_effect=AssertionError('external query')), \
             mock.patch.object(self.material.catalog,'operator',side_effect=AssertionError('external query')), \
             mock.patch.object(MaterialReplicationWorker,'settings',side_effect=AssertionError('secret access')):
            dto=self.service.task_dto(self.store.get('task',TASK))
            context=self.material.context(TASK,CREATOR,history_only=True)
        self.assertEqual(dto['material_replication']['count'],1)
        self.assertEqual(context['history'][0]['id'],item['id'])
        text=json.dumps(context)
        self.assertNotIn('private-unit-test-token',text);self.assertNotIn('materials.example.test',text)
        self.assertNotIn('request_hash',text)

    def test_catalog_failure_preserves_history(self):
        item=self.enqueue()
        self.service.catalog.variant.side_effect=RuntimeError('catalog unavailable')
        context=self.material.context(TASK,CREATOR)
        self.assertFalse(context['enabled']);self.assertTrue(context['disabled_reason'])
        self.assertEqual(context['summary']['id'],item['id'])
        self.assertEqual(len(context['history']),1)

    def test_pending_source_or_mismatched_output_is_not_eligible(self):
        self.change('material_upload',OUTPUT,status='verifying')
        self.assertFalse(self.material.for_task(self.store.get('task',TASK))['enabled'])
        with self.assertRaises(VideoEditingError):self.enqueue()
        self.change('material_upload',OUTPUT,status='succeeded')
        data=self.store.get('output',OUTPUT)['data'];data['artifact']['sha256']='b'*64
        self.change('output',OUTPUT,data=data)
        with self.assertRaises(VideoEditingError):self.enqueue()

    def test_server_builds_request_and_rejects_client_source_fields(self):
        with self.assertRaises(VideoEditingError):self.enqueue(resource_id=999)
        item=self.enqueue()
        request=self.row(item)['data']['request']
        self.assertEqual(request['resource_id'],55);self.assertEqual(request['series_langs'],['en'])
        self.assertEqual(request['orig_materials'][0],{'material_id':1001,'name':'Test material.mp4',
            'url':'https://materials.example.test/test.mp4','duration':60000})
        self.assertNotIn('descant_start_time',request)
        self.assertEqual(self.http.calls,[])

    def test_frozen_material_name_is_never_truncated_or_rewritten(self):
        upload=self.store.get('material_upload',OUTPUT)
        upload['data']['request']['name']='  Frozen name.mp4  '
        self.change('material_upload',OUTPUT,data=upload['data'])
        item=self.enqueue()
        self.assertEqual(self.row(item)['data']['request']['orig_materials'][0]['name'],'  Frozen name.mp4  ')
        upload['data']['request']['name']='x'*129
        self.change('material_upload',OUTPUT,data=upload['data'])
        with self.assertRaises(VideoEditingError) as caught:self.enqueue(operation_id='too-long-name-02')
        self.assertEqual(caught.exception.code,'replication_name_too_long')

    def test_all_documented_task_audio_choices_and_source_target_allowed(self):
        for task_type in (1,3,4):
            for audio_type in (0,1,2):
                item=self.enqueue(operation_id='choice-%s-%s'%(task_type,audio_type),task_type=task_type,audio_type=audio_type,target_langs=['en'])
                self.assertEqual(item['status'],'pending')
        self.assertEqual(self.material.for_task(self.store.get('task',TASK))['count'],9)

    def test_invalid_enums_languages_and_boolean_types_rejected(self):
        for changes in ({'task_type':2},{'task_type':True},{'audio_type':'1'},{'audio_type':3},
                        {'target_material_type':0},{'no_bgm':1},{'target_langs':[]},{'target_langs':['fr']}):
            with self.subTest(changes=changes),self.assertRaises(VideoEditingError):self.enqueue(**changes)

    def test_all_material_categories_pass_through_without_changing_production_options(self):
        categories=('解说','二创','混剪','精剪','切片','image','Dramawave Post')
        self.assertEqual(MATERIAL_CATEGORIES,categories)
        for index,category in enumerate(categories):
            with self.subTest(category=category):
                task_type=(1,3,4)[index//3];audio_type=index%3
                item=self.enqueue(operation_id='category-%s-request'%index,material_category=category,
                                  task_type=task_type,audio_type=audio_type)
                self.assertEqual(item['material_category'],category)
                frozen=copy.deepcopy(self.row(item)['data']['request'])
                self.assertEqual(canonical_request(frozen),frozen)
                self.assertTrue(self.worker.tick())
                sent=self.http.calls[-1][1]['json']
                self.assertEqual(sent,frozen)
                self.assertEqual(sent['material_category'],category)
                self.assertEqual((sent['task_type'],sent['audio_type'],sent['target_material_type']),
                                 (task_type,audio_type,1))
                self.assertNotIn('descant_start_time',sent)
                self.assertEqual(sent['orig_materials'][0]['name'],'Test material.mp4')
                self.assertEqual(self.material.history(TASK)[0]['material_category'],category)
        self.assertEqual(len(self.http.calls),7)

    def test_material_category_rejects_aliases_coercion_and_whitespace(self):
        for value in ('图片','Image','IMAGE','mix',' 混剪','混剪 ','Dramawave post','',None,0,True,[],{}):
            with self.subTest(value=value),self.assertRaises(VideoEditingError) as caught:
                self.enqueue(material_category=value)
            self.assertEqual(caught.exception.code,'replication_material_category_invalid')
            with self.assertRaises(VideoEditingError):
                canonical_request({'series_langs':['en'],'target_langs':['es'],'material_category':value})
        self.assertEqual(self.material.history(TASK),[])
        self.assertEqual(self.http.calls,[])

    def test_legacy_operation_and_request_hashes_keep_omitted_category(self):
        _,params=intent(self.payload())
        self.assertNotIn('material_category',params)
        self.assertEqual(digest(params),'15b1e850225e59c510fe7a37d37575dbc575ab684a490d6f09fb43b9e73c0eb7')
        item=self.enqueue();row=self.row(item)
        self.assertEqual(row['request_hash'],'a7be4990d0765f8281a9b4730661afadf711255d5a16acc14192b9eba42837bc')
        with closing(self.store.connect()) as db:
            stored_hash=db.execute('SELECT payload_hash FROM ve_material_replication_operation WHERE operation_id=?',
                                   ('operation-test-01',)).fetchone()[0]
        self.assertEqual(stored_hash,'15b1e850225e59c510fe7a37d37575dbc575ab684a490d6f09fb43b9e73c0eb7')
        self.assertIsNone(item['material_category'])
        self.assertNotIn('material_category',row['data']['request'])
        self.assertNotIn('material_category',canonical_request(row['data']['request']))
        self.service.catalog.variant.side_effect=AssertionError('legacy replay must not requery')
        self.marker.touch()
        replay=self.enqueue()
        self.assertEqual(replay['id'],item['id']);self.assertTrue(replay['reused'])
        self.assertIsNone(replay['material_category'])

    def test_legacy_pending_request_does_not_gain_category_at_dispatch(self):
        item=self.enqueue();before=self.row(item)
        self.worker.tick()
        self.assertEqual(self.http.calls[0][1]['json'],before['data']['request'])
        self.assertNotIn('material_category',self.http.calls[0][1]['json'])
        self.assertEqual(self.row(item)['data']['request'],before['data']['request'])
        self.assertEqual(self.row(item)['request_hash'],before['request_hash'])
        self.assertIsNone(self.material.history(TASK)[0]['material_category'])

    def test_category_change_with_same_operation_is_409(self):
        self.enqueue(material_category='混剪')
        with self.assertRaises(VideoEditingError) as caught:self.enqueue(material_category='精剪')
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(caught.exception.code,'replication_operation_conflict')
        with self.assertRaises(VideoEditingError):self.enqueue()
        self.assertEqual(len(self.material.history(TASK)),1)
        self.assertEqual(self.http.calls,[])

    def test_category_change_same_b_key_conflicts_without_new_record(self):
        first=self.enqueue(material_category='混剪')
        request=self.row(first)['data']['request']
        changed={**request,'material_category':'精剪'}
        self.assertEqual(b_key(request),b_key(changed))
        self.assertNotEqual(digest(request),digest(changed))
        result=self.enqueue(operation_id='different-category',material_category='精剪')
        self.assertEqual(result['id'],first['id']);self.assertTrue(result['conflict'])
        self.assertEqual(result['material_category'],'混剪')
        self.assertEqual(result['conflict_fields'],[{'field':'material_category','previous':'混剪','requested':'精剪'}])
        self.assertEqual(len(self.material.history(TASK)),1)
        replay=self.enqueue(operation_id='different-category',material_category='精剪')
        self.assertEqual(replay['conflict_fields'],result['conflict_fields'])
        self.assertEqual(self.http.calls,[])

    def test_legacy_missing_category_is_not_relabelled_as_explicit_default(self):
        first=self.enqueue();before=self.row(first)
        result=self.enqueue(operation_id='explicit-new-default',material_category='混剪')
        self.assertEqual(result['id'],first['id']);self.assertTrue(result['conflict'])
        self.assertIsNone(result['material_category'])
        self.assertEqual(result['conflict_fields'],[{'field':'material_category','previous':None,'requested':'混剪'}])
        self.assertEqual(self.row(first),before)
        self.assertEqual(len(self.material.history(TASK)),1)
        with self.assertRaises(VideoEditingError) as caught:self.enqueue(material_category='混剪')
        self.assertEqual(caught.exception.status,409)

    def test_legacy_import_fingerprint_and_receipt_are_unchanged(self):
        pending=self.enqueue();request=self.row(pending)['data']['request']
        with self.store.transaction() as db:
            db.execute('DELETE FROM ve_material_replication_operation');db.execute('DELETE FROM ve_material_replication')
        body=accepted(request)
        legacy_fingerprint=digest({'request':request,'response':body,'submitted_at':200,'override':None})
        imported=self.material.import_history(TASK,request,body,import_source='legacy-category-absent#sha',submitted_at=200)
        before=self.row(imported)
        self.assertEqual(before['data']['import_fingerprint'],legacy_fingerprint)
        self.assertEqual(before['request_hash'],'a7be4990d0765f8281a9b4730661afadf711255d5a16acc14192b9eba42837bc')
        self.assertNotIn('material_category',before['data']['request'])
        replay=self.material.import_history(TASK,request,body,import_source='legacy-category-absent#sha',submitted_at=200)
        self.assertEqual(replay['id'],imported['id']);self.assertTrue(replay['reused'])
        self.assertIsNone(replay['material_category']);self.assertEqual(self.row(imported),before)
        self.assertEqual(self.http.calls,[])

    def test_narration_time_format_order_and_duration(self):
        for start,end in (('0:00:01','00:00:03'),('00:00:20','00:00:10'),('00:00:00','00:01:00.001')):
            with self.subTest(start=start,end=end),self.assertRaises(VideoEditingError):
                self.enqueue(target_material_type=2,descant_start_time=start,descant_end_time=end)
        item=self.enqueue(target_material_type=2,descant_start_time='00:00:01.5',descant_end_time='00:00:50.000',no_bgm=True)
        self.assertEqual(item['descant_start_time'],'00:00:01.500')
        self.assertEqual(item['descant_end_time'],'00:00:50')
        self.assertTrue(item['no_bgm'])

    def test_operation_id_replay_never_needs_catalog_and_changed_intent_is_409(self):
        item=self.enqueue()
        self.service.catalog.variant.side_effect=AssertionError('must not query')
        self.marker.touch()
        replay=self.enqueue()
        self.assertEqual(replay['id'],item['id']);self.assertTrue(replay['reused'])
        with self.assertRaises(VideoEditingError) as caught:self.enqueue(audio_type=1)
        self.assertEqual(caught.exception.status,409)

    def test_same_request_new_operation_reuses_and_records_alias(self):
        item=self.enqueue(target_langs=['es','en'])
        replay=self.enqueue(operation_id='operation-test-02',target_langs=['en','es','en'])
        self.assertEqual(replay['id'],item['id']);self.assertTrue(replay['reused'])
        self.assertIn('operation-test-02',self.material.history(TASK)[0]['operation_ids'])
        self.assertEqual(replay['submitted_operation_id'],'operation-test-02')
        with self.assertRaises(VideoEditingError) as caught:self.enqueue(operation_id='operation-test-02',no_bgm=True)
        self.assertEqual(caught.exception.status,409)

    def test_b_key_conflict_records_original_and_does_not_queue(self):
        item=self.enqueue()
        conflict=self.material.enqueue(TASK,ADMIN,self.payload(operation_id='override-account-01',operator='reviewed_editor',
            target_material_type=2,descant_start_time='00:00:00',descant_end_time='00:00:30',no_bgm=True))
        self.assertEqual(conflict['id'],item['id']);self.assertTrue(conflict['conflict'])
        self.assertEqual({v['field'] for v in conflict['conflict_fields']},
                         {'operator','target_material_type','descant_start_time','descant_end_time','no_bgm'})
        self.assertEqual(len(self.material.history(TASK)),1)
        again=self.material.enqueue(TASK,ADMIN,self.payload(operation_id='override-account-01',operator='reviewed_editor',
            target_material_type=2,descant_start_time='00:00:00',descant_end_time='00:00:30',no_bgm=True))
        self.assertEqual(again['conflict_fields'],conflict['conflict_fields'])

    def test_other_b_keys_allowed_without_global_material_block(self):
        first=self.enqueue()
        for changes in ({'audio_type':1},{'task_type':3},{'target_langs':['es','en']}):
            result=self.enqueue(operation_id='new-'+str(len(self.material.history(TASK)))+'-request',**changes)
            self.assertNotEqual(result['id'],first['id'])
        self.assertEqual(len(self.material.history(TASK)),4)

    def test_concurrent_duplicate_clicks_create_one_record(self):
        def submit(index):
            return self.enqueue(operation_id='parallel-operation-%s'%index)
        with ThreadPoolExecutor(max_workers=6) as executor:
            result=list(executor.map(submit,range(6)))
        self.assertEqual(len({item['id'] for item in result}),1)
        self.assertEqual(len(self.material.history(TASK)),1)
        self.assertEqual(len(self.material.history(TASK)[0]['operation_ids']),6)

    def test_worker_acceptance_and_response_is_not_completion(self):
        item=self.enqueue();self.assertTrue(self.worker.tick())
        result=self.material.history(TASK)[0]
        self.assertEqual(result['status'],'accepted');self.assertEqual(result['upstream_status'],'waiting_download')
        self.assertEqual(result['b_task_id'],101);self.assertEqual(result['b_task_material_ids'],[202])
        self.assertEqual(len(self.http.calls),1);self.assertFalse(self.worker.tick())
        self.assertEqual(self.row(item)['data']['attempts'],1)
        self.assertNotIn('private-unit-test-token',json.dumps(self.row(item)))
        self.assertFalse(self.http.calls[0][1]['allow_redirects'])
        self.assertEqual(self.http.calls[0][1]['timeout'],(5,60))
        self.assertEqual(self.http.calls[0][1]['headers'],{'token':'private-unit-test-token'})
        ledger=self.row(item)['data']
        self.assertEqual(ledger['http_status'],200)
        self.assertEqual(ledger['response_body'],accepted(ledger['request']))
        self.assertEqual(ledger['attempt_started_at'],ledger['submitted_at'])
        self.assertGreaterEqual(ledger['response_received_at'],ledger['attempt_started_at'])
        self.assertNotIn('response_body',result)

    def test_partial_acceptance_preserves_requested_effective_unsupported(self):
        item=self.enqueue(target_langs=['es','zh-TW'])
        self.http.response=accepted(self.row(item)['data']['request'],['zh-TW'])
        self.worker.tick()
        result=self.material.history(TASK)[0]
        self.assertEqual(result['status'],'partial');self.assertEqual(result['target_langs'],['es','zh-TW'])
        self.assertEqual(result['effective_langs'],['es']);self.assertEqual(result['unsupported_langs'],['zh-TW'])

    def test_explicit_rejection_can_be_manually_resubmitted_with_new_operation(self):
        item=self.enqueue();self.http.response={'code':400,'message':'account not bound'};self.worker.tick()
        self.assertEqual(self.material.history(TASK)[0]['status'],'rejected')
        self.assertEqual(self.enqueue()['id'],item['id'])
        next_item=self.enqueue(operation_id='manually-retry-02')
        self.assertNotEqual(next_item['id'],item['id']);self.assertEqual(next_item['status'],'pending')

    def test_timeout_unknown_is_never_reposted_or_automatically_retried(self):
        item=self.enqueue();self.http.exception=TimeoutError('lost response')
        self.worker.tick()
        self.assertEqual(self.material.history(TASK)[0]['status'],'unknown_outcome')
        for _ in range(3):self.worker.tick()
        reused=self.enqueue(operation_id='retry-unknown-02')
        self.assertEqual(reused['id'],item['id']);self.assertTrue(reused['reused'])
        self.assertEqual(len(self.http.calls),1)

    def test_http_500_malformed_or_mismatched_success_remain_unknown(self):
        request=self.row(self.enqueue())['data']['request']
        responses=[{'code':500,'message':'internal'},'invalid',{'code':200,'data':{}},accepted(request)]
        responses[-1]['data']['operator']='wrong-owner'
        for response in responses:
            with self.subTest(response=response):self.assertEqual(receipt(request,response)[0],'unknown_outcome')
        self.assertEqual(receipt(request,accepted(request),500)[0],'unknown_outcome')

    def test_duplicate_may_return_current_material_status_as_acceptance_snapshot(self):
        item=self.enqueue();request=self.row(item)['data']['request']
        self.http.response=accepted(request)
        self.http.response['data']['status']='duplicate'
        self.http.response['data']['items'][0]['status']='completed'
        self.worker.tick()
        result=self.material.history(TASK)[0]
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(result['upstream_status'],'duplicate')
        self.assertEqual(result['upstream_item_status'],'completed')
        self.assertEqual(self.row(item)['data']['response_body'],self.http.response)

    def test_malformed_json_keeps_sanitized_body_and_never_retries(self):
        item=self.enqueue()
        response=SimpleNamespace(status_code=502,text='<html>private-unit-test-token https://private.example.test/x</html>')
        response.json=mock.Mock(side_effect=ValueError('not JSON'))
        self.http.post=mock.Mock(return_value=response)
        self.worker.tick();self.worker.tick()
        data=self.row(item)['data']
        self.assertEqual(self.row(item)['status'],'unknown_outcome')
        self.assertEqual(data['http_status'],502)
        self.assertIsInstance(data['response_body'],str)
        self.assertNotIn('private-unit-test-token',data['response_body'])
        self.assertNotIn('https://',data['response_body'])
        self.http.post.assert_called_once()

    def test_crash_after_outbound_marker_does_not_issue_second_post(self):
        item=self.enqueue()
        with mock.patch.object(self.worker,'finish',side_effect=RuntimeError('storage write interrupted')):
            with self.assertRaises(RuntimeError):self.worker.tick()
        self.assertEqual(self.row(item)['status'],'submitting')
        self.worker.tick()
        self.assertEqual(self.row(item)['status'],'unknown_outcome');self.assertEqual(len(self.http.calls),1)

    def test_second_worker_lease_cannot_consume_same_queue(self):
        item=self.enqueue();other=MaterialReplicationWorker(self.material,self.http)
        self.assertTrue(self.store.lease(self.worker.holder,ttl=120,name='material_replication'))
        self.assertFalse(other.tick());self.assertEqual(self.row(item)['status'],'pending')
        self.worker.tick();self.assertEqual(len(self.http.calls),1)

    def test_marker_pauses_intake_dispatch_but_history_remains_available(self):
        item=self.enqueue();self.marker.touch()
        with self.assertRaises(VideoEditingError):self.enqueue(operation_id='disabled-new-02')
        self.assertFalse(self.worker.tick());self.assertEqual(self.http.calls,[])
        context=self.material.context(TASK,CREATOR)
        self.assertFalse(context['enabled']);self.assertEqual(context['summary']['id'],item['id'])
        self.marker.unlink();self.worker.tick();self.assertEqual(len(self.http.calls),1)

    def test_missing_worker_credentials_never_crosses_outbound_marker(self):
        item=self.enqueue()
        with mock.patch.dict(os.environ,{'A_GROUP_TASK_CREATE_TOKEN':''}):
            with self.assertRaises(VideoEditingError):self.worker.tick()
        self.assertEqual(self.row(item)['status'],'pending');self.assertEqual(self.http.calls,[])

    def test_upstream_errors_do_not_persist_token_or_url(self):
        item=self.enqueue();self.http.response={'code':400,'message':'private-unit-test-token https://materials.example.test/test.mp4',
            'data':{'token':'private-unit-test-token','Authorization':'Bearer test','headers':{'Set-Cookie':'private'}}}
        self.worker.tick();text=json.dumps(self.material.history(TASK))
        self.assertNotIn('private-unit-test-token',text);self.assertNotIn('https://',text)
        body=json.dumps(self.row(item)['data']['response_body'])
        self.assertNotIn('private-unit-test-token',body);self.assertNotIn('https://',body);self.assertNotIn('Set-Cookie',body)

    def test_audited_history_import_is_idempotent_and_overrides_only_new_metadata(self):
        pending=self.enqueue();request=self.row(pending)['data']['request']
        # Simulate a fresh deployment with independently verified historical files.
        with self.store.transaction() as db:
            db.execute('DELETE FROM ve_material_replication_operation');db.execute('DELETE FROM ve_material_replication')
        before={kind:self.store.get(kind,id) for kind,id in [('task',TASK),('output',OUTPUT),('material_upload',OUTPUT)]}
        rejected=self.material.import_history(TASK,request,{'code':400,'message':'account not bound'},
            import_source='audit/rejected#sha',submitted_at=100)
        request={**request,'operator':'reviewed_editor'}
        body=accepted(request)
        imported=self.material.import_history(TASK,request,body,import_source='audit/accepted#sha',
            submitted_at=200,default_operator_override='reviewed_editor')
        again=self.material.import_history(TASK,request,body,import_source='audit/accepted#sha',
            submitted_at=200,default_operator_override='reviewed_editor')
        self.assertEqual(again['id'],imported['id']);self.assertTrue(again['reused'])
        self.assertEqual(imported['status'],'accepted');self.assertTrue(imported['historical'])
        self.assertEqual(imported['submitted_at'],200);self.assertEqual(rejected['status'],'rejected')
        self.assertEqual(self.row(imported)['data']['response_body'],body)
        self.assertEqual(self.material.context(TASK,CREATOR)['default_operator'],'reviewed_editor')
        reused=self.enqueue(operation_id='after-import-click')
        self.assertEqual(reused['id'],imported['id']);self.assertTrue(reused['reused'])
        self.assertFalse(self.worker.tick());self.assertEqual(self.http.calls,[])
        for kind,id in [('task',TASK),('output',OUTPUT),('material_upload',OUTPUT)]:self.assertEqual(self.store.get(kind,id),before[kind])
        with self.assertRaises(VideoEditingError) as caught:
            self.material.import_history(TASK,request,body,import_source='audit/accepted#sha',submitted_at=201,
                default_operator_override='reviewed_editor')
        self.assertEqual(caught.exception.status,409)

    def test_latest_live_request_takes_summary_over_previous_success(self):
        item=self.enqueue();self.worker.tick()
        failed=self.enqueue(operation_id='different-task-type',task_type=3)
        self.http.response={'code':400,'message':'rejected'};self.worker.tick()
        with self.store.transaction() as db:
            db.execute('UPDATE ve_material_replication SET created_at=created_at+10 WHERE id=?',(failed['id'],))
        context=self.material.context(TASK,CREATOR,history_only=True)
        self.assertEqual(context['history'][0]['id'],failed['id'])
        self.assertEqual(context['summary']['id'],failed['id'])
        # Task summaries do not load full history even when it grows.
        with mock.patch.object(self.material,'history',side_effect=AssertionError('full history loaded')):
            small=self.material.for_task(self.store.get('task',TASK))
        self.assertEqual(small['summary']['id'],failed['id']);self.assertEqual(small['count'],2)

    def test_previous_accepted_summary_does_not_hide_pending_operation_history(self):
        accepted_item=self.enqueue();self.worker.tick()
        pending=self.enqueue(operation_id='new-target-operation',target_langs=['en'])
        context=self.material.context(TASK,CREATOR,history_only=True)
        self.assertEqual(context['summary']['id'],pending['id'])
        resolved=next(item for item in context['history'] if 'new-target-operation' in item['operation_ids'])
        self.assertEqual(resolved['status'],'pending');self.assertEqual(resolved['id'],pending['id'])
        self.http.exception=TimeoutError('unknown')
        self.worker.tick()
        context=self.material.context(TASK,CREATOR,history_only=True)
        resolved=next(item for item in context['history'] if 'new-target-operation' in item['operation_ids'])
        self.assertEqual(resolved['status'],'unknown_outcome')
        self.assertEqual(context['summary']['id'],pending['id'])

    def test_all_historical_summary_prioritizes_accepted_over_later_rejection(self):
        item=self.enqueue();self.worker.tick()
        failed=self.enqueue(operation_id='different-history-type',task_type=3)
        self.http.response={'code':400,'message':'rejected'};self.worker.tick()
        with self.store.transaction() as db:
            for result in (item,failed):
                row=self.row(result);row['data']['historical']=True
                self.store.update('material_replication',row['id'],{'data':row['data']},db)
            db.execute('UPDATE ve_material_replication SET created_at=created_at+10 WHERE id=?',(failed['id'],))
        self.assertEqual(self.material.context(TASK,CREATOR,history_only=True)['summary']['id'],item['id'])
        self.assertEqual(self.material.for_task(self.store.get('task',TASK))['summary']['id'],item['id'])

    def test_cross_task_reuse_is_readable_through_operation_alias(self):
        original=self.material.enqueue(TASK,ADMIN,self.payload())
        second_task='ve_'+'b'*32;second_output='vo_'+'b'*32
        with self.store.transaction() as db:
            task=self.store.get('task',TASK,db);task['id']=second_task;task['operation_id']='second-render'
            task['data']['output_id']=second_output;self.store.insert('task',task,db)
            output=self.store.get('output',OUTPUT,db);output.update(id=second_output,task_id=second_task)
            self.store.insert('output',output,db)
            upload=self.store.get('material_upload',OUTPUT,db);upload.update(id=second_output,output_id=second_output,task_id=second_task)
            self.store.insert('material_upload',upload,db)
        reused=self.material.enqueue(second_task,ADMIN,self.payload(operation_id='second-task-reuse'))
        self.assertEqual(reused['id'],original['id'])
        history=self.material.context(second_task,ADMIN,history_only=True)['history']
        self.assertEqual(len(history),1);self.assertIn('second-task-reuse',history[0]['operation_ids'])
        self.assertEqual(self.material.for_task(self.store.get('task',second_task))['count'],1)

    def test_import_rejects_wrong_source_and_unverified_override(self):
        pending=self.enqueue();request=self.row(pending)['data']['request']
        bad=copy.deepcopy(request);bad['orig_materials'][0]['material_id']=999
        with self.assertRaises(VideoEditingError):
            self.material.import_history(TASK,bad,accepted(bad),import_source='bad',submitted_at=100)
        with self.assertRaises(VideoEditingError):
            self.material.import_history(TASK,request,{'code':400},import_source='bad',submitted_at=100,
                default_operator_override='original_editor')

    def test_schema_initialization_is_additive_and_repeatable(self):
        item=self.enqueue();Store(self.store.path)
        self.assertEqual(self.row(item)['status'],'pending')
        self.assertEqual(self.store.get('task',TASK)['owner'],owner(CREATOR))


class OperatorCatalogTests(unittest.TestCase):
    def test_mapping_requires_active_groups_and_unique_username(self):
        catalog=ReplicationCatalog(SimpleNamespace(ADMIN_MAPPING_MYSQL_DATABASE='unit_test'))
        catalog.query=mock.Mock(return_value=[{'username':'editor'}])
        self.assertEqual(catalog.operator(' Creator@Example.Test '),'editor')
        sql=catalog.query.call_args.args[0]
        self.assertIn('aug.status=0',sql);self.assertIn('au.id=aug.sub_user_id',sql)
        self.assertIn('GROUP BY BINARY TRIM(au.username) LIMIT 2',sql)
        for rows in ([],[{'username':'first'},{'username':'second'}],[{'username':''}]):
            catalog.query.return_value=rows
            with self.assertRaises(VideoEditingError):catalog.operator('creator@example.test')


if __name__ == '__main__':unittest.main()
