"""Loopback-only identity and fixed catalog operations in the main API."""
import hmac
import sqlite3
from contextlib import closing
from .errors import PublishingError

def resolve_identity(app,tenant,user,*,publishing=True):
    if any(not isinstance(x,str) or not x.strip() or len(x)>200 or any(ord(c)<32 for c in x) for x in (tenant,user)):
        raise PublishingError('invalid_identity','发布人身份无效',400)
    try:
        with closing(sqlite3.connect('file:'+str(app['JOB_DB_PATH'])+'?mode=ro',uri=True,timeout=5)) as conn:
            conn.row_factory=sqlite3.Row
            rows=conn.execute('SELECT tenant_key,user_id,name,role,status,permissions_json FROM drama_admin_user '
                              'WHERE tenant_key=? AND user_id=? LIMIT 2',(tenant,user)).fetchall()
        if len(rows)!=1 or rows[0]['status']!='active' or rows[0]['role'] not in {'user','admin'}:
            raise PublishingError('identity_inactive','发布人账号已停用，请重新确认发布权限',403)
        current=dict(rows[0])
        current['permissions']=app['normalize_user_permissions'](current.pop('permissions_json'),current['role'])
        if not app['has_module_permission'](current,'x_video_publish'):
            raise PublishingError('identity_permission_denied','尚未开通 X 个人视频发布权限',403)
        nav=app['load_navigation_config']()
        keys=('xVideoPublish',) if publishing else ('xVideoPublish','xVideoPublishRecords')
        if not any(app['navigation_item_access'](current,k,nav).get('allowed') for k in keys):
            raise PublishingError('identity_permission_denied','X 发布入口不可用',403)
        return {k:str(current[k]) for k in ('tenant_key','user_id','name','role')}
    except PublishingError:
        raise
    except Exception:
        raise PublishingError('identity_unavailable','发布人身份验证暂不可用',503) from None

def dispatch(handler,app,kind):
    send=lambda status,data:app['json_response'](handler,status,data,no_store=True)
    from features.x_accounts import client
    headers=handler.headers
    forwarded=any(k.lower()=='forwarded' or k.lower().startswith('x-forwarded-') or k.lower()=='x-real-ip' for k in headers.keys())
    if (handler.command!='POST' or handler.client_address[0] not in {'127.0.0.1','::1'} or forwarded
            or len(client._INTERNAL_TOKEN)<32 or len(headers.get_all('Authorization',[]))!=1
            or not hmac.compare_digest(headers.get('Authorization',''),'Bearer '+client._INTERNAL_TOKEN)):
        handler.close_connection=True
        return send(403,{'code':'forbidden','message':'Internal route','accepted':False})
    try:
        from .gateway import read_json
        payload=read_json(handler,internal=True,limit=16384)
        if kind=='identity':
            if set(payload)!={'tenant_key','user_id'}:
                raise PublishingError('invalid_request','身份请求字段无效',400)
            return send(200,{'actor':resolve_identity(app,payload['tenant_key'],payload['user_id'])})
        from .sources import for_main
        source=for_main(app)
        if set(payload)=={'action','material_ids'} and payload['action']=='resolve':
            result=source.resolve(payload['material_ids'])
        elif set(payload)=={'action','query'} and payload['action']=='list' and isinstance(payload['query'],dict):
            result=source.listing(payload['query'])
        else:
            raise PublishingError('invalid_request','素材请求字段无效',400)
        send(200,result)
    except PublishingError as exc:
        handler.close_connection=True
        send(exc.status,{'code':exc.code,'message':str(exc),'accepted':False})
    except Exception:
        handler.close_connection=True
        send(503,{'code':'service_unavailable','message':'内部查询暂不可用','accepted':False})
