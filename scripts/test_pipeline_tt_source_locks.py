"""Real TT native claims: isolate slow sources without losing duplicate fences."""
import gc
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.pipeline_dispatch.tt_native import TTNativeBridge
from scripts import test_tt_auto_post_publisher as fixtures


class TTSourceLockTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TTAutoPostPublisherIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gpu = fixtures.FakeGPU()
        self.executor = self.fixture.executor(self.gpu)
        self.bridge = TTNativeBridge(SimpleNamespace(store=self.fixture.store, executor=self.executor), automatic=True)
        self.claim = {'claim_token': 'a' * 32, 'node_id': 'fixture-node'}

    def task(self, suffix, account, pending=False):
        task = self.fixture.reserved_task(suffix=suffix)
        with self.bridge.connect() as conn:
            conn.execute('UPDATE tt_auto_task SET account_id=?,status=? WHERE id=?',
                         (account, 'pending' if pending else 'reserved', task.id))
        return {'source_key': 'tt:auto:' + str(task.id), 'source_id': str(task.id),
                'channel': 'tt', 'family': 'auto', 'stage': 'prepare', 'generation': 1,
                'executor_backend': 'cpu'}

    def background(self, action):
        done, values, errors = threading.Event(), [], []
        def run():
            try:
                values.append(action())
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=run)
        thread.start()
        return thread, done, values, errors

    def test_slow_selection_does_not_block_another_source_account(self):
        slow = self.task('slow', '640', pending=True)
        other = self.task('other', '641')
        entered, release = threading.Event(), threading.Event()
        def select(task, token):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture selection release timeout')
            return self.fixture.store.transition_task(task.id, 'reserved', expected_statuses={'selecting'},
                claim_token=token, updates={}, event_type='fixture_selection_finished')
        threads = []
        with patch.object(self.executor, '_select', side_effect=select):
            try:
                first = self.background(lambda: self.bridge.reserve(slow, self.claim)); threads.append(first[0])
                self.assertTrue(entered.wait(2))
                second = self.background(lambda: self.bridge.reserve(other, self.claim)); threads.append(second[0])
                self.assertTrue(second[1].wait(2), 'independent source was blocked by slow selection')
                self.assertFalse(first[1].is_set())
                self.assertEqual(second[3], [])
                self.assertEqual(second[2][0]['kind'], 'tt_prepare')
            finally:
                release.set()
                for thread in threads: thread.join(5)
        self.assertEqual(first[3], [])
        self.assertEqual(self.gpu.prepare_calls, [])
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_claim').fetchone()[0], 2)

    def test_concurrent_same_source_reserves_once_and_reuses_exact_descriptor(self):
        job = self.task('same', '640')
        entered, release = threading.Event(), threading.Event()
        original = self.bridge._prepare_descriptor
        def slow_descriptor(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture descriptor release timeout')
            return original(*args, **kwargs)
        threads = []
        with patch.object(self.bridge, '_prepare_descriptor', side_effect=slow_descriptor) as prepare, \
                patch.object(self.fixture.store, 'claim_next_executable_task', wraps=self.fixture.store.claim_next_executable_task) as claim:
            try:
                first = self.background(lambda: self.bridge.reserve(job, self.claim)); threads.append(first[0])
                self.assertTrue(entered.wait(2))
                second = self.background(lambda: self.bridge.reserve(job, self.claim)); threads.append(second[0])
                self.assertFalse(second[1].wait(.1))
            finally:
                release.set()
                for thread in threads: thread.join(5)
            self.assertEqual(claim.call_count, 1)
            self.assertEqual(prepare.call_count, 1)
        self.assertEqual(first[3] + second[3], [])
        self.assertEqual(first[2], second[2])
        self.assertEqual(self.gpu.prepare_calls, [])
        with self.bridge.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_claim').fetchone()[0], 1)
        gc.collect()
        self.assertEqual(len(self.bridge._source_locks), 0)

    def test_stage_generation_share_live_lock_and_idle_history_is_not_retained(self):
        job = self.task('stages', '640')
        lock = self.bridge._source_lock(job)
        later = dict(job, stage='publish', generation=9)
        self.assertIs(self.bridge._source_lock(later), lock)
        entered = threading.Event()
        def callback():
            with self.bridge._source_lock(later): entered.set()
        with lock:
            # Reentrant calls on this source remain safe in the same thread.
            with self.bridge._source_lock(later): pass
            thread, done, _, errors = self.background(callback)
            self.assertFalse(entered.wait(.1))
        thread.join(3)
        self.assertTrue(done.is_set())
        self.assertTrue(entered.is_set())
        self.assertEqual(errors, [])
        del lock
        for ident in range(1000):
            key = dict(job, source_key='tt:auto:' + str(ident), source_id=str(ident))
            with self.bridge._source_lock(key): pass
        gc.collect()
        self.assertEqual(len(self.bridge._source_locks), 0)


if __name__ == '__main__': unittest.main()
