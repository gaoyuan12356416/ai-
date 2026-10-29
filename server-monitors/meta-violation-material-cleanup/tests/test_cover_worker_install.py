from contextlib import ExitStack, closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location('cover_worker_installer', DEPLOY / 'install_cover_worker.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class NarrowPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.web, self.systemd = self.base / 'runtime', self.base / 'web', self.base / 'units'
        self.current, self.release = self.root / 'releases/old', self.root / 'releases/new'
        self.shared = self.base / 'shared.sqlite3'
        source = (DEPLOY.parents[2] / installer.STORE).read_text(encoding='utf-8')
        for location in (self.current, self.release, self.web):
            path = location / installer.STORE
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding='utf-8')
        # Actual API evolves independently of this release's cover adapter.
        api = self.web / installer.STORE
        api.write_text(source.replace('class Store:\n',
            'class Store:\n    def new_job_page(self):\n        return []\n\n', 1), encoding='utf-8')
        self.systemd.mkdir()
        for unit in installer.UNITS + installer.OLD_UNITS:
            (self.systemd / unit).write_text('[Unit]\nDescription=fixture\n')
        (self.root / 'config.json').write_text('{"live_enabled":false,"cover_live_enabled":true}')
        self._database(self.shared, "CREATE TABLE fb_asset_delete_v2_runs(status TEXT)",
                       "INSERT INTO fb_asset_delete_v2_runs VALUES('running')")
        self._database(self.root / 'cover.sqlite3', 'CREATE TABLE facts(value TEXT)',
                       "INSERT INTO facts VALUES('original')")
        self._database(self.root / 'cover-audit/runs.sqlite3', 'CREATE TABLE events(value TEXT)',
                       "INSERT INTO events VALUES('preserved')")
        self.state = {unit: {'is-active': 'inactive', 'is-enabled': 'disabled'}
                      for unit in installer.UNITS + installer.OLD_UNITS}
        self.state['meta-cover-worker.service']['is-active'] = 'failed'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('ROOT', self.root), ('WEB', self.web), ('SHARED', self.shared), ('SYSTEMD', self.systemd)):
            self.stack.enter_context(patch.object(installer, name, value))
        self.stack.enter_context(patch.object(installer, 'disk'))
        self.stack.enter_context(patch.object(installer, 'unit_state', lambda: self.state))
        self.stack.enter_context(patch.object(installer.subprocess, 'check_output', self._git))
        # Avoid requiring Windows symlink privilege in this filesystem test.
        original_resolve = Path.resolve
        current_link = self.root / 'current'
        target = self.current
        def resolve(path, *args, **kwargs):
            return target if path == current_link else original_resolve(path, *args, **kwargs)
        self.stack.enter_context(patch.object(Path, 'resolve', resolve))

    @staticmethod
    def _database(path, schema, row):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(schema)
            db.execute(row)

    @staticmethod
    def _git(args, **kwargs):
        if args[:3] == ['git', 'rev-parse', 'HEAD']:
            return 'a' * 40 + '\n'
        if args[:3] == ['git', 'status', '--porcelain']:
            return ''
        raise AssertionError(args)

    def test_running_other_job_and_unrelated_api_methods_do_not_block(self):
        api_before = installer.digest(self.web / installer.STORE)
        shared_before = installer.digest(self.shared)
        backup = installer.prepare(self.release)
        plan = json.loads((backup / 'plan.json').read_text())
        self.assertEqual(plan['scope'], 'cover-worker-only')
        self.assertEqual(plan['web_store_before'], api_before)
        self.assertEqual(installer.digest(self.web / installer.STORE), api_before)
        self.assertEqual(installer.digest(self.shared), shared_before)
        self.assertNotEqual(api_before, plan['shared_adapter_sha256'])
        for name in ('config.json', 'cover.sqlite3', 'cover-audit.sqlite3', 'shared-tasks.sqlite3', *installer.UNITS):
            self.assertTrue((backup / name).is_file(), name)
        with closing(sqlite3.connect(backup / 'shared-tasks.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT status FROM fb_asset_delete_v2_runs').fetchone()[0], 'running')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        hashes = json.loads((backup / 'hashes.json').read_text())
        self.assertTrue(all(installer.digest(backup / name) == value for name, value in hashes.items()))

    def test_candidate_shared_adapter_change_is_rejected(self):
        path = self.release / installer.STORE
        path.write_text(path.read_text(encoding='utf-8') + '\n# changed adapter\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'shared_adapter_changed_in_cover_release'):
            installer.prepare(self.release)
        self.assertFalse((self.root / 'backups').exists())

    def test_active_cover_service_is_rejected(self):
        for unit in installer.COVER_SERVICES:
            with self.subTest(unit=unit):
                saved = self.state[unit]['is-active']
                self.state[unit]['is-active'] = 'active'
                with self.assertRaisesRegex(RuntimeError, 'service_not_idle:'):
                    installer.prepare(self.release)
                self.state[unit]['is-active'] = saved
        self.assertFalse((self.root / 'backups').exists())

    def test_api_guard_change_is_rejected_without_overwriting_api(self):
        path = self.web / installer.STORE
        changed = path.read_text(encoding='utf-8').replace("'cover_unknown_fence'", "'unsafe_removed_fence'", 1)
        path.write_text(changed, encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'shared_cover_guard_changed:'):
            installer.prepare(self.release)
        self.assertEqual(path.read_text(encoding='utf-8'), changed)

    def test_rollback_never_restores_backup_ledgers_or_api(self):
        backup = installer.prepare(self.release)
        with closing(sqlite3.connect(self.root / 'cover.sqlite3')) as db, db:
            db.execute("INSERT INTO facts VALUES('new_receipt_after_backup')")
        before = {path: installer.digest(path) for path in
                  (self.root / 'cover.sqlite3', self.root / 'cover-audit/runs.sqlite3',
                   self.shared, self.web / installer.STORE)}
        copied = []
        with patch.object(installer, 'switch_report') as switch, patch.object(installer, 'run'), \
             patch.object(installer, 'online_snapshot') as snapshot, \
             patch.object(installer, 'atomic', lambda source, target: copied.append((source, target))):
            installer.apply(backup, rollback=True)
        switch.assert_called_once_with(backup, True)
        snapshot.assert_not_called()
        self.assertEqual(copied, [(backup / 'meta-cover-worker.service', self.systemd / 'meta-cover-worker.service')])
        self.assertEqual({path: installer.digest(path) for path in before}, before)
        with closing(sqlite3.connect(self.root / 'cover.sqlite3')) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM facts').fetchone()[0], 2)


class SnapshotDeadlineTests(unittest.TestCase):
    def connections(self, *, fail=False):
        events = []
        class Cursor:
            def fetchone(self): return ('ok',)
        class Source:
            def execute(self, sql):
                events.append(sql)
                return Cursor()
            def backup(self, target, **kwargs):
                events.append('backup')
                assert kwargs['pages'] == 256
                kwargs['progress'](0, 100, 200)
                if fail:
                    raise sqlite3.OperationalError('fixture_copy_failed')
                kwargs['progress'](101, 0, 200)
            def rollback(self): events.append('source_rollback')
            def close(self): events.append('source_close')
        class Target:
            def execute(self, sql):
                assert 'source_rollback' in events
                events.append(sql)
                return Cursor()
            def close(self): events.append('target_close')
        return events, [Source(), Target()]

    def test_timeout_rolls_back_source_and_skips_integrity_scan(self):
        events, connections = self.connections()
        with patch.object(installer.sqlite3, 'connect', side_effect=connections), \
             patch.object(installer.time, 'monotonic', side_effect=[0., 15.]):
            with self.assertRaisesRegex(TimeoutError, 'backup_snapshot_deadline_exceeded'):
                installer.online_snapshot(Path.cwd() / 'source.sqlite3', Path.cwd() / 'target.sqlite3')
        self.assertIn('source_rollback', events)
        self.assertIn('source_close', events)
        self.assertNotIn('PRAGMA integrity_check', events)

    def test_backup_exception_rolls_back_source_before_close(self):
        events, connections = self.connections(fail=True)
        with patch.object(installer.sqlite3, 'connect', side_effect=connections), \
             patch.object(installer.time, 'monotonic', side_effect=[0., 1.]):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture_copy_failed'):
                installer.online_snapshot(Path.cwd() / 'source.sqlite3', Path.cwd() / 'target.sqlite3')
        self.assertLess(events.index('source_rollback'), events.index('source_close'))
        self.assertNotIn('PRAGMA integrity_check', events)

    def test_success_releases_source_lock_before_integrity_check(self):
        events, connections = self.connections()
        with patch.object(installer.sqlite3, 'connect', side_effect=connections), \
             patch.object(installer.time, 'monotonic', side_effect=[0., 1., 2.]):
            installer.online_snapshot(Path.cwd() / 'source.sqlite3', Path.cwd() / 'target.sqlite3')
        self.assertEqual(events[:3], ['BEGIN', 'SELECT name FROM sqlite_master LIMIT 1', 'backup'])
        self.assertLess(events.index('source_rollback'), events.index('PRAGMA integrity_check'))


if __name__ == '__main__':
    unittest.main()
