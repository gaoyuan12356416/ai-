from contextlib import closing, contextmanager, ExitStack
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import deploy_video_editing_material_category as deploy

common = deploy.common
COMMIT = 'c' * 40


class MaterialCategoryDeployTests(unittest.TestCase):
    @contextmanager
    def fixture(self, closed=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            marker = root / 'etc' / 'replication.disabled'
            marker.parent.mkdir()
            if closed:
                marker.write_bytes(b'operator paused existing feature\n')
            stack.enter_context(patch.multiple(common, LIVE=root / 'live', PUBLIC=root / 'public',
                                              DATA=data, DB=data / 'state.sqlite3', MARKER=marker))
            baseline, candidate = {}, {}
            for target, source in deploy.targets():
                baseline[source] = b'baseline = True\n'
                candidate[source] = b'category = True\n'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(baseline[source])
            other = common.LIVE / 'unrelated.py'
            other.write_bytes(b'unrelated production overlay\n')
            with closing(sqlite3.connect(common.DB)) as db:
                db.executescript('CREATE TABLE ve_material_replication(id TEXT PRIMARY KEY,status TEXT,data TEXT);')
                db.execute('INSERT INTO ve_material_replication VALUES(?,?,?)',
                           ('old', 'accepted', json.dumps({'request': {'operator': 'susiqi'}})))
                db.commit()
            stack.enter_context(patch.object(common, 'git_bytes', side_effect=lambda rev, name:
                baseline[name] if rev == deploy.BASE else candidate[name]))
            states = {unit: {'ActiveState': 'active', 'MainPID': str(10 + index), 'LoadState': 'loaded'}
                      for index, unit in enumerate((common.API, common.WORKER, *common.PROTECTED))}
            def run(*args, **kwargs):
                if args[0] == 'systemctl' and len(args) == 3:
                    _, action, unit = args
                    if action == 'stop':
                        states[unit].update(ActiveState='inactive', MainPID='0')
                    elif action in {'start', 'restart'}:
                        states[unit].update(ActiveState='active', MainPID='100')
                return ''
            calls = stack.enter_context(patch.object(common, 'run', side_effect=run))
            stack.enter_context(patch.object(common, 'state', side_effect=lambda unit: dict(states[unit])))
            stack.enter_context(patch.object(common, 'health'))
            yield root, deploy.build_plan(COMMIT), calls

    def test_only_seven_targets_no_schema_config_units_or_other_modules(self):
        with self.fixture() as (_, plan, __):
            self.assertEqual(len(plan), 7)
            self.assertEqual(sum(row['source'] == deploy.MODULE for row in plan), 1)
            self.assertEqual(sum(row['source'] == 'static/video-editing.js' for row in plan), 2)
            self.assertFalse(any(row['path'] in {str(common.CONFIG), str(common.UNIT)} for row in plan))
            self.assertFalse(any(row['source'].endswith('store.py') for row in plan))

    def test_runtime_drift_is_rejected_without_replacing_composite_files(self):
        with self.fixture() as (_, plan, __):
            target = Path(plan[0]['path'])
            target.write_bytes(b'newer composite production change\n')
            with self.assertRaisesRegex(RuntimeError, 'live_file_drift'):
                deploy.build_plan(COMMIT)
            self.assertEqual(target.read_bytes(), b'newer composite production change\n')

    def test_default_check_does_not_close_gate_backup_or_restart(self):
        with patch.object(common, 'storage'), patch.object(deploy, 'published', return_value=COMMIT), \
             patch.object(deploy, 'build_plan', return_value=[]), patch.object(deploy, 'submitting', return_value=0), \
             patch.object(deploy, 'apply') as apply, patch.object(common, 'atomic') as atomic, \
             patch.object(common, 'deployment_lock') as lock, patch('builtins.print'):
            deploy.main(['--commit', COMMIT])
        apply.assert_not_called()
        atomic.assert_not_called()
        lock.assert_not_called()

    def test_published_uses_category_branch_and_production_baseline(self):
        def run(*args, **kwargs):
            if 'rev-parse' in args: return COMMIT
            if 'get-url' in args: return 'git@github.com:gaoyuan12356416/ai-.git'
            if 'ls-remote' in args: return COMMIT + '\t' + deploy.REF
            return ''
        with patch.object(common, 'run', side_effect=run) as calls:
            self.assertEqual(deploy.published(COMMIT), COMMIT)
        self.assertTrue(any(deploy.BASE in call.args and 'merge-base' in call.args for call in calls.call_args_list))
        with patch.object(common, 'run', side_effect=lambda *args, **kwargs:
                          ' M changed.py' if 'status' in args else run(*args, **kwargs)):
            with self.assertRaisesRegex(RuntimeError, 'dirty_release_checkout'):
                deploy.published(COMMIT)

    def test_closed_gate_is_preserved_and_foreign_new_marker_never_removed(self):
        with self.fixture(closed=True):
            prior = deploy.gate_state()
            deploy.close_gate(prior)
            deploy.restore_gate(prior)
            self.assertEqual(common.MARKER.read_bytes(), b'operator paused existing feature\n')
            common.MARKER.unlink()
            prior = deploy.gate_state()
            deploy.close_gate(prior)
            common.MARKER.write_bytes(b'new independent pause\n')
            with self.assertRaisesRegex(RuntimeError, 'foreign_gate_preserved'):
                deploy.restore_gate(prior)
            self.assertEqual(common.MARKER.read_bytes(), b'new independent pause\n')

    def test_drain_writer_barrier_observes_preexisting_outbound_transaction(self):
        with self.fixture():
            ready, release, done = threading.Event(), threading.Event(), threading.Event()
            result = []
            def writer():
                with closing(sqlite3.connect(common.DB, isolation_level=None)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE ve_material_replication SET status='submitting'")
                    ready.set()
                    release.wait(5)
                    db.commit()
            def reader():
                result.append(deploy.submitting(barrier=True))
                done.set()
            write_thread = threading.Thread(target=writer)
            read_thread = threading.Thread(target=reader)
            write_thread.start()
            self.assertTrue(ready.wait(2))
            read_thread.start()
            try:
                self.assertFalse(done.wait(0.05))
            finally:
                release.set()
                write_thread.join(3)
                read_thread.join(3)
            self.assertEqual(result, [1])

    def test_drain_timeout_does_not_stop_workers_or_change_live_files(self):
        with self.fixture() as (_, plan, calls):
            before = common.table_hashes(common.DB)
            with patch.object(deploy, 'drain', side_effect=RuntimeError('outbound_request_not_drained_worker_untouched')):
                with self.assertRaisesRegex(RuntimeError, 'not_drained'):
                    deploy.apply(COMMIT, plan, 1)
            self.assertFalse(common.MARKER.exists())
            self.assertEqual(common.table_hashes(common.DB), before)
            calls.assert_not_called()
            for row in plan:
                self.assertEqual(common.file_sha(row['path']), row['before'])

    def test_apply_restores_open_gate_only_restarts_scoped_services_and_never_migrates(self):
        with self.fixture() as (_, plan, calls):
            before = common.table_hashes(common.DB)
            with patch.object(common, 'migrate') as migrate:
                result = deploy.apply(COMMIT, plan, 1)
            migrate.assert_not_called()
            self.assertEqual(result['status'], 'installed')
            self.assertFalse(common.MARKER.exists())
            self.assertEqual(common.table_hashes(common.DB), before)
            self.assertEqual((common.LIVE / 'unrelated.py').read_bytes(), b'unrelated production overlay\n')
            actions = [call.args for call in calls.call_args_list]
            self.assertEqual(actions[0], ('systemctl', 'stop', common.WORKER))
            self.assertFalse(any(unit in args for args in actions for unit in common.PROTECTED))
            self.assertTrue(all(common.file_sha(row['path']) == row['after'] for row in plan))

    def test_apply_preserves_preexisting_closed_gate(self):
        with self.fixture(closed=True) as (_, plan, __):
            result = deploy.apply(COMMIT, plan, 1)
            self.assertEqual(result['status'], 'installed')
            self.assertEqual(common.MARKER.read_bytes(), b'operator paused existing feature\n')

    def test_rollback_keeps_new_category_receipts_and_current_operator_pause(self):
        with self.fixture() as (_, plan, __):
            result = deploy.apply(COMMIT, plan, 1)
            path, manifest = deploy.load_manifest(result['manifest'])
            with closing(sqlite3.connect(common.DB)) as db:
                db.execute('INSERT INTO ve_material_replication VALUES(?,?,?)',
                           ('new-category', 'accepted', json.dumps({'request': {'material_category': 'image'}})))
                db.commit()
            before = common.table_hashes(common.DB)
            common.MARKER.write_bytes(b'operator pause after category release\n')
            result = deploy.rollback(path, manifest, 1)
            self.assertEqual(result['status'], 'rolled_back_category_files_ledger_preserved')
            self.assertEqual(common.table_hashes(common.DB), before)
            self.assertEqual(common.MARKER.read_bytes(), b'operator pause after category release\n')
            self.assertTrue(all(common.file_sha(row['path']) == row['before'] for row in plan))

    def test_partial_install_failure_restores_only_installed_files_and_original_gate(self):
        with self.fixture() as (_, plan, __):
            atomic, failed = common.atomic, []
            def fail_once(path, data, mode=0o644):
                if str(path) == plan[1]['path'] and not failed:
                    failed.append(True)
                    raise RuntimeError('write_failed')
                return atomic(path, data, mode)
            before = common.table_hashes(common.DB)
            with patch.object(common, 'atomic', side_effect=fail_once):
                with self.assertRaisesRegex(RuntimeError, 'write_failed'):
                    deploy.apply(COMMIT, plan, 1)
            self.assertFalse(common.MARKER.exists())
            self.assertEqual(common.table_hashes(common.DB), before)
            self.assertTrue(all(common.file_sha(row['path']) == row['before'] for row in plan))

    def test_bad_backup_rejects_rollback_before_gate_or_service_mutation(self):
        with self.fixture() as (_, plan, calls):
            result = deploy.apply(COMMIT, plan, 1)
            path, manifest = deploy.load_manifest(result['manifest'])
            (path.parent / manifest['files'][0]['backup']).write_bytes(b'bad backup')
            calls.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'backup_checksum_mismatch'):
                deploy.rollback(path, manifest, 1)
            calls.assert_not_called()
            self.assertFalse(common.MARKER.exists())


if __name__ == '__main__':
    unittest.main()
