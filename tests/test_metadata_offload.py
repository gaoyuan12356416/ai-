import datetime as dt
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from features.gpu_offload.metadata import recent_ids
from features.gpu_offload.metadata_reader import Server as Reader
from features.gpu_offload.metadata_gateway import Server as Gateway


class NamedQueryTests(unittest.TestCase):
    def test_original_twenty_four_hour_scope_and_ambiguous_resource_mapping(self):
        class Cursor:
            def __init__(self):self.calls=[]
            def execute(self,sql,args):self.calls.append((sql,args))
            def fetchall(self):
                if len(self.calls)==1:return [{'story_id':str(i)} for i in range(1001)]
                rows=[]
                for story in self.calls[-1][1]:
                    if story!='0':rows.append(dict(story_id=story,resource_id='shared' if story in ('1','2') else 'r'+story))
                    if story=='3':rows.append(dict(story_id=story,resource_id='ambiguous'))
                return rows
        cursor=Cursor();now=dt.datetime(2026,11,23,20,0,0)
        result=recent_ids(cursor,'institution-with-quote\'','resource',now)
        self.assertEqual(len(cursor.calls),4)
        self.assertEqual([len(call[1]) for call in cursor.calls[1:]],[500,500,1])
        self.assertEqual(cursor.calls[0][1],("institution-with-quote'",'2026-11-22 20:00:00','2026-11-23 20:00:00'))
        self.assertNotIn("institution-with-quote'",cursor.calls[0][0])
        self.assertEqual(result['story_count'],1001);self.assertEqual(result['unmapped_count'],2)
        self.assertEqual(result['ids'].count('shared'),1);self.assertNotIn('r3',result['ids'])
        self.assertTrue(all(sql.startswith('SELECT') for sql,args in cursor.calls))
        with self.assertRaises(ValueError):recent_ids(cursor,'org','arbitrary-sql')

    def test_permission_revocation_tenant_cache_and_outage_use_no_extra_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);actor=dict(tenant_key='company',user_id='u1',role='user',permissions={'moboreels_library':True})
            calls=[]
            def fetch(kind):calls.append(kind);return dict(ids=['story'],story_count=1,unmapped_count=0)
            reader=Reader(('127.0.0.1',0),dict(cache_root=str(root/'cache'),source_tenant='company'),'t'*40,fetch)
            class Auth(BaseHTTPRequestHandler):
                def log_message(self,*_):pass
                def do_GET(inner):
                    if inner.path=='/api/auth/status':status=200;value=dict(authenticated=inner.server.logged_in,user=inner.server.actor)
                    else:status=inner.server.permission;value={'run':{'id':'generation','status':'completed'}}
                    raw=json.dumps(value).encode();inner.send_response(status);inner.send_header('Content-Length',str(len(raw)));inner.end_headers();inner.wfile.write(raw)
            auth=ThreadingHTTPServer(('127.0.0.1',0),Auth);auth.actor=actor;auth.logged_in=True;auth.permission=200
            config=root/'gateway.json';config.write_text(json.dumps(dict(api_port=auth.server_port,reader_port=reader.server_port,source_tenant='company',percentage=100)))
            gateway=Gateway(('127.0.0.1',0),config,'t'*40);servers=[reader,auth,gateway];threads=[]
            for server in servers:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread)
            def call():
                c=http.client.HTTPConnection(*gateway.server_address,timeout=5)
                try:
                    c.request('GET','/api/moboreels/recent-ids?kind=story',headers={'Cookie':'session=fixture'})
                    response=c.getresponse();return response.status,json.loads(response.read())
                finally:c.close()
            try:
                self.assertEqual(call()[0],200);self.assertEqual(call()[0],200);self.assertEqual(calls,['story'])
                auth.logged_in=False;self.assertEqual(call()[0],401);auth.logged_in=True
                auth.permission=403;self.assertEqual(call()[0],403);auth.permission=200
                auth.actor=dict(actor,tenant_key='other');self.assertEqual(call()[0],403);auth.actor=actor
                self.assertEqual(calls,['story'])
                reader.shutdown();reader.server_close();servers.remove(reader)
                status,value=call();self.assertEqual(status,200);self.assertEqual(value['ids'],['story']);self.assertTrue(value['meta']['offload_cache_stale'])
                self.assertEqual(calls,['story'])
            finally:
                for server in servers:server.shutdown();server.server_close()
                for thread in threads:thread.join(timeout=5)


if __name__=='__main__':unittest.main()
