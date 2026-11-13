from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / 'ops/moboreels-attribution-20261113/prepare_main_rebase.py'
SPEC = importlib.util.spec_from_file_location('prepare_main_rebase_under_test', SCRIPT)
rebase = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rebase)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


class PrepareMainRebaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.release, self.old_release = root/'release', root/'old-release'
        self.deployment = root/'data/deployments/test-deployment'
        self.main_root, self.public = root/'main', root/'public'
        (self.deployment/'private').mkdir(parents=True)
        (self.deployment/'staged').mkdir()
        self.mappings = [
            ('app.py', self.main_root/'app.py', 'app.py'),
            ('static/quick-nav.js', self.main_root/'static/quick-nav.js', 'quick-nav.js'),
            ('static/quick-nav.js', self.public/'quick-nav.js', 'quick-nav.js'),
            ('static/fb-auto-publish-template.html', self.main_root/'static/fb-auto-publish-template.html', 'fb-auto-publish-template.html'),
            ('static/fb-auto-publish-template.html', self.public/'fb-auto-publish-template.html', 'fb-auto-publish-template.html'),
            ('features/fb_auto_posts/core.py', self.main_root/'features/fb_auto_posts/core.py', None),
            ('features/fb_auto_posts/service.py', self.main_root/'features/fb_auto_posts/service.py', None)]
        entries = []
        for number, (relative, target, baseline) in enumerate(self.mappings):
            self.write(self.old_release/relative, '# b53 planned source\n')
            self.write(self.release/relative, '# reviewed merged source\n')
            self.write(target, '# captured live\n' if baseline else '# original live\n')
            if baseline:
                self.write(self.release/rebase.BASELINE/baseline, '# captured live\n')
            staged = self.deployment/'staged'/str(number)
            self.write(staged, '# b53 planned source\n')
            before = hashlib.sha256(b'# original live\n').hexdigest() if baseline else digest(target)
            entries.append(dict(target=str(target), phase='main', before=before,
                                after=digest(staged), staged=str(staged), source=str(self.old_release/relative),
                                backup=str(self.deployment/'private'/str(number)), mode=420))
        self.manifest = dict(commit=rebase.BUSINESS_SHA, release=str(self.old_release),
                             phases={'fb': {'status': 'activated'}}, files=entries,
                             prepared_hotfixes={'fb-metric-startup': {'preserve': 'existing provenance'}})
        self.manifest_path = self.deployment/'manifest.json'
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')
        self.original_bytes = self.manifest_path.read_bytes()
        self.live_before = {str(target): target.read_bytes() for _, target, _ in self.mappings}
        def fence(entry):
            require(digest(entry['target']) == entry['before'], 'Live SHA changed')
        def atomic_copy(source, target, mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.deploy = SimpleNamespace(MAIN=self.main_root, PUBLIC=self.public, DATA=root/'data',
                                      require=require, verify=Mock(), digest=digest, fence=fence,
                                      atomic_copy=atomic_copy, utc=lambda: '2026-11-13T00:00:00Z')
        def durable(path, value):
            Path(path).write_text(json.dumps(value), encoding='utf-8')
        self.durable = SimpleNamespace(durable_json=durable)

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def run_preparation(self):
        fake_script = self.release/rebase.OPS/'prepare_main_rebase.py'
        with patch.object(rebase, '__file__', str(fake_script)), \
                patch.object(rebase, 'load', side_effect=[self.deploy, self.durable]), \
                patch.object(rebase, 'blob', side_effect=lambda release, sha, relative: digest(Path(release)/relative)), \
                patch('sys.argv', ['prepare_main_rebase.py', 'new-sha', '--deployment', str(self.deployment)]), \
                redirect_stdout(io.StringIO()):
            rebase.main()

    def test_seven_precise_overrides_keep_live_and_unrelated_provenance(self):
        self.run_preparation()
        result = json.loads(self.manifest_path.read_text())
        record = result['prepared_hotfixes'][rebase.NAME]
        self.assertEqual(len(record['main_overrides']), 7)
        self.assertEqual(result['prepared_hotfixes']['fb-metric-startup'], self.manifest['prepared_hotfixes']['fb-metric-startup'])
        self.assertEqual(json.loads(Path(record['receipt']).read_text()), record)
        for number, override in enumerate(record['main_overrides']):
            original, replacement = override['original'], override['replacement']
            self.assertEqual(original, self.manifest['files'][number])
            self.assertEqual(replacement['backup'], original['backup'])
            self.assertFalse(Path(replacement['backup']).exists())
            self.assertEqual(digest(replacement['staged']), replacement['after'])
            self.assertEqual(Path(replacement['target']).read_bytes(), self.live_before[replacement['target']])
            if number < 5:
                self.assertEqual(override['baseline_mode'], 'captured_git')
                self.assertEqual(digest(override['baseline_source']), replacement['before'])
                self.assertNotEqual(replacement['before'], original['before'])
            else:
                self.assertEqual(override['baseline_mode'], 'original_manifest')
                self.assertIsNone(override['baseline_source'])
                self.assertIsNone(override['baseline_relative'])
                self.assertEqual(replacement['before'], original['before'])
        self.deploy.verify.assert_any_call(self.release, 'new-sha')
        self.deploy.verify.assert_any_call(self.old_release, rebase.BUSINESS_SHA)

    def test_undrifted_module_drift_refuses_manifest_change(self):
        self.write(self.main_root/'features/fb_auto_posts/core.py', '# concurrent change\n')
        with self.assertRaisesRegex(RuntimeError, 'Live SHA changed'):
            self.run_preparation()
        self.assertEqual(self.manifest_path.read_bytes(), self.original_bytes)

    def test_captured_template_drift_refuses_manifest_change(self):
        self.write(self.public/'fb-auto-publish-template.html', '# concurrent change\n')
        with self.assertRaisesRegex(RuntimeError, 'Live baseline changed again'):
            self.run_preparation()
        self.assertEqual(self.manifest_path.read_bytes(), self.original_bytes)


if __name__ == '__main__':
    unittest.main()
