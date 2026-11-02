"""Local-only overlay rehearsal: no systemd or production access."""
import json
import hashlib
import os
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
        source_file.write_bytes(initial.encode('utf-8'))
        deploy.run(['git', 'init', '-q'], cwd=self.source)
        deploy.run(['git', 'add', '.'], cwd=self.source)
        self.git_commit('base')
        self.base = deploy.run(['git', 'rev-parse', 'HEAD'], cwd=self.source).decode().strip()
        source_file.write_bytes(initial.replace('value_10 = 10', 'value_10 = 100').encode('utf-8'))
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

    def production_file_base(self, *, unchanged=False, crlf=False):
        # Another release changed the adjacent context used by our old-base
        # patch. Its exact version becomes this file's explicitly pinned base.
        deploy.run(['git', 'checkout', '--detach', self.base], cwd=self.source)
        source_file = self.source / self.relative
        production = source_file.read_bytes().replace(b'\r\n', b'\n').replace(b'value_9 = 9', b'value_9 = 900') + b'LIVE_ONLY = True\n'
        source_file.write_bytes(production)
        deploy.run(['git', 'add', '.'], cwd=self.source)
        self.git_commit('independent production fix')
        base = deploy.run(['git', 'rev-parse', 'HEAD'], cwd=self.source).decode().strip()
        deploy.run(['git', 'update-ref', 'refs/remotes/origin/verified-production', base], cwd=self.source)
        source_file.write_bytes(production if unchanged else production.replace(b'value_10 = 10\n', b'value_10 = 100\n'))
        package = self.source / 'features/pipeline_dispatch'
        package.mkdir(parents=True, exist_ok=True)
        (package / '__init__.py').write_text('VERSION = 1\n', encoding='utf-8')
        deploy.run(['git', 'add', '.'], cwd=self.source)
        self.git_commit('integrated dispatch')
        self.commit = deploy.run(['git', 'rev-parse', 'HEAD'], cwd=self.source).decode().strip()
        self.live.write_bytes(production.replace(b'\n', b'\r\n') if crlf else production)
        self.before = self.live.read_bytes()
        record = {'runtime_path': str(self.live), 'sha256': deploy.sha(self.live),
                  'patch_base': base, 'patch_base_sha256': hashlib.sha256(production).hexdigest()}
        self.baseline.write_text(json.dumps({'x/' + self.relative: record}), encoding='utf-8')
        return record

    def test_verified_file_base_preserves_production_fix_and_records_identity(self):
        record = self.production_file_base(crlf=True)
        self.prepare()
        candidate = self.folder / 'candidates/x' / self.relative
        self.assertEqual(candidate.read_bytes(), (self.source / self.relative).read_bytes())
        self.assertIn(b'value_9 = 900\n', candidate.read_bytes())
        self.assertIn(b'value_10 = 100\n', candidate.read_bytes())
        self.assertEqual(self.live.read_bytes(), self.before)
        result = json.loads((self.folder / 'manifest.json').read_text())['lanes']['x']['files'][self.relative]
        self.assertEqual(result['strategy'], 'verified_file_base_patch')
        self.assertEqual(result['patch_base'], record['patch_base'])
        self.assertEqual(result['patch_base_sha256'], record['patch_base_sha256'])
        self.assertEqual(result['patch_base_match'], 'crlf_normalized')

    def test_verified_unchanged_dependency_is_audited_without_reapplying_addition(self):
        record = self.production_file_base(unchanged=True)
        self.prepare()
        manifest = json.loads((self.folder / 'manifest.json').read_text())['lanes']['x']
        self.assertNotIn(self.relative, manifest['files'])
        self.assertEqual(manifest['verified_unchanged'][self.relative]['patch_base'], record['patch_base'])
        self.assertEqual(self.live.read_bytes(), self.before)

    def test_per_file_base_rejects_live_bytes_even_when_raw_capture_was_updated(self):
        record = self.production_file_base()
        self.live.write_bytes(self.live.read_bytes() + b'UNREVIEWED = True\n')
        record['sha256'] = deploy.sha(self.live)
        self.baseline.write_text(json.dumps({'x/' + self.relative: record}), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'patch_base_live_hash_mismatch'):
            self.prepare()

    def test_per_file_base_rejects_wrong_declared_source_hash(self):
        record = self.production_file_base()
        record['patch_base_sha256'] = '0' * 64
        self.baseline.write_text(json.dumps({'x/' + self.relative: record}), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'patch_base_source_hash_mismatch'):
            self.prepare()

    def test_per_file_base_rejects_unfetched_commit(self):
        self.production_file_base()
        deploy.run(['git', 'update-ref', '-d', 'refs/remotes/origin/verified-production'], cwd=self.source)
        with self.assertRaisesRegex(RuntimeError, 'patch_base_not_in_fetched_origin'):
            self.prepare()

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

    def test_crlf_live_text_is_normalized_only_in_candidate_with_exact_backup(self):
        raw = self.before.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        self.live.write_bytes(raw)
        self.baseline.write_text(json.dumps({'x/' + self.relative: {'runtime_path': str(self.live), 'sha256': deploy.sha(self.live)}}), encoding='utf-8')
        self.prepare()
        candidate = self.folder / 'candidates/x' / self.relative
        backup = self.folder / 'backups/x' / self.relative
        self.assertNotIn(b'\r\n', candidate.read_bytes())
        self.assertIn(b'value_10 = 100\n', candidate.read_bytes())
        self.assertIn(b'LIVE_ONLY = True\n', candidate.read_bytes())
        self.assertEqual(self.live.read_bytes(), raw)
        self.assertEqual(backup.read_bytes(), raw)
        manifest = json.loads((self.folder / 'manifest.json').read_text())
        record = manifest['lanes']['x']['files'][self.relative]
        self.assertTrue(record['candidate_crlf_normalized'])
        self.assertEqual(record['before_sha256'], deploy.sha(self.live))
        with patch.object(deploy, 'assert_stopped'):
            deploy.apply(self.folder, 'x', self.controls)
            deploy.apply(self.folder, 'x', self.controls, rollback=True)
        self.assertEqual(self.live.read_bytes(), raw)

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

    def test_app_symlink_always_uses_file_overlay_and_never_copies_ledger(self):
        actual = self.root / 'data-disk-app'
        self.runtime.rename(actual)
        try:
            self.runtime.symlink_to(actual, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                actual.rename(self.runtime)
                raise
            # Windows junctions need no symlink privilege. Make the helper see
            # the same is_symlink result as Linux while retaining a real link.
            deploy.run(['cmd', '/c', 'mklink', '/J', self.runtime, actual])
            self.addCleanup(os.rmdir, self.runtime)
            original = Path.is_symlink
            patcher = patch.object(Path, 'is_symlink', lambda value: value == self.runtime or original(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        private = actual / 'data/ledger.sqlite3'
        private.parent.mkdir()
        private.write_bytes(b'live ledger must remain here')
        (self.source / 'static').mkdir()
        for relative in deploy.APP_ADDITIONS:
            (self.source / relative).write_text('monitor', encoding='utf-8')
        deploy.run(['git', 'add', '.'], cwd=self.source)
        self.git_commit('monitor assets')
        self.commit = deploy.run(['git', 'rev-parse', 'HEAD'], cwd=self.source).decode().strip()
        self.baseline.write_text(json.dumps({'app/' + self.relative: {'runtime_path': str(self.live), 'sha256': deploy.sha(self.live)}}), encoding='utf-8')
        self.prepare()
        candidate = self.folder / 'candidates/app'
        manifest = json.loads((self.folder / 'manifest.json').read_text())
        self.assertFalse(manifest['lanes']['app']['symlink'])
        self.assertFalse((candidate / 'data').exists())
        controls = {'app': self.controls['x']}
        with patch.object(deploy, 'assert_stopped'):
            deploy.apply(self.folder, 'app', controls)
        self.assertTrue(self.runtime.is_symlink())
        self.assertEqual(self.runtime.resolve(), actual.resolve())
        self.assertEqual(private.read_bytes(), b'live ledger must remain here')


if __name__ == '__main__':
    unittest.main()
