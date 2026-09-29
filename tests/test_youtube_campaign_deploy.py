"""Exact-file deploy and rollback contracts; never contacts a live service."""
import hashlib
import io
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import deploy_youtube_campaign_attribution as deploy


OLD = b"VERSION = 'before'\n"
NEW = b"VERSION = 'after'\n"
MOUNT_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'


class CampaignAttributionDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='campaign-deploy-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.live = self.root / 'live'
        self.stage = self.root / 'release'
        self.base = self.root / 'data-disk-deploy'
        self.base.mkdir()
        self.target = self.live / deploy.REL
        self.source = self.stage / deploy.REL
        self.target.parent.mkdir(parents=True)
        self.source.parent.mkdir(parents=True)
        self.target.write_bytes(OLD)
        self.source.write_bytes(NEW)
        self.db = self.live / 'jobs.sqlite3'
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute('CREATE TABLE ledger(id INTEGER PRIMARY KEY, event TEXT)')
                connection.execute('INSERT INTO ledger VALUES(1, ?)', ('keep-published-operation',))
        self.db_hash = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.app = self.live / 'app.py'
        self.app.write_bytes(b"UNCHANGED_API = True\n")
        self.calls = []
        self.restart_failures = 0
        self.dirty = ''
        self.mount_uuid = MOUNT_UUID
        patches = [patch.object(deploy, 'ROOT', self.live),
                   patch.object(deploy, 'BASE', self.base),
                   patch.object(deploy, 'EXPECTED', hashlib.sha256(OLD).hexdigest()),
                   patch.object(deploy, '__file__', str(self.stage / 'scripts' / 'deploy.py')),
                   patch.object(deploy, 'run', side_effect=self.run_fake),
                   patch.object(deploy, 'healthy'),
                   patch.object(deploy.os, 'umask'),
                   patch.object(deploy.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024**3)),
                   patch('sys.stdout', new_callable=io.StringIO)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def run_fake(self, *args):
        self.calls.append(args)
        if args[0] == 'findmnt':
            return self.mount_uuid
        if args[0] == 'git' and args[-2:] == ('rev-parse', 'HEAD'):
            return '1234567890abcdef1234567890abcdef12345678'
        if args[0] == 'git' and args[-2:] == ('status', '--porcelain'):
            return self.dirty
        if args[:2] == ('systemctl', 'restart'):
            if self.restart_failures:
                self.restart_failures -= 1
                raise subprocess.CalledProcessError(1, args, output='synthetic restart failure')
            return ''
        if args[:2] == ('systemctl', 'is-active'):
            return 'active'
        raise AssertionError('Unexpected external command: ' + repr(args))

    def invoke(self, *args):
        with patch('sys.argv', ['deploy-test', *map(str, args)]):
            deploy.main()

    def backups(self):
        return list((self.base / 'backups').glob('campaign-attribution-*'))

    def assert_database_retained(self):
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.db_hash)
        self.assertEqual(self.app.read_bytes(), b"UNCHANGED_API = True\n")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT * FROM ledger').fetchall(),
                             [(1, 'keep-published-operation')])

    def assert_no_restart(self):
        self.assertFalse(any(c[:2] == ('systemctl', 'restart') for c in self.calls))

    def test_preflight_does_not_write_or_restart(self):
        self.invoke('--check')
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(self.backups(), [])
        self.assert_no_restart()
        self.assert_database_retained()

    def test_live_drift_is_rejected_before_backup_or_restart(self):
        drift = b"CONCURRENT_PATCH = True\n"
        self.target.write_bytes(drift)
        with self.assertRaisesRegex(RuntimeError, 'Live report file drift'):
            self.invoke()
        self.assertEqual(self.target.read_bytes(), drift)
        self.assertEqual(self.backups(), [])
        self.assert_no_restart()
        self.assert_database_retained()

    def test_dirty_checkout_and_wrong_mount_refuse_writes(self):
        for dirty, mount, error in [(' M report.py', MOUNT_UUID, 'not clean'),
                                    ('', 'wrong-disk', 'mount mismatch')]:
            with self.subTest(error=error):
                self.dirty, self.mount_uuid = dirty, mount
                with self.assertRaisesRegex(RuntimeError, error):
                    self.invoke()
                self.assertEqual(self.target.read_bytes(), OLD)
                self.assertEqual(self.backups(), [])
        self.assert_no_restart()
        self.assert_database_retained()

    def test_apply_and_rollback_replace_only_report_and_preserve_database(self):
        self.invoke()
        self.assertEqual(self.target.read_bytes(), NEW)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'report.py').read_bytes(), OLD)
        manifest = json.loads((backups[0] / 'manifest.json').read_text())
        self.assertEqual(manifest['old'], hashlib.sha256(OLD).hexdigest())
        self.assertEqual(manifest['new'], hashlib.sha256(NEW).hexdigest())
        self.assert_database_retained()
        self.invoke('--rollback', backups[0])
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(sum(c[:2] == ('systemctl', 'restart') for c in self.calls), 2)
        self.assert_database_retained()

    def test_rollback_refuses_target_drift_without_touching_it(self):
        self.invoke()
        drift = b"CONCURRENT_PATCH = True\n"
        self.target.write_bytes(drift)
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'installed content drift'):
            self.invoke('--rollback', self.backups()[0])
        self.assertEqual(self.target.read_bytes(), drift)
        self.assert_no_restart()
        self.assert_database_retained()

    def test_rollback_refuses_modified_backup(self):
        self.invoke()
        backup = self.backups()[0]
        (backup / 'report.py').write_bytes(b'CORRUPT = True\n')
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'Backup content drift'):
            self.invoke('--rollback', backup)
        self.assertEqual(self.target.read_bytes(), NEW)
        self.assert_no_restart()
        self.assert_database_retained()

    def test_rollback_refuses_path_outside_backup_root(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid backup path'):
            self.invoke('--rollback', self.root / 'untrusted')
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assert_no_restart()

    def test_restart_failure_restores_old_code_and_restarts_old_service(self):
        self.restart_failures = 1
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke()
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(sum(c[:2] == ('systemctl', 'restart') for c in self.calls), 2)
        self.assertEqual(len(self.backups()), 1)
        deploy.healthy.assert_called_once()
        self.assert_database_retained()

    def test_health_failure_restores_old_code_and_verifies_recovery(self):
        deploy.healthy.side_effect = [RuntimeError('synthetic health failure'), None]
        with self.assertRaisesRegex(RuntimeError, 'synthetic health failure'):
            self.invoke()
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(sum(c[:2] == ('systemctl', 'restart') for c in self.calls), 2)
        self.assertEqual(deploy.healthy.call_count, 2)
        self.assert_database_retained()

    def test_error_after_atomic_replace_restores_old_code(self):
        original_install = deploy.install

        def install_then_fail(path, data, mode):
            original_install(path, data, mode)
            if data == NEW:
                raise RuntimeError('synthetic post-replace error')

        with patch.object(deploy, 'install', side_effect=install_then_fail), \
                self.assertRaisesRegex(RuntimeError, 'synthetic post-replace error'):
            self.invoke()
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(sum(c[:2] == ('systemctl', 'restart') for c in self.calls), 1)
        self.assert_database_retained()

    def test_error_before_atomic_replace_keeps_old_code_without_restart(self):
        with patch.object(deploy, 'install', side_effect=OSError('synthetic write failure')), \
                self.assertRaisesRegex(OSError, 'synthetic write failure'):
            self.invoke()
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assert_no_restart()
        self.assert_database_retained()

    def test_post_install_drift_refuses_automatic_rollback_without_overwrite(self):
        drift = b"CONCURRENT_PATCH = True\n"

        def concurrent_replacement(path, data, mode):
            path.write_bytes(drift)

        with patch.object(deploy, 'install', side_effect=concurrent_replacement), \
                self.assertRaisesRegex(RuntimeError, 'concurrent file drift'):
            self.invoke()
        self.assertEqual(self.target.read_bytes(), drift)
        self.assert_no_restart()
        self.assert_database_retained()


if __name__ == '__main__':
    unittest.main()
