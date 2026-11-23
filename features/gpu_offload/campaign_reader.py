"""Authenticated named Campaign Copy views from a local immutable index."""
import datetime as dt
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import threading
from urllib.parse import urlsplit

from .cache import SingleFlightCache,Response,cache_key
from .campaign_snapshot import current,compressed_view
from .campaign import service as original
from .identity import validate_actor


def authorize(actor,source_tenant):
    validate_actor(actor)
    if actor['tenant_key']!=source_tenant:raise PermissionError('tenant_denied')
    # This existing organization report has no ad_control_v3 entitlement.
    # Its source is shared within the configured tenant; the public gateway
    # checks the current login on every request, including cache hits.


def target(value):
    if not isinstance(value,str) or len(value)>16000:raise ValueError('invalid_target')
    parsed=urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in ('/','/index.html','/api/data'):
        raise ValueError('unsupported_query')
    return parsed


class Server(ThreadingHTTPServer):
    daemon_threads=False
    def __init__(self,address,root,token,source_tenant):
        super().__init__(address,Handler)
        self.root,self.token,self.source_tenant=Path(root),token,source_tenant
        self.views=self.root/'views';self.views.mkdir(exist_ok=True)
        self.cache=SingleFlightCache(ttl=300,max_entries=8,max_bytes=65536)
        self.compute=threading.BoundedSemaphore(1)
        self.upload=threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass

    def authenticated(self):
        return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token)

    def json(self,status,value):
        body=json.dumps(value,separators=(',',':')).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
        self.end_headers();self.wfile.write(body);self.close_connection=True

    def do_GET(self):
        if not self.authenticated():return self.json(401,{'error':'unauthorized'})
        parsed=urlsplit(self.path)
        if parsed.path not in ('/v1/status','/v1/index'):return self.json(404,{'error':'not_found'})
        try:
            manifest,_=current(self.server.root)
            if parsed.path=='/v1/index':
                # Only the exact manifest version may be exported. A caller
                # cannot choose a filesystem path or another local database.
                if parsed.query!='version='+manifest['version']:return self.json(409,{'error':'snapshot_version_changed'})
                archive=self.server.root/(manifest['version']+'.sqlite3.gz')
                self.connection.settimeout(180)
                with archive.open('rb') as stream:
                    self.send_response(200);self.send_header('Content-Type','application/gzip')
                    self.send_header('Content-Length',str(os.fstat(stream.fileno()).st_size));self.send_header('Connection','close');self.end_headers()
                    for block in iter(lambda:stream.read(65536),b''):self.wfile.write(block)
                self.close_connection=True;return
            self.json(200,dict(ok=True,**manifest))
        except Exception:self.json(503,{'error':'snapshot_unavailable'})

    def do_POST(self):
        if not self.authenticated():
            # Drain small named-query requests so the closing TCP socket does
            # not discard the 401 response because unread bytes remain.
            try:
                length=int(self.headers.get('Content-Length','0'))
                if 0<length<=65536:
                    self.connection.settimeout(5);self.rfile.read(length)
            except (ValueError,OSError):pass
            return self.json(401,{'error':'unauthorized'})
        self.connection.settimeout(240)
        if self.path=='/v1/seed':
            if not self.server.upload.acquire(False):return self.json(409,{'error':'seed_upload_running'})
            temporary=self.server.root.parent/'seed.incoming';destination=self.server.root.parent/'seed.json'
            try:
                if destination.exists():return self.json(409,{'error':'seed_already_installed'})
                size=int(self.headers.get('Content-Length','0'));expected=self.headers.get('X-Snapshot-SHA256','')
                if not 0<size<=512*1024*1024 or not re.fullmatch('[a-f0-9]{64}',expected):raise ValueError('invalid_seed')
                digest=hashlib.sha256();remaining=size
                with temporary.open('wb') as out:
                    while remaining:
                        data=self.rfile.read(min(1024*1024,remaining))
                        if not data:raise ValueError('truncated_seed')
                        out.write(data);digest.update(data);remaining-=len(data)
                    out.flush();os.fsync(out.fileno())
                if digest.hexdigest()!=expected:raise ValueError('seed_checksum_failed')
                temporary.chmod(0o600);temporary.replace(destination)
                return self.json(200,{'ok':True,'sha256':expected,'bytes':size})
            except (ValueError,OSError):return self.json(400,{'error':'invalid_seed'})
            finally:
                if temporary.exists():temporary.unlink()
                self.server.upload.release()
        if self.path!='/v1/query':return self.json(404,{'error':'not_found'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=65536:raise ValueError('invalid_body')
            request=json.loads(self.rfile.read(length));actor=request['actor'];authorize(actor,self.server.source_tenant)
            parsed=target(request['target']);snapshot=current(self.server.root);manifest,_=snapshot
            data_time=dt.datetime.fromisoformat(manifest['generated_at']).replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
            age=max(0,(dt.datetime.now(dt.timezone.utc)-data_time).total_seconds())
            stale=age>original.CACHE_TTL_SECONDS+300
            zipped=parsed.path=='/api/data'
            if zipped:
                key=cache_key(actor,request['target'],manifest['version']);file=self.server.views/(key+'.gz')
                def create():
                    if not self.server.compute.acquire(timeout=5):raise RuntimeError('campaign_compute_busy')
                    try:compressed_view(self.server.root,parsed.query,file,snapshot=snapshot)
                    finally:self.server.compute.release()
                    return Response(200,{},str(file).encode())
                cached,failed,hit=self.server.cache.get(key,create,wait=180);file=Path(cached.body.decode());stale=stale or failed
                # A cached artifact may be reused only for this actor/version.
                etag='W/"'+key+'"';os.utime(file,None)
            else:file=original.INDEX_PATH;etag='W/"'+hashlib.sha256(file.read_bytes()).hexdigest()+'"';hit=True
            with file.open('rb') as stream:
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8' if zipped else 'text/html; charset=utf-8')
                self.send_header('Content-Length',str(os.fstat(stream.fileno()).st_size))
                if zipped:self.send_header('Content-Encoding','gzip')
                for key,value in {'ETag':etag,'X-AI-Data-Time':data_time.isoformat(),'X-AI-Data-Version':manifest['version'],
                                  'X-AI-Stale':str(stale).lower(),'X-Report-Cache':'stale' if stale else 'hit' if hit else 'miss',
                                  'X-Report-Cache-Age':str(int(age)),'Cache-Control':'no-store','Connection':'close'}.items():self.send_header(key,value)
                self.end_headers()
                for block in iter(lambda:stream.read(65536),b''):self.wfile.write(block)
            self.close_connection=True
            files=sorted(self.server.views.glob('*.gz'),key=lambda p:p.stat().st_mtime,reverse=True)
            for old in files[16:]:
                if old!=file:old.unlink(missing_ok=True)
        except PermissionError:self.json(403,{'error':'permission_denied'})
        except (ValueError,KeyError):self.json(400,{'error':'invalid_request'})
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
        except Exception:self.json(503,{'error':'campaign_snapshot_unavailable'})


def main():
    token=os.environ['OFFLOAD_INTERNAL_TOKEN'];tenant=os.environ['OFFLOAD_SOURCE_TENANT']
    if len(token)<32 or not tenant:raise ValueError('private_reader_configuration_required')
    server=Server(('127.0.0.1',int(os.environ.get('CAMPAIGN_READER_PORT','19972'))),os.environ['CAMPAIGN_SNAPSHOT_ROOT'],token,tenant)
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
