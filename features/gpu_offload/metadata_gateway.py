"""Preserve Mobo's authoritative login/navigation checks on every cache hit."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import signal
import threading
import time
from urllib.parse import urlsplit,parse_qs

from .cache import Response,SingleFlightCache,cache_key,annotated
from .campaign_gateway import request
from .identity import validate_actor

PATH='/api/moboreels/recent-ids'


class Server(ThreadingHTTPServer):
    daemon_threads=False
    def __init__(self,address,config,token):
        super().__init__(address,Handler);self.config,self.token=Path(config),token
        # Freshness belongs to the HK five-minute cache. This front cache is
        # for coalescing/outage fallback and must not add another five minutes.
        self.cache=SingleFlightCache(ttl=0,max_bytes=16*1024*1024,max_entries=64)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def reply(self,value):
        self.status=value.status;self.send_response(value.status)
        for k,v in value.headers.items():
            if k.lower() not in ('cache-control','vary','connection','content-length'):self.send_header(k,v)
        self.send_header('Content-Length',str(len(value.body)));self.send_header('Connection','close');self.send_header('Cache-Control','no-store');self.send_header('Vary','Cookie');self.end_headers()
        self.wfile.write(value.body);self.close_connection=True
    def error(self,status,code):self.reply(Response(status,{'Content-Type':'application/json'},json.dumps({'error':code,'message':'数据暂不可用，未知数据不按零统计。'},ensure_ascii=False).encode()))

    def do_GET(self):
        started=time.monotonic();route,outcome='none','failed';self.status=503
        try:
            parsed=urlsplit(self.path)
            if parsed.path!=PATH or len(self.path)>16000:return self.error(404,'not_found')
            kind=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=32).get('kind',[''])[-1]
            if kind not in ('story','resource'):return self.error(400,'invalid_kind')
            cfg=json.loads(self.server.config.read_text());cookie=self.headers.get('Cookie','')
            if not cookie or len(cookie)>16384:return self.error(401,'cookie_auth_required')
            auth=request(cfg['api_port'],'/api/auth/status',cookie=cookie,timeout=5,limit=256*1024,encoding='identity')
            if auth.status!=200:return self.error(503,'authentication_unavailable')
            payload=json.loads(auth.body)
            if not payload.get('authenticated'):return self.error(401,'cookie_auth_required')
            actor=payload['user'];validate_actor(actor)
            if actor['tenant_key']!=cfg['source_tenant']:return self.error(403,'permission_denied')
            # This route invokes the existing bridge's current Cookie, module
            # and navigation checks, and reads only the local sync ledger.
            authority=request(cfg['api_port'],'/api/moboreels/sync-runs/latest',cookie=cookie,timeout=8,limit=256*1024,encoding='identity')
            if authority.status!=200:return self.reply(authority)
            source=json.loads(authority.body);run=source.get('run') or {}
            generation=json.dumps([run.get('run_id') or run.get('id'),run.get('status'),source.get('last_success_at')],separators=(',',':'))
            if random.randrange(100)>=int(cfg.get('percentage',0)):
                route,outcome='cpu-origin','forwarded'
                return self.reply(request(cfg['api_port'],self.path,cookie=cookie,timeout=100,limit=4*1024*1024,encoding='identity'))
            route='hk-metadata';key=cache_key(actor,PATH+'?kind='+kind,generation)
            def load():return request(cfg['reader_port'],'/v1/recent-ids',token=self.server.token,
                body=json.dumps(dict(actor=actor,kind=kind,generation=generation)).encode(),timeout=70,limit=4*1024*1024,encoding='identity')
            value,stale,hit=self.server.cache.get(key,load,wait=70)
            stale=stale or value.headers.get('X-Ai-Stale')=='true';outcome='stale' if stale else 'hit' if hit else 'miss'
            return self.reply(annotated(value,stale=stale,hit=hit,backend=route))
        except (ValueError,KeyError):self.error(400,'invalid_request')
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
        except Exception:self.error(503,'metadata_snapshot_unavailable')
        finally:print(json.dumps(dict(kind='metadata_read',route=route,outcome=outcome,status=self.status,milliseconds=round((time.monotonic()-started)*1000))),flush=True)


def main():
    server=Server(('127.0.0.1',19979),os.environ['METADATA_GATEWAY_CONFIG'],os.environ['OFFLOAD_INTERNAL_TOKEN'])
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
