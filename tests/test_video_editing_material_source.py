"""Material-source outbox: frozen identity, public copy, and unknown writes."""
import os
import tempfile
import unittest
from unittest import mock

from features.video_editing import VideoEditingError
from features.video_editing.material_source import MaterialSourceWorker,ReadOnlyCatalog,PRODUCT_NAME,CATEGORY
from features.video_editing.service import Service
from features.video_editing.store import Store,owner
from features.video_editing.uploads import Uploads


TASK_ID='ve_'+'a'*32
OUTPUT_ID='vo_'+'a'*32
ARTIFACT={'bucket':'private-12345','key':'video-editing/v1/outputs/example.mp4',
          'size_bytes':128,'sha256':'a'*64}
ACTOR={'tenant_key':'tenant','user_id':'actor','name':'Editor','email':'editor@example.test','is_admin':False}


class NotFound(Exception):
    def get_status_code(self):return 404


class Cos:
    def __init__(self):
        self.public=False;self.copy_calls=[];self.part_calls=[];self.parts={};self.source_size=128;self.target_sha='a'*64
        self.fail_part=0;self.public_acl=True
    def head_object(self,*,Bucket,Key):
        if Bucket=='private-12345':
            return {'Content-Length':str(self.source_size),'x-cos-meta-sha256':'a'*64,
                    'x-cos-hash-crc64ecma':'99'}
        if not self.public:raise NotFound()
        return {'Content-Length':str(self.source_size),'x-cos-meta-sha256':self.target_sha,
                'x-cos-hash-crc64ecma':'99'}
    def get_object_acl(self,**kwargs):
        grants=[{'Grantee':{'Type':'CanonicalUser'},'Permission':'FULL_CONTROL'}]
        if kwargs['Bucket']=='public-12345' and self.public_acl:
            grants.append({'Grantee':{'Type':'Group','URI':'http://cam.qcloud.com/groups/global/AllUsers'},
                           'Permission':'READ'})
        return {'AccessControlList':{'Grant':grants}}
    def copy_object(self,**kwargs):
        self.copy_calls.append(kwargs);self.public=True
        return {'ETag':'copied'}
    def create_multipart_upload(self,**kwargs):
        self.copy_calls.append(kwargs);return {'UploadId':'multipart-1'}
    def upload_part_copy(self,**kwargs):
        self.part_calls.append(kwargs)
        if kwargs['PartNumber']==self.fail_part:
            self.fail_part=0;raise TimeoutError('copy part response lost')
        number=kwargs['PartNumber'];etag='"part-%s"'%number
        span=kwargs['CopySourceRange'].removeprefix('bytes=').split('-')
        self.parts[number]={'PartNumber':number,'ETag':etag,'Size':int(span[1])-int(span[0])+1}
        return {'ETag':etag}
    def list_parts(self,**kwargs):return {'Part':list(self.parts.values()),'IsTruncated':'false'}
    def complete_multipart_upload(self,**kwargs):
        self.copy_calls.append(kwargs);self.public=True
        return {'ETag':'multipart-complete'}


class Response:
    def __init__(self,status=200,payload=None,size=128):
        self.status_code=status;self._payload=payload if payload is not None else {'code':0,'data':{'id':987654}}
        self.headers={'Content-Length':str(size)}
    def json(self):return self._payload


class Http:
    def __init__(self):self.post_calls=[];self.head_calls=[];self.response=Response();self.size=128
    def head(self,url,**kwargs):
        self.head_calls.append(url)
        return Response(size=self.size)
    def post(self,url,**kwargs):
        self.post_calls.append((url,kwargs))
        return self.response


class Catalog:
    def __init__(self):self.rows=[];self.person_calls=0
    def drama(self,app_id,content_id):
        assert (app_id,content_id)==('1479','D123')
        return 'US','en'
    def initiator(self,email):
        self.person_calls+=1
        assert email==ACTOR['email']
        return 777
    def formal(self,content_sign):
        return self.rows
    def receipt(self,receipt_id):
        return [{'id':receipt_id,'source_id':456789,'source_type':3}]


class MaterialSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=mock.patch.dict(os.environ,{
            'VIDEO_EDITING_MATERIAL_COS_BUCKET':'public-12345',
            'VIDEO_EDITING_MATERIAL_COS_REGION':'ap-hongkong',
            'VIDEO_EDITING_MATERIAL_COS_DOMAIN':'public-12345.cos.ap-hongkong.myqcloud.com',
            'VIDEO_EDITING_MATERIAL_COS_PREFIX':'video-editing/material-source/v1',
            'VIDEO_EDITING_MATERIAL_SOURCE_DISABLED_FILE':os.path.join(self.tmp.name,'disabled')})
        self.env.start();self.addCleanup(self.env.stop)
        self.store=Store(os.path.join(self.tmp.name,'state.sqlite3'))
        self.cos=Cos();self.http=Http();self.catalog=Catalog()
        app=mock.Mock(AD_MATERIAL_SOURCE_API_URL='https://aa.yingliangads.com/api/material/source',
                      AD_MATERIAL_SOURCE_API_TOKEN='test-token')
        uploads=Uploads(self.store,self.cos,'private-12345','ap-hongkong','video-editing/v1')
        self.service=Service(self.store,uploads,app=app)
        self.material=self.service.material_source
        self.material.catalog=self.catalog;self.material.cos=self.cos;self.material.http=self.http
        self._task()

    def _task(self,status='completed',artifact=None):
        artifact=artifact or dict(ARTIFACT)
        data={'output_id':OUTPUT_ID,'app_id':'1479','content_id':'D123','drama_title':'Drama',
              'result':{'artifact':artifact}}
        with self.store.transaction() as db:
            self.store.insert('task',{'id':TASK_ID,'owner':owner(ACTOR),'project_id':'vp_project',
                'operation_id':'render_operation','kind':'render','status':status,'data':data},db)
            self.store.insert('output',{'id':OUTPUT_ID,'owner':owner(ACTOR),'task_id':TASK_ID,
                'batch_id':'vb_batch','status':'ready' if status=='completed' else 'pending',
                'data':data['result']},db)

    def _queued(self):
        item=self.material.enqueue(TASK_ID,ACTOR)
        self.assertEqual(item['status'],'queued')
        return self.store.get('material_upload',OUTPUT_ID)

    def _formal(self,row):
        request=row['data']['request']
        return {'id':456789,'product':PRODUCT_NAME,'type':2,'data_source_id':request['content_id'],
            'content_sign':request['content_sign'],'category':CATEGORY,'country':'',
            'language':request['language'],'url':request['url'],'name':request['name'],
            'user_id':248,'initiator':777,'is_delete':0}

    def test_completed_owner_only_one_ledger_and_frozen_request(self):
        first=self._queued()
        request=first['data']['request']
        self.assertEqual(request['app_id'],3214)
        self.assertEqual(request['content_id'],'D123')
        self.assertEqual(request['category'],'dramawave_post')
        self.assertEqual(request['user_id'],248)
        self.assertEqual(request['initiator'],777)
        self.assertTrue(request['url'].startswith('https://public-12345.cos.ap-hongkong.myqcloud.com/'))
        self.assertEqual(self.material.enqueue(TASK_ID,ACTOR),{'status':'queued','error':''})
        self.assertEqual(self.catalog.person_calls,1)
        self.assertEqual(self.service.task_dto(self.service.task(TASK_ID,ACTOR))['material_upload'],
                         {'status':'queued','error':''})
        with self.assertRaises(VideoEditingError) as caught:
            self.material.enqueue(TASK_ID,{**ACTOR,'user_id':'other'})
        self.assertEqual(caught.exception.status,404)
        with self.store.transaction() as db:self.store.update('task',TASK_ID,{'status':'failed'},db)
        with self.assertRaises(VideoEditingError) as caught:self.material.enqueue(TASK_ID,ACTOR)
        self.assertEqual(caught.exception.status,409)

    def test_intake_marker_blocks_new_only(self):
        marker=os.environ['VIDEO_EDITING_MATERIAL_SOURCE_DISABLED_FILE']
        open(marker,'w').close()
        with self.assertRaises(VideoEditingError) as caught:self.material.enqueue(TASK_ID,ACTOR)
        self.assertEqual(caught.exception.code,'material_source_intake_disabled')
        os.unlink(marker)
        self._queued()
        open(marker,'w').close()
        self.assertEqual(self.material.enqueue(TASK_ID,ACTOR)['status'],'queued')

    def test_large_copy_receipt_and_formal_id_are_separate(self):
        with self.store.transaction() as db:
            task=self.store.get('task',TASK_ID,db);task['data']['result']['artifact']['size_bytes']=6*1024**3
            self.store.update('task',TASK_ID,{'data':task['data']},db)
            output=self.store.get('output',OUTPUT_ID,db);output['data']['artifact']['size_bytes']=6*1024**3
            self.store.update('output',OUTPUT_ID,{'data':output['data']},db)
        self.cos.source_size=6*1024**3;self.http.size=6*1024**3
        row=self._queued()
        worker=MaterialSourceWorker(self.material)
        worker.process(row)
        waiting=self.store.get('material_upload',OUTPUT_ID)
        self.assertEqual(waiting['status'],'verifying')
        self.assertEqual(waiting['data']['receipt_id'],'987654')
        self.assertEqual(len(self.cos.copy_calls),2)
        self.assertEqual(self.cos.copy_calls[0]['ACL'],'public-read')
        self.assertEqual(self.cos.copy_calls[0]['ContentType'],'video/mp4')
        self.assertEqual(len(self.cos.part_calls),6)
        self.assertEqual(self.cos.part_calls[0]['CopySourceRange'],'bytes=0-1073741823')
        self.assertEqual(self.cos.part_calls[-1]['CopySourceRange'],
                         'bytes=5368709120-6442450943')
        self.assertEqual(self.cos.copy_calls[-1]['MultipartUpload']['Part'][0],
                         {'PartNumber':1,'ETag':'"part-1"'})
        self.assertEqual(self.http.post_calls[0][1]['json']['app_id'],3214)
        self.catalog.rows=[self._formal(row)]
        worker.process(waiting)
        completed=self.store.get('material_upload',OUTPUT_ID)
        self.assertEqual(completed['status'],'succeeded')
        self.assertEqual(completed['data']['material_id'],'456789')
        self.assertEqual(completed['data']['receipt_id'],'987654')
        self.assertEqual(self.material.dto(completed)['material_id'],'456789')
        self.assertEqual(len(self.http.post_calls),1)

    def test_small_copy_uses_replace_and_anonymous_readback(self):
        row=self._queued()
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(len(self.cos.copy_calls),1)
        self.assertEqual(self.cos.copy_calls[0]['CopyStatus'],'Replaced')
        self.assertEqual(self.cos.copy_calls[0]['ACL'],'public-read')
        self.assertEqual(len(self.http.head_calls),1)

    def test_multipart_resume_reconciles_parts_without_new_session(self):
        with self.store.transaction() as db:
            task=self.store.get('task',TASK_ID,db);task['data']['result']['artifact']['size_bytes']=6*1024**3
            self.store.update('task',TASK_ID,{'data':task['data']},db)
            output=self.store.get('output',OUTPUT_ID,db);output['data']['artifact']['size_bytes']=6*1024**3
            self.store.update('output',OUTPUT_ID,{'data':output['data']},db)
        self.cos.source_size=6*1024**3;self.http.size=6*1024**3
        row=self._queued();self.cos.fail_part=3
        worker=MaterialSourceWorker(self.material);worker.process(row)
        interrupted=self.store.get('material_upload',OUTPUT_ID)
        self.assertEqual(interrupted['status'],'copying')
        self.assertEqual(set(interrupted['data']['copy_parts']),{'1','2'})
        worker.process(interrupted)
        self.assertEqual(self.store.get('material_upload',OUTPUT_ID)['status'],'verifying')
        self.assertEqual(len([x for x in self.cos.copy_calls if 'UploadId' not in x]),1)
        self.assertEqual([x['PartNumber'] for x in self.cos.part_calls],[1,2,3,3,4,5,6])
        self.assertEqual(len(self.http.post_calls),1)

    def test_unknown_submission_reconciles_without_repost(self):
        row=self._queued();self.http.response=Response(status=503,payload={'code':-2})
        worker=MaterialSourceWorker(self.material);worker.process(row)
        unknown=self.store.get('material_upload',OUTPUT_ID)
        self.assertEqual(unknown['status'],'unknown_outcome')
        self.assertEqual(unknown['data']['attempts'],1)
        self.catalog.rows=[self._formal(row)]
        worker.process(unknown)
        self.assertEqual(self.store.get('material_upload',OUTPUT_ID)['status'],'unknown_outcome')
        self.assertEqual(len(self.http.post_calls),1)

    def test_business_rejection_stays_failed_and_does_not_retry(self):
        row=self._queued();self.http.response=Response(status=200,payload={'code':-1,'message':'参数无效'})
        worker=MaterialSourceWorker(self.material);worker.process(row)
        failed=self.store.get('material_upload',OUTPUT_ID)
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(self.material.enqueue(TASK_ID,ACTOR)['status'],'failed')
        self.assertEqual(len(self.http.post_calls),1)

    def test_preexisting_formal_or_crashed_submitting_never_posts(self):
        row=self._queued();self.catalog.rows=[self._formal(row)]
        worker=MaterialSourceWorker(self.material);worker.process(row)
        self.assertEqual(self.store.get('material_upload',OUTPUT_ID)['status'],'unknown_outcome')
        self.assertFalse(self.http.post_calls)
        current=self.store.get('material_upload',OUTPUT_ID)
        current=self.material.change(current,'submitting',attempts=1,next_check_at=0)
        worker.process(current)
        self.assertEqual(self.store.get('material_upload',OUTPUT_ID)['status'],'unknown_outcome')
        self.assertFalse(self.http.post_calls)
        self.assertNotIn('material_id',self.material.dto(self.store.get('material_upload',OUTPUT_ID)))
        self.assertEqual(self.material.enqueue(TASK_ID,ACTOR)['status'],'unknown_outcome')
        self.assertFalse(self.http.post_calls)

    def test_formal_mismatch_fails_closed(self):
        row=self._queued();worker=MaterialSourceWorker(self.material);worker.process(row)
        formal=self._formal(row);formal['data_source_id']='wrong'
        self.catalog.rows=[formal]
        worker.process(self.store.get('material_upload',OUTPUT_ID))
        result=self.store.get('material_upload',OUTPUT_ID)
        self.assertEqual(result['status'],'unknown_outcome')
        self.assertIn('data_source_id',result['data']['error'])
        self.assertEqual(len(self.http.post_calls),1)

    def test_private_sha_change_or_public_mismatch_never_posts(self):
        row=self._queued();self.cos.source_size=127
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(self.store.get('material_upload',OUTPUT_ID)['status'],'failed')
        self.assertFalse(self.http.post_calls)

    def test_public_object_requires_explicit_read_acl(self):
        row=self._queued();self.cos.public_acl=False
        MaterialSourceWorker(self.material).process(row)
        self.assertEqual(self.store.get('material_upload',OUTPUT_ID)['status'],'failed')
        self.assertFalse(self.http.post_calls)

    def test_catalog_fails_closed_on_ambiguous_person_or_drama(self):
        app=mock.Mock(ADMIN_MAPPING_MYSQL_DATABASE='kunlunads_dev',DB_NAME='kunlunads_dev',
                      SOURCE_TABLE='ads_drama_resource')
        catalog=ReadOnlyCatalog(app)
        with mock.patch.object(catalog,'query',return_value=[{'id':'17'},{'id':'18'}]):
            with self.assertRaises(VideoEditingError) as caught:
                catalog.initiator('editor@example.test')
            self.assertEqual(caught.exception.code,'initiator_mapping_ambiguous')
        with mock.patch.object(catalog,'query',return_value=[
                {'country':'us','language':'en','count':90},
                {'country':'ca','language':'en','count':2}]):
            with self.assertRaises(VideoEditingError) as caught:
                catalog.drama('1479','D123')
            self.assertEqual(caught.exception.code,'drama_metadata_ambiguous')


if __name__=='__main__':unittest.main()
