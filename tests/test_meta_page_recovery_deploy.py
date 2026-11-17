"""Exercise the actual isolated release layout, not only the full checkout."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DeploymentTests(unittest.TestCase):
    def test_cold_import_in_packaged_release(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('recovery_deploy',root/'ops/meta-page-recovery/deploy.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            release=Path(temporary)/'release'
            module.build_release(root,release,root/'features/fb_page_portrait')
            # The local shared fixture contains current owner overlays, while
            # the checkout supplies their unchanged dependency packages.
            code='import sys,features; features.__path__.append(sys.argv[1]); import features.fb_page_portrait.collect; import features.meta_page_recovery.engine'
            result=subprocess.run([sys.executable,'-c',code,str(root/'features')],
                cwd=release,env=dict(os.environ),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
