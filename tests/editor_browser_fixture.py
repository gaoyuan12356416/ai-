"""Loopback-only browser fixture with the real editor API and isolated ownership.

python tests/editor_browser_fixture.py --root <new scratch directory> --port 18984
No production credentials, provider calls, model calls or render workers are used.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types
from urllib.parse import urlsplit
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
stub=types.ModuleType('app');stub.DramaMaterialHandler=BaseHTTPRequestHandler
sys.modules['app']=stub
from features.ai_video_editing.http_api import Handler
from features.ai_video_editing.runtime import Runtime, owner, packed
from features.ai_video_editing import studio, selections, catalog_imports, source_jobs, smart_api, previews
from features.ai_video_editing.catalog import source_key

ACTOR={'tenant_key':'isolated-browser-fixture','user_id':'fixture','name':'Browser fixture','permissions':{'video_editing':True}}
KEY=source_key(['42','DramaWave','TW','zh-TW'])


class Catalog:
    def search(self,product,query='',page=1,**kw):
        return {'items':[{'key':KEY,'id':'42','name':'测试剧目','language':'中文','cover':''}], 'has_more':False,'page':1}
    def episodes(self,*args,**kw):
        return {'items':[{'number':i,'name':'测试剧目','language':'中文'} for i in range(1,11)]}


def ready(rt,number):
    aid='aa_'+('%032x'%number)
    with rt.db(True) as db:
        catalog_imports.submit_rows(db,ACTOR,{'product':'dramawave','key':KEY,'episodes':[number]})
        db.execute("UPDATE catalog_imports SET status='ready',asset_id=?,bytes=102400,total=102400 WHERE episode=?",(aid,number))


class FixtureHandler(Handler):
    def log_message(self,*args):pass
    def _require_cookie_module(self,*args):return True
    def _session(self):return dict(ACTOR,tenant_key=self.headers.get('X-Fixture-Tenant',ACTOR['tenant_key']))
    def _require_same_origin_json(self):return True
    def dispatch(self):
        path=urlsplit(self.path).path
        if path=='/api/ui/topbar':return self.reply(200,{'authenticated':True,'user':ACTOR,'name':'Browser fixture'})
        if path=='/api/ui/navigation':return self.reply(200,{'items':[]})
        if path.startswith('/_fixture/ready/'):
            for n in path.rsplit('/',1)[1].split(','):ready(self.runtime,int(n))
            selections.tick(self.runtime)
            return self.reply(200,{'ok':True})
        if path=='/_fixture/analyze':
            selections.tick(self.runtime)
            with patch.object(smart_api,'process_source',return_value={'engine':'multimodal','candidates':[],'warnings':[]}):
                while source_jobs.execute_one(self.runtime):pass
            selections.tick(self.runtime)
            return self.reply(200,{'ok':True})
        if path=='/_fixture/preview':
            while job:=previews.claim(self.runtime):previews.process(self.runtime,job)
            return self.reply(200,{'ok':True})
        if path.startswith('/_fixture/production/'):
            status=path.rsplit('/',1)[-1]
            if status not in ('queued','running','completed','failed'):return self.reply(400,{'error':'fixture status'})
            with self.runtime.db(True) as db:
                db.execute('UPDATE tasks SET status=?,error=? WHERE id=?',(status,'Fixture render failed' if status=='failed' else '',self.server.production_task))
            return self.reply(200,{'ok':True})
        if path.startswith('/api/ai-video-editing/'):
            if path.endswith('/assets') and self.command=='GET':time.sleep(self.server.asset_delay)
            return super().dispatch()
        if path in ('/quick-nav.js','/ui-topbar.js'):
            data=b'window.QuickNav={render:async()=>{}};window.UiTopbar={render:()=>{},handleAuthAction:()=>{}};'
            self.send_response(200);self.send_header('Content-Type','text/javascript');self.end_headers();self.wfile.write(data);return
        name=Path(path).name or 'video-editing-ai.html';file=REPO/'static'/name
        if not file.is_file():self.send_error(404);return
        raw=file.read_bytes();self.send_response(200)
        self.send_header('Content-Type',mimetypes.guess_type(name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(raw)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)
    do_GET=do_POST=do_HEAD=do_PATCH=dispatch


def build(folder):
    if folder.exists():raise RuntimeError('Use a new isolated fixture directory')
    folder.mkdir(parents=True)
    rt=Runtime(folder/'private');studio.setup(rt)
    sample=folder/'sample.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=360x640:rate=24:duration=8',
                    '-c:v','libx264','-preset','ultrafast','-threads','2','-movflags','+faststart',str(sample)],check=True)
    sha=hashlib.sha256(sample.read_bytes()).hexdigest()
    from features.ai_video_editing.runtime import probe
    meta=packed(probe(sample))
    for n in range(1,31):
        aid='aa_'+('%032x'%n)
        shutil.copyfile(sample,rt.root/'uploads'/(aid+'.mp4'))
        with rt.db(True) as db:db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(aid,owner(ACTOR),'测试剧目 · 第 %s 集'%n,'Fixture',n,sha,meta,time.time()+n))
    ready(rt,1)
    return rt


def production(rt):
    """A render task whose parent/source ETag stays fixed while task receipts change."""
    aid='aa_'+('%032x'%1)
    item=studio.dispatch(rt,ACTOR,'POST','/sessions',{'items':[{'kind':'asset','asset_id':aid}],'operation_id':'production-fixture'}, {})['item']
    sid=item['id']
    rid=studio.submit(rt,ACTOR,sid,{'operation_id':'production-fixture-run','config':{'clip':{'method':'hook_first'}}})['run_id']
    project=rt.save(ACTOR,{'operation_id':'production-project','name':'Fixture output','units':[{'id':'one','title':'Fixture finished film','clips':[{'source_id':aid,'start_ms':0,'end_ms':4000}]}]})['item']
    task=rt.render(project['id'],ACTOR,{'operation_id':'production-render','expected_version':project['version']})['items'][0]
    shutil.copyfile(rt.root/'uploads'/(aid+'.mp4'),rt.root/'outputs'/(task['id']+'.mp4'))
    with rt.db(True) as db:
        db.execute('UPDATE tasks SET meta=? WHERE id=?',(packed({'duration_ms':8000,'width':360,'height':640}),task['id']))
        db.execute("UPDATE studio_runs SET status='rendering',state=? WHERE id=?",(packed({'task_ids':[task['id']],'project_id':project['id']}),rid))
        db.execute("UPDATE studio_source_batches SET status='succeeded' WHERE id=?",(item['selection_id'],))
        db.execute("UPDATE studio_sessions SET sources=? WHERE id=?",(packed([aid]),sid))
    return sid,task['id']


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--port',type=int,default=18984);p.add_argument('--asset-delay',type=float,default=2);p.add_argument('--production',action='store_true')
    args=p.parse_args();os.environ['AI_VIDEO_EDITING_PREVIEW_ACCEL']='0'
    rt=build(args.root)
    with ThreadingHTTPServer(('127.0.0.1',args.port),FixtureHandler) as server:
        server.runtime=rt;server.catalog=Catalog();server.asset_delay=args.asset_delay;server.worker_token='fixture-only'
        if args.production:
            sid,server.production_task=production(rt)
            print('Production fixture: http://127.0.0.1:%s/video-editing-ai.html#%s'%(args.port,sid),flush=True)
        print('Fixture ready on port',args.port,flush=True);server.serve_forever()
