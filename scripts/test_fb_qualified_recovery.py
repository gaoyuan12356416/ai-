"""Retained uploads and missing-artifact reconstruction preserve durable facts."""
from pathlib import Path
from contextlib import nullcontext
import hashlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts.test_pipeline_executors import Context, MemoryCOS, MemoryStore
from features.pipeline_dispatch.executors import COSArtifactStore, NodeExecutors, ExecutionError


class FBQualifiedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cloud=MemoryCOS();self.store=MemoryStore(self.cloud);self.store.config.work_root=self.root
        self.body=b'qualified original';self.sha=hashlib.sha256(self.body).hexdigest()
        self.request={'job_id':'same-job','expected_profile':'frozen-v1'}
        self.key='prepared/'+self.sha+'.mp4'
        self.result={'job_id':'same-job','content_id':7,'profile':'frozen-v1',
            'source_sha256':'b'*64,'source_size':120,'random_overlay_recipe':{'choice':7},
            'output_url':self.store.url(self.key),'output_sha256':self.sha,'output_size':len(self.body),'storage_key':self.key}
        self.journal=COSArtifactStore(self.store,'fb_prepare',self.request,source_key='fb:auto:1')
        self.journal.qualified(self.result,self.key);self.original=self.cloud.objects[self.journal.key]
        self.context=Context();self.context.job={'source_key':'fb:auto:1'}
        self.renderer=SimpleNamespace(config=self.store.config,object_store=self.store,prepare=lambda _:self.fail('unexpected render'))
        self.executor=NodeExecutors({'state_root':self.temp.name,'backend':'cpu'})
        self.output=self.root/'jobs'/'same-job'/'output.tmp.mp4';self.output.parent.mkdir(parents=True)
        self.uploads=[]
        def upload(path,job,sha,size):
            body=Path(path).read_bytes();self.assertEqual(hashlib.sha256(body).hexdigest(),sha)
            key='prepared/'+sha+'.mp4';self.cloud.objects[key]=body
            self.cloud.heads[key]={'Content-Length':size,'x-cos-meta-sha256':sha};self.uploads.append(sha)
            return {'key':key,'url':self.store.url(key)}
        self.store.upload=upload
    def tearDown(self):self.temp.cleanup()
    def run_recovery(self):return self.executor._prepare_processor('fb_prepare',self.renderer,self.request,self.context)
    def assert_original_preserved(self):self.assertEqual(self.cloud.objects[self.journal.key],self.original)
    def test_retained_exact_file_resumes_upload_without_render(self):
        self.output.write_bytes(self.body)
        self.assertEqual(self.run_recovery(),self.result)
        self.assertEqual(self.uploads,[self.sha]);self.assertFalse(self.output.exists());self.assert_original_preserved()
    def test_changed_local_file_cannot_be_uploaded(self):
        self.output.write_bytes(b'changed')
        with self.assertRaisesRegex(ExecutionError,'qualified_local_artifact_changed'):self.run_recovery()
        self.assertEqual(self.uploads,[]);self.assertTrue(self.output.exists());self.assert_original_preserved()
    def test_missing_bytes_require_the_scoped_recovery_identity(self):
        with self.assertRaisesRegex(ExecutionError,'artifact_upload_incomplete'):self.run_recovery()
        self.context.job['recover_missing_qualified_sha256']='c'*64
        with self.assertRaisesRegex(ExecutionError,'artifact_upload_incomplete'):self.run_recovery()
        self.assert_original_preserved()
    def rebuild(self,change_source=False,lose_upload=False):
        body=b'new encoding of the identical frozen source and recipe';sha=hashlib.sha256(body).hexdigest();key='prepared/'+sha+'.mp4'
        result=dict(self.result,output_url=self.store.url(key),output_sha256=sha,output_size=len(body),storage_key=key)
        if change_source:result['source_sha256']='c'*64
        def prepare(_):
            self.output.write_bytes(body);self.renderer.qualified_artifact(result,key)
            if lose_upload:raise OSError('upload interrupted')
            self.store.upload(self.output,'same-job',sha,len(body));self.output.unlink();return result
        self.renderer.prepare=prepare;self.context.job['recover_missing_qualified_sha256']=self.sha
        return result
    def test_reconstruction_adds_receipt_and_replays_without_rerender(self):
        result=self.rebuild();expected=dict(result,reconstructed_from_sha256=self.sha)
        self.assertEqual(self.run_recovery(),expected);self.assert_original_preserved()
        self.renderer.prepare=lambda _:self.fail('must reuse reconstructed receipt')
        self.assertEqual(self.run_recovery(),expected);self.assertEqual(len(self.uploads),1)
    def test_reconstruction_rejects_changed_frozen_source_before_upload(self):
        self.rebuild(change_source=True)
        with self.assertRaisesRegex(ExecutionError,'artifact_rebuild_identity_mismatch'):self.run_recovery()
        self.assertEqual(self.uploads,[]);self.assertEqual(len(self.cloud.objects),1);self.assert_original_preserved()
    def test_reconstruction_upload_failure_reuses_retained_new_bytes(self):
        result=self.rebuild(lose_upload=True)
        with self.assertRaisesRegex(OSError,'upload interrupted'):self.run_recovery()
        self.renderer.prepare=lambda _:self.fail('must upload retained reconstruction')
        self.assertEqual(self.run_recovery(),dict(result,reconstructed_from_sha256=self.sha))
        self.assertEqual(len(self.uploads),1);self.assert_original_preserved()

    def test_renderer_keeps_qualified_file_when_upload_fails(self):
        from features.fb_gpu import prepare_worker as worker
        request=dict(self.request,content_id=7,source_url='https://source.example/a.mp4')
        (self.output.parent/'source.mp4').write_bytes(b'frozen source')
        config=SimpleNamespace(work_root=self.root,timeout=10,cos_prefix='prepared',cos_domain='https://media.example',profile='frozen-v1',compositor_backend='cpu')
        processor=worker.PrepareProcessor.__new__(worker.PrepareProcessor)
        processor.config=config;processor.job_slot=lambda _:nullcontext()
        processor._recipe_for_job=lambda *args:({'choice':7},{})
        processor.runner=lambda *args,**kwargs:self.output.write_bytes(self.body)
        processor.qualified_artifact=lambda result,key:None
        def failed_upload(*args):raise OSError('COS unavailable')
        processor.object_store=SimpleNamespace(upload=failed_upload)
        probe={'duration':10,'video':{'codec_name':'h264','profile':'high','width':720,'height':1280}}
        with patch.object(worker,'validate_request',return_value=request),patch.object(worker,'_probe',return_value=probe),patch.object(worker,'selected_asset_paths',return_value={}),patch.object(worker,'build_command',return_value=['renderer']):
            with self.assertRaisesRegex(OSError,'COS unavailable'):processor.prepare(request)
        self.assertEqual(self.output.read_bytes(),self.body)


if __name__=='__main__':unittest.main()
