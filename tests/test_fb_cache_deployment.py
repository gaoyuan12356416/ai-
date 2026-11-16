from pathlib import Path
import unittest

from scripts.deploy_fb_source_cache import patched


class CacheDeploymentTests(unittest.TestCase):
    def test_patch_preserves_live_policies_and_compiles(self):
        root=Path(__file__).resolve().parents[1]
        source=(root/'features/fb_gpu/prepare_worker.py').read_bytes()
        # Retrieve the unmodified GPU baseline from Git; deployment must preserve
        # its actual request contract rather than copying a whole older release.
        import subprocess
        before=subprocess.check_output(['git','-C',str(root),'show',
            'f1be3a3b:features/fb_gpu/prepare_worker.py'])
        after=patched(before,source)
        compile(after,'installed_prepare_worker.py','exec')
        self.assertIn(b'SourceCache(',after)
        self.assertIn(b'source_cache_observation(root)',after)
        old_validation=before.split(b'def validate_request',1)[1].split(b'def _probe',1)[0]
        new_validation=after.split(b'def validate_request',1)[1].split(b'def _probe',1)[0]
        self.assertEqual(old_validation,new_validation)


if __name__=='__main__':
    unittest.main()
