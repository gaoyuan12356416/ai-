import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4))
spec = importlib.util.spec_from_file_location('migration', os.path.join(os.path.dirname(__file__), 'migrate.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MigrationSafetyTests(unittest.TestCase):
    def test_system_roots_and_prefix_siblings_are_rejected(self):
        for p in ['/', '/usr', '/var', '/root', '/root/.codex-other', '/root/../etc', '/var/lib/mysql/..']:
            with self.assertRaises(RuntimeError):
                m.paths(p)

    def test_allowlist_targets_remain_on_task_disk(self):
        for p in m.SOURCES:
            dest, old, backup, audit = m.paths(p)
            self.assertTrue(dest.startswith(m.BASE + '/rootfs/'))
            self.assertEqual(old, p + '.root-storage-20260915-original')
            self.assertTrue(backup.startswith(m.BASE + '/backups/'))
            self.assertTrue(audit.startswith(m.BASE + '/audit/'))

    def test_manifest_detects_same_length_content_change(self):
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, 'db')
            with open(p, 'wb') as f:
                f.write(b'AAAA')
            before = m.manifest(root)
            when = os.stat(p)
            with open(p, 'wb') as f:
                f.write(b'BBBB')
            os.utime(p, ns=(when.st_atime_ns, when.st_mtime_ns))
            self.assertNotEqual(before, m.manifest(root))

    @unittest.skipIf(sys.platform == 'win32', 'requires Unix symlink creation')
    def test_manifest_does_not_follow_external_symlink(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as external:
            with open(os.path.join(external, 'private'), 'w') as f:
                f.write('outside')
            os.symlink(external, os.path.join(root, 'link'))
            values = m.manifest(root)
            self.assertEqual(set(values), {'.', 'link'})
            self.assertEqual(values['link']['link'], external)

    def test_missing_mount_fails_before_write_probe(self):
        with mock.patch.object(m.os.path, 'ismount', return_value=False), mock.patch.object(m.tempfile, 'mkstemp') as create:
            with self.assertRaises(RuntimeError):
                m.verify_disk()
            create.assert_not_called()

    def test_wrong_uuid_fails_before_write_probe(self):
        with mock.patch.object(m.os.path, 'ismount', return_value=True), mock.patch.object(m.subprocess, 'check_output', return_value=b'wrong-uuid'), mock.patch.object(m.tempfile, 'mkstemp') as create:
            with self.assertRaises(RuntimeError):
                m.verify_disk()
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
