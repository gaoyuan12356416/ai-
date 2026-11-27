"""Verify only the correct retry for the original external key closes its incident."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from features.fb_page_assignment_v2.audit import Workers
from features.fb_page_assignment_v2.ledger import Store
from features.fb_page_assignment_v2.service import Assignment


def request(key,pool=62,drama="ok"):
    return {"external_task_id":key,"page_group_id":pool,"drama_ids":[drama]}


def decision(req):
    return 200, {"snapshot":{}, "rows":[{"drama_id":d,"page_id":"7","selection_stage":"language_profile"}
                                        for d in req["drama_ids"]]}


class KeyRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.started, self.release = asyncio.Event(), asyncio.Event()
        async def choose(batch,deadline):
            if self.hold:
                self.started.set()
                await self.release.wait()
            return [decision(req) if req["page_group_id"]==62 else (403,"publication_pool_not_authorized")
                    for req in batch]
        self.choose = choose
        self.hold = False
        self.app = await self.start()
    async def start(self):
        app = Assignment(self.store,compute=self.choose,window=.001)
        await app.start()
        app.alerts = Workers(self.store,sink=object(),relay=object())
        return app
    async def asyncTearDown(self):
        self.release.set()
        await self.app.close()
        self.temp.cleanup()
    async def post(self,p):
        return await self.app.submit(p,time.monotonic()+4)
    def events(self,kind):
        return [(e["action"],e["external_task_id"]) for e in self.store.alert_events() if e["kind"]==kind]
    async def test_successful_retry_only_closes_its_own_key_then_once_after_restart(self):
        for key in ("a","b","unrelated"):
            self.assertEqual((await self.post(request(key)))[0],200)
        for key in ("a","b"):
            await self.app.notify("request_conflict",key,62,"idempotency_conflict")
        await self.app.close()
        self.store = Store(self.root)
        self.app = await self.start()
        for key in ("unrelated","a","a"):
            self.assertEqual((await self.post(request(key)))[0],200)
        self.assertEqual(set(self.store.active_keyed_incidents()["request_conflict"]),{"b"})
        self.assertEqual(self.events("request_conflict"),[("fault","a"),("fault","b")])
        await asyncio.gather(self.post(request("b")),self.post(request("b")))
        self.assertEqual(self.events("request_conflict")[-1],("recovered","b"))
        self.assertEqual(self.events("request_conflict").count(("recovered","b")),1)
        self.assertFalse(self.store.active_keyed_incidents())
        self.assertNotIn("request_conflict",self.store.active_incidents())
    async def test_new_authorized_receipt_and_zero_recovery_writes_on_unrelated_success(self):
        for key in ("one","two"):
            self.assertEqual((await self.post(request(key,pool=24)))[0],403)
        with patch.object(self.store,"recover_key",wraps=self.store.recover_key) as clear:
            await self.post(request("other"))
            self.assertEqual(clear.call_count,0)
            await self.post(request("one"))
            self.assertEqual(self.events("pool_authorization"),[("fault","one"),("fault","two")])
            await self.post(request("two"))
            await self.post(request("two"))
            self.assertEqual(clear.call_count,2)
        self.assertEqual(self.events("pool_authorization")[-1],("recovered","two"))
    async def test_original_inflight_success_does_not_claim_a_retry(self):
        self.hold=True
        original=asyncio.create_task(self.post(request("flight")))
        await asyncio.wait_for(self.started.wait(),2)
        await self.app.notify("request_conflict","flight",62,"idempotency_conflict")
        self.release.set()
        self.assertEqual((await original)[0],200)
        self.assertEqual(self.events("request_conflict"),[("fault","flight")])
        self.assertEqual((await self.post(request("flight")))[0],200)
        self.assertEqual(self.events("request_conflict")[-1],("recovered","flight"))
    async def test_new_fault_fences_an_older_correct_retry(self):
        self.assertEqual((await self.post(request("mixed",pool=24)))[0],403)
        self.hold=True
        retry=asyncio.create_task(self.post(request("mixed")))
        await asyncio.wait_for(self.started.wait(),2)
        self.app.notify("pool_authorization","mixed",24,"publication_pool_not_authorized")
        self.release.set()
        self.assertEqual((await retry)[0],200)
        self.assertNotIn(("recovered","mixed"),self.events("pool_authorization"))
        await self.post(request("mixed"))
        self.assertEqual(self.events("pool_authorization")[-1],("recovered","mixed"))

    async def test_database_rejects_stale_events_and_keeps_siblings_even_if_caller_says_last(self):
        kind = "request_conflict"
        self.store.incident(kind,"a",event_id="older")
        self.store.incident(kind,"a",event_id="newer")
        self.store.incident(kind,"b",event_id="b1")
        self.store.recover_key(kind,"a","older",last=True)
        self.assertEqual(self.store.active_keyed_incidents()[kind],{"a":"newer","b":"b1"})
        self.store.recover_key(kind,"a","newer",last=True)
        self.store.recovery(kind)
        self.assertNotIn(("recovered","a"),self.events(kind))
        self.store.recover_key(kind,"b","b1",last=True)
        self.store.recover_key(kind,"b","b1",last=True)
        self.assertEqual(self.events(kind).count(("recovered","b")),1)

    async def test_slow_recovery_outbox_preserves_success_then_drains(self):
        await self.post(request("cached"))
        await self.app.notify("request_conflict","cached",62,"idempotency_conflict")
        gate=threading.Event()
        original=self.store.recover_key
        def delayed(*args,**kwargs):
            gate.wait(2)
            return original(*args,**kwargs)
        with patch.object(self.store,"recover_key",side_effect=delayed):
            try:
                answer = await asyncio.wait_for(self.app.submit(request("cached"),time.monotonic()+.5),.47)
                self.assertEqual(answer[0],200)
                self.assertNotIn(("recovered","cached"),self.events("request_conflict"))
            finally:
                gate.set()
                if self.app.notifications:
                    await asyncio.gather(*tuple(self.app.notifications))
        self.assertEqual(self.events("request_conflict")[-1],("recovered","cached"))

    async def test_disk_outage_new_key_generation_still_respects_other_failed_keys(self):
        worker=self.app.alerts
        kind="pool_authorization"
        worker.incident(kind,"a",24,"no_pool","a0")
        worker.incident(kind,"b",24,"no_pool","b0")
        with patch.object(self.store,"incident",side_effect=sqlite3.OperationalError("disk full")):
            worker.incident(kind,"a",24,"no_pool","a1")
        worker.recover_key(kind,"b","b0",last=False)
        self.assertNotIn(("recovered","b"),self.events(kind))
        worker.recover_key(kind,"a","a1",last=True)
        self.assertFalse(self.store.active_keyed_incidents())
        self.assertEqual(self.events(kind)[-1],("recovered","a"))
