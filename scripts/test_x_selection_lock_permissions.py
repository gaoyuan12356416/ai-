import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lock_permissions_subject', ROOT / 'features/x_auto_posts/selection_recovery.py')
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


@unittest.skipUnless(sys.platform == 'linux', 'Linux flock contract')
class LockPermissionTests(unittest.TestCase):
    def test_probe_missing_file_does_not_create_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(subject.execution_available(directory, 123))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_read_only_probe_preserves_held_lock_and_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            with subject.file_lock(directory, '123.lock'):
                inode = (Path(directory) / '123.lock').stat().st_ino
                self.assertFalse(subject.execution_available(directory, 123))
                self.assertEqual((Path(directory) / '123.lock').stat().st_ino, inode)
            self.assertTrue(subject.execution_available(directory, 123))

    @unittest.skipUnless(getattr(os, 'geteuid', lambda: -1)() == 0, 'root recovery owner')
    def test_root_created_lock_inherits_service_owner_without_replacing_inode(self):
        import pwd
        user = pwd.getpwnam('nobody')
        with tempfile.TemporaryDirectory() as directory:
            os.chown(directory, user.pw_uid, user.pw_gid)
            with subject.file_lock(directory, '123.lock'):
                path = Path(directory) / '123.lock'
                info = path.stat()
                self.assertEqual((info.st_uid, info.st_gid), (user.pw_uid, user.pw_gid))
                self.assertEqual(info.st_mode & 0o777, 0o660)
                self.assertFalse(subject.execution_available(directory, 123))
            self.assertEqual(path.stat().st_ino, info.st_ino)

if __name__ == '__main__': unittest.main()
