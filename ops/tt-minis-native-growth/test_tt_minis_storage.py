import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import tt_minis_storage as storage


class StorageGuardTests(unittest.TestCase):
    def test_missing_mount_does_not_create_directories(self):
        with patch.object(storage.subprocess, 'check_output', return_value=storage.EXPECTED_UUID.encode()), patch.object(storage.os.path, 'ismount', return_value=False), patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                storage.prepare_storage()
            mkdir.assert_not_called()

    def test_wrong_uuid_fails_closed(self):
        with patch.object(storage.subprocess, 'check_output', return_value=b'wrong'), patch.object(storage.os.path, 'ismount', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'UUID'):
                storage.prepare_storage()

    def test_low_space_fails_before_mkdir(self):
        with patch.object(storage.subprocess, 'check_output', return_value=storage.EXPECTED_UUID.encode()), patch.object(storage.os.path, 'ismount', return_value=True), patch.object(storage.os, 'access', return_value=True), patch.object(storage.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()), patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaisesRegex(RuntimeError, 'space'):
                storage.prepare_storage()
            mkdir.assert_not_called()

    def test_root_database_is_rejected(self):
        with patch.object(storage.subprocess, 'check_output', return_value=storage.EXPECTED_UUID.encode()), patch.object(storage.os.path, 'ismount', return_value=True), patch.object(storage.os, 'access', return_value=True), patch.object(storage.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 10 * 1024**3})()):
            with self.assertRaisesRegex(RuntimeError, 'resolve'):
                storage.prepare_storage(Path(tempfile.gettempdir()) / 'root-backed.sqlite3')


if __name__ == '__main__':
    unittest.main()
