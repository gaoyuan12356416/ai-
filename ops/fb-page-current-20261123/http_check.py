"""Exercise the actual HTTP/ACL/storage handlers using private login fixtures and read-only live data."""
from contextlib import closing,ExitStack
from functools import partial
from http.server import ThreadingHTTPServer
import json
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch
from verify import QA, Gate, Pins, Store, atomic, disk, encoded, stamp
from features.fb_page_portrait import server, page_list


def check():
    from pathlib import Path
    disk()
    pid=json.loads((QA/'pin-open.json').read_text())['pid']
    other='9999999999999'
    with tempfile.TemporaryDirectory(dir=QA) as temp:
        folder=Path(temp);auth=folder/'auth.sqlite3';config=folder/'pages.sqlite3'
        actors={k:dict(user_id=k,tenant_key=tenant,is_admin=admin) for k,tenant,admin in
                [('admin','A',True),('other','B',True),('ordinary','A',False),('blocked','A',True)]}
        with closing(sqlite3.connect(auth)) as db,db:
            db.execute('CREATE TABLE drama_admin_session(id INTEGER PRIMARY KEY,session_token TEXT,expires_at INTEGER)')
            db.execute('CREATE TABLE drama_admin_user(tenant_key TEXT,user_id TEXT,role TEXT,status TEXT)')
            for i,(token,actor) in enumerate(actors.items(),1):
                db.execute('INSERT INTO drama_admin_session VALUES(?,?,?)',(i,token,int(time.time())+900))
                db.execute('INSERT INTO drama_admin_user VALUES(?,?,?,?)',(actor['tenant_key'],token,'admin' if actor['is_admin'] else 'member','active'))
        with closing(sqlite3.connect(config)) as db,db:
            db.execute('CREATE TABLE fb_video_page('+','.join(k+' TEXT' for k in page_list.FIELDS)+',deleted INTEGER)')
            db.executemany('INSERT INTO fb_video_page(tenant_key,page_id,deleted) VALUES(?,?,0)',[('A',pid),('B',other)])
        def upstream(path,cookie):
            token=cookie.split('=',1)[-1]
            if token not in actors:raise urllib.error.HTTPError(path,401,'expired',None,None)
            if path=='/api/ui/topbar':return dict(authenticated=True,user=actors[token])
            if token=='blocked':raise urllib.error.HTTPError(path,403,'module denied',None,None)
            if path=='/api/fb-video-publish/languages':return dict(items=[])
            if path=='/api/fb-video-publish/pages':return dict(items=[dict(page_id=pid)] if token=='ordinary' else [])
            raise AssertionError('unexpected authentication route')
        class Handler(server.Handler):
            store=Store(reader=Gate(),pins=Pins(folder/'pins.sqlite3',auth))
        with ExitStack() as stack:
            stack.enter_context(patch.object(server,'authenticated_get',upstream))
            stack.enter_context(patch.object(server,'admin_scope',partial(server.admin_scope,config_db=config,user_db=auth)))
            stack.enter_context(patch.object(server,'configured_list',partial(page_list.configured_list,config_db=config,user_db=auth)))
            http=stack.enter_context(ThreadingHTTPServer(('127.0.0.1',0),Handler))
            runner=threading.Thread(target=http.serve_forever,daemon=True);runner.start()
            client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            results={};version=None
            def get(path,token):
                req=urllib.request.Request('http://127.0.0.1:'+str(http.server_port)+server.PREFIX+path,
                                           headers={'Cookie':'drama_admin_session='+token} if token else {})
                try:
                    with client.open(req,timeout=50) as response:return response.status,json.load(response)
                except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
            try:
                for label,path,token,expected in [
                    ('anonymous','?ids='+pid,'',401),('expired','?ids='+pid,'expired',401),
                    ('module','?ids='+pid,'blocked',403),('tenant','?ids='+pid,'other',404),
                    ('mixed','?ids='+pid+','+other,'admin',404),('tenant_config','/pages','admin',200),
                    ('ordinary','?ids='+pid,'ordinary',200),('summary','?ids='+pid,'admin',200),
                    ('detail','/'+pid,'admin',200)]:
                    code,body=get(path,token);results[label]=code
                    if code!=expected:raise RuntimeError('HTTP fixture mismatch '+label+' '+str(code))
                    if label=='tenant_config' and [r['page_id'] for r in body['items']]!=[pid]:raise ValueError('tenant config leakage')
                    if label in ('summary','detail','ordinary'):
                        if set(body['items'])!={pid}:raise ValueError('invalid authorized result')
                        version=body['meta']['version']
                path=json.loads((QA/'pin-open.json').read_text())['path']
                code,_=get('/'+pid+'/dramas?path='+path+'&version='+version+'&page=1','other')
                if code!=404:raise ValueError('pagination authorization bypass')
                results['unauthorized_pagination']=code
            finally:
                http.shutdown();runner.join()
    fact=dict(at=stamp(),checks=results,version=version,auth='isolated session/module/tenant upstream fixture',rows='live replica')
    atomic(QA/('http-'+version+'.json'),fact)
    return fact


if __name__=='__main__':print(encoded(check()),flush=True)
