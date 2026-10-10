import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import deploy_fb_hit_material as deploy


class RollbackTests(unittest.TestCase):
    def test_partial_install_before_new_module_still_restores_and_starts(self):
        for installed_count in (0, 1, 4, 5):
            with self.subTest(installed_count=installed_count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                items = []
                for index in range(5):
                    target, backup = root / f'target-{index}', root / f'backup-{index}'
                    if index < 4:
                        target.write_text('old')
                        backup.write_text('old')
                    if index < installed_count:
                        target.write_text('new')
                    items.append({'target': str(target), 'backup': str(backup) if index < 4 else None,
                                  'mode': 0o600, 'after': hashlib.sha256(b'new').hexdigest()})
                with patch.object(deploy.subprocess, 'check_call') as systemctl, patch.object(deploy, 'health'):
                    deploy.restore({'files': items})
                    systemctl.assert_any_call(['systemctl', 'start', deploy.SERVICE])
                for index in range(4): self.assertEqual((root / f'target-{index}').read_text(), 'old')
                self.assertFalse((root / 'target-4').exists())

    def test_restore_error_does_not_skip_remaining_files_or_service_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, backup = root / 'target', root / 'backup'
            target.write_text('new'); backup.write_text('old')
            items = [{'target': str(root / 'bad'), 'backup': str(root / 'missing'), 'mode': 0o600, 'after': 'unused'},
                     {'target': str(target), 'backup': str(backup), 'mode': 0o600, 'after': 'unused'}]
            with patch.object(deploy.subprocess, 'check_call') as systemctl, patch.object(deploy, 'health'):
                with self.assertRaisesRegex(RuntimeError, 'restore needs inspection'):
                    deploy.restore({'files': items})
                systemctl.assert_any_call(['systemctl', 'start', deploy.SERVICE])
            self.assertEqual(target.read_text(), 'old')


if __name__ == '__main__': unittest.main()
