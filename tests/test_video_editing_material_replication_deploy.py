from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import deploy_video_editing_material_replication as deploy


COMMIT = 'a' * 40


class MaterialReplicationDeployTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            live = root / 'live'
            public = root / 'public'
            config = root / 'etc' / 'video-editing.env'
            config.parent.mkdir()
            config.write_bytes(b'EXISTING_VALUE=unchanged\n')
            db = data / 'state.sqlite3'
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("CREATE TABLE ve_task(id TEXT, status TEXT);"
                                   "INSERT INTO ve_task VALUES('old-task','completed');")
            with patch.multiple(deploy, LIVE=live, PUBLIC=public, DATA=data, DB=db, CONFIG=config,
                                UNIT=root / 'etc' / 'replication.service', MARKER=root / 'etc' / 'replication.disabled'):
                sources = {}
                old_sources = {}
                for target, source in deploy.target_sources():
                    content = (b'candidate = True\n' if source.endswith('.py') else b'candidate\n')
                    if target == deploy.UNIT:
                        content += ('EnvironmentFile=' + str(deploy.SECRET_ENV) + '\n').encode()
                    sources[source] = content
                    new = 'material_replication' in source or 'material-replication' in source
                    old_sources[source] = None if new else b'baseline = True\n'
                    if old_sources[source] is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(old_sources[source])
                def git_bytes(rev, source, optional=False):
                    return old_sources[source] if rev == deploy.BASE else sources[source]
                with patch.object(deploy, 'git_bytes', side_effect=git_bytes):
                    yield root, deploy.build_plan(COMMIT)

    def installed_manifest(self, root, plan):
        backup = deploy.DATA / 'backups' / 'test'
        backup.mkdir(parents=True)
        rows = []
        for index, row in enumerate(plan):
            item = {key: value for key, value in row.items() if key != 'data'}
            if row['before'] is not None:
                saved = backup / ('file-' + str(index))
                saved.write_bytes(Path(row['path']).read_bytes())
                item['backup'] = saved.name
            deploy.atomic(row['path'], row['data'])
            rows.append(item)
        manifest = {'commit': COMMIT, 'status': 'installed_disabled', 'files': rows,
                    'api_before': {'ActiveState': 'active', 'MainPID': '11'},
                    'protected_before': self.protected()}
        path = backup / 'manifest.json'
        deploy.save(path, manifest)
        return path, manifest

    @staticmethod
    def protected():
        return {name: {'ActiveState': 'active', 'MainPID': str(20 + index)}
                for index, name in enumerate(deploy.PROTECTED)}

    def test_plan_is_exact_allowlist_with_both_static_copies_and_no_gpu(self):
        with self.fixture() as (_, plan):
            expected = len(deploy.FILES) + 2 * len(deploy.STATIC) + 2
            self.assertEqual(len(plan), expected)
            self.assertEqual(sum(row['source'] == 'static/video-editing.js' for row in plan), 2)
            self.assertTrue(any(row['source'] == 'scripts/import_video_editing_material_replication_history.py'
                                for row in plan))
            self.assertFalse(any('gpu' in row['path'] for row in plan))
            self.assertFalse(any(row['path'] == str(deploy.SECRET_ENV) for row in plan))
            self.assertIn(b'VIDEO_EDITING_MATERIAL_REPLICATION_ENABLED=1', plan[-1]['data'])
            self.assertEqual(plan[-1]['install_mode'], 0o600)

    def test_drift_requires_exact_explicit_allowlisted_hash(self):
        with self.fixture() as (_, plan):
            target = Path(plan[0]['path'])
            target.write_bytes(b'reviewed_override = True\n')
            with self.assertRaisesRegex(RuntimeError, 'live_file_drift'):
                deploy.build_plan(COMMIT)
            fixed = deploy.build_plan(COMMIT, {str(target): deploy.file_sha(target)})
            self.assertEqual(fixed[0]['before'], deploy.file_sha(target))
            with self.assertRaisesRegex(RuntimeError, 'unexpected_before_hash_path'):
                deploy.build_plan(COMMIT, {'/etc/shadow': None})

    def test_foreign_marker_cannot_be_opened_or_silently_overwritten(self):
        with self.fixture() as (_, plan):
            deploy.MARKER.write_bytes(b'operator maintenance\n')
            with self.assertRaisesRegex(RuntimeError, 'foreign_replication_marker'):
                deploy.build_plan(COMMIT)
            self.assertEqual(deploy.MARKER.read_bytes(), b'operator maintenance\n')

    def test_default_check_has_no_mutations(self):
        with patch.object(deploy, 'storage'), patch.object(deploy, 'published', return_value=COMMIT), \
             patch.object(deploy, 'secret_preflight'), patch.object(deploy, 'build_plan', return_value=[]), \
             patch.object(deploy, 'atomic') as atomic, patch.object(deploy, 'apply') as apply, \
             patch.object(deploy, 'deployment_lock') as lock, patch('builtins.print'):
            deploy.main(['--commit', COMMIT])
        atomic.assert_not_called()
        apply.assert_not_called()
        lock.assert_not_called()

    def test_published_rejects_wrong_sha_dirty_or_remote_ref(self):
        def good(*args, **kwargs):
            if 'rev-parse' in args: return COMMIT
            if 'status' in args: return ''
            if 'get-url' in args: return 'git@github.com:gaoyuan12356416/ai-.git'
            if 'ls-remote' in args: return COMMIT + '\t' + deploy.REF
            return ''
        with patch.object(deploy, 'run', side_effect=good):
            self.assertEqual(deploy.published(COMMIT), COMMIT)
            with self.assertRaisesRegex(RuntimeError, 'checkout_commit_mismatch'):
                deploy.published('b' * 40)
        def bad(*args, **kwargs):
            return 'b' * 40 + '\t' + deploy.REF if 'ls-remote' in args else good(*args, **kwargs)
        with patch.object(deploy, 'run', side_effect=bad):
            with self.assertRaisesRegex(RuntimeError, 'commit_not_pushed'):
                deploy.published(COMMIT)
        with patch.object(deploy, 'run', side_effect=lambda *args, **kwargs:
                          ' M tracked.py' if 'status' in args else good(*args, **kwargs)):
            with self.assertRaisesRegex(RuntimeError, 'dirty_release_checkout'):
                deploy.published(COMMIT)

    def test_sqlite_online_backup_and_additive_rehearsal_preserve_old_rows(self):
        with self.fixture() as (_, __):
            backup = deploy.DATA / 'rehearsal'
            backup.mkdir()
            with closing(sqlite3.connect(deploy.DB)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute("INSERT INTO ve_task VALUES('committed-in-wal','running')")
                writer.commit()
                def migrate(path):
                    with closing(sqlite3.connect(path)) as db:
                        db.execute('CREATE TABLE ve_material_replication(id TEXT,status TEXT)')
                        db.commit()
                before = deploy.table_hashes(deploy.DB)
                with patch.object(deploy, 'migrate', side_effect=migrate):
                    proof = deploy.rehearse(backup)
                self.assertEqual(proof['result'], 'passed')
                self.assertEqual(deploy.table_hashes(deploy.DB), before)
                self.assertEqual(proof['existing_tables']['ve_task']['rows'], 2)
                self.assertNotIn('ve_material_replication', deploy.table_hashes(deploy.DB))

    def test_rehearsal_rejects_rewriting_old_ledger_and_does_not_touch_live(self):
        with self.fixture() as (_, __):
            backup = deploy.DATA / 'rehearsal'
            backup.mkdir()
            before = deploy.table_hashes(deploy.DB)
            def bad_migrate(path):
                with closing(sqlite3.connect(path)) as db:
                    db.execute('DELETE FROM ve_task')
                    db.commit()
            with patch.object(deploy, 'migrate', side_effect=bad_migrate):
                with self.assertRaisesRegex(RuntimeError, 'migration_changed_existing_ledger'):
                    deploy.rehearse(backup)
            self.assertEqual(deploy.table_hashes(deploy.DB), before)

    def test_rollback_restores_exact_files_but_preserves_new_business_rows(self):
        with self.fixture() as (root, plan):
            path, manifest = self.installed_manifest(root, plan)
            with closing(sqlite3.connect(deploy.DB)) as db:
                db.executescript("CREATE TABLE ve_material_replication(id TEXT,status TEXT);"
                                 "INSERT INTO ve_material_replication VALUES('new-receipt','accepted');")
            before = deploy.table_hashes(deploy.DB)
            with patch.object(deploy, 'run') as run, patch.object(deploy, 'health'), \
                 patch.object(deploy, 'protected_state', side_effect=self.protected):
                result = deploy.rollback(path, manifest)
            self.assertEqual(result['status'], 'rolled_back_ledger_preserved')
            self.assertEqual(deploy.table_hashes(deploy.DB), before)
            self.assertTrue(deploy.MARKER.exists())
            for row in plan:
                self.assertEqual(deploy.file_sha(row['path']), row['before'])
            mutations = [call.args for call in run.call_args_list]
            self.assertEqual(mutations[0], ('systemctl', 'disable', '--now', deploy.WORKER))
            self.assertFalse(any(unit in args for args in mutations for unit in deploy.PROTECTED))

    def test_bad_backup_rejects_rollback_before_marker_or_service_changes(self):
        with self.fixture() as (root, plan):
            path, manifest = self.installed_manifest(root, plan)
            (path.parent / manifest['files'][0]['backup']).write_bytes(b'corrupted')
            with patch.object(deploy, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'backup_checksum_mismatch'):
                    deploy.rollback(path, manifest)
            run.assert_not_called()
            self.assertFalse(deploy.MARKER.exists())

    def test_install_failure_restores_files_and_keeps_migrated_records(self):
        with self.fixture() as (_, plan):
            def migrate(path):
                with closing(sqlite3.connect(path)) as db:
                    db.executescript("CREATE TABLE ve_material_replication(id TEXT);"
                                     "INSERT INTO ve_material_replication VALUES('preserve-me');")
            def state(unit):
                return {'ActiveState': 'active', 'MainPID': '12'}
            with patch.object(deploy, 'protected_state', side_effect=self.protected), \
                 patch.object(deploy, 'state', side_effect=state), patch.object(deploy, 'run'), \
                 patch.object(deploy, 'rehearse', return_value={'result': 'passed'}), \
                 patch.object(deploy, 'migrate', side_effect=migrate), \
                 patch.object(deploy, 'health', side_effect=[RuntimeError('api_health_failed'), None]):
                with self.assertRaisesRegex(RuntimeError, 'api_health_failed'):
                    deploy.apply(COMMIT, plan)
            for row in plan:
                self.assertEqual(deploy.file_sha(row['path']), row['before'])
            self.assertEqual(deploy.table_hashes(deploy.DB)['ve_material_replication']['rows'], 1)
            manifests = list((deploy.DATA / 'backups').glob('*/manifest.json'))
            self.assertEqual(json.loads(manifests[0].read_text())['status'],
                             'failed_files_restored_ledger_preserved')
            self.assertTrue(deploy.MARKER.exists())

    def test_enable_only_removes_owned_marker_after_checks(self):
        with self.fixture() as (root, plan):
            path, manifest = self.installed_manifest(root, plan)
            deploy.MARKER.write_bytes(deploy.MARKER_TEXT)
            before = deploy.table_hashes(deploy.DB)
            with patch.object(deploy, 'secret_preflight'), patch.object(deploy, 'health'), \
                 patch.object(deploy, 'state', return_value={'ActiveState': 'active', 'MainPID': '91'}), \
                 patch.object(deploy, 'protected_state', side_effect=self.protected):
                self.assertEqual(deploy.enable(path, manifest)['status'], 'enabled')
                self.assertEqual(deploy.enable(path, manifest)['status'], 'enabled')
            self.assertFalse(deploy.MARKER.exists())
            self.assertEqual(deploy.table_hashes(deploy.DB), before)

    def test_partial_install_before_unit_exists_restores_without_disabling_absent_unit(self):
        with self.fixture() as (_, plan):
            real_atomic = deploy.atomic
            writes = []
            def fail_once(path, data, mode=0o644):
                if str(path) == plan[1]['path'] and not writes:
                    writes.append(str(path))
                    raise RuntimeError('simulated_write_failure')
                return real_atomic(path, data, mode)
            def state(unit):
                return {'ActiveState': 'inactive' if unit == deploy.WORKER else 'active', 'MainPID': '0'}
            with patch.object(deploy, 'atomic', side_effect=fail_once), \
                 patch.object(deploy, 'protected_state', side_effect=self.protected), \
                 patch.object(deploy, 'state', side_effect=state), patch.object(deploy, 'run') as run, \
                 patch.object(deploy, 'rehearse', return_value={'result': 'passed'}), patch.object(deploy, 'health'):
                with self.assertRaisesRegex(RuntimeError, 'simulated_write_failure'):
                    deploy.apply(COMMIT, plan)
            self.assertFalse(any(call.args == ('systemctl', 'disable', '--now', deploy.WORKER)
                                 for call in run.call_args_list))
            for row in plan:
                self.assertEqual(deploy.file_sha(row['path']), row['before'])

    def test_enable_can_reconcile_lost_manifest_acknowledgement(self):
        with self.fixture() as (root, plan):
            path, manifest = self.installed_manifest(root, plan)
            self.assertEqual(manifest['status'], 'installed_disabled')
            self.assertFalse(deploy.MARKER.exists())
            with patch.object(deploy, 'secret_preflight'), patch.object(deploy, 'health'), \
                 patch.object(deploy, 'state', return_value={'ActiveState': 'active', 'MainPID': '91'}), \
                 patch.object(deploy, 'protected_state', side_effect=self.protected):
                self.assertEqual(deploy.enable(path, manifest)['status'], 'enabled')

    def test_protected_worker_pid_is_not_just_an_active_state_check(self):
        before = self.protected()
        after = self.protected()
        after[deploy.PROTECTED[0]]['MainPID'] = '999'
        with patch.object(deploy, 'protected_state', return_value=after):
            with self.assertRaisesRegex(RuntimeError, 'protected_worker_changed'):
                deploy.check_protected(before)

    def test_missing_new_worker_state_is_allowed_but_missing_api_is_not(self):
        result = SimpleNamespace(returncode=1, stdout=b'LoadState=not-found\nActiveState=inactive\nMainPID=0\n')
        with patch.object(deploy.subprocess, 'run', return_value=result):
            self.assertEqual(deploy.state(deploy.WORKER)['LoadState'], 'not-found')
            with self.assertRaisesRegex(RuntimeError, 'service_state_unavailable'):
                deploy.state(deploy.API)


if __name__ == '__main__':
    unittest.main()
