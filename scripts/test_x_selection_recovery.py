import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import test_x_auto_post_publisher as fixtures
from features.x_auto_posts.selection_recovery import (
    ClaimLifetime, execution_available, file_lock, recover_proven_selection)
from scripts.x_auto_selection_watchdog import external_proof
from features.pipeline_dispatch.drain_diagnostics import read_snapshot
from features.pipeline_dispatch.drain_gateway import authenticate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.XAutoPostPublisherTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture
        self.store, self.clock, self.task = fixture.store, fixture.clock, fixture.task

    def claim(self):
        return self.store.claim_next_executable_task(worker_id='test', lease_seconds=10800)

    def row(self):
        with self.store._reader() as c:
            return dict(c.execute('SELECT * FROM x_auto_task WHERE id=?', (self.task.id,)).fetchone())

    def recover(self):
        return recover_proven_selection(self.store, self.task.id, self.row()['updated_at'],
                                         proof={'executor_ended': True})

    def test_selection_lease_and_heartbeat_then_prepare_lease(self):
        claim = self.claim()
        self.assertEqual(dt.datetime.fromisoformat(self.row()['lease_expires_at_utc']) - self.clock(), dt.timedelta(seconds=180))
        self.clock.value += dt.timedelta(seconds=90)
        heartbeat = ClaimLifetime(self.store, claim, True, lease_seconds=10800, now_fn=self.clock)
        self.assertTrue(heartbeat.pulse())
        self.assertEqual(dt.datetime.fromisoformat(self.row()['lease_expires_at_utc']) - self.clock(), dt.timedelta(seconds=180))
        with self.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET status='preparing' WHERE id=?", (self.task.id,))
        self.assertTrue(heartbeat.pulse())
        self.assertEqual(dt.datetime.fromisoformat(self.row()['lease_expires_at_utc']) - self.clock(), dt.timedelta(seconds=900))

    def test_ended_selection_recovers_once_and_is_audited(self):
        self.claim()
        self.assertTrue(self.recover())
        self.assertFalse(self.recover())
        row = self.row()
        self.assertEqual((row['status'], row['claim_token'], row['claim_phase']), ('retry_wait', '', 'selection'))
        with self.store._reader() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM x_auto_event WHERE event_type='task_selection_claim_recovered'").fetchone()[0], 1)

    def test_each_external_effect_blocks_recovery(self):
        self.claim()
        for column, value in [('material_id', '42'), ('execution_run_id', 1), ('execution_queue_id', 2),
                              ('execution_log_id', 3), ('unknown_outcome', 1), ('publish_attempt_count', 1),
                              ('preparation_attempt_count', 1), ('publish_id', 'post-id')]:
            with self.subTest(column=column):
                with self.store._transaction() as c:
                    original = self.row()[column]
                    c.execute('UPDATE x_auto_task SET ' + column + '=? WHERE id=?', (value, self.task.id))
                self.assertFalse(self.recover())
                with self.store._transaction() as c:
                    c.execute('UPDATE x_auto_task SET ' + column + '=? WHERE id=?', (original, self.task.id))

    def test_ledger_and_concurrent_update_block_recovery(self):
        token, _ = self.f._reserve()
        with self.store._transaction() as c:
            c.execute("UPDATE x_auto_task SET status='selecting',material_id='',reserved_at_utc='' WHERE id=?", (self.task.id,))
        self.assertFalse(self.recover())
        self.assertFalse(recover_proven_selection(self.store, self.task.id, 'old', proof={}))

    def test_expired_selection_is_prioritized_over_pending(self):
        claim = self.claim()
        self.clock.value += dt.timedelta(seconds=181)
        self.store.create_task(run_id=self.f.run.id, account_id='641', language='en',
                               account_snapshot_version=1, account_snapshot={})
        self.assertEqual(self.claim().task.id, claim.task.id)

    @unittest.skipIf(os.name == 'nt', 'Linux production flock contract')
    def test_active_request_cannot_be_reclaimed_despite_expired_heartbeat(self):
        claim = self.claim()
        locks = Path(self.f.temp.name) / 'locks'
        with file_lock(locks, str(self.task.id) + '.lock'):
            self.clock.value += dt.timedelta(seconds=181)
            self.assertFalse(execution_available(locks, self.task.id))
            self.assertIsNone(self.store.claim_next_executable_task(worker_id='other', lease_seconds=10800,
                candidate_guard=lambda task_id: execution_available(locks, task_id)))
        self.assertTrue(execution_available(locks, self.task.id))

    @unittest.skipIf(os.name == 'nt', 'Linux production flock contract')
    def test_error_handler_failure_releases_execution_lock_for_recovery(self):
        locks = Path(self.f.temp.name) / 'locks'
        executor = self.f._executor(fixtures.FakeBridge(self.task))
        with patch.dict(os.environ, {'X_AUTO_EXECUTION_LOCK_DIR': str(locks)}), \
                patch.object(executor, '_select', side_effect=RuntimeError('query failed')), \
                patch.object(executor, '_retry_or_fail', side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(sqlite3.OperationalError):
                executor.execute_next('test')
        self.assertTrue(execution_available(locks, self.task.id))
        self.assertTrue(self.recover())


class ExternalProofTests(unittest.TestCase):
    def test_external_execution_and_dispatch_receipts_are_never_retried(self):
        with sqlite3.connect(':memory:') as posts, sqlite3.connect(':memory:') as dispatch:
            dispatch.row_factory = sqlite3.Row
            posts.execute('CREATE TABLE x_post_manual_run(external_task_key TEXT)')
            dispatch.execute('CREATE TABLE pipeline_job(id INTEGER,source_key TEXT,generation INTEGER,claim_hash TEXT,external_intent TEXT,canonical_receipt TEXT,stage TEXT,status TEXT)')
            for table in ('pipeline_operation', 'pipeline_receipt', 'pipeline_claim_history'):
                dispatch.execute('CREATE TABLE ' + table + '(job_id INTEGER)')
            self.assertEqual(external_proof(1, posts, dispatch), '')
            posts.execute("INSERT INTO x_post_manual_run VALUES('x-auto-task-1')")
            self.assertEqual(external_proof(1, posts, dispatch), 'native_execution_exists')
            posts.execute('DELETE FROM x_post_manual_run')
            dispatch.execute("INSERT INTO pipeline_job VALUES(1,'x:auto:1',0,'','','','prepare','legacy_running')")
            for table in ('pipeline_operation', 'pipeline_receipt', 'pipeline_claim_history'):
                dispatch.execute('INSERT INTO ' + table + ' VALUES(1)')
                self.assertEqual(external_proof(1, posts, dispatch), 'dispatch_history_exists')
                dispatch.execute('DELETE FROM ' + table)

    def test_stale_diagnostics_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            path.write_text(json.dumps({'checked_at': '2020-01-01T00:00:00+00:00', 'holds': []}))
            self.assertTrue(read_snapshot(path)['stale'])


class DiagnosticAuthTests(unittest.TestCase):
    def test_missing_cookie_does_not_read_snapshot_or_call_auth(self):
        with patch('http.client.HTTPConnection') as connection:
            self.assertEqual(authenticate(''), 401)
            connection.assert_not_called()

    def test_only_existing_authenticated_admin_is_allowed(self):
        for auth, expected in [({}, 401),
                ({'authenticated': True, 'user': {'user_id': 'u', 'is_admin': False}}, 403),
                ({'authenticated': True, 'user': {'user_id': 'u', 'is_admin': True}}, 200)]:
            with self.subTest(expected=expected), patch('http.client.HTTPConnection') as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = json.dumps(auth).encode()
                self.assertEqual(authenticate('existing-cookie'), expected)


if __name__ == '__main__':
    unittest.main()
