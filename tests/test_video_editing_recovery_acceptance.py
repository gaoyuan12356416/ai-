import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('recovery_acceptance',Path(__file__).resolve().parents[1]/'scripts/validate_video_editing_recovery.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


class RecoveryAcceptanceTests(unittest.TestCase):
    def test_snapshot_matches_real_runtime_dto_and_completed_restart(self):
        from features.drama_synthesis.async_runtime import AsyncRuntime
        from features.video_editing.gpu_integration import mixed_validate, mixed_fingerprint, public_result
        from features.video_editing.recipe import recipe_fingerprint
        job_id='ve_'+'4'*32
        payload={'job_id':job_id,'task_type':'video_editing_v1','operation':'render','recipe':{
            'version':1,'profile':'video-editing-720-v1','sources':[{'source_id':'s','kind':'episode',
                'url':'https://source.test/one.mp4','identity':{'size_bytes':100,'etag':'v1'},
                'duration_ms':1000,'width':720,'height':1280,'has_audio':True}],
            'clips':[{'id':'c','source_id':'s','start_ms':0,'end_ms':1000}]}}
        artifact={'bucket':'bucket-123','key':'video-editing/v1/outputs/'+job_id+'/material.mp4',
            'sha256':'a'*64,'size_bytes':100,'binding':'b'*32}
        result={'job_id':job_id,'task_type':'video_editing_v1','operation':'render',
            'recipe_sha256':recipe_fingerprint(payload['recipe']),'artifact':artifact}
        client=mock.Mock()
        client.head_object.return_value={'Content-Length':'100','ETag':'"etag"','Last-Modified':'Thu, 22 Oct 2026 00:00:00 GMT',
            'x-cos-meta-sha256':'a'*64,'x-cos-meta-size':'100'}
        client.get_object_acl.return_value={'AccessControlList':{'Grant':[{'Grantee':{'ID':'owner'},'Permission':'FULL_CONTROL'}]}}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            args=SimpleNamespace(base_url='http://127.0.0.1:18798',job_id=job_id,worker_pid=101,
                runtime_work_root=root/'runtime',editing_work_root=root/'media',editing_state_root=root/'state')
            state=args.editing_state_root/job_id;state.mkdir(parents=True)
            (state/'result.json').write_text(json.dumps({'fingerprint':mixed_fingerprint(payload),'result':result}))
            cached=mock.Mock(return_value=result)
            def create():
                return AsyncRuntime(args.runtime_work_root,mock.Mock(side_effect=AssertionError('unexpected render')),cached,
                    validate_payload=mixed_validate,fingerprint=mixed_fingerprint,public_result=public_result,autostart=False)
            runtime=create()
            try:
                runtime.submit(payload)
                def get(_base,path,_token):
                    return {'ok':True,'render_concurrency':1,'release_sha':'a'*40} if path=='/healthz' else runtime.get(job_id)
                with mock.patch.dict('os.environ',{'GPU_VIDEO_WORKER_TOKEN':'test-only'}), \
                     mock.patch.object(helper,'get_json',side_effect=get), \
                     mock.patch.object(helper,'listener_identity',side_effect=lambda pid,_port:{'pid':pid,'start_ticks':str(pid),'boot_id':'test'}):
                    before=helper.snapshot(args,client)
                    runtime.close();runtime=create();args.worker_pid=102
                    after=helper.snapshot(args,client)
                    self.assertTrue(helper.verify(before,after)['ok'])
                    self.assertEqual(before['job']['fingerprint'],mixed_fingerprint(payload))
                    self.assertEqual(before['job']['generation'],1)
                    self.assertEqual(cached.call_count,1)
            finally:runtime.close()

    def values(self):
        before={'version':1,'worker':{'pid':101,'start_ticks':'1','boot_id':'boot'},'release_sha':'sha',
                'job':{'job_id':'ve_'+'1'*32,'generation':1,'status':'completed','completed_at':'time'},
                'result':{'artifact':{'key':'video-editing/validation/one','sha256':'f'*64}},
                'cos':{'etag':'etag','last-modified':'time'},'runtime_record_sha256':'r','result_manifest_sha256':'m',
                'media_work_directory_absent':True}
        after=copy.deepcopy(before);after['worker'].update(pid=102,start_ticks='2')
        return before,after
    def test_real_restart_with_immutable_output_passes(self):
        before,after=self.values();self.assertTrue(helper.verify(before,after)['ok'])
    def test_polling_same_process_is_not_restart_proof(self):
        before,_=self.values()
        with self.assertRaises(helper.ValidationError):helper.verify(before,before)
    def test_same_bytes_uploaded_again_fail_last_modified_check(self):
        before,after=self.values();after['cos']['last-modified']='later'
        with self.assertRaises(helper.ValidationError):helper.verify(before,after)
    def test_new_generation_or_rewritten_result_is_not_idempotence(self):
        for field in ('job','runtime_record_sha256','result_manifest_sha256'):
            before,after=self.values();after[field]='changed'
            with self.assertRaises(helper.ValidationError):helper.verify(before,after)


if __name__=='__main__':unittest.main()
