"""Receipt timeouts and browser retries must retain the same publishing intent."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

import requests

from features.fb_auto_posts.client import FBAutoPostAdminClientError, error_payload, request_admin


ROOT = Path(__file__).resolve().parents[1]
RUN = '/api/admin/fb-auto-publish/templates/2/run-now'
ENV = {'FB_AUTO_POST_INTERNAL_TOKEN': 'fixture-only-not-a-real-token-00000000'}
DRAFT = {'drama_id': '29899', 'id_type': 'resource_id', 'expected_version': 1, 'operation_id': 'same-operation-1'}


class ClientReceiptTests(unittest.TestCase):
    def session(self):
        session = Mock()
        session.request.return_value = Mock(status_code=202, content=json.dumps({'ok': True, 'run_id': 7}).encode())
        return session

    def test_only_named_drama_run_waits_for_full_screening(self):
        for method, path, payload, timeout in (
            ('POST', RUN, DRAFT, 240),
            ('POST', RUN, {'operation_id': 'regular-run', 'expected_version': 1}, 30),
            ('POST', RUN, {'material_id': '111', 'expected_version': 1}, 30),
            ('POST', RUN.replace('run-now', 'enable'), {'expected_version': 1}, 900),
            ('GET', '/api/admin/fb-auto-publish/templates/2', None, 120),
        ):
            with self.subTest(path=path, payload=payload):
                session = self.session()
                with patch('features.fb_auto_posts.client.requests.Session', return_value=session):
                    result = request_admin(method, path, payload=payload, environ=ENV)
                self.assertEqual(result['run_id'], 7)
                self.assertEqual(session.request.call_args.kwargs['timeout'], timeout)
                self.assertFalse(session.trust_env)
                session.close.assert_called_once()

    def test_drama_read_timeout_returns_pending_without_automatic_retry(self):
        session = self.session()
        session.request.side_effect = requests.ReadTimeout('upstream details must stay private')
        with patch('features.fb_auto_posts.client.requests.Session', return_value=session):
            with self.assertRaises(FBAutoPostAdminClientError) as caught:
                request_admin('POST', RUN, payload=DRAFT, environ=ENV)
        status, response = error_payload(caught.exception)
        self.assertEqual(status, 503)
        self.assertEqual(response['code'], 'fb_auto_result_pending')
        self.assertIn('同一轮任务', response['message'])
        self.assertNotIn('upstream', response['message'])
        session.request.assert_called_once()
        self.assertEqual(session.request.call_args.kwargs['json'], DRAFT)
        session.close.assert_called_once()

    def test_unrelated_timeout_and_connection_errors_keep_existing_contract(self):
        for payload, failure in (({'material_id': '111'}, requests.ReadTimeout()),
                                 (DRAFT, requests.ConnectTimeout()), (DRAFT, requests.ConnectionError())):
            with self.subTest(payload=payload, failure=type(failure).__name__):
                session = self.session()
                session.request.side_effect = failure
                with patch('features.fb_auto_posts.client.requests.Session', return_value=session):
                    with self.assertRaises(FBAutoPostAdminClientError) as caught:
                        request_admin('POST', RUN, payload=payload, environ=ENV)
                self.assertEqual(caught.exception.code, 'fb_auto_post_service_unavailable')

    def test_recovered_receipt_keeps_server_result(self):
        session = self.session()
        receipt = {'ok': True, 'run_id': 324, 'queued': 0, 'skipped': 236, 'idempotent': True,
                   'matched_pages': 12, 'skip_summary': [{'reason': 'fb_auto_no_eligible_video', 'count': 236}]}
        session.request.return_value.content = json.dumps(receipt).encode()
        with patch('features.fb_auto_posts.client.requests.Session', return_value=session):
            self.assertEqual(request_admin('POST', RUN, payload=DRAFT, environ=ENV), receipt)


UI_SCRIPT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.style={};this.events={};this._text='';this.value='';}
  set textContent(text){this._text=String(text);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  append(...nodes){for(const node of nodes){this.children.push(node);node.parent=this;if(this.tagName==='select'&&!this.value)this.value=node.value;}}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,fn){this.events[name]=fn;}
  showModal(){} focus(){}
  close(){if(this.events.close)this.events.close();}
  remove(){this.parent.children=this.parent.children.filter(c=>c!==this);}
}
function all(node){return [node,...node.children.flatMap(all)];}
async function open(platform,responses){
  const body=new Element('body'),saved=new Map(),posts=[];
  const document={body,createElement:tag=>new Element(tag),getElementById:id=>all(body).find(n=>n.id===id)};
  const context={document,window:{},crypto:{randomUUID:()=> 'fixed-operation-000000000000'},
    sessionStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
    fetch:async(url,options)=>{
      if(url==='/api/auth/status')return {ok:true,json:async()=>({authenticated:true,user:{tenant_key:'t',user_id:'u'}})};
      if(!url.endsWith('/run-now'))return {ok:true,json:async()=>({template:{id:2,current_version:1,name:'fixture'}})};
      posts.push(JSON.parse(options.body));const response=responses.shift();
      return {ok:response.ok,json:async()=>response.body};
    }};
  vm.runInNewContext(source,context);
  await context.window.HitDramaPublish.open(platform,2);
  document.getElementById('hitDramaInput').value='29899';
  document.getElementById('hitDramaType').value='resource_id';
  const submit=all(body).find(n=>n.tagName==='button'&&n.textContent==='确认发布');
  return {body,saved,posts,submit,document};
}
(async()=>{
  let ui=await open('fb',[{ok:true,body:{ok:true,run_id:324,queued:0,skipped:236,matched_pages:12,
    skipped_pages:[{page_id:'11',reason:'theme_not_matched'}],
    skip_summary:[{reason:'fb_auto_no_eligible_video',count:236}]}}]);
  assert(ui.body.textContent.includes('语言、受众方向和题材画像均匹配的 Page'));
  await ui.submit.onclick();
  assert(ui.body.textContent.includes('本轮未发布'));
  assert(ui.body.textContent.includes('画像匹配 12 个 Page'));
  assert(ui.body.textContent.includes('Page 11：题材画像不匹配'));
  assert(ui.body.textContent.includes('没有合格素材：236 个 Page'));
  assert.equal(ui.saved.size,0);

  ui=await open('fb',[{ok:false,body:{code:'fb_auto_result_pending',message:'等待回执超时'}},
                     {ok:true,body:{ok:true,run_id:325,queued:1,skipped:0,matched_pages:1,idempotent:true}}]);
  await ui.submit.onclick();
  assert.equal(ui.saved.size,1);assert.equal(ui.submit.textContent,'重试确认');
  assert.equal(ui.document.getElementById('hitDramaInput').readOnly,true);
  await ui.submit.onclick();assert.deepEqual(ui.posts[0],ui.posts[1]);assert.equal(ui.saved.size,0);
  assert(ui.body.textContent.includes('已排队 1 个 Page'));
  assert(!ui.body.textContent.includes('本轮未发布'));

  ui=await open('fb',[{ok:false,body:{code:'hit_drama_no_matching_page',message:'没有匹配的 Page',
    conflicts:[{page_id:'11',language:'en',reason:'candidate_metadata_unavailable'},
               {page_id:'12',language:'pt',reason:'unsupported_profile_product'},
               {page_id:'13',reason:'signed_income_adjustment'},
               {page_id:'14',reason:'portrait_invalid'}]}},
    {ok:true,body:{ok:true,run_id:326,queued:1,skipped:0,matched_pages:1}}]);
  await ui.submit.onclick();assert.equal(ui.saved.size,0);assert.equal(ui.submit.textContent,'确认发布');
  assert.equal(ui.document.getElementById('hitDramaInput').readOnly,false);
  assert(ui.body.textContent.includes('Page 11：剧目分类暂不可用'));
  assert(ui.body.textContent.includes('Page 12：当前产品没有可用画像'));
  assert(ui.body.textContent.includes('Page 13：收入含冲正，暂无可用偏好'));
  assert(ui.body.textContent.includes('Page 14：画像数据异常'));
  await ui.submit.onclick();assert(!ui.body.textContent.includes('Page 11：剧目分类暂不可用'));
  assert(ui.body.textContent.includes('已排队 1 个 Page'));

  ui=await open('x',[{ok:true,body:{ok:true,run_id:71,matched_channels:1,
      skipped_channels:[{account_id:'12',reason:'theme_not_matched'}]}}]);
  await ui.submit.onclick();assert(ui.body.textContent.includes('匹配 1 个频道，跳过 1 个频道'));
  assert(ui.body.textContent.includes('频道 12：题材画像不匹配'));
  assert(!ui.body.textContent.includes('画像匹配 1 个 Page'));
  assert.equal(ui.posts[0].confirmed,true);assert(ui.posts[0].idempotency_key);assert(!ui.posts[0].operation_id);
  console.log('FB receipt UI cases passed');
})().catch(error=>{console.error(error);process.exit(1);});
'''


class BrowserReceiptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the browser-flow fixture')
    def test_render_receipt_and_preserve_same_operation_after_timeout(self):
        result = subprocess.run(['node', '-e', UI_SCRIPT, str(ROOT/'static/hit-drama-publish.js')],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
