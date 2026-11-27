import asyncio
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from features.fb_page_assignment_v2.ledger import Store
from features.fb_page_assignment_v2.service import Assignment


def request(key="cross:1:a", group=62):
    return {"external_task_id": key, "page_group_id": group, "drama_ids": ["D"]}


def select_ok(requests, deadline):
    # Top-level so the real spawned worker used in the crash test can import it.
    return [(200, {"rows": [{"drama_id": item, "page_id": "17"} for item in req["drama_ids"]],
                   "snapshot": {}}) for req in requests]


class Deadlines(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    async def app(self, compute, *, window=.001, parallel=2):
        service = Assignment(self.store, window=window, parallel=parallel, compute=compute)
        await service.start()
        self.addAsyncCleanup(service.close)
        return service

    async def test_expired_request_is_not_selected_or_recorded(self):
        async def unused(*args):
            self.fail("expired request cannot schedule selection")
        service = await self.app(unused)
        result = await service.submit(request(), time.monotonic() - .01)
        self.assertEqual(result[1]["error"], "request_deadline")
        self.assertFalse(service.pending)
        self.assertEqual(self.store.status()["pending"],0)

    async def test_short_waiter_does_not_limit_its_sibling_batch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []
        async def compute(reqs, deadline):
            observed.append((len(reqs), deadline))
            entered.set()
            await release.wait()
            return select_ok(reqs, deadline) if time.monotonic() < deadline else [(503, "selection_deadline")] * len(reqs)

        service = await self.app(compute, window=.04)
        now = time.monotonic()
        short = asyncio.create_task(service.submit(request(), now+.22))
        long = asyncio.create_task(service.submit(request("cross:2:a"), now+1.5))
        try:
            await asyncio.wait_for(entered.wait(), .6)
            self.assertEqual((await short)[1]["error"], "request_deadline")
        finally:
            release.set()
        self.assertEqual((await long)[0], 200)
        self.assertEqual(observed, [(2, now+1.5)])

    async def test_identical_retry_runs_independently_while_original_is_waiting(self):
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def compute(reqs, deadline):
            requests.append(reqs)
            if len(requests) == 1:
                entered.set()
                await release.wait()
                return [(503, "selection_deadline")]
            return select_ok(reqs, deadline)

        service = await self.app(compute)
        early = asyncio.create_task(service.submit(request(), time.monotonic()+.16))
        await asyncio.wait_for(entered.wait(), .5)
        later = asyncio.create_task(service.submit(request(), time.monotonic()+2))
        try:
            accepted = await asyncio.wait_for(later,.5)
            self.assertEqual(accepted[0],200)
            self.assertEqual((await early)[1]["error"], "request_deadline")
        finally:
            release.set()
        third=await service.submit(request(), time.monotonic()+.5)
        self.assertNotEqual(third[1]["task_id"],accepted[1]["task_id"])
        self.assertEqual(len(requests), 3)

    async def test_record_after_http_timeout_finishes_and_retry_gets_its_own_receipt(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store.record
        calls = []
        def slow_save(*args):
            calls.append(args[0])
            entered.set()
            release.wait(3)
            return original(*args)
        async def calculate(reqs, deadline):
            return select_ok(reqs, deadline)

        service = await self.app(calculate)
        with patch.object(self.store, "record", slow_save):
            early = asyncio.create_task(service.submit(request(), time.monotonic()+.15))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, .6))
                self.assertEqual((await early)[1]["error"], "request_deadline")
                later = asyncio.create_task(service.submit(request(), time.monotonic()+1.5))
            finally:
                release.set()
            receipt = await later
            self.assertEqual(receipt[0], 200)
            third=await service.submit(request(), time.monotonic()+.5)
            self.assertNotEqual(third[1]["task_id"],receipt[1]["task_id"])
        self.assertEqual(calls,["cross:1:a"]*3)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assignment").fetchone()[0],3)

    async def test_malformed_worker_result_affects_only_one_request(self):
        async def compute(reqs, deadline):
            self.assertEqual(len(reqs), 2)
            return [None, select_ok(reqs[1:], deadline)[0]]

        service = await self.app(compute, window=.04)
        deadline = time.monotonic()+1.5
        results = await asyncio.gather(service.submit(request(), deadline),
                                       service.submit(request("cross:2:a"), deadline))
        self.assertEqual(sorted(result[0] for result in results), [200,503])

    async def test_real_dead_child_is_replaced_and_next_read_completes(self):
        with patch("features.fb_page_assignment_v2.service.select_process", select_ok):
            service = await self.app(None, parallel=1)
            pool = service.pools
            pid = await asyncio.get_running_loop().run_in_executor(pool, os.getpid)
            self.assertNotEqual(pid, os.getpid())
            os.kill(pid, signal.SIGTERM)
            status, body = await service.submit(request(), time.monotonic()+10)
            self.assertEqual(status, 200, body)
            self.assertIsNot(service.pools, pool)
            next_pid = await asyncio.get_running_loop().run_in_executor(service.pools, os.getpid)
            self.assertNotEqual(pid, next_pid)
            self.assertEqual(body["results"][0]["page_id"], "17")
