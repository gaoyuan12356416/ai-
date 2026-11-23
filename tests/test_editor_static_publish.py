import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class StaticPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location('editor_static_publish', Path(__file__).parents[1] / 'ops/editor-performance/static_update.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.public = self.root / 'public'
        self.public.mkdir()
        self.module.ROOT = self.root / 'release'
        self.module.DESTINATIONS = [self.public]
        self.html = self.public / 'video-editing-ai.html'
        self.helper = self.public / 'video-editing-ai-production.js'
        self.html.write_bytes(b'old html')

    def git(self, repo, *args):
        if args[0] == 'rev-parse': return b'new\n'
        if args[0] == 'status': return b''
        if args[0] == 'branch': return b'origin/test\n'
        if args[0] == 'ls-tree': return b'static/video-editing-ai.html\n' if args[-1].endswith('.html') else b''
        if args[0] == 'show':
            return {'old:static/video-editing-ai.html': b'old html', 'new:static/video-editing-ai.html': b'new html', 'new:static/video-editing-ai-production.js': b'new helper'}[args[1]]
        raise AssertionError(args)

    def install(self):
        with patch.object(self.module, 'git', self.git), patch('sys.argv', ['publish', 'install', '--repo', str(self.root), '--base', 'old', '--version', 'new', '--files', self.helper.name, self.html.name]):
            self.module.main()

    def test_new_asset_and_existing_html_install_then_rollback(self):
        self.install()
        self.assertEqual(self.helper.read_bytes(), b'new helper')
        self.assertEqual(self.html.read_bytes(), b'new html')
        manifest = next(self.module.ROOT.glob('backups/*/manifest.json'))
        with patch('sys.argv', ['publish', 'rollback', '--manifest', str(manifest)]): self.module.main()
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.html.read_bytes(), b'old html')

    def test_concurrent_existing_new_asset_is_not_overwritten(self):
        self.helper.write_bytes(b'parallel work')
        with self.assertRaisesRegex(RuntimeError, 'Concurrent static change'): self.install()
        self.assertEqual(self.helper.read_bytes(), b'parallel work')
        self.assertEqual(self.html.read_bytes(), b'old html')


if __name__ == '__main__': unittest.main()
