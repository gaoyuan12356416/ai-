import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.fb_page_assignment_v2.ledger import Store
from features.fb_page_assignment_v2.server import Endpoint, ROUTE, Sources
from features.fb_page_assignment_v2.service import Assignment


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

    async def test_identical_concurrent_calls_each_execute_and_return_their_own_result(self):
        counter=0
        async def compute(requests, deadline):
            nonlocal counter
            answers=[]
            for req in requests:
                counter+=1
                answers.append((200,{"snapshot":{},"rows":[{"drama_id":d,"page_id":str(counter)}
                                                            for d in req["drama_ids"]]}))
            return answers
        self.app.compute=compute
        value={"page_group_id":24,"external_task_id":"3092716","drama_ids":["D","D"]}
        answers=await asyncio.gather(*(self.post(value) for _ in range(4)))
        self.assertTrue(all(a[0]==200 for a in answers))
        self.assertEqual(counter,4)
        self.assertEqual(len({a[1]["task_id"] for a in answers}),4)
        self.assertEqual(len({a[1]["results"][0]["page_id"] for a in answers}),4)

    async def test_no_ledger_reads_before_selection_or_during_receipt_append(self):
        db=self.store.db
        statements=[]
        @contextmanager
        def guarded_db(*args,**kwargs):
            self.assertTrue(self.calls,"selection must run before the ledger is opened")
            with db(*args,**kwargs) as connection:
                connection.set_trace_callback(statements.append)
                yield connection
        value={"page_group_id":24,"external_task_id":"same-queue","drama_ids":["D","N","D"]}
        with patch.object(self.store,"db",guarded_db):
            a=await self.post(value)
            b=await self.post(value)
        self.assertEqual((a[0],b[0]),(200,200))
        self.assertNotEqual(a[1]["task_id"],b[1]["task_id"])
        self.assertEqual(sum(map(len,self.calls)),2)
        self.assertFalse([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")])

    async def test_same_queue_changed_pool_order_and_100_plus_20_slices_all_execute(self):
        first={"external_task_id":"3092716","page_group_id":24,"drama_ids":["D","N"]*50}
        requests=[first,{**first,"drama_ids":["N","D"]*10},{**first,"page_group_id":62},first]
        answers=await asyncio.gather(*(self.post(req) for req in requests))
        self.assertTrue(all(a[0]==200 for a in answers))
        self.assertEqual(len({a[1]["task_id"] for a in answers}),4)
        self.assertEqual(sorted(len(req["drama_ids"]) for call in self.calls for req in call),[20,100,100,100])
        for req,answer in zip(requests,answers):
            self.assertEqual([r["drama_id"] for r in answer[1]["results"]],req["drama_ids"])
            self.assertEqual([r["page_id"] for r in answer[1]["results"]],
                             ["12" if d=="D" else None for d in req["drama_ids"]])
        self.assertFalse(self.store.active_keyed_incidents())

    async def test_legacy_receipts_and_pair_table_are_preserved_and_never_reused(self):
        original=self.store.record("3092717","old",24,["D"],
                                   {"snapshot":{},"rows":[{"drama_id":"D","page_id":"999"}]})
        with self.store.db(write=True) as db:
            db.execute("UPDATE assignment SET external_id='3092717' WHERE task_id=?",(original[1]["task_id"],))
            db.execute("CREATE TABLE queue_drama(queue_id,drama_id,pool,result,task_id,PRIMARY KEY(queue_id,drama_id))")
            db.execute("INSERT INTO queue_drama VALUES(?,?,?,?,?)",("3092717","D",24,'{"page_id":"999"}',original[1]["task_id"]))
        value={"external_task_id":"3092717","page_group_id":24,"drama_ids":["D"]}
        first=await self.post(value)
        self.assertEqual(first[1]["results"][0]["page_id"],"12")
        compute=self.app.compute
        await self.app.close()
        self.store=Store(Path(self.temp.name))
        self.app=Assignment(self.store,window=.01,compute=compute)
        await self.app.start();self.endpoint.service=self.app
        second=await self.post(value)
        self.assertNotEqual(first[1]["task_id"],second[1]["task_id"])
        self.assertEqual(sum(map(len,self.calls)),2)
        with self.store.db() as db:
            self.assertEqual(json.loads(db.execute("SELECT response FROM assignment WHERE external_id='3092717'").fetchone()[0]),original[1])
            self.assertEqual(db.execute("SELECT count(*) FROM queue_drama").fetchone()[0],1)

    async def test_unauthorized_and_invalid_do_not_compute(self):
        self.assertEqual((await self.post({},token="other"))[0],401)
        self.assertEqual((await self.post({"external_task_id":"a","drama_ids":["b"]}))[0],400)
        self.assertEqual(self.calls,[])

    async def test_empty_inventory_and_stale_snapshot_revoke_remote(self):
        src=self.endpoint.sources
        self.assertTrue(src.allows("10.89.0.17"))
        for addresses,age in (([],0),(["10.89.0.17"],31)):
            stamp=datetime.now(timezone.utc)-timedelta(minutes=age)
            src.path.write_text(json.dumps({"addresses":addresses,"generated_at":stamp.isoformat()}))
            src.next_check,src.version=0,None
            self.assertFalse(src.allows("10.89.0.17"))
            self.assertTrue(src.allows("127.0.0.1"))
