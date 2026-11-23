"""Keep Campaign Copy's public URL while authenticating every snapshot read."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import signal
import threading
import time

from .cache import Response,SingleFlightCache,cache_key
from .campaign_reader import authorize,target

PREFIX='/reports/campaign-copy-performance'
HEADERS={'content-type','content-encoding','content-disposition','etag','x-report-cache','x-report-cache-age',
         'x-ai-data-time','x-ai-data-version','x-ai-stale','content-security-policy','x-content-type-options','referrer-policy'}


def request(port,path,*,cookie='',token='',body=None,timeout=180,limit=256*1024*1024,encoding='gzip'):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
    headers={'Connection':'close','Accept-Encoding':encoding}
    if cookie:headers['Cookie']=cookie
    if token:headers['Authorization']='Bearer '+token
    if body is not None:headers['Content-Type']='application/json'
    try:
        c.request('POST' if body is not None else 'GET',path,body,headers)
        r=c.getresponse();data=r.read(limit+1)
        if len(data)>limit:raise RuntimeError('snapshot_response_too_large')
        return Response(r.status,{k.title():v for k,v in r.getheaders() if k.lower() in HEADERS},data)
    finally:c.close()


class Server(ThreadingHTTPServer):
    daemon_threads=False
    def __init__(self,address,config,token):
        super().__init__(address,Handler)
        self.config,self.token=Path(config),token
        self.cache=SingleFlightCache(ttl=300,max_bytes=128*1024*1024,max_entries=16)
        self.status=SingleFlightCache(ttl=3,max_bytes=65536,max_entries=1)
        self.requests=threading.BoundedSemaphore(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass

    def reply(self,value,route,stale=False):
        status=value.status
        if status==200 and self.headers.get('If-None-Match')==value.headers.get('Etag'):status=304
        self.send_response(status)
        for k,v in value.headers.items():
            if stale and k.lower() in ('x-report-cache','x-ai-stale'):continue
            self.send_header(k,v)
        if stale:
            self.send_header('X-Report-Cache','stale');self.send_header('X-AI-Stale','true')
        self.send_header('X-AI-Backend',route);self.send_header('Cache-Control','no-store')
        self.send_header('Vary','Cookie, Accept-Encoding');self.send_header('Connection','close')
        self.send_header('Content-Length','0' if status==304 else str(len(value.body)))
        self.end_headers()
        if status!=304 and self.command!='HEAD':self.wfile.write(value.body)
        self.close_connection=True;self.result_status=status

    def error(self,status,code):
        self.reply(Response(status,{'Content-Type':'application/json'},json.dumps({'error':code}).encode()),'none')

    def do_HEAD(self):self.do_GET()

    def do_GET(self):
        started=time.monotonic();route,outcome='none','failed';self.result_status=503
        if not self.server.requests.acquire(timeout=1):return self.error(503,'campaign_reader_busy')
        try:
            self.connection.settimeout(180)
            if not self.path.startswith(PREFIX+'/'):return self.error(404,'not_found')
            local=self.path[len(PREFIX):];target(local)
            cfg=json.loads(self.server.config.read_text());cookie=self.headers.get('Cookie','')
            if not cookie or len(cookie)>16384:return self.error(401,'cookie_auth_required')
            authenticated=request(cfg['api_port'],'/api/auth/status',cookie=cookie,timeout=5,limit=256*1024,encoding='identity')
            if authenticated.status!=200:return self.error(503,'authentication_unavailable')
            payload=json.loads(authenticated.body)
            if not payload.get('authenticated'):return self.error(401,'cookie_auth_required')
            actor=payload.get('user');authorize(actor,cfg['source_tenant'])
            # Preserve current identity and the complete permission scope in
            # the cache key. Authentication is never answered from this cache.
            if random.randrange(100)>=int(cfg.get('percentage',0)):
                route,outcome='cpu-campaign-origin','forwarded'
                if cfg.get('origin_reader_port'):
                    route='cpu-campaign-cache'
                    value=request(cfg['origin_reader_port'],'/v1/query',token=self.server.token,
                        body=json.dumps({'actor':actor,'target':local},separators=(',',':')).encode())
                    return self.reply(value,route,value.headers.get('X-Ai-Stale')=='true')
                return self.reply(request(cfg['origin_port'],local,cookie=cookie),route)
            route='hk-campaign-snapshot'
            def load_status():return request(cfg['reader_port'],'/v1/status',token=self.server.token,timeout=5,limit=65536)
            status,status_stale,_=self.server.status.get('current',load_status,wait=6)
            version=json.loads(status.body)['version'];key=cache_key(actor,local,version)
            def load():
                return request(cfg['reader_port'],'/v1/query',token=self.server.token,
                    body=json.dumps({'actor':actor,'target':local},separators=(',',':')).encode())
            value,stale,hit=self.server.cache.get(key,load,wait=180)
            stale=stale or status_stale or value.headers.get('X-Ai-Stale')=='true'
            outcome='stale' if stale else 'hit' if hit else 'miss'
            return self.reply(value,route,stale)
        except PermissionError:return self.error(403,'permission_denied')
        except (ValueError,KeyError):return self.error(400,'invalid_request')
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
        except Exception:return self.error(503,'campaign_snapshot_unavailable')
        finally:
            self.server.requests.release()
            print(json.dumps({'kind':'campaign_read','route':route,'outcome':outcome,'status':self.result_status,
                              'milliseconds':round((time.monotonic()-started)*1000)}),flush=True)


def main():
    server=Server(('127.0.0.1',19974),os.environ['CAMPAIGN_GATEWAY_CONFIG'],os.environ['OFFLOAD_INTERNAL_TOKEN'])
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
