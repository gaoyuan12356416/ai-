import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.worker_runtime import PreparationLane, run_generator


class WorkerRuntimeCase(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
