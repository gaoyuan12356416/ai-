"""Cookie and navigation boundary; the browser never supplies an actor."""
import json
import re
from urllib.parse import parse_qs, urlsplit
from .errors import PublishingError
from .transport import MAX_BYTES, sidecar

PREFIX='/api/x-video-publish'
UNTRUSTED={'actor','_actor','owner','tenant_key','user_id','owner_user_id','owner_tenant_key','role','is_admin','credentials','token','x_platform_access','_pool_subject'}

def route(method,path):
    suffix=path[len(PREFIX):] if path.startswith(PREFIX+'/') else ''
    valid = ((method=='GET' and (suffix in {'/accounts','/languages','/materials','/draft','/batches'}
              or re.fullmatch(r'/(?:batches|operations)/[A-Za-z0-9._:-]{1,160}',suffix)
              or re.fullmatch(r'/accounts/[1-9][0-9]{0,18}/pool(?:/operations/[A-Za-z0-9._:-]{1,160})?',suffix)))
             or (method=='POST' and (suffix in {'/draft','/preflight','/excel/preview','/batches'}
              or re.fullmatch(r'/accounts/[1-9][0-9]{0,18}/(?:settings|pool/(?:settings|enable|pause|remove|move|items(?:/preview)?))',suffix))))
    if not valid:
        raise PublishingError('not_found','接口不存在',404)
    return suffix

def read_json(handler, *, internal=False, limit=MAX_BYTES):
    lengths=handler.headers.get_all('Content-Length',[])
    if (handler.headers.get('Transfer-Encoding') is not None or len(lengths)!=1
            or not re.fullmatch(r'[0-9]{1,9}',lengths[0]) or not 2<=int(lengths[0])<=limit):
        raise PublishingError('invalid_request_length','请求长度无效或超过限制',413)
    if handler.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
        raise PublishingError('json_required','请求必须为 JSON',415)
    if not internal:
        try:
            origin=urlsplit(handler.headers.get('Origin') or handler.headers.get('Referer') or '')
            if (origin.scheme not in {'http','https'} or not origin.netloc or origin.username
                    or handler.headers.get('Sec-Fetch-Site','').lower()=='cross-site'):
                raise ValueError()
        except ValueError:
            raise PublishingError('same_origin_required','请从本站页面发起操作',403) from None
        if not handler._require_same_origin_json():
            handler.close_connection=True
            return None
    try:
        raw=handler.rfile.read(int(lengths[0]))
        if len(raw)!=int(lengths[0]):
            raise ValueError()
        body=json.loads(raw.decode('utf-8'),parse_constant=lambda x: (_ for _ in ()).throw(ValueError()))
        if not isinstance(body,dict):
            raise ValueError()
    except (ValueError,TypeError,UnicodeError):
        raise PublishingError('invalid_json','请求必须为 JSON 对象',400) from None
    if not internal:
        for key in UNTRUSTED:
            body.pop(key,None)
    return body

def dispatch(handler,parsed,app):
    send=lambda status,data:app['json_response'](handler,status,data,no_store=True)
    try:
        suffix=route(handler.command,parsed.path)
        session=app['load_session'](handler._cookies().get(app['SESSION_COOKIE_NAME'],''))
        if (not session or session.get('auth_type')=='api_token' or not session.get('tenant_key') or not session.get('user_id')):
            raise PublishingError('cookie_auth_required','请先使用飞书登录',401)
        from .identity_broker import resolve_identity
        actor=resolve_identity(app,str(session['tenant_key']),str(session['user_id']),publishing=handler.command=='POST')
        if handler.command=='POST':
            payload=read_json(handler)
            if payload is None:
                return
        else:
            payload={}
        raw=parse_qs(parsed.query,keep_blank_values=True,strict_parsing=False)
        allowed_query = {'q','language','cursor','limit','state'}
        if suffix == '/materials':
            allowed_query.add('product')
        if any(len(v)!=1 for v in raw.values()) or set(raw)-allowed_query:
            raise PublishingError('invalid_query','查询参数无效',400)
        query={k:v[0] for k,v in raw.items()}
        result=sidecar(handler.command,suffix,actor,payload,query)
        send(200,result)
    except PublishingError as exc:
        handler.close_connection=True
        data={'ok':False,'code':exc.code,'error':exc.code,'message':str(exc)}
        if exc.accepted is not None:
            data['accepted']=exc.accepted
        send(exc.status,data)
    except Exception:
        handler.close_connection=True
        send(503,{'ok':False,'code':'service_unavailable','message':'发布服务暂不可用，请保留原操作并查询结果'})
