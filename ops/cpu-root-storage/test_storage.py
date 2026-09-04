import os
import tempfile
import unittest
from unittest.mock import patch, Mock

import migrate_cold as migration
import storage_guard as guard


class ManifestTests(unittest.TestCase):
    def test_hash_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'data')
            with open(p, 'w') as f:
                f.write('old')
            before = migration.manifest(p)
            with open(p, 'w') as f:
                f.write('new')
            self.assertNotEqual(before, migration.manifest(p))

    @unittest.skipUnless(os.name == 'posix', 'Linux deployment test')
    def test_exchange_preserves_contents_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst, link = [os.path.join(d, n) for n in ['source', 'target', 'link']]
            os.mkdir(src)
            with open(os.path.join(src, 'file'), 'w') as f:
                f.write('payload')
            import shutil
            shutil.copytree(src, dst)
            before = migration.manifest(src)
            os.symlink(dst, link)
            migration.exchange(src, link)
            self.assertTrue(os.path.islink(src))
            self.assertEqual(before, migration.manifest(src))
            self.assertEqual(before, migration.manifest(link))
            migration.exchange(src, link)
            self.assertFalse(os.path.islink(src))


class GuardTests(unittest.TestCase):
    @patch.object(guard.os.path, 'ismount', return_value=False)
    def test_missing_mount(self, _):
        with self.assertRaisesRegex(RuntimeError, 'not mounted'):
            guard.verify()

    @patch.object(guard.os.path, 'ismount', return_value=True)
    @patch.object(guard.subprocess, 'check_output', return_value=b'wrong-uuid')
    def test_wrong_uuid(self, *_):
        with self.assertRaisesRegex(RuntimeError, 'unexpected filesystem'):
            guard.verify()

    @patch.object(guard.os.path, 'ismount', return_value=True)
    @patch.object(guard.subprocess, 'check_output', return_value=guard.UUID.encode())
    @patch.object(guard.os, 'stat', side_effect=[Mock(st_dev=1), Mock(st_dev=1)])
    def test_root_filesystem(self, *_):
        with self.assertRaisesRegex(RuntimeError, 'unexpected filesystem'):
            guard.verify()

    @patch.object(guard.os.path, 'ismount', return_value=True)
    @patch.object(guard.subprocess, 'check_output', return_value=guard.UUID.encode())
    @patch.object(guard.os, 'stat', side_effect=[Mock(st_dev=2), Mock(st_dev=1)])
    @patch.object(guard.os.path, 'realpath', return_value=guard.TEMP)
    @patch.object(guard.os.path, 'commonpath', return_value=guard.ROOT)
    @patch.object(guard.os, 'statvfs', return_value=Mock(f_bavail=1, f_frsize=4096), create=True)
    def test_full_disk(self, *_):
        with self.assertRaisesRegex(RuntimeError, 'insufficient'):
            guard.verify()


if __name__ == '__main__':
    unittest.main()
