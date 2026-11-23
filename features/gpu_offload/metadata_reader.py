"""Small authenticated read service; shares the existing global SQL leases."""
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
import time

from .cache import Response,SingleFlightCache,cache_key,annotated
from .identity import validate_actor
from .metadata import recent_ids
from .sql_client import Broker,read_session
from .sql_reconciliation import atomic_json


class Server(ThreadingHTTPServer):
    daemon_threads=False
    def __init__(self,address,config,token,fetch):
        super().__init__(address,Handler)
        self.config,self.token,self.fetch=config,token,fetch
        self.cache=SingleFlightCache(ttl=300,max_bytes=16*1024*1024,max_entries=64)
        self.compute=threading.BoundedSemaphore(1)
        self.root=Path(config['cache_root']);self.root.mkdir(parents=True,exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def reply(self,value):
        self.send_response(value.status)
        for k,v in value.headers.items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(value.body)));self.send_header('Connection','close');self.end_headers()
        self.wfile.write(value.body);self.close_connection=True

    def error(self,status,code):
        self.reply(Response(status,{'Content-Type':'application/json'},json.dumps({'error':code,'message':'数据暂不可用，未知数据不按零统计。'},ensure_ascii=False).encode()))

    def do_POST(self):
        self.connection.settimeout(65)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=65536:return self.error(400,'invalid_request')
            raw=self.rfile.read(length)
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):return self.error(401,'unauthorized')
            if self.path!='/v1/recent-ids':return self.error(404,'not_found')
            value=json.loads(raw);actor=value['actor'];validate_actor(actor)
            if actor['tenant_key']!=self.server.config['source_tenant'] or (actor['role']!='admin' and not actor.get('permissions',{}).get('moboreels_library')):return self.error(403,'permission_denied')
            kind=value['kind'];generation=value['generation']
            if kind not in ('story','resource') or not isinstance(generation,str) or len(generation)>256:raise ValueError('invalid_query')
            key=cache_key(actor,'/api/moboreels/recent-ids?kind='+kind,'mobo-v1:'+generation)
            file=self.server.root/(key+'.json')
            # Restore only this exact actor, permission, filter and source
            # generation. An expired disk snapshot is never relabeled fresh.
            with self.server.cache.lock:
                if key not in self.server.cache.entries and file.exists():
                    saved=json.loads(file.read_text());body=json.dumps(saved['body'],ensure_ascii=False,separators=(',',':')).encode()
                    age=max(0,time.time()-saved['created_at'])
                    if len(body)<=4*1024*1024:
                        self.server.cache.entries[key]=(time.monotonic()-age,Response(200,saved['headers'],body));self.server.cache.size+=len(body)
                        while self.server.cache.size>self.server.cache.max_bytes or len(self.server.cache.entries)>self.server.cache.max_entries:
                            _,removed=self.server.cache.entries.popitem(last=False);self.server.cache.size-=len(removed[1].body)
            def load():
                if not self.server.compute.acquire(timeout=1):raise RuntimeError('metadata_reader_busy')
                try:payload=self.server.fetch(kind)
                finally:self.server.compute.release()
                created=time.time();data_time=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(created))
                headers={'Content-Type':'application/json; charset=utf-8','X-AI-Data-Time':data_time,'X-AI-Data-Version':generation}
                body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
                if len(body)>4*1024*1024:raise ValueError('existing_response_limit')
                atomic_json(file,dict(created_at=created,headers=headers,body=payload))
                files=sorted(self.server.root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
                for old in files[64:]:
                    if old!=file:old.unlink(missing_ok=True)
                return Response(200,headers,body)
            result,stale,hit=self.server.cache.get(key,load,wait=65)
            self.reply(annotated(result,stale=stale,hit=hit,backend='hk-metadata'))
        except (ValueError,KeyError):self.error(400,'invalid_request')
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
        except Exception:self.error(503,'metadata_snapshot_unavailable')


def main():
    config=json.loads(Path(os.environ['METADATA_CONFIG']).read_text());token=os.environ['OFFLOAD_INTERNAL_TOKEN']
    broker=Broker(int(os.environ['SQL_OFFLOAD_PORT']),os.environ['SQL_OFFLOAD_TOKEN'],os.environ['SQL_OFFLOAD_RECEIPTS'])
    def fetch(kind):
        with read_session(broker,'mobo-recent-'+kind,user=os.environ['SQL_OFFLOAD_USER'],password=os.environ['SQL_OFFLOAD_PASSWORD'],database='ads_ai',budget_seconds=60,statement_timeout_ms=15000) as session:
            with session.cursor() as cursor:return recent_ids(cursor,config['institution_user_id'],kind)
    server=Server(('127.0.0.1',19980),config,token,fetch)
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
