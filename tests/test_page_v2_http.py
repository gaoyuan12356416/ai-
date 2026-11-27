import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from features.fb_page_assignment_v2.ledger import Store
from features.fb_page_assignment_v2.server import Endpoint, ROUTE, Sources
from features.fb_page_assignment_v2.service import Assignment, normalize


class HTTPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        (path/"peers.json").write_text(json.dumps({"addresses":["10.89.0.17"],"generated_at":datetime.now(timezone.utc).isoformat()}))
        self.calls = []
        self.store = Store(path)
        async def compute(requests, deadline):
            self.calls.append(requests)
            return [(200,{"rows":[{"drama_id":v,"page_id":"12" if v=="D" else None,
                                   "selection_stage":"language_profile" if v=="D" else "none",
                                   "error_code":"" if v=="D" else "no_profile_match"}
                                  for v in req["drama_ids"]],"snapshot":{"candidates":[]}})
                    for req in requests]
        self.app = Assignment(self.store, window=.01, compute=compute)
        await self.app.start()
        self.token = "t"*32
        self.endpoint = Endpoint(self.app, self.token, Sources(path/"peers.json","127.0.0.1"))
        self.server = await asyncio.start_server(self.endpoint.serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.app.close()
        self.temp.cleanup()

    async def post(self, value, token=None):
        reader,writer=await asyncio.open_connection("127.0.0.1",self.port)
        body=json.dumps(value).encode()
        writer.write(b"POST "+ROUTE+b" HTTP/1.1\r\nHost: local\r\nConnection: close\r\nAuthorization: Bearer "+
                     (token or self.token).encode()+b"\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
        await writer.drain()
        data=await reader.read()
        writer.close()
        await writer.wait_closed()
        headers,content=data.split(b"\r\n\r\n",1)
        return int(headers.split()[1]),json.loads(content)

    async def test_coalesce_retry_order_and_conflict(self):
        value={"page_group_id":62,"external_task_id":"cross:1:a","drama_ids":["D","N","D"]}
        dup,other,first=await asyncio.gather(self.post(value),self.post({**value,"external_task_id":"cross:2:a"}),self.post(value))
        self.assertEqual(first,dup)
        self.assertEqual([r['page_id'] for r in first[1]['results']],["12",None,"12"])
        self.assertEqual(sum(map(len,self.calls)),2)
        self.assertEqual(await self.post(value),first)
        self.assertEqual((await self.post({**value,"page_group_id":2}))[0],409)
        self.assertEqual(other[0],200)

    async def test_unauthorized_and_invalid_do_not_compute(self):
        self.assertEqual((await self.post({},token="other"))[0],401)
        self.assertEqual((await self.post({"external_task_id":"a","drama_ids":["b"]}))[0],400)
        self.assertEqual(self.calls,[])

    async def test_numeric_parent_handles_100_and_20_items_with_concurrent_retries(self):
        first = {"external_task_id":"3092716", "page_group_id":24, "drama_ids":["D","N"]*50}
        rest = {**first, "drama_ids":["N","D"]*10}
        a,b,again_a,again_b = await asyncio.gather(
            self.post(first), self.post(rest), self.post(first), self.post(rest))
        self.assertEqual(a[0],200);self.assertEqual(b[0],200)
        self.assertEqual(a,again_a);self.assertEqual(b,again_b)
        self.assertNotEqual(a[1]["task_id"],b[1]["task_id"])
        for request, answer in [(first,a),(rest,b)]:
            self.assertEqual([r["drama_id"] for r in answer[1]["results"]],request["drama_ids"])
        self.assertTrue(all(len(req["drama_ids"]) == len(set(req["drama_ids"]))
                            for call in self.calls for req in call))
        with self.store.db() as db:
            keys=[r[0] for r in db.execute("SELECT external_id FROM assignment")]
            self.assertEqual(db.execute("SELECT count(*) FROM queue_drama").fetchone()[0],2)
        self.assertEqual(len(keys),2)
        self.assertTrue(all(k.startswith("3092716:chunk:") and len(k)<=128 for k in keys))
        self.assertFalse(self.store.active_keyed_incidents())

    async def test_original_numeric_receipt_is_preserved_and_only_new_slice_clears_old_fault(self):
        first = {"external_task_id":"3092717", "page_group_id":24, "drama_ids":["D"]*100}
        rest = {**first, "drama_ids":["D"]*20}
        _, fingerprint = normalize(first)
        original = self.store.save(first["external_task_id"],fingerprint,24,first["drama_ids"],
            {"snapshot":{}, "rows":[{"drama_id":"D","page_id":"999" if i==0 else "888"}
                                    for i in range(100)]})
        await self.app.notify("request_conflict","3092717",24,"idempotency_conflict")
        self.assertEqual(await self.post(first),original)
        self.assertIn("3092717",self.store.active_keyed_incidents()["request_conflict"])
        answer=await self.post(rest)
        self.assertEqual(answer[0],200)
        self.assertEqual(len(answer[1]["results"]),20)
        self.assertFalse(self.store.active_keyed_incidents())
        self.assertEqual(self.store.lookup("3092717",fingerprint),original)
        compute=self.app.compute
        await self.app.close()
        self.store=Store(Path(self.temp.name))
        self.app=Assignment(self.store,window=.01,compute=compute)
        await self.app.start();self.endpoint.service=self.app
        self.assertEqual(await self.post(first),original)
        self.assertEqual(await self.post(rest),answer)
        self.assertTrue(all(row["page_id"]=="999" for row in answer[1]["results"]))
        self.assertEqual(self.calls,[])

    async def test_queue_pairs_reuse_across_order_and_duplicates_but_reject_other_pool(self):
        parent={"external_task_id":"18446744073709551615","page_group_id":24,"drama_ids":["D","N","D"]}
        requests=[parent,{**parent,"drama_ids":["D","D","N"]},
                  {**parent,"drama_ids":["D","N"]}]
        answers=[await self.post(request) for request in requests]
        self.assertTrue(all(a[0]==200 for a in answers))
        self.assertEqual(len({a[1]["task_id"] for a in answers}),3)
        for request, answer in zip(requests,answers):
            self.assertEqual(await self.post(request),answer)
            self.assertEqual([r["page_id"] for r in answer[1]["results"]],
                             ["12" if d=="D" else None for d in request["drama_ids"]])
        conflict=await self.post({**parent,"page_group_id":62})
        self.assertEqual(conflict,(409,{"error":"queue_drama_pool_conflict","results":[]}))
        self.assertEqual(sum(map(len,self.calls)),1)

    async def test_string_queue_ids_only_select_new_pairs_and_other_queues_are_independent(self):
        parent={"external_task_id":"cross:"+"9"*122,"page_group_id":24,"drama_ids":["D","D","N"]}
        self.assertEqual((await self.post(parent))[0],200)
        changed={**parent,"drama_ids":["N","D","new","new"]}
        result=await self.post(changed)
        self.assertEqual(result[0],200)
        self.assertEqual(self.calls[-1][0]["drama_ids"],["new"])
        self.assertEqual((await self.post({**parent,"external_task_id":"another-queue"}))[0],200)
        self.assertEqual(self.calls[-1][0]["drama_ids"],["D","N"])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM queue_drama").fetchone()[0],5)
            self.assertLessEqual(db.execute("SELECT max(length(external_id)) FROM assignment").fetchone()[0],128)

    async def test_concurrent_overlapping_slices_return_the_same_winning_page(self):
        async def different_decisions(requests, deadline):
            return [(200,{"rows":[{"drama_id":d,"page_id":str(100+i)} for d in req["drama_ids"]],
                          "snapshot":{}}) for i,req in enumerate(requests)]
        self.app.compute=different_decisions
        parent={"external_task_id":"3099999","page_group_id":24,"drama_ids":["D","A"]}
        answers=await asyncio.gather(self.post(parent),self.post({**parent,"drama_ids":["D","B","D"]}))
        self.assertTrue(all(a[0]==200 for a in answers))
        pages={r["page_id"] for a in answers for r in a[1]["results"] if r["drama_id"]=="D"}
        self.assertEqual(len(pages),1)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM queue_drama").fetchone()[0],3)

    async def test_empty_inventory_and_stale_snapshot_revoke_remote(self):
        src = self.endpoint.sources
        self.assertTrue(src.allows("10.89.0.17"))
        for addresses, age in (([],0), (["10.89.0.17"],31)):
            stamp = datetime.now(timezone.utc)-timedelta(minutes=age)
            src.path.write_text(json.dumps({"addresses":addresses,"generated_at":stamp.isoformat()}))
            src.next_check, src.version = 0, None
            self.assertFalse(src.allows("10.89.0.17"))
            self.assertTrue(src.allows("127.0.0.1"))
