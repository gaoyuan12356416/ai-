import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from features.ai_video_editing import studio,selections,previews
from features.ai_video_editing.runtime import Runtime,owner,packed
from features.ai_video_editing.catalog import source_key


class SelectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        rt=self.rt=Runtime(Path(self.tmp.name)/'private');studio.setup(rt)
        self.actor={'tenant_key':'http-fixture','user_id':'owner'}
        self.aid='aa_'+'1'*32;sha=hashlib.sha256(b'file').hexdigest()
        with rt.db(True) as db:db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(self.aid,owner(self.actor),'Fixture','',1,sha,packed({'duration_ms':4000,'has_audio':False,'width':320,'height':240}),time.time()))
        spec=importlib.util.spec_from_file_location('features.ai_video_editing._test_http',Path(__file__).parents[1]/'features/ai_video_editing/http_api.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules',{'app':types.SimpleNamespace(DramaMaterialHandler=BaseHTTPRequestHandler)}):spec.loader.exec_module(module)
        actor=self.actor
        class Handler(module.Handler):
            def log_message(self,*a):pass
            def _require_cookie_module(self,*a):
                if self.headers.get('X-Fixture-Login')!='yes':self.reply(401,{'error':'login required'});return False
                return True
            def _require_same_origin_json(self):return True
            def _session(self):return dict(actor,tenant_key=self.headers.get('X-Fixture-Tenant',actor['tenant_key']))
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.http.runtime=rt
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):self.http.shutdown();self.thread.join(2);self.http.server_close()

    def request(self,path,body=None,headers=None,method=None):
        c=HTTPConnection('127.0.0.1',self.http.server_port,timeout=5)
        h={'X-Fixture-Login':'yes','Content-Type':'application/json',**(headers or {})}
        c.request(method or ('POST' if body is not None else 'GET'),'/api/ai-video-editing'+path,None if body is None else json.dumps(body),h)
        r=c.getresponse();raw=r.read();answer=(r.status,dict(r.headers),json.loads(raw) if raw else None);c.close();return answer

    def create(self):
        key=source_key(['42','DramaWave','TW','zh-TW'])
        body={'items':[{'kind':'asset','asset_id':self.aid}]+[{'kind':'catalog','product':'dramawave','key':key,'episode':i} for i in range(2,6)],'operation_id':'five-http'}
        code,headers,data=self.request('/studio/sessions',body);self.assertEqual(code,200)
        self.assertIn('Server-Timing',headers)
        return body,data['item']

    def test_five_progress_etag_and_version_conflict(self):
        body,s=self.create();url='/studio/sessions/'+s['id']
        code,headers,data=self.request(url+'/source-progress');self.assertEqual(code,200)
        self.assertEqual(data['selection']['summary']['total'],5)
        self.assertEqual(self.request(url+'/source-progress',headers={'If-None-Match':headers['ETag']})[0],304)
        self.assertEqual(self.request('/studio/sessions',body)[2]['item']['id'],s['id'])
        self.assertEqual(self.request(url+'/selection',dict(body,operation_id='second',version=0))[0],409)
        self.assertEqual(self.request(url,{'source_ids':[self.aid],'version':s['version']},method='PATCH')[0],409)
        self.assertEqual(self.request(url,headers={'X-Fixture-Tenant':'other'})[0],404)
        self.assertEqual(self.request(url,headers={'X-Fixture-Login':'no'})[0],401)

    def test_lost_response_reconciles_original_receipt(self):
        body,s=self.create();body.update(operation_id='lost-response',version=s['version'])
        c=HTTPConnection('127.0.0.1',self.http.server_port)
        c.request('POST','/api/ai-video-editing/studio/sessions/'+s['id']+'/selection',json.dumps(body),{'X-Fixture-Login':'yes','Content-Type':'application/json'});c.close()
        for _ in range(30):
            receipt=self.request('/studio/selection-operations?operation_id=lost-response')[2]['receipt']
            if receipt:break
            time.sleep(.01)
        self.assertTrue(receipt)
        self.assertEqual(self.request('/studio/sessions/'+s['id']+'/selection',body)[2]['selection_id'],receipt['selection_id'])
        with self.rt.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM studio_source_batches').fetchone()[0],2)

    def test_private_derivative_authorization_precedes_internal_redirect(self):
        previews.request(self.rt,self.actor,self.aid)
        with self.rt.db(True) as db:
            row=db.execute('SELECT id FROM editor_previews').fetchone();db.execute("UPDATE editor_previews SET status='ready',poster=1")
        folder=previews.root(self.rt);folder.mkdir();(folder/(row['id']+'.mp4')).write_bytes(b'fixture')
        path='/assets/'+self.aid+'/preview'
        with patch.dict('os.environ',{'AI_VIDEO_EDITING_PREVIEW_ACCEL':'1'}):
            code,h,_=self.request(path);self.assertEqual(code,200);self.assertTrue(h['X-Accel-Redirect'].startswith('/_private_ai_editor_preview/'))
            self.assertEqual(self.request(path,headers={'If-None-Match':h['ETag']})[0],304)
            self.assertEqual(self.request(path,headers={'X-Fixture-Tenant':'other'})[0],404)
            self.assertEqual(self.request(path,headers={'X-Fixture-Login':'no'})[0],401)


if __name__=='__main__':unittest.main()
