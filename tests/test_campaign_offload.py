import gzip
import copy
import io
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
from features.gpu_offload.campaign_history import write_merged_seed
from features.gpu_offload.campaign_snapshot import current


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

    def test_streaming_history_matches_original_refresh_and_removed_membership(self):
        payload=dict(meta=dict(generated_at='2026-11-14 09:00:00',read_only_verified=True),
            copy_pipeline={},copy_pipelines={},
            campaigns=[dict(zip(self.wire['cf'],row)) for row in self.wire['c'] if row[1]!='empty'],
            extra_entities=[dict(zip(self.wire['xf'],self.wire['x'][0]))],
            daily=[dict(platform=0,campaign_id='same',dt='2026-11-11',spend=44)],
            extra_daily=[dict(entity_key='0:1:7',dt='2026-11-11',spend=55)])
        dates=['2026-11-11'];next_history='2026-10-01'
        expected=service.encode_wire_payload(service.merge_history(copy.deepcopy(payload),self.wire,dates,next_history))
        output=io.StringIO()
        write_merged_seed(service.encode_wire_payload(payload),current(self.root/'index')[1],self.wire['m'],dates,next_history,output)
        self.assertEqual(json.loads(output.getvalue()),expected)
        self.assertEqual(len(expected['d']),3) # New day plus both platforms' old facts.
        self.assertEqual(len(expected['xd']),2) # Removed Ad's old facts are excluded.

    def test_smaller_disjoint_copy_log_pages_preserve_sparse_full_history(self):
        identities=[1,2,10,100,115,1000,10**8]
        class Cursor:
            def __init__(self):self.sql='';self.args=();self.ranges=[]
            def execute(self,sql,args=None):self.sql=' '.join(sql.split());self.args=args
            def fetchone(self):
                if 'MAX(id)' in self.sql:return {'max_id':max(identities)}
                choices=[value for value in identities if self.args[0]<value<=self.args[1]]
                assert self.sql.endswith('ORDER BY id LIMIT 1')
                return {'id':choices[0]} if choices else None
            def fetchall(self):
                self.ranges.append(self.args)
                return [{'id':value} for value in identities if self.args[0]<value<=self.args[1]]
        full,small=Cursor(),Cursor()
        with patch.object(service,'COPY_LOG_PAGE_ROWS',250000):expected=service.read_copy_logs(full)
        with patch.object(service,'COPY_LOG_PAGE_ROWS',2):actual=service.read_copy_logs(small)
        self.assertEqual(actual,expected);self.assertEqual([row['id'] for row in actual],identities)
        self.assertTrue(all(previous[1]<=following[0] for previous,following in zip(small.ranges,small.ranges[1:])))
        self.assertTrue(all(end-start<=2 for start,end in small.ranges))
        self.assertLess(len(small.ranges),2*len(identities))

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
            self.assertEqual(call(dict(actor,permissions={'ad_control_v3':False}))[0],200)
            self.assertEqual(call(actor,'bad')[0],401)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_public_gateway_authenticates_cache_hits_and_keeps_stale_snapshot_on_outage(self):
        actor=dict(tenant_key='company',user_id='u1',role='user',permissions={'ad_control_v3':True})
        class Auth(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(inner):
                body=json.dumps(dict(authenticated=inner.server.allowed,user=inner.server.actor)).encode()
                inner.server.checks+=1
                inner.send_response(200);inner.send_header('Content-Length',str(len(body)));inner.end_headers();inner.wfile.write(body)
        auth=ThreadingHTTPServer(('127.0.0.1',0),Auth);auth.actor=actor;auth.checks=0;auth.allowed=True
        reader=Server(('127.0.0.1',0),self.root/'index','t'*40,'company')
        fallback=Server(('127.0.0.1',0),self.root/'index','t'*40,'company')
        config=self.root/'gateway.json';config.write_text(json.dumps({'api_port':auth.server_port,'source_tenant':'company','reader_port':reader.server_port,'origin_port':1,'percentage':100}))
        gateway=Gateway(('127.0.0.1',0),config,'t'*40)
        servers=[auth,reader,fallback,gateway];threads=[]
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
            auth.allowed=False;self.assertEqual(call()[0],401);auth.allowed=True
            auth.actor=dict(actor,permissions={'ad_control_v3':False});self.assertEqual(call()[0],200)
            auth.actor=dict(actor,tenant_key='other');self.assertEqual(call()[0],403)
            auth.actor=actor
            configured=json.loads(config.read_text());configured.update(percentage=0,origin_reader_port=reader.server_port);config.write_text(json.dumps(configured))
            local=call();self.assertEqual(local[2],first[2]);self.assertEqual(local[1]['X-AI-Backend'],'cpu-campaign-cache')
            configured['percentage']=100;config.write_text(json.dumps(configured))
            reader.shutdown();reader.server_close();servers.remove(reader)
            _,value=gateway.status.entries['current'];gateway.status.entries['current']=(time.monotonic()-10,value)
            result=call();self.assertEqual(result[0],200);self.assertEqual(result[1]['X-Report-Cache'],'stale');self.assertEqual(result[2],first[2])
            # A different current user has no actor-keyed front cache. It
            # still receives the verified local index, never an origin SQL
            # refresh, and remains subject to fresh login/tenant checks.
            configured['origin_reader_port']=fallback.server_port;config.write_text(json.dumps(configured))
            gateway.status.entries.clear();auth.actor=dict(actor,user_id='new-user')
            result=call();self.assertEqual(result[0],200);self.assertEqual(gzip.decompress(result[2]),gzip.decompress(first[2]))
            self.assertEqual(result[1]['X-AI-Backend'],'cpu-campaign-cache');self.assertEqual(result[1]['X-AI-Stale'],'true')
            auth.allowed=False;self.assertEqual(call()[0],401);auth.allowed=True
            auth.actor=dict(actor,tenant_key='other');self.assertEqual(call()[0],403)
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(timeout=5)


if __name__=='__main__':unittest.main()
