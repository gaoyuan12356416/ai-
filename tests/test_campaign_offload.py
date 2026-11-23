import gzip
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from features.gpu_offload.campaign import service
from features.gpu_offload.campaign_snapshot import index_snapshot,render,compressed_view
from features.gpu_offload.campaign_reader import Server
from features.gpu_offload.campaign_gateway import Server as Gateway


class CampaignSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.wire=dict(v=2,m={'generated_at':'2026-11-13 23:00:00','read_only_verified':True},p={'success':3},lp={'0':{'success':3}},
            cf=['platform','campaign_id','copy_date'],c=[[0,'same','2026-11-10'],[3,'same','2026-10-01'],[0,'empty','2026-11-10']],
            df=['platform','campaign_id','dt','spend'],d=[[0,'same','2026-11-10',1],[0,'same','2026-11-11',2],[3,'same','2026-11-10',9]],
            xf=['level','entity_key','copy_date'],x=[[1,'0:1:7','2026-11-10'],[2,'0:2:7','2026-11-10']],
            xdf=['entity_key','dt','spend'],xd=[['0:1:7','2026-11-10',4],['0:2:7','2026-11-10',8]])
        seed=self.root/'fixture-source.json';seed.write_text(json.dumps(self.wire))
        index_snapshot(seed,self.root/'index')

    def tearDown(self):self.temp.cleanup()

    def test_existing_cohort_arithmetic_and_full_history_are_preserved(self):
        with patch.object(service,'query_raw',side_effect=AssertionError('business SQL forbidden')):
            for level,start,end in [(0,'2026-11-10','2026-11-10'),(1,'',''),(2,'',''),(0,'','')]:
                query=urlencode(dict(view='cohort',level=level,copy_start=start,copy_end=end))
                _,body=render(self.root/'index',query)
                self.assertEqual(json.loads(body),service.scoped_wire(self.wire,level,start,end))
            self.assertEqual(json.loads(render(self.root/'index','')[1]),self.wire)

    def test_compressed_stream_matches_complete_source_without_loading_fact_objects(self):
        target=self.root/'view.gz';compressed_view(self.root/'index','',target)
        self.assertEqual(json.loads(gzip.decompress(target.read_bytes())),self.wire)

    def test_invalid_seed_does_not_replace_last_successful_index(self):
        manifest=(self.root/'index/current.json').read_bytes()
        broken=self.root/'broken.json';broken.write_text('{"v":2,"m":{}}')
        with self.assertRaises(ValueError):index_snapshot(broken,self.root/'index')
        self.assertEqual((self.root/'index/current.json').read_bytes(),manifest)

    def test_parameters_remain_bounded_and_cannot_inject_sql(self):
        for query in ('view=cohort&level=7','view=cohort&copy_start=invalid','view=cohort&copy_start=2026-12-01&copy_end=2026-11-01'):
            with self.assertRaises(ValueError):render(self.root/'index',query)

    def test_private_http_rechecks_tenant_and_permission_before_cached_response(self):
        server=Server(('127.0.0.1',0),self.root/'index','t'*40,'company')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        actor=dict(tenant_key='company',user_id='u1',role='user',permissions={'ad_control_v3':True})
        def call(actor,token='t'*40):
            c=http.client.HTTPConnection(*server.server_address,timeout=5)
            try:
                c.request('POST','/v1/query',json.dumps({'actor':actor,'target':'/api/data?view=cohort&level=0&copy_start=&copy_end='}),{'Authorization':'Bearer '+token})
                r=c.getresponse();return r.status,r.read()
            finally:c.close()
        try:
            status,body=call(actor);self.assertEqual(status,200)
            self.assertEqual(json.loads(gzip.decompress(body)),service.scoped_wire(self.wire,0,'',''))
            self.assertEqual(call(dict(actor,tenant_key='other'))[0],403)
            self.assertEqual(call(dict(actor,permissions={'ad_control_v3':False}))[0],403)
            self.assertEqual(call(actor,'bad')[0],401)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_public_gateway_authenticates_cache_hits_and_keeps_stale_snapshot_on_outage(self):
        actor=dict(tenant_key='company',user_id='u1',role='user',permissions={'ad_control_v3':True})
        class Auth(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(inner):
                body=json.dumps(dict(authenticated=True,user=inner.server.actor)).encode()
                inner.server.checks+=1
                inner.send_response(200);inner.send_header('Content-Length',str(len(body)));inner.end_headers();inner.wfile.write(body)
        auth=ThreadingHTTPServer(('127.0.0.1',0),Auth);auth.actor=actor;auth.checks=0
        reader=Server(('127.0.0.1',0),self.root/'index','t'*40,'company')
        config=self.root/'gateway.json';config.write_text(json.dumps({'api_port':auth.server_port,'source_tenant':'company','reader_port':reader.server_port,'origin_port':1,'percentage':100}))
        gateway=Gateway(('127.0.0.1',0),config,'t'*40)
        servers=[auth,reader,gateway];threads=[]
        for server in servers:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread)
        def call(cookie='session=fixture'):
            c=http.client.HTTPConnection(*gateway.server_address,timeout=5)
            try:
                c.request('GET','/reports/campaign-copy-performance/api/data?view=cohort&level=0&copy_start=&copy_end=',headers={'Cookie':cookie})
                response=c.getresponse();return response.status,dict(response.getheaders()),response.read()
            finally:c.close()
        try:
            self.assertEqual(call('')[0],401)
            first=call();second=call();self.assertEqual(first[0],200);self.assertEqual(first[2],second[2]);self.assertEqual(auth.checks,2)
            auth.actor=dict(actor,permissions={'ad_control_v3':False});self.assertEqual(call()[0],403)
            auth.actor=dict(actor,tenant_key='other');self.assertEqual(call()[0],403)
            auth.actor=actor
            reader.shutdown();reader.server_close();servers.remove(reader)
            _,value=gateway.status.entries['current'];gateway.status.entries['current']=(time.monotonic()-10,value)
            result=call();self.assertEqual(result[0],200);self.assertEqual(result[1]['X-Report-Cache'],'stale');self.assertEqual(result[2],first[2])
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(timeout=5)


if __name__=='__main__':unittest.main()
