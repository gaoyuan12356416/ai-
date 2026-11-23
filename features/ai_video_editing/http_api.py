"""Authenticated, independent AI editing HTTP API and private worker transport."""
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .runtime import Fault, MAX_BYTES, PREFIX, Runtime, check, public_asset
from .smart_common import SmartError

# Only the common Feishu session and module permission are provided by the host.
# Its business routes never dispatch here.
sys.path.append(os.environ.get('AI_PLATFORM_PATH','/root/drama_material_service'))
import app as platform


@contextmanager
def spool(stream,count,directory):
    with tempfile.NamedTemporaryFile(dir=directory,delete=False) as f:
        path=Path(f.name)
    try:
        digest=hashlib.sha256()
        with path.open('wb') as out:
            left=count
            while left:
                chunk=stream.read(min(left,1024**2))
                check(chunk,'incomplete_upload','上传未完成，请重新选择文件')
                out.write(chunk)
                digest.update(chunk)
                left-=len(chunk)
        yield path,digest.hexdigest()
    finally:
        path.unlink(missing_ok=True)


def byte_interval(header,size):
    if not header:return 0,size-1
    m=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
    check(m and any(m.groups()),'invalid_range','无效的读取范围',416)
    left,right=m.groups()
    start=int(left) if left else max(0,size-int(right))
    end=min(int(right),size-1) if left and right else size-1
    check(0<=start<=end<size,'invalid_range','读取范围超出文件',416)
    return start,end


class Handler(platform.DramaMaterialHandler):
    @property
    def runtime(self):
        return self.server.runtime

    def reply(self,status,obj):
        raw=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','private, no-store')
        self.send_header('Server-Timing','app;dur=%.1f'%((time.monotonic()-getattr(self,'_request_start',time.monotonic()))*1000))
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)

    def conditional(self,obj,etag):
        self.send_response(304 if self.headers.get('If-None-Match')==etag else 200)
        self.send_header('ETag',etag)
        self.send_header('Cache-Control','private, no-cache')
        if self.headers.get('If-None-Match')==etag:
            self.end_headers();return
        raw=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)

    def length(self,limit):
        raw=self.headers.get('Content-Length','')
        check(raw.isdecimal() and 0<int(raw)<=limit,'length_invalid','请上传有效大小的文件或配置',413)
        return int(raw)

    def payload(self):
        count=self.length(2*1024**2)
        try: value=json.loads(self.rfile.read(count))
        except (ValueError,UnicodeError): raise Fault('invalid_json','JSON 格式无效') from None
        check(isinstance(value,dict),'invalid_json','请求应为 JSON 对象')
        return value

    def stream(self,path,name,digest='',download=False,mime='video/mp4'):
        path=Path(path)
        check(path.is_file(),'media_missing','文件暂不可读取',404)
        size=path.stat().st_size
        try: start,end=byte_interval(self.headers.get('Range'),size)
        except Fault:
            self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
        partial=bool(self.headers.get('Range'))
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',mime)
        self.send_header('Cache-Control','private, no-store')
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('X-Content-Type-Options','nosniff')
        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if digest:self.send_header('X-Source-Sha256',digest)
        if download:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(name,safe=''))
        self.end_headers()
        if self.command=='HEAD':return
        with path.open('rb') as f:
            f.seek(start)
            left=end-start+1
            while left:
                chunk=f.read(min(512*1024,left))
                if not chunk: break
                self.wfile.write(chunk)
                left-=len(chunk)

    def internal(self,path):
        expected=self.server.worker_token
        check(expected and hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+expected),
              'unauthorized','需要制作节点身份',401)
        output = re.fullmatch(r'/internal/tasks/(at_[a-f0-9]{32})/(complete-object|output-location|output-source|attach-output)', path)
        if output:
            from . import output_storage
            tid, action = output.groups()
            if action in ('complete-object', 'attach-output') and self.command == 'POST':
                return self.reply(200, output_storage.complete(self.runtime, tid,
                    self.headers.get('X-Edit-Lease', ''), self.payload(), migrate=action=='attach-output'))
            check(self.command in ('GET','HEAD') and action in ('output-location','output-source'),
                  'method_invalid','请求方式无效',405)
            meta = output_storage.record(self.runtime, tid)
            if action == 'output-location':
                return self.reply(200, output_storage.location(self.runtime, tid) or
                    {'url':None,'sha256':meta['sha256'],'size':output_storage.file_size(self.runtime,tid)})
            return self.stream(output_storage.local_file(self.runtime, tid), tid+'.mp4', meta['sha256'])
        if path == '/internal/uploads/claim' and self.command == 'POST':
            self.payload()
            return self.reply(200, {'item': self.runtime.cloud.claim()})
        upload = re.fullmatch(r'/internal/uploads/(au_[a-f0-9]{32})/(heartbeat|verified|failed)', path)
        if upload and self.command == 'POST':
            return self.reply(200, self.runtime.cloud.worker_update(upload[1],
                self.headers.get('X-Edit-Lease', ''), upload[2], self.payload()))
        source_location = re.fullmatch(r'/internal/tasks/(at_[a-f0-9]{32})/sources/(aa_[a-f0-9]{32})/location', path)
        if source_location and self.command == 'GET':
            item = self.runtime.worker_asset(source_location[1], self.headers.get('X-Edit-Lease', ''), source_location[2])
            return self.reply(200, self.runtime.cloud.location(item) if item.get('_cos') else {'url': None})
        if path=='/internal/claim' and self.command=='POST':
            self.payload()
            return self.reply(200,{'item':self.runtime.claim()})
        sound=re.fullmatch(r'/internal/tasks/(at_[0-9a-f]{32})/audio/(ag_[0-9a-f]{32})',path)
        if sound and self.command=='GET':
            from .composition import audio
            with self.runtime.db() as db:
                job=self.runtime.leased(db,sound[1],self.headers.get('X-Edit-Lease',''))
                check(json.loads(job['unit_data']).get('composition',{}).get('speech_id')==sound[2], 'not_found','音轨不属于此任务',404)
            file,digest=audio(self.runtime,sound[2],job['owner'])
            return self.stream(file,'voice.wav',digest,mime='audio/wav')
        m=re.fullmatch(r'/internal/tasks/(at_[0-9a-f]{32})/(sources/(aa_[0-9a-f]{32})|heartbeat|complete|fail|receipt)',path)
        check(m,'not_found','接口不存在',404)
        tid,action,sid=m.groups()
        lease=self.headers.get('X-Edit-Lease','')
        check(re.fullmatch(r'[0-9a-f]{32}',lease),'lease_invalid','缺少有效的制作租约',409)
        if sid and self.command in ('GET','HEAD'):
            item=self.runtime.worker_asset(tid,lease,sid)
            if item.get('_cos'):
                from .cloud_media import materialize
                materialize(self.runtime, item)
            return self.stream(item['path'],sid+'.mp4',item['sha256'])
        if action=='receipt' and self.command=='GET':
            digest=self.headers.get('X-Output-Sha256','').lower()
            check(re.fullmatch('[0-9a-f]{64}',digest),'digest_missing','缺少成片摘要')
            done=self.runtime.completion_receipt(tid,lease,digest)
            if not done:
                with self.runtime.db() as db:self.runtime.leased(db,tid,lease)
            return self.reply(200,{'completed':done})
        check(not sid and self.command=='POST','method_invalid','请求方式无效',405)
        if action=='complete':
            digest=self.headers.get('X-Output-Sha256','').lower()
            check(re.fullmatch('[0-9a-f]{64}',digest),'digest_missing','缺少成片摘要')
            if self.runtime.completion_receipt(tid,lease,digest):return self.reply(200,{'ok':True})
            with self.runtime.db() as db:
                task = self.runtime.leased(db,tid,lease)
                limit = 8 * MAX_BYTES if json.loads(task['unit_data']).get('youtube_regular') else MAX_BYTES
            with spool(self.rfile,self.length(limit),self.runtime.root/'temp') as (file_path,actual):
                check(actual==digest,'digest_mismatch','成片传输校验失败',422)
                return self.reply(200,self.runtime.update_job(tid,lease,action,file_path,digest))
        body = self.payload()
        self.reply(200,self.runtime.update_job(tid,lease,action,reason=body.get('reason', '')))

    def user_route(self,path,params,actor):
        rt=self.runtime
        verb=self.command
        def q(name,default=''):return params.get(name,[default])[0]
        if path=='/assets' and verb=='POST':
            ref=self.headers.get('Origin') or self.headers.get('Referer','')
            if ref:
                url=urlsplit(ref)
                check(url.netloc.lower()==self.headers.get('Host','').lower() and url.scheme==self.headers.get('X-Forwarded-Proto','https'),
                      'origin_invalid','请求来源无效',403)
            mime=self.headers.get('Content-Type','').split(';')[0].lower()
            check(mime in ('application/octet-stream','video/mp4','video/quicktime','video/x-m4v'),'unsupported_media','请上传 MP4 或 MOV 格式',415)
            name=q('name').strip()
            series=q('series').strip()
            check(0<len(name)<=240 and len(series)<=180,'name_invalid','素材名称或剧名过长')
            episode=int(q('episode') or 0)
            check(0<=episode<=100000,'episode_invalid','集号无效')
            with spool(self.rfile,self.length(MAX_BYTES),rt.root/'temp') as (file_path,digest):
                return self.reply(201,{'item':public_asset(rt.import_asset(file_path,digest,name,series,actor,episode))})
        body={}
        if verb in ('POST','PATCH'):
            if not self._require_same_origin_json():return
            body=self.payload()
        if path.startswith('/youtube-material/'):
            from . import youtube_material
            status, data = youtube_material.dispatch(rt, actor, verb, path, body, params)
            return self.reply(status, data)
        regular_task = re.fullmatch(r'/youtube-regular/tasks/(at_[0-9a-f]{32})/timeline', path)
        if regular_task and verb in ('GET', 'POST'):
            from . import youtube_regular
            if verb == 'GET':
                return self.reply(200, youtube_regular.timeline(rt, actor, regular_task[1]))
            return self.reply(200, youtube_regular.refine(rt, actor, regular_task[1], body))
        if path == '/uploads' or path.startswith('/uploads/'):
            return self.reply(200, rt.cloud.dispatch(actor, verb, path, body))
        if path.startswith('/catalog/'):
            from .catalog_imports import dispatch
            return self.reply(200, dispatch(rt, self.server.catalog, actor, verb, path, params, body))
        if path.startswith('/studio/'):
            from .studio import dispatch
            response=dispatch(rt,actor,verb,path[len('/studio'):],body,params)
            if path.endswith('/source-progress') and response.get('selection'):
                return self.conditional(response,response['selection']['etag'])
            if 'file' in response:
                return self.stream(response['file'],response['name'],download=True,mime='application/zip')
            return self.reply(200,response)
        if path=='/assets' and verb=='GET':
            return self.reply(200,rt.assets(actor,q('q'),max(1,min(int(q('limit','50')),200)),max(0,int(q('offset','0'))),q('series')))
        if path=='/series' and verb=='GET':return self.reply(200,rt.series(actor))
        derivative=re.fullmatch('/assets/(aa_[0-9a-f]{32})/(thumbnail|preview)',path)
        if derivative:
            from . import previews
            aid,kind=derivative.groups()
            if verb=='POST' and kind=='preview':
                return self.reply(202,previews.request(rt,actor,aid,priority=100,retry=body.get('retry') is True))
            if verb in ('GET','HEAD'):
                target,mime,etag=previews.file(rt,actor,aid,kind)
                etag='"'+etag+'"'
                if self.headers.get('If-None-Match')==etag:
                    self.send_response(304);self.send_header('ETag',etag);self.end_headers();return
                if os.environ.get('AI_VIDEO_EDITING_PREVIEW_ACCEL','1')=='1':
                    self.send_response(200)
                    self.send_header('X-Accel-Redirect','/_private_ai_editor_preview/'+target.name)
                    self.send_header('Content-Type',mime)
                    self.send_header('ETag',etag)
                    self.send_header('Cache-Control','private, no-cache')
                    self.end_headers();return
                return self.stream(target,target.name,mime=mime)
        m=re.fullmatch('/assets/(aa_[0-9a-f]{32})(/media)?',path)
        if m and verb in ('GET','HEAD'):
            item=rt.asset(m[1],actor)
            if m[2] and item.get('_cos'):
                self.send_response(307)
                self.send_header('Location', rt.cloud.location(item, download=q('download')=='1')['url'])
                self.send_header('Cache-Control', 'private, no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if m[2]:return self.stream(item['path'],item['name'],download=q('download')=='1')
            return self.reply(200,{'item':public_asset(item)})
        if path.startswith('/analyses') or path=='/plans':
            from .smart_api import dispatch
            status,answer=dispatch(rt,actor,verb,path,body)
            return self.reply(status,answer)
        if path=='/questions' and verb=='POST':
            from .questions import answer
            return self.reply(200,answer(rt,actor,body))
        if path in ('/projects','/tasks') and verb=='GET':return self.reply(200,rt.list_records(path[1:],actor))
        if path=='/projects' and verb=='POST':return self.reply(200,rt.save(actor,body))
        m=re.fullmatch('/projects/(ap_[0-9a-f]{32})(/render)?',path)
        if m:
            if m[2] and verb=='POST':return self.reply(200,rt.render(m[1],actor,body))
            if not m[2] and verb=='GET':return self.reply(200,rt.list_records('projects',actor,m[1]))
            if not m[2] and verb=='PATCH':return self.reply(200,rt.save(actor,body,m[1]))
        m=re.fullmatch('/tasks/(at_[0-9a-f]{32})(/media|/retry)?',path)
        if m:
            if m[2]=='/retry' and verb=='POST':return self.reply(200,rt.retry(actor,m[1]))
            if verb in ('GET','HEAD') and m[2]!='/retry':
                rec=rt.list_records('tasks',actor,m[1])
                if not m[2]:return self.reply(200,rec)
                check(rec['item']['status']=='completed','not_ready','成片尚未就绪',409)
                from . import output_storage
                loc = output_storage.location(rt, m[1], rec['item']['name']+'.mp4', q('download')=='1',
                                              self.command, preview=q('quality')!='original')
                if loc:
                    self.send_response(307)
                    self.send_header('Location',loc['url'])
                    self.send_header('Cache-Control','private, no-store')
                    self.send_header('Referrer-Policy','no-referrer')
                    self.send_header('Content-Length','0')
                    self.end_headers()
                    return
                return self.stream(rt.root/'outputs'/(m[1]+'.mp4'),rec['item']['name']+'.mp4',download=q('download')=='1')
        if path.startswith('/language/'):
            from . import language_tools as lang
            from . import transcript_jobs
            transcript_job = re.fullmatch(r'/language/transcripts/(tr_[a-f0-9]{32})', path)
            if transcript_job and verb == 'GET':
                return self.reply(200, transcript_jobs.receipt(rt, actor, transcript_job[1]))
            if path=='/language/capabilities' and verb=='GET':return self.reply(200,lang.capabilities())
            if path=='/language/generate' and verb=='POST':
                if body.get('mode') == 'transcribe' and body.get('async') is True:
                    return self.reply(202, transcript_jobs.submit(rt, actor, body))
                task=body.get('task_id')
                if task:
                    rec=rt.list_records('tasks',actor,task)['item']
                    check(rec['status']=='completed','not_ready','请先完成制作',409)
                    from .output_storage import local_file
                    return self.reply(200,lang.generate(body,media_path=str(local_file(rt,rec['id']))))
                return self.reply(200,lang.generate(body))
            if path=='/language/speech' and verb=='POST':return self.reply(200,lang.synthesize(body))
        raise Fault('not_found','接口不存在',404)

    def dispatch(self):
        self._request_start=time.monotonic()
        try:
            parsed=urlsplit(self.path)
            check(parsed.path.startswith(PREFIX+'/'),'not_found','接口不存在',404)
            path=parsed.path[len(PREFIX):]
            if path.startswith('/internal/'):return self.internal(path)
            if not self._require_cookie_module('video_editing'):return
            session=self._session() or {}
            actor={k:session.get(k) for k in ('tenant_key','user_id','name')}
            if path.startswith('/youtube-material/'):
                actor['email'] = session.get('email')
            return self.user_route(path,parse_qs(parsed.query),actor)
        except (Fault,SmartError) as exc:self.reply(exc.status,{'code':exc.code,'error':exc.message})
        except (ConnectionError,BrokenPipeError):return
        except ValueError as exc:
            self.reply(getattr(exc,'status',400),{'code':getattr(exc,'code','invalid_request'),'error':getattr(exc,'message','请检查输入参数')})
        except Exception:
            logging.exception('Independent editing request failed at %s',urlsplit(self.path).path)
            self.reply(503,{'code':'service_unavailable','error':'处理失败，请稍后重试'})

    do_GET=do_HEAD=do_POST=do_PATCH=dispatch
    def reject(self):self.reply(405,{'error':'method_not_allowed'})
    do_DELETE=do_PUT=do_OPTIONS=reject


def main():
    rt=Runtime(os.getenv('AI_VIDEO_EDITING_ROOT','/mnt/data-disk/ai-video-editing'))
    from .cloud_uploads import CloudUploads
    rt.cloud = CloudUploads(rt, platform.get_cos_client(timeout=20, retry=0), platform.COS_BUCKET, platform.COS_REGION)
    from .studio import start
    start(rt)
    from .selections import start as start_selections
    start_selections(rt)
    from .transcript_jobs import start as start_transcripts
    start_transcripts(rt)
    from .catalog import Catalog
    from .catalog_imports import start as start_imports
    catalog = Catalog(platform, browse_cache_path=rt.root/'catalog-browse.sqlite3')
    start_imports(rt, catalog)
    from . import youtube_material
    youtube_material.start(rt, catalog, platform)
    with ThreadingHTTPServer(('127.0.0.1',18983),Handler) as http:
        http.runtime=rt
        http.catalog=catalog
        http.worker_token=Path('/etc/ai-video-editing/worker.token').read_text().strip()
        http.serve_forever()


if __name__=='__main__':main()
