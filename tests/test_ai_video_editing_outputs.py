import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch, Mock

from features.ai_video_editing import output_storage as storage, render_worker as worker
from features.ai_video_editing.cloud_uploads import CloudUploads
from features.ai_video_editing.runtime import Runtime, Fault, owner, packed


class Missing(Exception):
    def get_status_code(self):return 404


class COS:
    def __init__(self):
        self.objects={};self.uploads=0;self.lose_response=False;self.last_sign=None
    def head_object(self,**kw):
        if kw['Key'] not in self.objects:raise Missing()
        return self.objects[kw['Key']]
    def upload_file(self,**kw):
        assert kw['ACL']=='private' and kw['EnableMD5']
        self.uploads+=1
        self.objects[kw['Key']]={'Content-Length':Path(kw['LocalFilePath']).stat().st_size,
                                'ETag':'"verified"',**kw['Metadata']}
        if self.lose_response:raise TimeoutError('lost response after commit')
    def get_presigned_url(self,**kw):
        self.last_sign=kw
        return 'https://test-123.cos.ap-hongkong.myqcloud.com/'+kw['Key']+'?signature=fixture'


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.rt=Runtime(Path(self.tmp.name)/'cpu');self.cos=COS()
        self.rt.cloud=CloudUploads(self.rt,self.cos,'test-123','ap-hongkong')
        self.actor={'tenant_key':'one','user_id':'user'}
        self.tid='at_'+'1'*32;self.lease='a'*32;self.digest=hashlib.sha256(b'film').hexdigest()
        self.meta={'width':720,'height':1280,'duration_ms':1000,'has_audio':True,'sha256':self.digest}
        unit={'title':'成片','clips':[{'source_id':'aa_'+'2'*32,'start_ms':0,'end_ms':1000}]}
        with self.rt.db(True) as db:
            db.execute('INSERT INTO tasks(id,owner,project,version,unit_id,unit_data,status,lease,created) VALUES(?,?,?,?,?,?,?,?,?)',
                       (self.tid,owner(self.actor),'fixture',1,'one',packed(unit),'running',self.lease,time.time()))
        self.body={'metadata':self.meta,'size':4}
        self.key=storage.object_key(self.tid,self.digest)
        self.cos.objects[self.key]={'Content-Length':4,'ETag':'"verified"',
            'x-cos-meta-sha256':self.digest,'x-cos-meta-ai-edit-task':self.tid}

    def finish(self):return storage.complete(self.rt,self.tid,self.lease,self.body)

    def test_verified_object_completes_without_cpu_media_and_keeps_owner_boundary(self):
        self.assertTrue(self.finish()['ok']);self.assertTrue(self.finish()['ok'])
        item=self.rt.list_records('tasks',self.actor,self.tid)['item']
        self.assertEqual(item['output']['storage'],'cos')
        self.assertNotIn('_cos',item['output']['metadata'])
        self.assertFalse((self.rt.root/'outputs'/(self.tid+'.mp4')).exists())
        self.assertTrue(self.rt.completion_receipt(self.tid,self.lease,self.digest))
        with self.assertRaises(Fault):self.rt.list_records('tasks',{'tenant_key':'two','user_id':'user'},self.tid)

    def test_rejects_wrong_lease_shape_and_remote_object(self):
        with self.assertRaises(Fault):storage.complete(self.rt,self.tid,'b'*32,self.body)
        with self.assertRaises(Fault):storage.complete(self.rt,self.tid,self.lease,{**self.body,'metadata':{**self.meta,'width':480}})
        self.cos.objects[self.key]['Content-Length']=3
        with self.assertRaises(Fault):self.finish()
        self.cos.objects.clear()
        with self.assertRaises(Fault):self.finish()
        self.assertEqual(self.rt.list_records('tasks',self.actor,self.tid)['item']['status'],'running')

    def test_migration_only_attaches_identical_completed_film(self):
        with self.assertRaises(Fault):storage.complete(self.rt,self.tid,'',self.body,migrate=True)
        with self.rt.db(True) as db:db.execute("UPDATE tasks SET status='completed',meta=?",(packed(self.meta),))
        original=self.rt.root/'outputs'/(self.tid+'.mp4');original.write_bytes(b'film')
        storage.complete(self.rt,self.tid,'',self.body,migrate=True)
        self.assertEqual(original.read_bytes(),b'film')
        self.assertEqual(storage.file_size(self.rt,self.tid),4)

    def test_materializes_only_for_local_consumer(self):
        self.finish()
        with patch('features.ai_video_editing.cloud_media.download') as download:
            download.side_effect=lambda loc,path,**kw:path.write_bytes(b'film')
            storage.location(self.rt,self.tid)
            self.assertEqual(download.call_count,0)
            self.assertEqual(storage.local_file(self.rt,self.tid).read_bytes(),b'film')
            storage.local_file(self.rt,self.tid)
            self.assertEqual(download.call_count,1)
            self.assertTrue(download.call_args.kwargs['output'])

    def test_http_auth_before_redirect_and_download_name(self):
        self.finish()
        spec=importlib.util.spec_from_file_location('features.ai_video_editing._output_http',Path(__file__).parents[1]/'features/ai_video_editing/http_api.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules',{'app':types.SimpleNamespace(DramaMaterialHandler=BaseHTTPRequestHandler)}):spec.loader.exec_module(module)
        actor=self.actor
        class Handler(module.Handler):
            def log_message(self,*a):pass
            def _require_cookie_module(self,*a):
                if self.headers.get('Login')!='yes':self.reply(401,{'error':'login'});return False
                return True
            def _session(self):return dict(actor,tenant_key=self.headers.get('Tenant','one'))
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.runtime=self.rt
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            def request(headers,query=''):
                c=HTTPConnection('127.0.0.1',http.server_port,timeout=3)
                c.request('HEAD','/api/ai-video-editing/tasks/'+self.tid+'/media'+query,headers=headers)
                r=c.getresponse();answer=(r.status,dict(r.headers));r.read();c.close();return answer
            self.assertEqual(request({})[0],401)
            self.assertEqual(request({'Login':'yes','Tenant':'two'})[0],404)
            code,headers=request({'Login':'yes','Range':'bytes=0-31'})
            self.assertEqual(code,307);self.assertIn('.cos.ap-hongkong.',headers['Location'])
            request({'Login':'yes'},'?download=1')
            self.assertEqual(self.cos.last_sign['Method'],'HEAD')
            self.assertIn('attachment;',self.cos.last_sign['Params']['response-content-disposition'])
        finally:http.shutdown();thread.join(2);http.server_close()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'requires FFmpeg')
    def test_gpu_file_upload_reconciles_lost_cos_response_without_reupload(self):
        file=Path(self.tmp.name)/'output.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=720x1280:r=30:d=1',
                        '-f','lavfi','-i','sine=frequency=500:duration=1','-c:v','libx264','-preset','ultrafast',
                        '-c:a','aac','-t','1','-movflags','+faststart',str(file)],check=True)
        sha=storage.file_digest(file);self.cos.lose_response=True
        result=storage.upload(self.cos,'test-123',self.tid,file,sha)
        self.assertEqual(result['metadata']['sha256'],sha)
        self.assertEqual(result['metadata']['duration_ms'],1000)
        storage.upload(self.cos,'test-123',self.tid,file,sha)
        self.assertEqual(self.cos.uploads,1)
        preview=storage.make_preview(file,Path(self.tmp.name)/'preview.mp4',encoder='libx264')
        meta=storage.probe(preview)
        self.assertLessEqual(meta['width'],480);self.assertLessEqual(meta['height'],854)
        self.assertEqual(meta['duration_ms'],1000);self.assertTrue(meta['has_audio'])

    def test_worker_sends_only_metadata_and_retains_pending_on_lost_api_receipt(self):
        folder=Path(self.tmp.name)/'gpu';folder.mkdir();(folder/'output.mp4').write_bytes(b'film')
        (folder/'pending.json').write_text(json.dumps({'id':self.tid,'lease':self.lease,'sha256':self.digest}))
        api=worker.InternalAPI('http://127.0.0.1/internal','fixture',output_store=(self.cos,'test-123'))
        with patch.object(storage,'upload_bundle',return_value=self.body),patch.object(api,'open',side_effect=worker.TransferError() ) as opening:
            with self.assertRaises(worker.TransferError):worker.upload_saved(api,folder)
            self.assertTrue(opening.call_args.args[0].endswith('/complete-object'))
            self.assertIsInstance(opening.call_args.kwargs['data'],bytes)
            self.assertLess(len(opening.call_args.kwargs['data']),2048)
        self.assertTrue((folder/'pending.json').exists())
        self.finish()
        with patch.object(api,'completed',return_value=True),patch.object(api,'complete',side_effect=AssertionError('duplicate')):
            worker.upload_saved(api,folder,reconcile_first=True)

    def test_material_library_copies_cloud_film_without_cpu_file(self):
        from features.ai_video_editing.youtube_material_io import MaterialIO
        io=object.__new__(MaterialIO);client=Mock()
        io.bucket='test-123';io.region='ap-hongkong'
        io.platform=types.SimpleNamespace(AD_MATERIAL_SOURCE_API_URL='https://aa.yingliangads.com/api/material/source',
            AD_MATERIAL_SOURCE_API_TOKEN='fixture',get_cos_client=lambda **kw:client)
        io.source=types.SimpleNamespace(_target_head=Mock(side_effect=[None,{'verified':True}]),
                                       _public_acl=Mock(),_public_head=Mock())
        item={'task_id':self.tid,'sha256':self.digest,'size':4,
              'request':{'url':'https://test-123.cos.ap-hongkong.myqcloud.com/final.mp4'},
              'cos_source':{'bucket':'test-123','key':self.key,'size':4,'etag':'"verified"'}}
        io.ensure_file(Path(self.tmp.name)/'absent.mp4',item)
        self.assertEqual(client.copy.call_args.kwargs['CopyStatus'],'Replaced')
        self.assertEqual(client.copy.call_args.kwargs['CopySourceIfMatch'],'"verified"')
        client.upload_file.assert_not_called()

    def test_preview_and_download_keep_separate_verified_objects(self):
        pd=hashlib.sha256(b'preview').hexdigest()
        self.cos.objects[storage.object_key(self.tid,pd)]={'Content-Length':7,'ETag':'"preview"',
            'x-cos-meta-sha256':pd,'x-cos-meta-ai-edit-task':self.tid}
        self.body['preview']={'metadata':{**self.meta,'width':480,'height':852,'sha256':pd},'size':7}
        self.finish()
        self.assertEqual(storage.location(self.rt,self.tid,preview=True)['sha256'],pd)
        self.assertEqual(storage.location(self.rt,self.tid,preview=True,download=True)['sha256'],self.digest)
        item=self.rt.list_records('tasks',self.actor,self.tid)['item']['output']
        self.assertTrue(item['preview_available'])
        self.assertFalse(any(k.startswith('_cos') for k in item['metadata']))


if __name__=='__main__':unittest.main()
