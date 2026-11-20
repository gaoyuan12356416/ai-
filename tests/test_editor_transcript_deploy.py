"""Verify that the narrow switch refuses active work and preserves data."""
import importlib.util
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('editor_deploy',Path(__file__).resolve().parents[1]/'ops/editor-transcript/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeployTests(unittest.TestCase):
    def test_active_work_is_resumed_without_termination_or_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root/'old', root/'new'
            old.mkdir(); new.mkdir()
            current=root/'current';current.symlink_to(old)
            record={'previous':str(old),'release':str(new),'static':[]}
            def command(*args):
                return '123' if 'MainPID' in args else 'active'
            with patch.object(deploy,'CURRENT',current),patch.object(deploy,'cmd',side_effect=command), \
                 patch.object(deploy,'idle',side_effect=RuntimeError('work active')),patch.object(deploy.os,'kill') as kill:
                with self.assertRaises(RuntimeError):
                    deploy.switch(record,True,root)
            self.assertEqual(current.resolve(),old)
            self.assertEqual(kill.call_args_list[0].args,(123,signal.SIGSTOP))
            self.assertEqual(kill.call_args_list[-1].args,(123,signal.SIGCONT))
            self.assertNotIn(signal.SIGTERM,[c.args[1] for c in kill.call_args_list])

    def test_later_release_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);current=root/'current';current.symlink_to(root/'concurrent')
            record={'previous':str(root/'old'),'release':str(root/'new'),'static':[]}
            with patch.object(deploy,'CURRENT',current),patch.object(deploy.os,'kill') as kill:
                with self.assertRaises(RuntimeError):
                    deploy.switch(record,True,root)
                kill.assert_not_called()
