import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
import migrate_cpu as migration
import mount_guard


class CpuMigrationTests(unittest.TestCase):
    def test_run_id_cannot_escape_backup_directory(self):
        self.assertEqual(migration.validate_run_id('gpu-service-migration-20260828T1502'),
                         'gpu-service-migration-20260828T1502')
        for value in ('../other', 'gpu-service-migration-20260828T1502/other', 'different-run'):
            with self.assertRaises(ValueError):
                migration.validate_run_id(value)

    def test_manifest_detects_changed_missing_files_without_deleting_extra_files(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source', Path(folder) / 'target'
            source.mkdir(); target.mkdir()
            (source / 'image.jpg').write_bytes(b'original-image')
            (source / 'second.jpg').write_bytes(b'second-image')
            shutil.copy2(str(source / 'image.jpg'), str(target / 'image.jpg'))
            (target / 'other-history.jpg').write_bytes(b'preserve-target-history')
            self.assertEqual(migration.compare_manifests(migration.tree_manifest(source),
                                                        migration.tree_manifest(target)), ['second.jpg'])
            (target / 'image.jpg').write_bytes(b'different-image')
            self.assertEqual(migration.compare_manifests(migration.tree_manifest(source),
                                                        migration.tree_manifest(target)), ['image.jpg', 'second.jpg'])

    def test_target_cannot_escape_data_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'base'; base.mkdir()
            with mock.patch.object(migration, 'BASE', base), mock.patch.object(migration, 'check_mount'):
                migration.validate_target(base / 'storage')
                with self.assertRaises(RuntimeError):
                    migration.validate_target(base / '..' / 'outside')

    def test_frozen_source_matches_exact_deployed_bytes(self):
        self.assertEqual(len(migration.verify_frozen_source(PACKAGE)['files']), 2)

    def test_frozen_source_refuses_changed_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder)
            (package / 'runtime').mkdir()
            (package / 'runtime/a.py').write_text('print(1)')
            manifest = {'files': [{'file': 'runtime/a.py', 'sha256': migration.digest(package / 'runtime/a.py')}]}
            (package / 'source-manifest.json').write_text(json.dumps(manifest))
            (package / 'runtime/a.py').write_text('print(2)')
            with self.assertRaises(RuntimeError):
                migration.verify_frozen_source(package)

    def valid_proof(self):
        return {'run_id': 'gpu-service-migration-20260828T1502', 'authorized_by_parent': True,
                'observed_at_epoch': 1000, 'writes_fenced': True, 'cron_fenced': True,
                'test_api_fenced': True, 'source_tunnels_stopped': True,
                'source_units': {unit: {'active': 'inactive', 'enabled': 'masked', 'children': 0}
                                 for unit in migration.SOURCE_UNITS}}

    def check_proof(self, value):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'proof.json'; path.write_text(json.dumps(value))
            with mock.patch.object(migration.time, 'time', return_value=1010):
                return migration.proof(path, 'gpu-service-migration-20260828T1502', source_stopped=True)

    def test_start_accepts_fresh_fenced_stopped_evidence(self):
        self.check_proof(self.valid_proof())

    def test_start_rejects_active_disabled_source(self):
        value = self.valid_proof()
        value['source_units'][migration.SOURCE_UNITS[0]]['active'] = 'active'
        with self.assertRaises(RuntimeError):
            self.check_proof(value)

    def test_start_rejects_remaining_source_children(self):
        value = self.valid_proof()
        value['source_units'][migration.SOURCE_UNITS[0]]['children'] = 1
        with self.assertRaises(RuntimeError):
            self.check_proof(value)

    def test_start_rejects_missing_test_or_cron_fence(self):
        for key in ('test_api_fenced', 'cron_fenced'):
            value = self.valid_proof(); value[key] = False
            with self.assertRaises(RuntimeError):
                self.check_proof(value)

    def test_start_rejects_stale_evidence(self):
        value = self.valid_proof(); value['observed_at_epoch'] = 1
        with self.assertRaises(RuntimeError):
            self.check_proof(value)

    def test_mount_guard_rejects_root_directory_fallback(self):
        with mock.patch.object(mount_guard.subprocess, 'check_output',
                               return_value='/ different-uuid rw,relatime\n'):
            with self.assertRaises(RuntimeError):
                mount_guard.check_mount()

    def test_mount_guard_rejects_readonly_disk(self):
        with mock.patch.object(mount_guard.subprocess, 'check_output',
                               return_value='/mnt/data-disk %s ro,relatime\n' % mount_guard.UUID):
            with self.assertRaises(RuntimeError):
                mount_guard.check_mount()

    def test_start_never_steals_existing_source_port(self):
        socket_context = mock.MagicMock()
        socket_context.__enter__.return_value.bind.side_effect = OSError('Address already in use')
        with mock.patch.object(migration, 'assert_drained'), mock.patch.object(migration, 'check_mount'), \
             mock.patch.object(migration.socket, 'socket', return_value=socket_context), \
             mock.patch.object(migration, 'command') as execute:
            with self.assertRaises(OSError):
                migration.start_units()
            execute.assert_not_called()

    def test_storage_switch_waits_for_lease_after_job_reports_done(self):
        counts = {'drama_material_job': {'done': 1}, 'drama_material_job_worker_lease': {'running': 1}}
        with mock.patch.object(migration, 'status_counts', return_value=counts), \
             mock.patch.object(migration.os.path, 'isfile', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'lease must be released'):
                migration.assert_drained(include_drama=True)


if __name__ == '__main__':
    unittest.main()
