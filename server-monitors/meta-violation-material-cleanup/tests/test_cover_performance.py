from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import cover
from meta_violation_cleanup.cover_performance import Timings, idle_delay, record_cycle
from meta_violation_cleanup.cover_worker import CoverWorker
from meta_violation_cleanup.runtime import LegacyPriorityWindow, _LegacyLease
from meta_violation_cleanup.throttle import Governor
import test_cover_worker as fixtures
from test_cover_worker import Graph, ad, creative
from test_cover_graph import Response


class PrefetchTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    enqueue = fixtures.WorkerTests.enqueue
    worker = fixtures.WorkerTests.worker

    def test_limit_one_prepares_fresh_context_for_each_slow_task(self):
        clock = [0.]
        ads = [ad(str(10+i), str(20+i), str(100+i)) for i in range(8)]
        old = [creative(str(20+i), str(100+i)) for i in range(8)]
        self.enqueue(ads)
        class SlowGraph(Graph):
            def get_cover_ad(self, *args):
                clock[0] += 3
                return super().get_cover_ad(*args)
            def get_creative(self, *args):
                clock[0] += 3
                return super().get_creative(*args)
        governor = Governor(maximum=32, initial_limit=1)
        graph = SlowGraph(ads, old)
        result = self.worker(graph, clock=lambda: clock[0], governor=governor).run_once(process_deletes=False)
        self.assertEqual(result['tasks'], {'succeeded': 8})
        self.assertEqual([len(x) for x in self.source.calls], [1] * 8)
        self.assertEqual(result['defer_reasons'], {})
        self.assertEqual(result['timings']['ad_attempt_succeeded']['count'], 8)
        self.assertEqual(sum(x[0] == 'bind' for x in graph.calls), 8)

    def test_prefetch_rechecks_governor_after_each_wave_and_caps_account_queue(self):
        ads = [ad(str(10+i), str(20+i), str(100+i % 3)) for i in range(10)]
        self.enqueue(ads)
        governor = MagicMock()
        governor.snapshot.side_effect = [{'concurrency_limit': 32, 'per_account_limit': 2}] + [
            {'concurrency_limit': 1, 'per_account_limit': 2}] * 10
        worker = self.worker(Graph(ads, []), governor=governor)
        with patch.object(worker, '_execute', lambda task, context: {'task_id': task['id'], 'status': 'failed'}):
            worker.run_once(process_deletes=False)
        self.assertEqual([len(x) for x in self.source.calls], [6, 1, 1, 1, 1])
        self.assertEqual(len({x for part in self.source.calls for x in part}), 10)


class RecoveryTests(unittest.TestCase):
    def test_pressure_recovery_ramps_up_without_ignoring_cooldown(self):
        now = [0.]
        governor = Governor(maximum=32, initial_limit=32, ramp_up=True, clock=lambda: now[0])
        for _ in range(5):
            governor.observe(Response({}, headers={'x-app-usage': '{"call_count":75}'}), '100')
        self.assertEqual(governor.limit, 1)
        now[0] = 21
        governor.observe(Response({}, headers={'x-app-usage': '{"call_count":10}'}), '100')
        self.assertEqual(governor.limit, 3)
        governor.observe(Response({'error': {'code': 613}}, 400), '100')
        self.assertEqual(governor.limit, 1)
        self.assertGreaterEqual(governor.global_until, 81)
        now[0] = 200
        for i in range(20):
            now[0] += 21
            governor.observe(Response({}, headers={'x-app-usage': '{"call_count":10}'}), '100')
        self.assertEqual(governor.limit, 32)
        self.assertEqual(governor.per_account, 2)
        self.assertEqual(governor.rps, 24)

    def test_idle_only_sleeps_when_no_progress_or_no_ready_work(self):
        self.assertEqual(idle_delay({'tasks': {'succeeded': 20, 'pending': 1}}, True), 0)
        self.assertEqual(idle_delay({'tasks': {'waiting_review': 20}}, True), 0)
        self.assertEqual(idle_delay({'tasks': {'pending': 20}}, True), 2)
        self.assertEqual(idle_delay({'tasks': {}}, False), 30)

    def test_priority_exception_has_specific_type(self):
        with patch('meta_violation_cleanup.runtime._legacy_priority', return_value=True):
            with self.assertRaises(LegacyPriorityWindow):
                _LegacyLease(lambda: False).check()

    def test_worker_reacquires_after_priority_handoff_without_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, locks = [False], []
            @contextmanager
            def lease(*args):
                locks.append(True)
                if len(locks) == 1:
                    raise LegacyPriorityWindow('legacy_priority_window')
                yield MagicMock()
            class Worker:
                def __init__(self, *args, **kwargs): pass
                def recover_reservations(self): return {}
                def reconcile_unknown(self, **kwargs): return {}
                def run_once(self, **kwargs):
                    done[0] = True
                    return {'tasks': {'succeeded': 1}}
                def close(self): pass
            config = {'cover_live_enabled': True, 'runtime_dir': tmp, 'shared_ledger': 'unused',
                      'legacy_lock': 'unused'}
            store = MagicMock()
            store.ready_tasks.return_value = []
            with patch.object(cover, 'Store'), patch.object(cover, 'CoverSource'), \
                 patch.object(cover, 'legacy_delete_lock', lease), \
                 patch('meta_violation_cleanup.cover_worker.CoverWorker', Worker):
                cover.work(config, store, MagicMock(), lambda: done[0], False, 256)
            self.assertEqual(len(locks), 2)
            self.assertFalse((Path(tmp) / 'cover-write-paused.json').exists())

    def test_write_pause_has_nonrestartable_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'cover-write-paused.json').write_text('{}')
            with patch.object(cover, 'Store'):
                with self.assertRaises(cover.CoverWritePaused) as caught:
                    cover.work({'cover_live_enabled': True, 'runtime_dir': tmp, 'shared_ledger': 'unused'},
                               MagicMock(), MagicMock(), lambda: False, False, 1)
            self.assertEqual(caught.exception.exit_status, 78)
        unit = (Path(cover.__file__).parent / 'deploy/meta-cover-worker.service').read_text()
        self.assertIn('Restart=on-failure', unit)
        self.assertIn('RestartPreventExitStatus=78', unit)

    def test_timing_includes_failed_operation_and_record_has_no_write_payload(self):
        clock = [0.]
        timings = Timings(lambda: clock[0])
        with self.assertRaises(ValueError):
            with timings.measure('preflight'):
                clock[0] = 2.5
                raise ValueError('fixture')
        self.assertEqual(timings.snapshot()['preflight']['mean_seconds'], 2.5)
        with tempfile.TemporaryDirectory() as tmp:
            record_cycle(tmp, {'result': {'timings': timings.snapshot()}})
            value = json.loads(next((Path(tmp) / 'performance').glob('*.jsonl')).read_text())
            self.assertEqual(value['result']['timings']['preflight']['count'], 1)


if __name__ == '__main__':
    unittest.main()
