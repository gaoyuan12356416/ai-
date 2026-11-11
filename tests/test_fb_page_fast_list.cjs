/* UI orchestration test: hold the remote query open while exercising the list. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/fb-video-publish.js','utf8');
const code=source.slice(source.indexOf('  async function pageManagement() {'),source.indexOf('  async function publishing()'));
const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false,disabled:false,value:'',setAttribute(){}});return elements.get(id);};
const remote=[];let localVersion=1;
const rows=()=>['11','22'].map((id,i)=>({page_id:id,page_name:'Page '+id+' v'+localVersion,language:'en',created_by:i?'u2':'u1',created_by_name:i?'乙':'甲',version:localVersion,authorization_pending:true,eligible_token_count:null}));
const context={AbortController,Promise,Map,Set,console,setTimeout,clearTimeout,$:node,
  auth:{user:{is_admin:true}},window:{FbPagePortraits:{load(){},cells(){return '<td>summary</td><td>portrait</td>';},clear(){}}},
  esc:v=>String(v??''),lang:v=>v,atBJ:v=>v||'',poolPending:()=>false,errorAt(){},loadLanguages:async()=>{},busy:async(_,fn)=>fn(),
  api:async(path,options={})=>path==='/api/fb-page-portraits/pages'?{items:rows(),authorization_pending:true}:new Promise((resolve,reject)=>remote.push({resolve,reject,options})),
};
const run=vm.runInNewContext('('+code+')',context);
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
  await Promise.race([run(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('initial list waited for source query')),200))]);
  assert(node('pageRows').innerHTML.includes('Page 11 v1'));
  assert(node('pageRows').innerHTML.includes('正在核验'));
  assert(!node('pageRows').innerHTML.includes('授权可用'));
  node('pageCreator').onchange({target:{value:'u1'}});
  assert(node('pageRows').innerHTML.includes('Page 11'));
  assert(!node('pageRows').innerHTML.includes('Page 22'));
  localVersion=2;await node('reloadPages').onclick({target:node('reloadPages')});
  assert(remote[0].options.signal.aborted);
  remote[0].resolve({items:[{...rows()[0],page_name:'STALE',authorization_pending:false,eligible_token_count:1}]});await tick();
  assert(!node('pageRows').innerHTML.includes('STALE'));
  remote[1].reject(new Error('source unavailable'));await tick();
  assert(node('pageRows').innerHTML.includes('Page 11 v2'));assert(node('pageRows').innerHTML.includes('暂未核验'));
  localVersion=3;await node('reloadPages').onclick({target:node('reloadPages')});
  remote[2].resolve({items:rows().map(p=>({...p,authorization_pending:false,eligible_token_count:1}))});await tick();
  assert(node('pageRows').innerHTML.includes('授权可用'));assert(node('pageAuthorizationNotice').hidden);
  console.log('PASS: immediate rows, creator filter, pending authorization, stale-response fence, partial failure, fresh verification');
})().catch(error=>{console.error(error);process.exit(1);});
