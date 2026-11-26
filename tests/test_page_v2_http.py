import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

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

    async def test_empty_inventory_and_stale_snapshot_revoke_remote(self):
        src = self.endpoint.sources
        self.assertTrue(src.allows("10.89.0.17"))
        for addresses, age in (([],0), (["10.89.0.17"],31)):
            stamp = datetime.now(timezone.utc)-timedelta(minutes=age)
            src.path.write_text(json.dumps({"addresses":addresses,"generated_at":stamp.isoformat()}))
            src.next_check, src.version = 0, None
            self.assertFalse(src.allows("10.89.0.17"))
            self.assertTrue(src.allows("127.0.0.1"))
