"""The production installer must reject drift and touch only the resolver."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import deploy_youtube_drama_app_scope as deploy


class ScopeDeployCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'live'
        self.stage = Path(self.tmp.name) / 'stage'
        for directory in (self.root, self.stage):
            path = directory / deploy.RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(b'VERSION = 1\n')
        self.old = deploy.common.sha(self.root / deploy.RELATIVE_PATH)
        self.root_patch = patch.object(deploy.common, 'ROOT', self.root)
        self.hash_patch = patch.object(deploy, 'EXPECTED', self.old)
        self.root_patch.start()
        self.hash_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.hash_patch.stop)

    def test_only_resolver_is_in_install_plan(self):
        (self.stage / deploy.RELATIVE_PATH).write_bytes(b'VERSION = 2\n')
        plans = deploy.prepare(self.stage)
        self.assertEqual(set(plans), {self.root / deploy.RELATIVE_PATH})
        self.assertEqual(plans[self.root / deploy.RELATIVE_PATH]['old'], self.old)
        self.assertEqual((self.root / deploy.RELATIVE_PATH).read_bytes(), b'VERSION = 1\n')

    def test_unreviewed_live_change_refuses_deployment(self):
        (self.root / deploy.RELATIVE_PATH).write_bytes(b'VERSION = 3\n')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            deploy.prepare(self.stage)

    def test_invalid_python_refuses_deployment(self):
        (self.stage / deploy.RELATIVE_PATH).write_bytes(b'def broken(:\n')
        with self.assertRaises(SyntaxError):
            deploy.prepare(self.stage)


if __name__ == '__main__':
    unittest.main()
