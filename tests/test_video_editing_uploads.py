import base64
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit,urlunsplit,parse_qsl
from unittest.mock import patch

from features.video_editing import VideoEditingError
from features.video_editing.store import Store
from features.video_editing.uploads import Uploads, PART_BYTES, MAX_BYTES

ACTOR={'tenant_key':'t1','user_id':'u1','is_admin':False}
OTHER={'tenant_key':'t1','user_id':'u2','is_admin':False}


class Missing(Exception):
    def get_status_code(self):return 404


class FakeCos:
    def __init__(self):
        self.initializations=[];self.objects={};self.parts={};self.completions=0;self.public=False;self.fail_create=False;self.fail_complete=False
        self.signs=[]
    def create_multipart_upload(self,**kwargs):
        self.initializations.append(kwargs);self.parts['remote-1']=[]
        if self.fail_create:raise TimeoutError()
        return {'UploadId':'remote-1'}
    def get_presigned_url(self,**kwargs):
        self.signs.append(kwargs)
        return 'https://bucket.cos.ap-hongkong.myqcloud.com/'+kwargs['Key']+'?'+urlencode({**kwargs.get('Params',{}),
            'q-sign-algorithm':'sha1','q-ak':'id','q-sign-time':'1;2','q-key-time':'1;2','q-header-list':'content-md5;host;x-cos-psize-max;x-cos-psize-min',
            'q-url-param-list':'partnumber;uploadid','q-signature':'signature'})
    def head_object(self,**kwargs):
        if kwargs['Key'] not in self.objects:raise Missing()
        return self.objects[kwargs['Key']]
    def list_parts(self,**kwargs):return {'Part':self.parts[kwargs['UploadId']],'IsTruncated':'false'}
    def list_multipart_uploads(self,**kwargs):
        return {'Upload':[{'Key':self.initializations[0]['Key'],'UploadId':'remote-1'}]}
    def complete_multipart_upload(self,**kwargs):
        self.completions+=1
        init=self.initializations[0]
        self.objects[kwargs['Key']]={**init['Metadata'],'Content-Length':init['Metadata']['x-cos-meta-ve-size'],'ETag':'whole-etag'}
        if self.fail_complete:raise TimeoutError()
        return {}
    def abort_multipart_upload(self,**kwargs):return {}
    def get_object_acl(self,**kwargs):
        return {'AccessControlList':{'Grant':[{'Grantee':{'URI':'http://cam.qcloud.com/groups/global/AllUsers'} if self.public else {'ID':'owner'},'Permission':'FULL_CONTROL'}]}}


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'state.sqlite3');self.cos=FakeCos()
        self.uploads=Uploads(self.store,self.cos,'bucket','ap-hongkong')
    def create(self,size=12):
        return self.uploads.create({'operation_id':'operation_1','name':'source.mp4','size_bytes':size,'content_type':'video/mp4'},ACTOR)
    def signed(self,row,body=b'hello world!'):
        md5=base64.b64encode(hashlib.md5(body).digest()).decode()
        result=self.uploads.sign(row['id'],1,{'content_md5':md5},ACTOR)
        self.cos.parts['remote-1']=[{'PartNumber':1,'Size':len(body),'ETag':'"'+hashlib.md5(body).hexdigest()+'"'}]
        return result
    def test_direct_upload_private_and_immutable_key(self):
        row=self.create();self.assertEqual(self.cos.initializations[0]['ACL'],'private')
        self.assertTrue(row['data']['key'].startswith('video-editing/v1/sources/va_'))
        signed=self.signed(row)
        self.assertEqual(self.cos.signs[-1]['Params'],{'uploadId':'remote-1','partNumber':'1'})
        self.assertIn('Authorization',signed['headers']);self.assertEqual(signed['headers']['x-cos-psize-max'],'12')
        self.assertNotIn('q-signature',signed['url'])
        self.assertEqual(self.uploads.complete(row['id'],ACTOR)['status'],'completed')
        self.assertEqual(self.store.get('source',row['asset_id'])['status'],'uploaded')
    def test_create_idempotent_and_content_conflict(self):
        a=self.create();b=self.create();self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.cos.initializations),1)
        with self.assertRaises(VideoEditingError):self.create(13)
    def test_same_user_other_tenant_denied(self):
        row=self.create()
        for actor in (OTHER,{**ACTOR,'tenant_key':'other'}):
            with self.assertRaises(VideoEditingError):self.uploads.get(row['id'],actor)
    def test_part_change_and_invalid_part_denied(self):
        row=self.create();self.signed(row)
        with self.assertRaises(VideoEditingError):self.uploads.sign(row['id'],1,{'content_md5':base64.b64encode(b'x'*16).decode()},ACTOR)
        with self.assertRaises(VideoEditingError):self.uploads.sign(row['id'],2,{'content_md5':base64.b64encode(b'x'*16).decode()},ACTOR)
    def test_partial_or_wrong_size_cannot_complete(self):
        row=self.create()
        with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        self.signed(row);self.cos.parts['remote-1'][0]['Size']=11
        with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        self.assertEqual(self.cos.completions,0)
    def test_private_acl_required(self):
        row=self.create();self.signed(row);self.cos.public=True
        with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        self.assertNotEqual(self.store.get('source',row['asset_id'])['status'],'uploaded')
    def test_unknown_create_reconciles_without_second_create(self):
        self.cos.fail_create=True
        with self.assertRaises(VideoEditingError):self.create()
        row=self.create();self.assertEqual(row['status'],'initializing_unknown')
        row=self.uploads.reconcile(row['id'],ACTOR)
        self.assertEqual(row['status'],'uploading');self.assertEqual(len(self.cos.initializations),1)
    def test_unknown_completion_head_recovery_and_idempotence(self):
        row=self.create();self.signed(row);self.cos.fail_complete=True
        with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        self.assertEqual(self.uploads.reconcile(row['id'],ACTOR)['status'],'completed')
        self.assertEqual(self.uploads.complete(row['id'],ACTOR)['status'],'completed');self.assertEqual(self.cos.completions,1)
    def test_unknown_completion_retries_only_same_upload_and_manifest(self):
        row=self.create();self.signed(row)
        with patch.object(self.cos,'complete_multipart_upload',side_effect=TimeoutError('secret URL')) as request:
            with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
            original=request.call_args.kwargs
        self.assertEqual(self.uploads.get(row['id'],ACTOR)['status'],'completing_unknown')
        with patch.object(self.cos,'complete_multipart_upload',wraps=self.cos.complete_multipart_upload) as request:
            self.assertEqual(self.uploads.complete(row['id'],ACTOR)['status'],'completed')
            self.assertEqual(request.call_args.kwargs,original)
        self.assertEqual(len(self.cos.initializations),1)
    def test_unknown_completion_without_upload_or_object_stays_unknown(self):
        row=self.create();self.signed(row)
        with patch.object(self.cos,'complete_multipart_upload',side_effect=TimeoutError()):
            with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        with patch.object(self.cos,'list_parts',side_effect=Missing()):
            with self.assertRaises(VideoEditingError) as caught:self.uploads.complete(row['id'],ACTOR)
        self.assertEqual(caught.exception.code,'cos_complete_unknown')
        self.assertEqual(self.uploads.get(row['id'],ACTOR)['status'],'completing_unknown')
        self.assertEqual(self.cos.completions,0);self.assertEqual(len(self.cos.initializations),1)
    def test_unknown_completion_rejects_changed_parts(self):
        row=self.create();self.signed(row)
        with patch.object(self.cos,'complete_multipart_upload',side_effect=TimeoutError()):
            with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        self.cos.parts['remote-1'][0]['ETag']='"'+('0'*32)+'"'
        with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
        self.assertEqual(self.cos.completions,0)
    def test_completing_does_not_reset_to_uploading(self):
        row=self.create()
        with self.store.transaction() as db:self.store.update('upload',row['id'],{'status':'completing'},db)
        self.assertEqual(self.uploads.reconcile(row['id'],ACTOR)['status'],'completing')
        with self.assertRaises(VideoEditingError):self.uploads.complete(row['id'],ACTOR)
    def test_crashed_completion_can_recover_same_upload_after_deadline(self):
        row=self.create();self.signed(row)
        with self.store.transaction() as db:
            current=self.uploads.get(row['id'],ACTOR,db);current['data']['completion_started_at']=1
            self.store.update('upload',row['id'],{'status':'completing','data':current['data']},db)
        self.assertEqual(self.uploads.reconcile(row['id'],ACTOR)['status'],'completing_unknown')
        self.assertEqual(self.uploads.complete(row['id'],ACTOR)['status'],'completed')
        self.assertEqual(len(self.cos.initializations),1)
    def test_limits(self):
        with self.assertRaises(VideoEditingError):self.create(MAX_BYTES+1)
        row=self.create(PART_BYTES+3);self.assertEqual(row['data']['part_count'],2)
    def test_parallel_idempotence_creates_only_one_cos_session(self):
        with ThreadPoolExecutor(max_workers=6) as pool:rows=list(pool.map(lambda _:self.create(),range(12)))
        self.assertEqual(len({r['id'] for r in rows}),1);self.assertEqual(len(self.cos.initializations),1)
    def test_real_sdk_authorization_conversion_keeps_exact_request_binding(self):
        try:
            from qcloud_cos import CosConfig,CosS3Client
            from qcloud_cos.cos_auth import CosS3Auth
            import requests
        except ImportError:
            self.skipTest('optional COS SDK is not installed on this test host')
        client=CosS3Client(CosConfig(Region='ap-hongkong',SecretId='DUMMY_TEST_ID',SecretKey='DUMMY_TEST_KEY',Scheme='https'),retry=0)
        row=self.create();self.uploads.client=client
        digest=base64.b64encode(hashlib.md5(b'hello world!').digest()).decode()
        with patch('qcloud_cos.cos_auth.time.time',return_value=1700000000):
            signed=self.uploads.sign(row['id'],1,{'content_md5':digest},ACTOR)
            header=signed['headers'].pop('Authorization')
            request=requests.Request('PUT',signed['url'],headers=signed['headers']).prepare()
            request.headers.pop('Content-Length',None)
            expected=CosS3Auth(client._conf,key=row['data']['key'],expire=600,params={'uploadId':'remote-1','partNumber':'1'})(request)
        self.assertEqual(header,expected.headers['Authorization'])


if __name__=='__main__':unittest.main()
