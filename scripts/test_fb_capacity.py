import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from scripts.fb_auto_post_runner import drain
from features.fb_gpu.prepare_worker import PrepareProcessor


class CapacityTests(unittest.TestCase):
    def test_drain_covers_145_without_more_than_four_workers(self):
        lock=threading.Lock()
        active=peak=issued=0
        def one(lane):
            nonlocal active,peak,issued
            with lock:
                active+=1;peak=max(peak,active);issued+=1
            time.sleep(.001)
            with lock:active-=1
            return {'status':'submitted'}
        rows=drain(one,workers=4,max_tasks=145,max_seconds=10,terminal='no_pending')
        self.assertEqual(len(rows),145)
        self.assertEqual(issued,145)
        self.assertLessEqual(peak,4)

    def test_deadline_stops_claims_and_finishes_inflight(self):
        clock=iter([0,11,11])
        rows=drain(lambda lane:{'status':'ready'},workers=2,max_tasks=145,max_seconds=10,terminal='no_planned',monotonic=lambda:next(clock))
        self.assertEqual(len(rows),2)

    def test_idle_lanes_do_not_spin(self):
        rows=drain(lambda lane:{'status':'no_pending'},workers=4,max_tasks=145,max_seconds=10,terminal='no_pending')
        self.assertEqual(len(rows),4)

    def test_job_locks_bound_concurrency_and_serialize_same_job(self):
        obj=PrepareProcessor.__new__(PrepareProcessor)
        obj.config=SimpleNamespace(work_root='unused')
        obj.lock=threading.Lock();obj.slots=threading.BoundedSemaphore(2)
        obj.job_locks=[threading.Lock() for _ in range(64)]
        obj.active_jobs=set();obj.last_cleanup_at=time.monotonic()
        lock=threading.Lock();active=set();peak=0
        def one(job):
            nonlocal peak
            with obj.job_slot(job):
                with lock:
                    self.assertNotIn(job,active);active.add(job);peak=max(peak,len(active))
                time.sleep(.01)
                with lock:active.remove(job)
        with patch('features.fb_gpu.prepare_worker.shutil.disk_usage',return_value=SimpleNamespace(free=64*1024**3)):
            with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(one,['a','b','a','c','d','a','e','f']))
        self.assertLessEqual(peak,2);self.assertGreaterEqual(peak,2)
        self.assertEqual(obj.active_jobs,set())


if __name__=='__main__':unittest.main()
