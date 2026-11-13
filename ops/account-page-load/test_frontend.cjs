/* Exercise the real page functions with delayed reads; no production writes. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const auth = {authenticated:true,user:{is_admin:true,role:'admin',permissions:{x_accounts:true,fb_video_publish:true}}};
const account = id => ({id,username:'account'+id,display_name:'Account '+id,status:'active',drama_language:'en',public_metrics:{}});
function harness(source, extra={}) {
  const nodes = new Map(), requests=[], scripts=[], marks=[];
  const node = id => {
    if(!nodes.has(id))nodes.set(id,{id,value:'',innerHTML:'',textContent:'',disabled:false,hidden:false,isConnected:true,dataset:{},classList:{add(){},remove(){},toggle(){}},
      addEventListener(){},setAttribute(){},querySelectorAll(){return [];},querySelector(){return null;},remove(){},closest(){return null;}});
    return nodes.get(id);
  };
  const P={accounts:[],allowed:()=>true,api:p=>request('/api/x-video-publish'+p),account:id=>P.accounts.find(a=>a.account_id===String(id))};
  const responses = new Map();
  function request(url){requests.push(url);if(!responses.has(url))throw Error('Unexpected request: '+url);const value=responses.get(url);return typeof value==='function'?value():Promise.resolve(value);}
  const window={XVideoPublish:P,XTagRevenue:{refresh(){},paint(){},cell(){return 'pending';}},FbPagePortraits:{cells(){return '<td>pending</td><td>pending</td>';},load(){},clear(){}},setInterval(){}};
  const quickNav={render:()=>new Promise(()=>{})};
  const context=vm.createContext({window,document:{body:{dataset:{fbView:'pages'}},head:{append(s){scripts.push(s);}},getElementById:node,createElement:()=>({remove(){}}),addEventListener(){}},
    fetch:async url=>{const data=await request(url);return {ok:true,status:200,json:async()=>data,text:async()=>JSON.stringify(data)};},
    AbortController,AbortSignal,URL,URLSearchParams,TextEncoder,Intl,Map,Set,Date,performance:{mark:n=>marks.push(n)},
    CSS:{escape:String},setTimeout:(...args)=>{const timer=setTimeout(...args);timer.unref();return timer;},clearTimeout,location:{search:'',pathname:'/'},crypto:{randomUUID:()=> 'fixture'},
    QuickNav:quickNav,UiTopbar:{render(){},handleAuthAction(){}},...extra});
  window.QuickNav=quickNav;window.UiTopbar=context.UiTopbar;
  vm.runInContext(source,context);
  return {api:context.exposed,node,requests,responses,scripts,marks,P,window};
}
const xhtml=fs.readFileSync(path.join(root,'static/x-account-list.html'),'utf8');
const xsource=xhtml.match(/<script>\s*([\s\S]*?)<\/script>/)[1].replace('      init();','      globalThis.exposed={state,loadData,loadPublishingAccounts,publishingReason,init,openExcel};');
function xh(){const h=harness(xsource);h.api.state.auth=auth;h.responses.set('/api/admin/x-accounts/credits',{status:'live'});return h;}

(async()=>{
  let checks=0;
  {
    const h=xh(), settings=deferred();
    h.responses.set('/api/admin/x-accounts',{items:[account(1)]});h.responses.set('/api/x-video-publish/accounts',()=>settings.promise);
    await h.api.loadData();
    assert.match(h.node('accountRows').innerHTML,/Account 1/);
    assert.equal(h.api.state.publishingLoading,true);
    assert.equal(h.marks.length,1);
    settings.reject(Error('fixture unavailable'));await tick();
    assert.match(h.node('accountRows').innerHTML,/Account 1/);
    assert.match(h.node('publishingNotice').innerHTML,/重试发布设置/);
    h.responses.set('/api/x-video-publish/accounts',{items:[{account_id:1}]});
    await h.api.loadPublishingAccounts(h.api.state.loadVersion);
    assert.equal(h.api.publishingReason(1),'');checks+=3;
  }
  {
    const h=xh(), first=deferred(), oldSettings=deferred();
    h.responses.set('/api/admin/x-accounts',()=>first.promise);h.responses.set('/api/x-video-publish/accounts',()=>oldSettings.promise);
    const old=h.api.loadData();
    h.responses.set('/api/admin/x-accounts',{items:[account(2)]});h.responses.set('/api/x-video-publish/accounts',{items:[{account_id:2}]});
    await h.api.loadData();oldSettings.resolve({items:[{account_id:1}]});first.resolve({items:[account(1)]});await old;await tick();
    assert.match(h.node('accountRows').innerHTML,/Account 2/);assert.doesNotMatch(h.node('accountRows').innerHTML,/Account 1/);
    assert.equal(h.P.accounts[0].account_id,'2');checks++;
  }
  {
    const h=xh();h.responses.set('/api/ui/topbar',auth);h.responses.set('/api/admin/x-accounts',{items:[account(3)]});h.responses.set('/api/x-video-publish/accounts',{items:[]});
    await Promise.race([h.api.init(),new Promise((_,reject)=>setTimeout(()=>reject(Error('navigation blocked list')),300))]);
    assert.match(h.node('accountRows').innerHTML,/Account 3/);checks++;
  }
  {
    const h=xh();let opened=0;
    assert.equal(h.scripts.length,0);assert.doesNotMatch(xhtml,/<script src="\/vendor\/xlsx/);
    const pending=h.api.openExcel();await h.api.openExcel();assert.equal(h.scripts.length,2);
    h.window.XLSX={};h.P.openExcel=async()=>opened++;h.scripts.forEach(s=>s.onload());await pending;
    assert.equal(opened,1);assert.equal(h.node('xOpenExcel').disabled,false);checks++;
  }
  {
    const h=xh();const pending=h.api.openExcel();h.scripts.forEach(s=>s.onerror());await pending;
    assert.equal(h.node('xOpenExcel').disabled,false);assert.match(h.node('toast').textContent,/失败/);checks++;
  }
  {
    const fb=fs.readFileSync(path.join(root,'static/fb-video-publish.js'),'utf8').replace('  boot();','  globalThis.exposed={boot};');
    const h=harness(fb),source=deferred();h.responses.set('/api/ui/topbar',auth);
    h.responses.set('/api/fb-page-portraits/pages',{items:[{page_id:'1',page_name:'Fixture Page',created_by:'fixture'}],authorization_pending:true});
    h.responses.set('/api/fb-video-publish/pages',()=>source.promise);h.responses.set('/api/fb-video-publish/languages',{items:[{value:'en',label:'English'}]});
    await h.api.boot();assert.match(h.node('pageRows').innerHTML,/Fixture Page/);assert.equal(h.node('pageSearch').disabled,false);
    assert.ok(h.marks.includes('fb-page-list-ready'));source.reject(Error('fixture authorization unavailable'));await tick();
    assert.match(h.node('pageRows').innerHTML,/Fixture Page/);assert.match(h.node('pageRows').innerHTML,/暂未核验/);checks+=2;
    let opened=0;const opening=h.node('excelPublishButton').onclick({target:h.node('excelPublishButton')});
    assert.equal(h.scripts.length,1);h.window.FbExcelPublish={open:async()=>opened++};h.scripts[0].onload();await opening;assert.equal(opened,1);checks++;
  }
  console.log(JSON.stringify({passed:checks,coverage:'slow/failed optional reads, stale refreshes, slow navigation, lazy Excel, Page authorization fallback'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
