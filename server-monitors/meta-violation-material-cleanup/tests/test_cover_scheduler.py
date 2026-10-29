"""Concurrent scheduling keeps source reads fresh and never replays an Ad."""
from collections import Counter
import threading
import time
import unittest
from unittest.mock import patch

import test_cover_worker as fixtures
from test_cover_worker import Graph, ad, creative
from meta_violation_cleanup.throttle import Governor


class SchedulerTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    enqueue = fixtures.WorkerTests.enqueue
    worker = fixtures.WorkerTests.worker

    def test_free_slot_is_refilled_while_first_ad_is_still_running(self):
        self.config['execution_workers'] = 2
        ads = [ad(str(10+i), str(20+i), str(100+i)) for i in range(4)]
        self.enqueue(ads)
        first_started, replacement_started = threading.Event(), threading.Event()
        worker = self.worker(Graph(ads, []))
        calls = []
        def execute(task, context):
            calls.append(task['id'])
            if task['id'] == 1:
                first_started.set()
                self.assertTrue(replacement_started.wait(2), 'slow Ad held an entire wave')
            elif task['id'] >= 3:
                replacement_started.set()
            else:
                self.assertTrue(first_started.wait(2))
            return {'task_id': task['id'], 'status': 'failed'}
        with patch.object(worker, '_execute', execute):
            report = worker.run_once(max_ads=4, process_deletes=False)
        self.assertEqual(report['tasks'], {'failed': 4})
        self.assertEqual(sorted(calls), [1, 2, 3, 4])
        self.assertTrue(replacement_started.is_set())

    def test_next_source_batch_overlaps_running_ads_on_caller_thread(self):
        self.config.update(execution_workers=2, execution_batch_size=2)
        ads = [ad(str(10+i), str(20+i), str(100+i)) for i in range(4)]
        self.enqueue(ads)
        first_started, source_overlapped = threading.Event(), threading.Event()
        original_prepare = self.source.prepare_candidates
        def prepare(tasks):
            if self.source.calls:
                self.assertTrue(first_started.wait(2))
                source_overlapped.set()
            return original_prepare(tasks)  # Fixture checks owner thread.
        worker = self.worker(Graph(ads, []))
        def execute(task, context):
            if task['id'] <= 2:
                first_started.set()
                self.assertTrue(source_overlapped.wait(2), 'SQL preparation waited for Ad completion')
            return {'task_id': task['id'], 'status': 'failed'}
        with patch.object(self.source, 'prepare_candidates', prepare), \
             patch.object(worker, '_execute', execute):
            report = worker.run_once(max_ads=4, process_deletes=False)
        self.assertEqual(report['source_prepare_tasks'], 4)
        self.assertEqual(report['source_prepare_batches'], 2)
        self.assertEqual(report['source_prepare_batch_sizes'], {2: 2})

    def test_batches_64_without_exceeding_32_workers_or_two_per_account(self):
        ads = [ad(str(1000+i), str(2000+i), str(100+i % 20)) for i in range(80)]
        self.enqueue(ads)
        worker = self.worker(Graph(ads, []), governor=Governor(maximum=32, initial_limit=32))
        active, peak, counts, account_peaks = [0], [0], Counter(), Counter()
        seen, lock = [], threading.Lock()
        def execute(task, context):
            account = task['account_id']
            with lock:
                active[0] += 1
                counts[account] += 1
                peak[0] = max(peak[0], active[0])
                account_peaks[account] = max(account_peaks[account], counts[account])
                seen.append(task['id'])
            time.sleep(.01)
            with lock:
                active[0] -= 1
                counts[account] -= 1
            return {'task_id': task['id'], 'status': 'failed'}
        with patch.object(worker, '_execute', execute):
            report = worker.run_once(max_ads=80, process_deletes=False)
        self.assertEqual(len(self.source.calls[0]), 64)
        self.assertTrue(all(len(part) <= 64 for part in self.source.calls))
        self.assertLessEqual(peak[0], 32)
        self.assertGreater(peak[0], 2)
        self.assertLessEqual(max(account_peaks.values()), 2)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(report['source_prepare_tasks'], 80)
        self.assertEqual(report['tasks'], {'failed': 80})

    def test_staggered_completions_consume_buffer_before_another_sql_batch(self):
        self.config['execution_workers'] = 4
        ads = [ad(str(1000+i), str(2000+i), str(100+i)) for i in range(12)]
        self.enqueue(ads)
        first_wave = threading.Barrier(4, timeout=2)
        replacements = {identifier: threading.Event() for identifier in (5, 6, 7)}
        next_source_ready = threading.Event()
        original_prepare = self.source.prepare_candidates
        def prepare(tasks):
            if self.source.calls:
                self.assertEqual(len(tasks), 4, 'fresh buffer was topped up with single-Ad SQL')
                result = original_prepare(tasks)
                next_source_ready.set()
                return result
            return original_prepare(tasks)
        worker = self.worker(Graph(ads, []))
        def execute(task, context):
            identifier = task['id']
            if identifier <= 4:
                first_wave.wait()
                if identifier > 1:
                    self.assertTrue(replacements[identifier + 3].wait(2))
            elif identifier <= 8:
                if identifier in replacements:
                    replacements[identifier].set()
                self.assertTrue(next_source_ready.wait(2))
            return {'task_id': identifier, 'status': 'failed'}
        with patch.object(self.source, 'prepare_candidates', prepare), \
             patch.object(worker, '_execute', execute):
            report = worker.run_once(max_ads=12, process_deletes=False)
        self.assertEqual([len(part) for part in self.source.calls], [8, 4])
        self.assertEqual(report['tasks'], {'failed': 12})

    def test_expired_source_is_refreshed_once_then_deferred_without_graph_write(self):
        clock = [0.]
        original = ad()
        self.enqueue([original])
        graph = Graph([original], [creative()])
        prepare = self.source.prepare_candidates
        def slow_source(tasks):
            result = prepare(tasks)
            clock[0] += 31
            return result
        worker = self.worker(graph, clock=lambda: clock[0])
        with patch.object(self.source, 'prepare_candidates', slow_source):
            report = worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(report['tasks'], {'pending': 1})
        self.assertEqual(report['prepared_context_refreshes'], 1)
        self.assertEqual(report['source_prepare_tasks'], 2)
        self.assertEqual(self.store.get_task(1)['status'], 'pending')
        self.assertFalse(self.store.get_task(1)['request_sent'])
        self.assertEqual(graph.calls, [])

    def test_refreshed_source_revalidates_credential_before_any_ad_write(self):
        clock = [0.]
        original = ad()
        self.enqueue([original])
        graph = Graph([original], [creative()])
        prepare = self.source.prepare_candidates
        def changed_source(tasks):
            if not self.source.calls:
                result = prepare(tasks)
                clock[0] += 31
                self.source.errors['10'] = {'code': 'publishing_credential_unavailable'}
                return result
            return prepare(tasks)
        worker = self.worker(graph, clock=lambda: clock[0])
        with patch.object(self.source, 'prepare_candidates', changed_source):
            report = worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(report['tasks'], {'failed': 1})
        self.assertEqual(report['prepared_context_refreshes'], 1)
        self.assertEqual(self.store.get_task(1)['result']['reason'], 'source_validation_failed')
        self.assertEqual(graph.calls, [])

    def test_stop_after_preparation_leaves_unsubmitted_tasks_pending(self):
        stopping = threading.Event()
        original = ad()
        self.enqueue([original])
        graph = Graph([original], [creative()])
        prepare = self.source.prepare_candidates
        def stop_source(tasks):
            result = prepare(tasks)
            stopping.set()
            return result
        worker = self.worker(graph, stop=stopping.is_set)
        with patch.object(self.source, 'prepare_candidates', stop_source):
            report = worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(report['tasks'], {})
        self.assertEqual(self.store.get_task(1)['status'], 'pending')
        self.assertEqual(graph.calls, [])


if __name__ == '__main__':
    unittest.main()
