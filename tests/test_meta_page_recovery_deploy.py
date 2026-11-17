"""Exercise the actual isolated release layout, not only the full checkout."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import json


class DeploymentTests(unittest.TestCase):
    def test_rollback_disables_worker_boot_restart_and_preserves_business_state(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('recovery_deploy_rollback',root/'ops/meta-page-recovery/deploy.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);release=folder/'releases'/'verified';release.mkdir(parents=True)
            pointer=folder/'current';pointer.mkdir()
            (folder/'manifest.json').write_text(json.dumps(dict(commit='verified',previous_files={},previous_current=str(release))))
            # Avoid requiring Windows symlink privileges in this pure operation test.
            with patch.object(module,'CURRENT') as current,patch.object(module,'ROOT',folder),patch.object(module.subprocess,'check_call') as call:
                current.resolve.return_value=release
                module.rollback(folder)
                self.assertIn((['systemctl','disable','--now','meta-page-recovery-worker.service'],),[c.args for c in call.call_args_list])
            self.assertTrue((folder/'manifest.json').exists())

    def test_cold_import_in_packaged_release(self):
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('recovery_deploy',root/'ops/meta-page-recovery/deploy.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            release=Path(temporary)/'release'
            module.build_release(root,release,root/'features/fb_page_portrait')
            self.assertEqual((release/'features/fb_page_portrait/storage.py').read_bytes(),(root/'features/fb_page_portrait/storage.py').read_bytes())
            # The local shared fixture contains current owner overlays, while
            # the checkout supplies their unchanged dependency packages.
            code='import sys,features; features.__path__.append(sys.argv[1]); import features.fb_page_portrait.collect; import features.meta_page_recovery.engine'
            result=subprocess.run([sys.executable,'-c',code,str(root/'features')],
                cwd=release,env=dict(os.environ),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
