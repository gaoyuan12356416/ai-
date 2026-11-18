"""Source outages must not pause writes or mask concurrent integrity failures."""
from contextlib import contextmanager
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import cover
from meta_violation_cleanup.sql_source import SqlSourceError
from meta_violation_cleanup.cover_pipeline import run_lanes
import threading


class SourceRetryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.pause = Path(folder.name) / 'cover-write-paused.json'
        self.config = dict(runtime_dir=folder.name, cover_live_enabled=True,
                           shared_ledger='unused', legacy_lock='unused')
        self.store, self.audit = MagicMock(), MagicMock()
        self.store.ready_tasks.return_value = [object()]
        self.store.pending_deletions.return_value = [object()]
        self.source = MagicMock()
        self.source.last_diagnostic = {}
        self.worker = MagicMock()
        self.worker.halted.is_set.return_value = False
        self.worker.halt_reason = ''
        self.worker.recover_reservations.return_value = {}
        self.worker.reconcile_unknown.return_value = {}
        self.worker.run_once.return_value = {'tasks': {'succeeded': 1}, 'seconds': 1}
        self.worker.process_deletions.return_value = []
        self.worker.timings.snapshot.return_value = {}

    def run_work(self, lanes, *, once=False, stop=lambda: False):
        @contextmanager
        def lease(*args):
            yield MagicMock()
        with patch.object(cover, 'Store'), patch.object(cover, 'CoverSource', return_value=self.source), \
             patch.object(cover, 'legacy_delete_lock', lease), patch.object(cover, 'run_lanes', lanes), \
             patch.object(cover, 'record_cycle'), \
             patch('meta_violation_cleanup.cover_worker.CoverWorker', return_value=self.worker):
            cover.work(self.config, self.store, self.audit, stop, once, 256)

    def test_ad_source_outage_backs_off_cleanup_continues_and_fresh_cycle_succeeds(self):
        stopped = []
        self.worker.run_once.side_effect = [SqlSourceError('sql_query_timeout'),
                                           {'tasks': {'succeeded': 1}, 'seconds': 1}]
        def lanes(cycle, stop, **kwargs):
            self.assertEqual(cycle('ads', stop), 30)
            cycle('cleanup', stop)
            cycle('ads', stop)
            stopped.append(True)
        self.run_work(lanes, stop=lambda: bool(stopped))
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.worker.run_once.call_count, 2)
        self.worker.process_deletions.assert_called_once()
        self.audit.artifact.assert_not_called()
        self.assertEqual(self.store.recover_inflight.call_count, 1)

    def test_startup_credentials_timeout_retries_without_durable_pause(self):
        stopped = []
        self.source.credentials.side_effect = [SqlSourceError('sql_query_timeout'), {}]
        def lanes(*args, **kwargs):
            stopped.append(True)
        with patch.object(cover.time, 'monotonic', side_effect=itertools.count(0, 31)):
            self.run_work(lanes, stop=lambda: bool(stopped))
        self.assertFalse(self.pause.exists())
        self.assertEqual(self.source.credentials.call_count, 2)
        self.worker.reconcile_unknown.assert_called_once()

    def test_backoff_caps_at_five_minutes_and_resets_after_success(self):
        stopped = []
        error = SqlSourceError('sql_query_timeout')
        self.worker.run_once.side_effect = [error] * 7 + [{'tasks': {}, 'seconds': 1}, error]
        def lanes(cycle, stop, **kwargs):
            self.assertEqual([cycle('ads', stop) for _ in range(7)], [30, 60, 120, 240, 300, 300, 300])
            cycle('ads', stop)
            self.assertEqual(cycle('ads', stop), 30)
            stopped.append(True)
        self.run_work(lanes, stop=lambda: bool(stopped))
        self.assertFalse(self.pause.exists())

    def test_once_returns_failure_without_clearing_or_creating_pause(self):
        self.source.credentials.side_effect = SqlSourceError('sql_query_timeout')
        with self.assertRaises(cover.RetryableSourceRead):
            self.run_work(MagicMock(), once=True)
        self.assertFalse(self.pause.exists())

    def test_read_only_proof_and_permission_errors_still_pause(self):
        for code, diagnostics in [('read_only_proof_missing', {}),
                                  ('sql_stream_incomplete', {'mysql_errors': [{'errno': 1045}]})]:
            with self.subTest(code=code):
                self.source.last_diagnostic = diagnostics
                self.source.credentials.side_effect = SqlSourceError(code)
                with self.assertRaises(cover.CoverWritePaused):
                    self.run_work(MagicMock(), once=True)
                self.assertTrue(self.pause.exists())
                self.pause.unlink()

    def test_concurrent_write_halt_takes_precedence_over_source_timeout(self):
        def failed(**kwargs):
            self.worker.halted.is_set.return_value = True
            self.worker.halt_reason = 'ad_parameters_changed_after_bind'
            raise SqlSourceError('sql_query_timeout')
        self.worker.run_once.side_effect = failed
        def lanes(cycle, stop, **kwargs):
            cycle('ads', stop)
        with self.assertRaises(cover.CoverWritePaused):
            self.run_work(lanes, once=True)
        self.assertIn('ad_parameters_changed_after_bind', self.pause.read_text())

    def test_once_read_failure_cannot_mask_sibling_write_failure_while_draining(self):
        started = threading.Barrier(2)
        def cycle(lane, stopped):
            started.wait(2)
            if lane == 'ads':
                raise cover.RetryableSourceRead(SqlSourceError('sql_query_timeout'), {})
            while not stopped():
                threading.Event().wait(.01)
            raise ValueError('write_integrity_failure')
        with self.assertRaisesRegex(ValueError, 'write_integrity_failure'):
            run_lanes(cycle, lambda: False, once=True)


if __name__ == '__main__':
    unittest.main()
