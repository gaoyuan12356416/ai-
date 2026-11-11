"""Cookie Page-list authorization on every request; no SQL in HTTP handlers."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit,parse_qs
from .storage import Store,ROOT,VERSION_RE
from .runtime import encoded

PREFIX='/api/fb-page-portraits'
LIMIT=threading.BoundedSemaphore(8)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def authenticated_get(path,cookie):
    request=urllib.request.Request('http://127.0.0.1:8787'+path,headers={'Cookie':cookie,'Accept':'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
        raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('authorization_response_oversized')
        return json.loads(raw)


def admin_scope(user,ids,*,config_db='/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3',
                user_db='/root/drama_material_service/data/drama_material_jobs.sqlite3'):
    """Admins have no source-owner predicate. Never trust creator as an ACL.

    The current active admin and current tenant configuration are re-read for
    every request. Normal users retain the existing source-owner Page gate.
    """
    if not user.get('tenant_key') or not user.get('user_id'):return None
    with closing(sqlite3.connect('file:'+str(user_db)+'?mode=ro',uri=True,timeout=3)) as db:
        rows=db.execute('SELECT role,status FROM drama_admin_user WHERE tenant_key=? AND user_id=? LIMIT 2',
                        (user['tenant_key'],user['user_id'])).fetchall()
    if rows!=[('admin','active')]:return None
    with closing(sqlite3.connect('file:'+str(config_db)+'?mode=ro',uri=True,timeout=3)) as db:
        return {r[0] for r in db.execute('SELECT page_id FROM fb_video_page WHERE tenant_key=? AND deleted=0 AND page_id IN ('+','.join('?' for _ in ids)+')',[user['tenant_key'],*ids])}


def authorize(cookie,ids):
    if not cookie:return 401,set()
    try:
        auth=authenticated_get('/api/ui/topbar',cookie)
        user=auth.get('user') or {}
        if auth.get('authenticated') and user.get('is_admin') is True:
            # This cheap existing endpoint enforces the identical Cookie,
            # module and navigation gates as the Page list, without source SQL.
            gate=authenticated_get('/api/fb-video-publish/languages',cookie)
            if gate.get('ok') is False or not isinstance(gate.get('items'),list):raise ValueError('invalid_navigation_gate')
            allowed=admin_scope(user,ids)
            if allowed is not None:return 200,allowed
        data=authenticated_get('/api/fb-video-publish/pages',cookie)
        return 200,{str(r['page_id']) for r in data['items']}
    except urllib.error.HTTPError as exc:return exc.code,set()


def select(path,query,allowed,store):
    match=re.fullmatch(re.escape(PREFIX)+r'(?:/([1-9][0-9]{0,30})(/dramas)?)?',path)
    if not match:return 404,{'message':'接口不存在'}
    pid,dramas=match.groups()
    if any(len(v)!=1 for v in query.values()):raise ValueError('duplicate_parameter')
    if pid:
        if pid not in allowed:return 404,{'message':'Page 不存在或无权查看'}
        if dramas:
            if set(query)!={'path','version','page'}:raise ValueError('invalid_detail_parameters')
            version=query['version'][0];node=query['path'][0];page=int(query['page'][0])
            if not re.fullmatch(VERSION_RE,version) or not re.fullmatch(r'[a-z_]+/[a-z_]+',node) or not 1<=page<=10000:raise ValueError('invalid_detail_parameters')
            return 200,store.dramas(pid,node,version,page)
        if set(query)-{'version'}:raise ValueError('invalid_tree_parameters')
        result=store.load([pid],detail=True,version=query.get('version',[None])[0])
    else:
        if set(query)!={'ids'}:raise ValueError('ids_required')
        ids=query['ids'][0].split(',')
        if not 1<=len(ids)<=20 or len(set(ids))!=len(ids) or any(not re.fullmatch(r'[1-9][0-9]{0,30}',i) for i in ids):raise ValueError('invalid_page_ids')
        # Fail closed, including mixed authorized/unauthorized batches.
        if not set(ids).issubset(allowed):return 404,{'message':'Page 不存在或无权查看'}
        result=store.load(ids)
    meta=result['meta']
    meta['stale']=(datetime.now(timezone.utc)-datetime.fromisoformat(meta['generated_at'])).total_seconds()>10800
    return 200,result


class Handler(BaseHTTPRequestHandler):
    store=Store()
    def log_message(self,*args):pass
    def send(self,status,payload):
        body=encoded(payload).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if self.path=='/health':return self.send(200,{'ok':True})
        if len(self.path)>4096:return self.send(414,{'message':'请求过长'})
        if not LIMIT.acquire(blocking=False):return self.send(503,{'message':'画像请求较多，请稍后重试'})
        try:
            parsed=urlsplit(self.path)
            query=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=5)
            match=re.fullmatch(re.escape(PREFIX)+r'(?:/([1-9][0-9]{0,30})(?:/dramas)?)?',parsed.path)
            if not match:return self.send(404,{'message':'接口不存在'})
            ids=[match[1]] if match[1] else query.get('ids',[''])[0].split(',')
            if not 1<=len(ids)<=20 or any(not re.fullmatch(r'[1-9][0-9]{0,30}',i) for i in ids):raise ValueError('invalid_page_ids')
            status,allowed=authorize(self.headers.get('Cookie',''),ids)
            if status!=200:return self.send(status if status in (401,403) else 503,{'message':'请登录并使用已获授权的 Page 管理'})
            status,result=select(parsed.path,query,allowed,self.store)
            self.send(status,result)
        except FileNotFoundError:self.send(409,{'message':'画像正在刷新，请稍后重新打开'})
        except ValueError:self.send(400,{'message':'画像请求参数无效'})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.send(503,{'message':'画像暂不可用，Page 管理其他功能可正常使用'})
        finally:LIMIT.release()


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18852);p.add_argument('--root',default=ROOT);a=p.parse_args()
    Handler.store=Store(a.root)
    ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()


if __name__=='__main__':main()
