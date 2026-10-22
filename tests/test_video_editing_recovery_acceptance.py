import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('recovery_acceptance',Path(__file__).resolve().parents[1]/'scripts/validate_video_editing_recovery.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


class RecoveryAcceptanceTests(unittest.TestCase):
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
