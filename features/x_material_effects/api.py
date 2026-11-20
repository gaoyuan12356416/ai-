"""Read-only report API; authentication remains owned by the main application."""
from contextlib import closing
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import hmac
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
import urllib.request
import urllib.error
from . import store
from .model import aggregate,instant,METRICS

def user(cookie):
    if not cookie or len(cookie)>16384:raise PermissionError(401)
    request=urllib.request.Request('http://127.0.0.1:8787/api/auth/status',headers={'Cookie':cookie})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=10) as response:
        data=json.loads(response.read(1024*1024))
    actor=data.get('user') or {}
    if not data.get('authenticated') or not actor.get('user_id'):raise PermissionError(401)
    if actor.get('role')!='admin' and not (actor.get('permissions') or {}).get('x_material_effects'):raise PermissionError(403)
    return actor

def query(params,actor,root=None):
    get=lambda k,default='':params.get(k,[default])[0]
    page=max(1,int(get('page','1')));size=min(100,max(1,int(get('page_size','50'))))
    sort=get('sort','views');direction=get('direction','desc')
    if sort not in (*METRICS,'post_count','account_count','last_published_at') or direction not in ('asc','desc'):raise ValueError('invalid_sort')
    meta,path=store.current(root);posts=store.rows(path,'posts')
    details=get('view')=='posts'
    if details and actor.get('role')!='admin':
        # Match the current authorizing owner; ownership may change independently of receipts.
        from .collector import readonly,ACCOUNT_DB
        with closing(readonly(ACCOUNT_DB)) as db:
            visible={str(r[0]) for r in db.execute('SELECT id FROM x_authorized_account WHERE owner_user_id=? AND owner_tenant_key=?',(actor['user_id'],actor.get('tenant_key','')))}
        posts=[p for p in posts if str(p['account_id']) in visible]
    search=get('q').strip().casefold()
    for key in ('namespace','material_id','product','language','account_id'):
        if get(key):posts=[p for p in posts if str(p.get(key,''))==get(key)]
    if search:posts=[p for p in posts if any(search in str(p.get(k,'')).casefold() for k in ('material_id','material_name','content_id','drama_name'))]
    from datetime import timedelta
    for name,is_start in [('start',True),('end',False)]:
        if get(name):
            boundary=datetime.fromisoformat(get(name)).replace(tzinfo=timezone(timedelta(hours=8)))
            if not is_start:boundary+=timedelta(days=1)
            posts=[p for p in posts if (instant(p['published_at'])>=boundary if is_start else instant(p['published_at'])<boundary)]
    items=posts if details else aggregate(posts)
    items.sort(key=lambda p:str(p.get('post_id') or p['material_id']))
    known=[p for p in items if p.get(sort) is not None];missing=[p for p in items if p.get(sort) is None]
    known.sort(key=lambda p:p[sort],reverse=direction=='desc');items=known+missing
    meta=dict(meta,stale=(datetime.now(timezone.utc)-instant(meta['generated_at'])).total_seconds()>7200,
              revenue_stale=(datetime.now(timezone.utc)-instant(meta['revenue_source_generated_at'])).total_seconds()>7200)
    summary={k:sum(p[k] for p in posts if p.get(k) is not None) for k in METRICS}
    coverage={k:sum(p.get(k) is not None for p in posts) for k in METRICS}
    return dict(ok=True,items=items[(page-1)*size:page*size],total=len(items),page=page,page_size=size,
                meta=meta,summary=summary,coverage=coverage,post_count=len(posts))

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send_json(self,status,data):
        body=store.encoded(data).encode();self.send_response(status)
        for key,value in [('Content-Type','application/json; charset=utf-8'),('Cache-Control','private, no-store'),('X-Content-Type-Options','nosniff'),('Content-Length',str(len(body)))]:self.send_header(key,value)
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=='/healthz':return self.send_json(200,dict(ok=True))
        if path.path!='/api/x-material-effects':return self.send_json(404,dict(message='未找到接口'))
        try:
            actor=user(self.headers.get('Cookie',''));return self.send_json(200,query(parse_qs(path.query),actor))
        except PermissionError as exc:return self.send_json(int(exc.args[0]),dict(message='请登录或联系管理员开通 X 素材效果权限'))
        except (ValueError,TypeError):return self.send_json(400,dict(message='查询参数无效'))
        except Exception:return self.send_json(503,dict(message='素材效果数据暂不可用，请稍后重试'))

    def do_POST(self):
        if self.path!='/internal/x-material-effects/reuse':return self.send_json(404,dict(message='未找到接口'))
        tokens=[]
        for name in ('/etc/x-tag-publisher.env','/etc/x-auto-post.secrets','/etc/x-post-automation.env'):
            path=Path(name)
            if not path.exists():continue
            for line in path.read_text().splitlines():
                if '=' in line:
                    key,value=line.split('=',1)
                    if key in ('X_AUTO_POST_INTERNAL_TOKEN','X_POST_AUTO_INTERNAL_TOKEN','X_TAG_ACCOUNT_BROKER_TOKEN'):
                        tokens.append(value.strip().strip('"').strip("'"))
        supplied=self.headers.get('Authorization','').encode()
        if not any(token and hmac.compare_digest(supplied,('Bearer '+token).encode()) for token in tokens):return self.send_json(401,dict(ok=False))
        try:
            size=int(self.headers.get('Content-Length',0))
            if not 0<size<=65536:raise ValueError()
            self.connection.settimeout(30)
            from .reuse import handle
            return self.send_json(200,handle(json.loads(self.rfile.read(size))))
        except (ValueError,KeyError,TypeError):return self.send_json(400,dict(ok=False,code='invalid_reuse_request'))
        except Exception:return self.send_json(503,dict(ok=False,code='reuse_broker_unavailable'))

if __name__=='__main__':ThreadingHTTPServer(('127.0.0.1',8899),Handler).serve_forever()
