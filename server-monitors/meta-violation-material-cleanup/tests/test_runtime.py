import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meta_violation_cleanup import runtime as module
from meta_violation_cleanup.runtime import Runtime, RuntimeLedgerError


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.tmp.name)
        self.runtime.begin('run-1', 'preview', {'marker': 'original'})

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_run_id_and_artifact_traversal_rejected(self):
        for run_id in ('../escape', '/tmp/x', 'a/b', 'a\\b', '', '.', 'a' * 101, '中文'):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    self.runtime.begin(run_id, 'run', {})
                with self.assertRaises(ValueError):
                    self.runtime.load_artifact(run_id, 'manifest.json')
        for name in ('../x', '/x', 'a/b', 'a\\b', 'a..json', ''):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.runtime.artifact('run-1', name, {})

    def test_duplicate_begin_is_immutable(self):
        self.assertFalse(self.runtime.begin('run-1', 'run', {'marker': 'replacement'}))
        row = self.runtime.get('run-1')
        self.assertEqual(row['mode'], 'preview')
        self.assertEqual(row['data']['marker'], 'original')

    def test_artifact_atomic_durable_and_loadable(self):
        with patch.object(module, '_fsync_directory') as sync:
            filename = self.runtime.artifact('run-1', 'manifest.json', {'中文': [1]})
        self.assertEqual(self.runtime.load_artifact('run-1', 'manifest.json'), {'中文': [1]})
        self.assertEqual(Path(filename).read_text(encoding='utf-8'), '{"中文":[1]}')
        self.assertEqual(len(sync.call_args_list), 3)
        self.assertEqual(list(Path(filename).parent.glob('.write-*')), [])

    def test_artifact_failed_replace_preserves_old_and_fences_writes(self):
        filename = self.runtime.artifact('run-1', 'manifest.json', {'value': 1})
        with patch.object(module.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(RuntimeLedgerError):
                self.runtime.artifact('run-1', 'manifest.json', {'value': 2})
        self.assertEqual(json.loads(Path(filename).read_text()), {'value': 1})
        self.assertEqual(list(Path(filename).parent.glob('.write-*')), [])
        with self.assertRaises(RuntimeLedgerError):
            self.runtime.write_barrier('run-1')

    def test_artifact_symlink_rejected(self):
        target = Path(self.tmp.name) / 'outside.json'
        target.write_text('{}')
        folder = Path(self.tmp.name) / 'runs' / 'run-1'
        folder.mkdir(parents=True)
        try:
            (folder / 'manifest.json').symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink privilege unavailable')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.runtime.load_artifact('run-1', 'manifest.json')

    def test_write_failure_permanently_fences_new_writes(self):
        self.runtime.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(RuntimeLedgerError):
            self.runtime.write_barrier('run-1')
        self.runtime.db.execute('PRAGMA query_only=OFF')
        with self.assertRaises(RuntimeLedgerError):
            self.runtime.event('run-1', 'must_not_resume', {})
        count = self.runtime.db.execute('SELECT count(*) FROM events').fetchone()[0]
        self.assertEqual(count, 0)

    def test_write_barrier_is_a_durable_event(self):
        self.runtime.write_barrier('run-1')
        other = Runtime(self.tmp.name)
        try:
            row = other.db.execute('SELECT kind FROM events').fetchone()
            self.assertEqual(row['kind'], 'mutation_write_barrier')
        finally:
            other.close()

    def test_inventory_conflicts_roll_back_entire_batch(self):
        self.runtime.inventory('run-1', '11', [{'id': '1', 'creative': {'id': '9'}}])
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            self.runtime.inventory('run-1', '11', [
                {'id': '2', 'creative': {'id': '8'}},
                {'id': '1', 'creative': {'id': '10'}},
            ])
        self.assertEqual(self.runtime.references('run-1', '8'), [])
        self.assertEqual(len(self.runtime.references('run-1', '9')), 1)
        self.runtime.write_barrier('run-1')  # invalid input is not a disk failure

    def test_recovery_only_dead_owned_running_rows_and_notices(self):
        self.runtime.begin('finished', 'run', {})
        self.runtime.update('finished', status='completed')
        self.runtime.claim_notice('run-1', 'start', 'body')
        self.runtime.start_notice('run-1', 'start')
        with patch.object(module, '_owner_alive', return_value=False):
            result = self.runtime.recover_interrupted()
        self.assertEqual(result['run_ids'], ['run-1'])
        self.assertEqual(result['notices_unknown'], 1)
        self.assertEqual(self.runtime.get('run-1')['status'], 'interrupted')
        self.assertTrue(self.runtime.get('run-1')['data']['recovery_pending'])
        self.assertEqual(self.runtime.get('finished')['status'], 'completed')
        self.assertEqual(self.runtime.notice('run-1', 'start')['status'], 'unknown')
        with patch.object(module, '_owner_alive', return_value=False):
            self.assertEqual(self.runtime.recover_interrupted()['run_ids'], ['run-1'])
        self.runtime.update('run-1', recovery_pending=False)
        with patch.object(module, '_owner_alive', return_value=False):
            self.assertEqual(self.runtime.recover_interrupted()['run_ids'], [])

    def test_pending_recovery_survives_reopen_and_preserves_shared_identity(self):
        self.runtime.update('run-1', shared_job_id='policy-cleanup-run-1', shared_run_id='shared-1')
        with patch.object(module, '_owner_alive', return_value=False):
            self.runtime.recover_interrupted()
        self.runtime.close()
        self.runtime = Runtime(self.tmp.name)
        with patch.object(module, '_owner_alive', return_value=False):
            result = self.runtime.recover_interrupted()
        self.assertEqual(result['run_ids'], ['run-1'])
        self.assertEqual(self.runtime.get('run-1')['data']['shared_run_id'], 'shared-1')
        count = self.runtime.db.execute("SELECT count(*) FROM events WHERE kind='run_recovered'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_pending_recovery_does_not_return_alive_or_unknown_owners(self):
        self.runtime.update('run-1', status='interrupted', recovery_pending=True)
        for state in (True, None):
            with patch.object(module, '_owner_alive', return_value=state):
                self.assertEqual(self.runtime.recover_interrupted()['run_ids'], [])
        self.assertTrue(self.runtime.get('run-1')['data']['recovery_pending'])

    def test_alive_and_indeterminate_owners_remain_fenced(self):
        for state in (True, None):
            with patch.object(module, '_owner_alive', return_value=state):
                self.assertEqual(self.runtime.recover_interrupted()['run_ids'], [])
            self.assertEqual(self.runtime.get('run-1')['status'], 'running')

    def test_opening_runtime_never_automatically_recovers(self):
        with patch.object(module, '_owner_alive', return_value=False):
            other = Runtime(self.tmp.name)
            try:
                self.assertEqual(other.get('run-1')['status'], 'running')
            finally:
                other.close()

    def test_running_transition_refreshes_owner_and_end_time(self):
        self.runtime.update('run-1', status='interrupted')
        with patch.object(module, '_owner', return_value={'pid': 987, 'start': 'fresh'}):
            self.runtime.update('run-1', status='running', execution_attempt=2)
        row = self.runtime.get('run-1')
        self.assertIsNone(row['ended'])
        self.assertEqual(row['data']['_owner'], {'pid': 987, 'start': 'fresh'})

    def test_two_connections_only_one_can_claim_notice(self):
        other = Runtime(self.tmp.name)
        try:
            first = self.runtime.claim_notice('run-1', 'start', 'original')
            second = other.claim_notice('run-1', 'start', 'changed')
            self.assertEqual(first['body'], second['body'])
            self.assertEqual(first['uuid'], second['uuid'])
            self.assertTrue(self.runtime.start_notice('run-1', 'start')['claimed'])
            self.assertFalse(other.start_notice('run-1', 'start')['claimed'])
        finally:
            other.close()

    def test_unknown_notice_cannot_be_reset_or_claimed(self):
        self.runtime.claim_notice('run-1', 'end', 'original')
        self.runtime.start_notice('run-1', 'end')
        self.runtime.finish_notice('run-1', 'end', 'unknown', {'reason': 'timeout'})
        self.assertFalse(self.runtime.start_notice('run-1', 'end')['claimed'])
        with self.assertRaisesRegex(ValueError, 'terminal'):
            self.runtime.finish_notice('run-1', 'end', 'failed', {})

    def test_continuation_notice_phases(self):
        for phase in ('start', 'end', 'start-2', 'end-10', 'end-part-001', 'end-2-part-010', 'end-part-1000'):
            self.runtime.claim_notice('run-1', phase, 'body')
        for phase in ('start-1', 'end-0', 'start-02', 'end-x', '../end', 'end-part-000',
                      'end-part-01', 'end-part-0001', 'end-1-part-001', 'end-2-part-001-part-001'):
            with self.assertRaises(ValueError):
                self.runtime.claim_notice('run-1', phase, 'body')

    def test_multipart_parent_cannot_be_sent_before_all_parts_are_sent(self):
        self.runtime.claim_notice('run-1', 'end', 'entire body')
        plan = {'delivery': 'multipart', 'parts': []}
        for index in (1, 2):
            phase, body = 'end-part-00' + str(index), 'body-' + str(index)
            self.runtime.claim_notice('run-1', phase, body)
            plan['parts'].append({'phase': phase, 'body_sha256': module.digest(body)})
        self.runtime.start_notice('run-1', 'end', receipt=plan)
        with self.assertRaisesRegex(ValueError, 'plan_changed'):
            self.runtime.finish_notice('run-1', 'end', 'sent', {})
        with self.assertRaisesRegex(ValueError, 'not_all_sent'):
            self.runtime.finish_notice('run-1', 'end', 'sent', plan)
        for part in plan['parts']:
            self.runtime.start_notice('run-1', part['phase'])
            receipt = {'code': 0, 'message_id': part['phase']}
            self.runtime.finish_notice('run-1', part['phase'], 'sent', receipt)
            part['receipt'] = receipt
        self.runtime.finish_notice('run-1', 'end', 'sent', plan)
        self.assertEqual(self.runtime.notice('run-1', 'end')['status'], 'sent')

    def test_multipart_plan_survives_dead_owner_recovery(self):
        self.runtime.claim_notice('run-1', 'end', 'entire body')
        parts = []
        for index in (1, 2):
            phase = 'end-part-00' + str(index)
            self.runtime.claim_notice('run-1', phase, phase)
            parts.append({'phase': phase, 'body_sha256': module.digest(phase)})
        self.runtime.start_notice('run-1', 'end', receipt={'delivery': 'multipart', 'parts': parts})
        with patch.object(module, '_owner_alive', return_value=False):
            self.runtime.recover_interrupted()
        row = self.runtime.notice('run-1', 'end')
        self.assertEqual(row['status'], 'unknown')
        self.assertEqual(json.loads(row['receipt'])['parts'], parts)

    def test_owner_pid_reuse_does_not_block_recovery(self):
        with patch.object(module, '_process_start', return_value='new-start'):
            self.assertFalse(module._owner_alive(88, 'old-start'))
        with patch.object(module, '_process_start', return_value=None):
            self.assertIsNone(module._owner_alive(88, 'old-start'))

    def test_old_notice_schema_is_migrated_without_losing_body(self):
        self.runtime.close()
        path = Path(self.tmp.name) / 'runs.sqlite3'
        db = sqlite3.connect(str(path))
        db.execute('DROP TABLE notices')
        db.execute('CREATE TABLE notices(run_id TEXT,phase TEXT,uuid TEXT,status TEXT,body TEXT,receipt TEXT,updated TEXT,PRIMARY KEY(run_id,phase))')
        db.execute("INSERT INTO notices VALUES('run-1','start','uuid','sending','preserved','{}','today')")
        db.commit()
        db.close()
        self.runtime = Runtime(self.tmp.name)
        self.assertEqual(self.runtime.notice('run-1', 'start')['body'], 'preserved')
        self.assertIsNone(self.runtime.notice('run-1', 'start')['owner_pid'])
        self.assertFalse(self.runtime.start_notice('run-1', 'start')['claimed'])


class LegacyLockTests(unittest.TestCase):
    def test_priority_window_boundaries(self):
        for hour, minute, expected in ((9, 54, False), (9, 55, True),
                                      (10, 0, True), (10, 4, True), (10, 5, False)):
            value = module.datetime(2026, 10, 23, hour, minute, tzinfo=module.CST)
            self.assertEqual(module._legacy_priority(value), expected)

    def test_lease_rechecks_priority_and_releases_lock(self):
        fake = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4)
        calls = []
        fake.flock = lambda fp, operation: calls.append(operation)
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(sys.modules, {'fcntl': fake}):
                with patch.object(module, '_legacy_priority', side_effect=[False, False, True]):
                    with self.assertRaisesRegex(InterruptedError, 'legacy_priority_window'):
                        with module.legacy_delete_lock(str(Path(folder) / 'legacy.lock'), lambda: False) as lease:
                            lease.check()
        self.assertEqual(calls, [3, 4])

    def test_wait_for_legacy_window_before_acquisition(self):
        fake = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=lambda *a: None)
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(sys.modules, {'fcntl': fake}):
                with patch.object(module, '_legacy_priority', side_effect=[True, False, False]):
                    with patch.object(module.time, 'sleep') as sleep:
                        with module.legacy_delete_lock(str(Path(folder) / 'legacy.lock'), lambda: False):
                            pass
                        sleep.assert_called_once_with(5)


if __name__ == '__main__':
    unittest.main()
