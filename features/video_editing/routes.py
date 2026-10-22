"""Explicitly dispatched main API routes, with module and object permissions."""
import logging
import re
from urllib.parse import parse_qs,urlsplit

from . import VideoEditingError,require
from .service import get_service
from .store import can_manage,owner
from features.drama_synthesis.core import DramaSynthesisError


def handle(handler,method,app_module):
    parsed=urlsplit(handler.path);prefix='/api/video-editing'
    if parsed.path!=prefix and not parsed.path.startswith(prefix+'/'):return False
    if not handler._require_cookie_module('video_editing'):return True
    if method not in {'GET','POST','PATCH','DELETE'}:
        app_module.json_response(handler,405,{'code':'method_not_allowed','error':'请求方法不支持'});return True
    if method!='GET' and not handler._require_same_origin_json():return True
    session=handler._session() or {}
    actor={'user_id':str(session.get('user_id','')),'tenant_key':str(session.get('tenant_key','')),
           'name':str(session.get('name','')),'is_admin':session.get('role')=='admin'}
    try:
        owner(actor)
        require(0<=int(handler.headers.get('Content-Length','0') or 0)<=2*1024*1024,'request_too_large','剪辑配置大小无效',413)
        body=handler._read_json() if method!='GET' else {}
        require(isinstance(body,dict),'json_object_required','请求须为 JSON 对象')
        service=get_service(app_module);path=parsed.path[len(prefix):];query=parse_qs(parsed.query)
        q=lambda k,default='':query.get(k,[default])[0]
        answer=None
        if method=='GET' and path=='/config':answer={'item':service.config()}
        elif method=='GET' and path=='/assets':answer=service.assets(actor,q('q'),q('status'),int(q('limit','50')),int(q('offset','0')))
        elif method=='GET' and path=='/uploads':
            rows,total=service.store.list('upload',"owner=? AND status NOT IN ('completed','aborted')",(owner(actor),),100)
            answer={'items':[service.uploads.dto(r) for r in rows],'total':total}
        elif method=='POST' and path=='/uploads':answer={'item':service.uploads.dto(service.uploads.create(body,actor,allow_create=service.intake_enabled))}
        elif re.fullmatch(r'/uploads/vu_[0-9a-f]{32}(?:/(?:parts/[1-9][0-9]{0,4}/sign|complete|reconcile|abort))?',path):
            parts=path.strip('/').split('/');uid=parts[1]
            if method=='GET' and len(parts)==2:answer={'item':service.uploads.dto(service.uploads.get(uid,actor))}
            elif method=='POST' and len(parts)==5 and parts[2]=='parts':answer={'item':service.uploads.sign(uid,int(parts[3]),body,actor)}
            elif method=='POST' and len(parts)==3 and parts[2] in {'complete','reconcile'}:answer={'item':service.complete_upload(uid,actor,reconcile=parts[2]=='reconcile')}
            elif method=='POST' and len(parts)==3 and parts[2]=='abort':answer={'item':service.uploads.dto(service.uploads.abort(uid,actor))}
        elif re.fullmatch(r'/assets/va_[0-9a-f]{32}',path) and method in {'PATCH','DELETE'}:
            answer={'item':service.mutate_asset(path.split('/')[-1],actor,name=body.get('name'),delete=method=='DELETE')}
        elif re.fullmatch(r'/sources/(?:va_|vep_)[0-9a-f]{32}',path) and method=='GET':answer={'item':service.source_dto(service.source(path.split('/')[-1],actor),actor)}
        elif path=='/catalog/products' and method=='GET':answer={'items':service.catalog.products()}
        elif path=='/catalog/dramas' and method=='GET':answer={'items':service.catalog.dramas(q('app_id'),q('q'))}
        elif path=='/catalog/episodes' and method=='GET':answer={'items':[service.source_dto(r,actor) for r in service.catalog.episodes(q('app_id'),q('content_id'),actor)]}
        elif path=='/catalog/probe' and method=='POST':
            ids=body.get('source_ids');require(isinstance(ids,list) and 1<=len(ids)<=100 and len(ids)==len(set(ids)),'source_ids_invalid','请选择1至100个不同剧集')
            answer={'items':[service.source_dto(service.enqueue_probe(str(sid),actor),actor) for sid in ids]}
        elif path=='/projects' and method=='GET':
            where,args=('1=1',()) if actor['is_admin'] else ('owner=?',(owner(actor),))
            rows,total=service.store.list('project',where,args,100)
            answer={'items':[service.project_dto(r) for r in rows],'total':total}
        elif path=='/projects' and method=='POST':answer={'item':service.save_project(body,actor)}
        elif re.fullmatch(r'/projects/vp_[0-9a-f]{32}(?:/render)?',path):
            pid=path.split('/')[2]
            if method=='GET' and not path.endswith('/render'):answer={'item':service.project_dto(service.project(pid,actor))}
            elif method=='PATCH' and not path.endswith('/render'):answer={'item':service.save_project(body,actor,pid)}
            elif method=='POST' and path.endswith('/render'):answer=service.render(pid,body,actor)
        elif path=='/tasks' and method=='GET':
            where,args=("kind='render'",()) if actor['is_admin'] else ("kind='render' AND owner=?",(owner(actor),))
            rows,total=service.store.list('task',where,args,100)
            answer={'items':[service.task_dto(r) for r in rows],'total':total}
        elif re.fullmatch(r'/tasks/ve_[0-9a-f]{32}(?:/resume)?',path):
            tid=path.split('/')[2]
            if method=='GET' and not path.endswith('/resume'):answer={'item':service.task_dto(service.task(tid,actor))}
            elif method=='POST' and path.endswith('/resume'):answer={'item':service.resume(tid,actor)}
        require(answer is not None,'not_found','接口不存在',404)
        app_module.json_response(handler,200,answer,no_store=True)
    except VideoEditingError as exc:
        app_module.json_response(handler,exc.status,{'code':exc.code,'error':exc.message},no_store=True)
    except DramaSynthesisError as exc:
        app_module.json_response(handler,exc.status,{'code':exc.code,'error':str(exc)},no_store=True)
    except (TypeError,ValueError,KeyError):
        app_module.json_response(handler,400,{'code':'request_invalid','error':'请求参数无效'},no_store=True)
    except Exception:
        logging.exception('video editing request failed: %s %s',method,parsed.path)
        app_module.json_response(handler,503,{'code':'video_editing_unavailable','error':'剪辑服务暂时不可用，请稍后重试'},no_store=True)
    return True
