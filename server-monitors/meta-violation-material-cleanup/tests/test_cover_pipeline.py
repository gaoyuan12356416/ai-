from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import cover
from meta_violation_cleanup.cover_performance import Timings
from meta_violation_cleanup.cover_pipeline import run_lanes
from meta_violation_cleanup.runtime import LegacyPriorityWindow


class PipelineTests(unittest.TestCase):
    def test_ads_continue_while_cleanup_is_blocked_and_shutdown_drains(self):
        cleanup_started, cleanup_release = threading.Event(), threading.Event()
        ads_progress, stopping, returned = threading.Event(), threading.Event(), threading.Event()
        counts = {'ads': 0, 'cleanup': 0}
        errors = []

        def cycle(lane, stopped):
            counts[lane] += 1
            if lane == 'cleanup':
                cleanup_started.set()
                if not cleanup_release.wait(5):
                    raise AssertionError('cleanup was not released')
            else:
                if not cleanup_started.wait(5):
                    raise AssertionError('cleanup did not start')
                if counts[lane] >= 2:
                    ads_progress.set()
            return .01

        def run():
            try:
                run_lanes(cycle, stopping.is_set)
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(ads_progress.wait(5))
            self.assertEqual(counts['cleanup'], 1)
            stopping.set()
            self.assertFalse(returned.wait(.2))
        finally:
            cleanup_release.set()
            stopping.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_error_stops_sibling_but_waits_for_its_receipt(self):
        cleanup_started, drained = threading.Event(), threading.Event()
        def cycle(lane, stopped):
            if lane == 'ads':
                self.assertTrue(cleanup_started.wait(5))
                raise ValueError('fixture_write_guard')
            cleanup_started.set()
            while not stopped():
                threading.Event().wait(.01)
            drained.set()
            return 0
        with self.assertRaisesRegex(ValueError, 'fixture_write_guard'):
            run_lanes(cycle, lambda: False)
        self.assertTrue(drained.is_set())

    def test_priority_handoff_is_propagated_only_after_both_lanes_finish(self):
        started = threading.Barrier(2)
        handoff, drained = threading.Event(), []
        def stop():
            if handoff.is_set():
                raise LegacyPriorityWindow('legacy_priority_window')
            return False
        def cycle(lane, stopped):
            started.wait(5)
            handoff.set()
            while not stopped():
                threading.Event().wait(.01)
            drained.append(lane)
            return 0
        with self.assertRaises(LegacyPriorityWindow):
            run_lanes(cycle, stop)
        self.assertEqual(sorted(drained), ['ads', 'cleanup'])

    def test_once_has_one_cycle_per_lane(self):
        called = []
        run_lanes(lambda lane, stop: called.append(lane) or 0, lambda: False, once=True)
        self.assertEqual(sorted(called), ['ads', 'cleanup'])

    def test_handoff_does_not_hide_sibling_failure_during_drain(self):
        started = threading.Barrier(2)
        handoff, observed = threading.Event(), threading.Event()
        def stop():
            if handoff.is_set():
                raise LegacyPriorityWindow('legacy_priority_window')
            return False
        def cycle(lane, stopped):
            started.wait(5)
            if lane == 'cleanup':
                handoff.set()
                self.assertTrue(stopped())
                observed.set()
                return 0
            self.assertTrue(observed.wait(5))
            raise ValueError('binding_integrity_failure')
        with self.assertRaisesRegex(ValueError, 'binding_integrity_failure'):
            run_lanes(cycle, stop)


class WorkPipelineTests(unittest.TestCase):
    def test_shared_governor_recovery_before_lanes_and_cleanup_has_four_workers(self):
        log, workers, metrics = [], [], []
        owner = threading.get_ident()
        store = MagicMock()
        store.ready_tasks.return_value = [object()]
        store.pending_deletions.return_value = [object()]
        store.recover_inflight.side_effect = lambda: log.append('recover_inflight')
        class Source:
            def __init__(self, config): self.thread = threading.get_ident()
            def credentials(self):
                assert self.thread == threading.get_ident()
                return {}
            def close(self): assert self.thread == threading.get_ident()
        class Worker:
            def __init__(self, config, store, source, factory, shared, **kw):
                self.config, self.governor = config, kw['governor']
                self.halted, self.halt_reason = threading.Event(), ''
                self.timings = Timings()
                self.thread = threading.get_ident()
                workers.append(self)
            def recover_reservations(self):
                log.append('recover_reservations')
                return {}
            def reconcile_unknown(self, **kw):
                log.append('reconcile')
                return {}
            def run_once(self, **kw):
                assert log == ['recover_inflight', 'recover_reservations', 'reconcile']
                assert kw['process_deletes'] is False
                assert self.thread != owner
                return {'tasks': {'succeeded': 1}, 'seconds': .01}
            def process_deletions(self):
                assert log == ['recover_inflight', 'recover_reservations', 'reconcile']
                assert self.thread != owner
                return [{'status': 'deleted_verified'}]
            def close(self): assert self.thread == threading.get_ident()
        @contextmanager
        def lease(*args):
            yield MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            config = {'runtime_dir': tmp, 'cover_live_enabled': True, 'shared_ledger': 'unused',
                      'legacy_lock': 'unused', 'execution_workers': 32}
            with patch.object(cover, 'Store'), patch.object(cover, 'CoverSource', Source), \
                 patch.object(cover, 'legacy_delete_lock', lease), \
                 patch.object(cover, 'record_cycle', lambda root, item: metrics.append(item)), \
                 patch('meta_violation_cleanup.cover_worker.CoverWorker', Worker):
                cover.work(config, store, MagicMock(), lambda: False, True, 256)
        self.assertEqual(len({id(w.governor) for w in workers}), 1)
        self.assertEqual(sorted(w.config['execution_workers'] for w in workers), [4, 32, 32])
        self.assertEqual(sorted(m['lane'] for m in metrics), ['ads', 'cleanup'])
        self.assertTrue(all('execution_started_at' in m for m in metrics))

    def test_empty_cleanup_does_not_load_credentials(self):
        store = MagicMock()
        store.ready_tasks.return_value = []
        store.pending_deletions.return_value = []
        worker = MagicMock()
        worker.recover_reservations.return_value = {}
        worker.reconcile_unknown.return_value = {}
        worker.halted.is_set.return_value = False
        @contextmanager
        def lease(*args): yield MagicMock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(cover, 'Store'), \
             patch.object(cover, 'CoverSource') as source, \
             patch.object(cover, 'legacy_delete_lock', lease), \
             patch('meta_violation_cleanup.cover_worker.CoverWorker', return_value=worker):
            cover.work({'runtime_dir': tmp, 'cover_live_enabled': True, 'shared_ledger': 'unused',
                        'legacy_lock': 'unused'}, store, MagicMock(), lambda: False, True, 256)
            self.assertEqual(source.call_count, 1)  # Startup reconciliation only.
            worker.run_once.assert_not_called()
            worker.process_deletions.assert_not_called()

    def test_pause_created_during_execution_is_preserved(self):
        store, audit, worker = MagicMock(), MagicMock(), MagicMock()
        worker.recover_reservations.return_value = {}
        worker.reconcile_unknown.return_value = {}
        worker.halted.is_set.return_value = False
        @contextmanager
        def lease(*args): yield MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / 'cover-write-paused.json'
            original = '{"error":{"code":"external_review_required"}}'
            def execute(cycle, stop, **kwargs):
                pause.write_text(original)
                stop()
            with patch.object(cover, 'Store'), patch.object(cover, 'CoverSource'), \
                 patch.object(cover, 'legacy_delete_lock', lease), \
                 patch.object(cover, 'run_lanes', execute), \
                 patch('meta_violation_cleanup.cover_worker.CoverWorker', return_value=worker):
                with self.assertRaises(cover.CoverWritePaused):
                    cover.work({'runtime_dir': tmp, 'cover_live_enabled': True,
                                'shared_ledger': 'unused', 'legacy_lock': 'unused'},
                               store, audit, lambda: False, False, 256)
            self.assertEqual(pause.read_text(), original)
            audit.artifact.assert_not_called()
            store.fail_batch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
