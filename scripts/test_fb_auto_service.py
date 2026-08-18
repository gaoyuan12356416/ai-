import unittest
import threading

from features.fb_auto_posts.service import Runtime


class Store:
    def __init__(self):
        self.due_calls = 0

    def mark_stale_running_unknown(self):
        return 2

    def due_templates(self):
        self.due_calls += 1
        return [{"template_id": 1}]


class Executor:
    live_enabled = False

class Preparer: pass


class ServiceTests(unittest.TestCase):
    def test_closed_gate_tick_only_performs_safe_cleanup(self):
        store = Store()
        runtime = Runtime(store, object(), object(), Executor(), Preparer(), "x" * 32)
        result = runtime.tick()
        self.assertEqual(result["status"], "live_gate_closed")
        self.assertEqual(result["stale_marked_unknown"], 2)
        self.assertEqual(result["enqueued"], 0)
        self.assertEqual(store.due_calls, 0)

    def test_tick_is_single_flight_and_drains_all_due_templates(self):
        class BlockingStore:
            def __init__(self): self.entered=threading.Event(); self.release=threading.Event(); self.created=[]
            def mark_stale_running_unknown(self): self.entered.set(); self.release.wait(2); return 0
            def enqueue_due_slots(self, **_kwargs): self.entered.set(); self.release.wait(2); return {"ok":True,"status":"scheduled","enqueued":5,"missed":0}
        class LiveExecutor: live_enabled=True
        store=BlockingStore(); runtime=Runtime(store,object(),object(),LiveExecutor(),Preparer(),"x"*32); first=[]
        worker=threading.Thread(target=lambda:first.append(runtime.tick())); worker.start(); self.assertTrue(store.entered.wait(1))
        try:
            duplicate=runtime.tick(); self.assertEqual(duplicate["status"],"already_running")
        finally:
            store.release.set(); worker.join(2)
        self.assertFalse(worker.is_alive()); self.assertEqual(first[0]["enqueued"],5)


if __name__ == "__main__":
    unittest.main()
