"""Local-only overlay rehearsal: no systemd or production access."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy_pipeline_runtime as deploy
from features.pipeline_dispatch.adapters import ClosingConnection


class RuntimeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.runtime = self.root / 'source', self.root / 'runtime'
        self.source.mkdir(); self.runtime.mkdir()
        self.relative = 'features/example/service.py'
        source_file = self.source / self.relative
        source_file.parent.mkdir(parents=True)
        initial = '\n'.join('value_%d = %d' % (i, i) for i in range(30)) + '\n'
        source_file.write_text(initial, encoding='utf-8')
        deploy.run(['git', 'init', '-q'], cwd=self.source)
        deploy.run(['git', 'add', '.'], cwd=self.source)
        self.git_commit('base')
        self.base = deploy.run(['git', 'rev-parse', 'HEAD'], cwd=self.source).decode().strip()
        source_file.write_text(initial.replace('value_10 = 10', 'value_10 = 100'), encoding='utf-8')
        package = self.source / 'features/pipeline_dispatch'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('VERSION = 1\n', encoding='utf-8')
        deploy.run(['git', 'add', '.'], cwd=self.source)
        self.git_commit('new')
        self.commit = deploy.run(['git', 'rev-parse', 'HEAD'], cwd=self.source).decode().strip()
        self.live = self.runtime / self.relative
        self.live.parent.mkdir(parents=True)
        self.live.write_text(initial + 'LIVE_ONLY = True\n', encoding='utf-8')
        self.before = self.live.read_bytes()
        self.baseline = self.root / 'baseline.json'
        self.baseline.write_text(json.dumps({'x/' + self.relative: {'runtime_path': str(self.live), 'sha256': deploy.sha(self.live)}}), encoding='utf-8')
        self.folder = self.root / 'release'
        self.ledger = self.root / 'live.db'
        with sqlite3.connect(self.ledger, factory=ClosingConnection) as connection:
            connection.execute('CREATE TABLE receipt(id INTEGER PRIMARY KEY, state TEXT)')
            connection.execute("INSERT INTO receipt VALUES(1,'published')")
        self.controls = {'x': {'units': ['example.service'], 'ledgers': [str(self.ledger)]}}

    def git_commit(self, message):
        deploy.run(['git', '-c', 'user.name=Local Test', '-c', 'user.email=test@example.invalid',
                    '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', message], cwd=self.source)

    def prepare(self):
        with patch.object(deploy, 'verify_source', return_value=self.commit):
            return deploy.prepare(self.source, self.commit, self.baseline, self.folder, base=self.base)

    def test_patch_preserves_independent_live_changes_and_leaves_runtime_untouched(self):
        result = self.prepare()
        candidate = self.folder / 'candidates/x' / self.relative
        self.assertEqual(result['status'], 'prepared')
        self.assertIn('value_10 = 100', candidate.read_text())
        self.assertIn('LIVE_ONLY = True', candidate.read_text())
        self.assertEqual(self.live.read_bytes(), self.before)
        self.assertFalse((self.runtime / 'features/pipeline_dispatch').exists())

    def test_runtime_drift_fails_before_creating_stage(self):
        self.live.write_text('drift = True\n')
        with self.assertRaisesRegex(RuntimeError, 'runtime_drift'):
            self.prepare()
        self.assertFalse(self.folder.exists())

    def test_busy_service_never_changes_runtime(self):
        self.prepare()
        with patch.object(deploy, 'assert_stopped', side_effect=RuntimeError('lane_not_drained_and_stopped')):
            with self.assertRaisesRegex(RuntimeError, 'not_drained'):
                deploy.apply(self.folder, 'x', self.controls)
        self.assertEqual(self.live.read_bytes(), self.before)

    def test_apply_and_rollback_never_restore_or_replay_ledgers(self):
        self.prepare()
        with patch.object(deploy, 'assert_stopped') as stopped:
            result = deploy.apply(self.folder, 'x', self.controls)
            self.assertFalse(result['services_restarted'])
            self.assertEqual(stopped.call_count, 2)
        self.assertIn('value_10 = 100', self.live.read_text())
        with sqlite3.connect(self.ledger, factory=ClosingConnection) as connection:
            connection.execute("INSERT INTO receipt VALUES(2,'published')")
        with patch.object(deploy, 'assert_stopped'):
            result = deploy.apply(self.folder, 'x', self.controls, rollback=True)
        self.assertFalse(result['ledgers_restored'])
        self.assertEqual(self.live.read_bytes(), self.before)
        self.assertFalse((self.runtime / 'features/pipeline_dispatch/__init__.py').exists())
        with sqlite3.connect(self.ledger, factory=ClosingConnection) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM receipt').fetchone()[0], 2)

    def test_candidate_tamper_or_live_drift_blocks_apply(self):
        self.prepare()
        (self.folder / 'candidates/x' / self.relative).write_text('changed = True\n')
        with patch.object(deploy, 'assert_stopped'):
            with self.assertRaisesRegex(RuntimeError, 'candidate_changed'):
                deploy.apply(self.folder, 'x', self.controls)
        self.assertEqual(self.live.read_bytes(), self.before)


if __name__ == '__main__':
    unittest.main()
