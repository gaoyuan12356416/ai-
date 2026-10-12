import os
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.worker_runtime import (
    GeneratorCancelled, PreparationLane, generation_cancel_check, run_generator)


class WorkerRuntimeCase(unittest.TestCase):
    def test_cancelled_attempt_never_starts_generator(self):
        with self.assertRaises(GeneratorCancelled):
            run_generator(['must-not-run'], input='', env={}, timeout=10,
                          should_cancel=lambda: True)

    def test_success_preserves_output_and_sends_input_once(self):
        code = "import sys,time; value=sys.stdin.read();time.sleep(.6);print(value);print('error stream',file=sys.stderr)"
        result = run_generator([sys.executable, '-c', code], input='one prompt',
                               env=os.environ.copy(), timeout=3, should_cancel=lambda: False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), 'one prompt')
        self.assertEqual(result.stderr.strip(), 'error stream')

    def test_durable_task_cancellation_stops_pending_generator(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'tasks.sqlite3'
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute('CREATE TABLE youtube_auto_preparation(id TEXT PRIMARY KEY,state TEXT,version INTEGER,lease_until REAL)')
                connection.execute('INSERT INTO youtube_auto_preparation VALUES(?,?,?,?)',
                                   ('task', 'generating', 1, time.time() + 30))
            def cancel():
                time.sleep(.7)
                with closing(sqlite3.connect(db)) as connection, connection:
                    connection.execute("UPDATE youtube_auto_preparation SET state='cancelled',lease_until=0 WHERE id='task'")
            writer = threading.Thread(target=cancel)
            writer.start()
            start = time.monotonic()
            try:
                with self.assertRaises(GeneratorCancelled):
                    run_generator([sys.executable, '-c', 'import time;time.sleep(20)'],
                                  input='', env=os.environ.copy(), timeout=10,
                                  should_cancel=generation_cancel_check(db, 'task', 1))
            finally:
                writer.join()
            self.assertLess(time.monotonic() - start, 3)
            with closing(sqlite3.connect(db)) as connection, connection:
                self.assertEqual(connection.execute('SELECT state,lease_until FROM youtube_auto_preparation').fetchone(), ('cancelled', 0))

    def test_watcher_rejects_missing_obsolete_and_expired_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'tasks.sqlite3'
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute('CREATE TABLE youtube_auto_preparation(id TEXT PRIMARY KEY,state TEXT,version INTEGER,lease_until REAL)')
                connection.executemany('INSERT INTO youtube_auto_preparation VALUES(?,?,?,?)', [
                    ('current', 'generating', 1, time.time() + 30),
                    ('expired', 'generating', 1, 0),
                    ('review', 'review', 1, time.time() + 30)])
            before = db.read_bytes()
            self.assertFalse(generation_cancel_check(db, 'current', 1)())
            for task, version in [('missing', 1), ('current', 2), ('expired', 1), ('review', 1)]:
                self.assertTrue(generation_cancel_check(db, task, version)())
            self.assertEqual(db.read_bytes(), before)

    def test_slow_cover_does_not_block_other_covers_or_require_publish_ticks(self):
        entered, release, remaining_done = (threading.Event() for _ in range(3))
        lock = threading.Lock()
        pending, calls, active = list(range(4)), [], set()
        peak = []
        def generate(worker):
            with lock:
                if not pending:return {'claimed': False}
                task = pending.pop(0)
                calls.append(task)
                active.add(task)
                peak.append(len(active))
            if task == 0:
                entered.set()
                release.wait(5)
            elif task == 3:
                remaining_done.set()
            with lock:active.remove(task)
            return {'claimed': True, 'task_id': str(task)}
        lane = PreparationLane(2, idle_seconds=.01)
        try:
            workflow = SimpleNamespace(run_once=generate)
            lane.start(workflow, 'test')
            lane.start(workflow, 'test')  # Starting again must not add slots.
            self.assertTrue(entered.wait(1))
            # Simulate a blocked publisher: no main-loop calls refill the pool.
            self.assertTrue(remaining_done.wait(1))
            self.assertEqual(calls, [0, 1, 2, 3])
            self.assertEqual(max(peak), 2)
            self.assertIn(0, active)
        finally:
            release.set()
            lane.close()

    def test_transient_dispatch_error_does_not_stop_lane_or_leak_exception(self):
        recovered = threading.Event()
        calls = []
        def prepare(worker):
            calls.append(worker)
            if len(calls) == 1:raise RuntimeError('secret-exception-content')
            recovered.set()
            return {'claimed': False}
        lane = PreparationLane(1, idle_seconds=.01)
        with self.assertLogs(level='ERROR') as logs:
            try:
                lane.start(SimpleNamespace(run_once=prepare), 'test')
                self.assertTrue(recovered.wait(1))
            finally:lane.close()
        self.assertNotIn('secret-exception-content', '\n'.join(logs.output))

    def test_disabled_gate_and_graceful_drain_stop_new_claims(self):
        enabled, entered, release = (threading.Event() for _ in range(3))
        calls = []
        def prepare(worker):
            calls.append(worker)
            entered.set()
            release.wait(5)
            return {'claimed': True}
        lane = PreparationLane(1, enabled=enabled.is_set, idle_seconds=.01)
        closer = None
        try:
            lane.start(SimpleNamespace(run_once=prepare), 'test')
            self.assertFalse(entered.wait(.05))
            enabled.set()
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=lane.close)
            closer.start()
            self.assertTrue(lane.stopping.wait(1))
            self.assertTrue(closer.is_alive(), 'close must wait for active work')
            release.set()
            closer.join(1)
            self.assertFalse(closer.is_alive())
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            if closer:closer.join(2)
            lane.close()

    def test_preparation_worker_limit_is_bounded(self):
        for value in (0, 5, -1, '', '2.0', True, 'unlimited'):
            with self.assertRaises(ValueError):PreparationLane(value)
        for value in (1, '2', 4):
            lane = PreparationLane(value)
            self.assertEqual(lane.workers, int(value))
            lane.close()

    @unittest.skipUnless(os.name == 'posix', 'Linux process groups')
    def test_timeout_kills_descendant_holding_capture_pipe(self):
        code = "import subprocess,time,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']); time.sleep(20)"
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_generator([sys.executable, '-c', code], input='', env=os.environ.copy(), timeout=.2)
        self.assertLess(time.monotonic()-start, 3)

    @unittest.skipUnless(os.name == 'posix', 'Linux process sessions')
    def test_cancel_kills_child_in_separate_session_and_releases_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            child_file = Path(folder) / 'child.pid'
            child = "import os,time,pathlib;pathlib.Path(%r).write_text(str(os.getpid()));time.sleep(20)" % str(child_file)
            code = "import subprocess,time,sys;subprocess.Popen([sys.executable,'-c',%r],start_new_session=True);time.sleep(20)" % child
            start = time.monotonic()
            with self.assertRaises(GeneratorCancelled):
                run_generator([sys.executable, '-c', code], input='', env=os.environ.copy(), timeout=10,
                              should_cancel=lambda: time.monotonic() - start > .8)
            self.assertLess(time.monotonic() - start, 3)
            pid = int(child_file.read_text())
            stat = Path('/proc') / str(pid) / 'stat'
            deadline = time.monotonic() + 1
            while stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z')
            result = run_generator([sys.executable, '-c', "print('next task')"], input='', env=os.environ.copy(), timeout=3)
            self.assertEqual(result.stdout.strip(), 'next task')

    @unittest.skipUnless(os.name == 'posix', 'Linux process sessions')
    def test_timeout_kills_child_in_separate_session(self):
        code = "import subprocess,time,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],start_new_session=True);time.sleep(20)"
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_generator([sys.executable, '-c', code], input='', env=os.environ.copy(), timeout=.2)
        self.assertLess(time.monotonic() - start, 3)


if __name__ == '__main__':
    unittest.main()
