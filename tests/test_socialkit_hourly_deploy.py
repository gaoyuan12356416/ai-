import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hourly_install', ROOT / 'ops/socialkit-analytics/install_hourly.py')
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


class HourlyDeployTests(unittest.TestCase):
    def test_only_report_static_and_four_units_are_replaced(self):
        writes = install.intended_writes()
        self.assertEqual(len(writes), 8)
        self.assertFalse(any(p.name in {'app.py', 'quick-nav.js'} for p in writes))
        self.assertEqual({p.name for p in writes}, {
            'socialkit-analytics.html', 'socialkit-analytics.js',
            'socialkit-analytics-refresh.service', 'socialkit-analytics-refresh.timer',
            'socialkit-gap-digest.service', 'socialkit-gap-digest.timer'})

    def test_changed_live_file_refuses_rollback_before_system_commands(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            backups = root / 'backups' / 'release'
            backups.mkdir(parents=True)
            target = root / 'page.html'
            target.write_text('another change', encoding='utf-8')
            manifest = backups / 'manifest.json'
            manifest.write_text(json.dumps(dict(release=str(root / 'release'), files=[
                dict(path=str(target), new_sha256=hashlib.sha256(b'new').hexdigest(), existed=False)
            ])), encoding='utf-8')
            with patch.object(install, 'DATA', root), patch.object(install, 'CURRENT') as current:
                current.is_symlink.return_value = True
                current.resolve.return_value = (root / 'release').resolve()
                with patch.object(install.base, 'check_mount'), patch.object(install, 'command') as commands:
                    with self.assertRaisesRegex(RuntimeError, 'Later file change'):
                        install.rollback(manifest)
                    commands.assert_not_called()

    def test_backup_checksum_verified_and_check_mode_has_no_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            backup = root / 'backups' / 'release'
            backup.mkdir(parents=True)
            target = root / 'page.html'
            target.write_bytes(b'new')
            (backup / '0').write_bytes(b'old')
            manifest = backup / 'manifest.json'
            manifest.write_text(json.dumps(dict(release=str(root / 'release'), files=[dict(
                path=str(target), new_sha256=hashlib.sha256(b'new').hexdigest(), existed=True,
                backup='0', old_sha256=hashlib.sha256(b'old').hexdigest())])), encoding='utf-8')
            with patch.object(install, 'DATA', root), patch.object(install, 'CURRENT') as current:
                current.is_symlink.return_value = True
                current.resolve.return_value = (root / 'release').resolve()
                with patch.object(install.base, 'check_mount'), patch.object(install, 'command') as commands:
                    install.rollback(manifest, verify_only=True)
                    commands.assert_not_called()
                    (backup / '0').write_bytes(b'corrupt')
                    with self.assertRaisesRegex(RuntimeError, 'checksum'):
                        install.rollback(manifest, verify_only=True)
            self.assertEqual(target.read_bytes(), b'new')


if __name__ == '__main__':
    unittest.main()
