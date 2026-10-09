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

    def test_generation_does_not_block_publishing_or_duplicate_preparation(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def generate(worker):
            calls.append(worker)
            entered.set()
            release.wait(5)
            return {'claimed': True}
        lane = PreparationLane()
        try:
            workflow = SimpleNamespace(run_once=generate)
            lane.tick(workflow, 'test')
            self.assertTrue(entered.wait(1))
            published = []
            for _ in range(20):
                published.append('publish tick')
                self.assertIsNone(lane.tick(workflow, 'test'))
            self.assertEqual(len(published), 20)
            self.assertEqual(calls, ['test'])
        finally:
            release.set()
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
