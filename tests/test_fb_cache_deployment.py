from pathlib import Path
import unittest

from scripts.deploy_fb_source_cache import patched


class CacheDeploymentTests(unittest.TestCase):
    def test_patch_preserves_live_policies_and_compiles(self):
        root=Path(__file__).resolve().parents[1]
        source=(root/'features/fb_gpu/prepare_worker.py').read_bytes()
        # A structural fixture makes this check usable in shallow deployments.
        # Real CPU/GPU preimages are separately compiled by the scoped installer.
        before=b'''from .render_process import RenderStalled, run_render
def validate_request(value,config):
    return value.get("page_pool") or "retained_source_policy"
def _probe(config,path):
    pass
class PrepareProcessor:
    def job_slot(self,job):
        with self.slots:
            with self.lock:
                if shutil.disk_usage(self.config.work_root).free<32*1024**3:
                    raise RuntimeError("existing_reserve")
            yield
    def _download(self,url,path):
        return legacy_download(url,path)
    def save(self,render_seconds):
        return json.dumps({"render_seconds":render_seconds},sort_keys=True)
'''
        after=patched(before,source)
        compile(after,'installed_prepare_worker.py','exec')
        self.assertIn(b'SourceCache(',after)
        self.assertIn(b'source_cache_observation(root)',after)
        old_validation=before.split(b'def validate_request',1)[1].split(b'def _probe',1)[0]
        new_validation=after.split(b'def validate_request',1)[1].split(b'def _probe',1)[0]
        self.assertEqual(old_validation,new_validation)


if __name__=='__main__':
    unittest.main()
