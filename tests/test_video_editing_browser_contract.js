'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const crypto=require('node:crypto');
const test=require('node:test');
const script=fs.readFileSync(path.join(__dirname,'../static/video-editing.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const response=(payload,status=200)=>({ok:status>=200&&status<300,status,json:async()=>payload});

// A minimal browser contract harness exercises real event handlers and asynchronous API calls.
// It intentionally does not reproduce the editing or save implementation.
async function browser(handler,extras={}){
  const elements=new Map(),calls=[],timers=new Map();let nextTimer=1;
  class Element{
    constructor(id){this.id=id;this.value='';this.textContent='';this.innerHTML='';this.dataset={};this.style={};this.listeners={};this.files=[];this.classList={add(){},remove(){},toggle(){}};}
    addEventListener(type,fn){(this.listeners[type]||(this.listeners[type]=[])).push(fn);}
    emit(type,event={}){return Promise.all((this.listeners[type]||[]).map(fn=>fn({target:this,...event})));}
    setAttribute(){}removeAttribute(){}pause(){}load(){}click(){return this.emit('click');}showModal(){this.open=true;}close(){this.open=false;}play(){return Promise.resolve();}
  }
  const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},querySelectorAll(){return [];},addEventListener(){},createElement(){return new Element();}};
  const fetch=async(url,options={})=>{calls.push({url,options,body:options.body?JSON.parse(options.body):null});const custom=await handler(url,options,calls);if(custom)return custom;
    if(url==='/api/ui/topbar')return response({authenticated:true,user:{name:'Tester',role:'admin',tenant_key:'tenant'}});
    if(url.endsWith('/config'))return response({item:{enabled:true,intake_enabled:true,worker_configured:true,cos_configured:true}});
    if(url.includes('/assets?')||url.endsWith('/projects')||url.endsWith('/tasks'))return response({items:[],total:0});
    throw new Error(`Unexpected request ${options.method||'GET'} ${url}`);
  };
  const window={document,crypto:crypto.webcrypto,addEventListener(){},QuickNav:{render:async()=>{}},UiTopbar:{render(){},handleAuthAction:async()=>{}}};
  const context={window,document,fetch,URLSearchParams,location:{search:'',pathname:'/video-editing.html'},performance:{now:()=>0},requestAnimationFrame:()=>0,cancelAnimationFrame(){},setInterval(){},setTimeout(fn){const id=nextTimer++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},console,...extras};
  vm.runInNewContext(script,context,{filename:'video-editing.js'});for(let i=0;i<6;i++)await tick();
  return {elements,calls,timers,field:id=>document.getElementById(id),rename:async name=>{const input=document.getElementById('projectName');input.value=name;await input.emit('change');},response};
}

test('edits made during first save are flushed before a new project replaces the working draft',async()=>{
  let finishCreate;const created=new Promise(resolve=>{finishCreate=resolve;});let remote;
  const app=await browser(async(url,options)=>{
    if(options.method==='POST'&&url.endsWith('/projects')){const body=JSON.parse(options.body);await created;remote={...body,id:'vp_first',version:1};delete remote.operation_id;return response({item:remote});}
    if(options.method==='PATCH'){const body=JSON.parse(options.body);assert.equal(body.expected_version,remote.version);remote={...body,id:remote.id,version:remote.version+1};delete remote.expected_version;return response({item:remote});}
  });
  await app.rename('first draft');const save=app.field('saveProject').click();await tick();
  await app.rename('edited while saving');const leave=app.field('newProject').click();await tick();
  assert.equal(app.field('projectName').value,'edited while saving');
  finishCreate();await Promise.all([save,leave]);
  assert.equal(remote.name,'edited while saving');assert.equal(remote.version,2);
  assert.equal(app.field('projectName').value,'未命名剪辑项目');
  assert.equal(app.calls.filter(call=>call.options.method==='POST').length,1);
});

test('catalog product choices use the existing product label and retain its app identifier',async()=>{
  const app=await browser(async url=>url.endsWith('/catalog/products')?response({items:[{app_id:'2310',app:'DramaWave',label:'DramaWave | 美国 | 英语'},{app_id:'8123',app:'HotDrama'}]}):null);
  await app.field('addDrama').click();const markup=app.field('productPicker').innerHTML;
  assert.match(markup,/<option value="2310">DramaWave \| 美国 \| 英语<\/option>/);
  assert.match(markup,/<option value="8123">HotDrama<\/option>/);
});

test('real catalog never sends an unbounded empty or single-character name search',async()=>{
  const app=await browser(async url=>url.includes('/catalog/dramas?')?response({items:[]}):null);
  app.field('productPicker').value='2310';await app.field('productPicker').emit('change');
  app.field('dramaSearch').value='她';await app.field('searchDramas').click();
  assert.equal(app.calls.filter(call=>call.url.includes('/catalog/dramas?')).length,0);
  app.field('dramaSearch').value='她的';await app.field('searchDramas').click();
  const requests=app.calls.filter(call=>call.url.includes('/catalog/dramas?'));
  assert.equal(requests.length,1);assert.match(requests[0].url,new RegExp(`q=${encodeURIComponent('她的')}`));
});

test('unknown create outcome retries exactly the frozen operation then saves newer edits as a version',async()=>{
  let firstBody,remote,attempts=0;
  const app=await browser(async(url,options)=>{
    if(options.method==='POST'&&url.endsWith('/projects')){const body=JSON.parse(options.body);attempts++;if(attempts===1){firstBody=body;remote={...body,id:'vp_unknown',version:1};delete remote.operation_id;throw new Error('connection lost after acceptance');}assert.deepEqual(body,firstBody);return response({item:remote});}
    if(options.method==='PATCH'){const body=JSON.parse(options.body);remote={...body,id:'vp_unknown',version:2};delete remote.expected_version;return response({item:remote});}
  });
  await app.rename('accepted draft');await app.field('saveProject').click();
  await app.rename('new local edit');await app.field('newProject').click();
  assert.equal(attempts,2);assert.equal(remote.name,'new local edit');assert.equal(remote.version,2);
  assert.equal(app.field('projectName').value,'未命名剪辑项目');
});

test('unknown PATCH outcome reads back matching saved draft without repeating the write',async()=>{
  let remote,patches=0;
  const app=await browser(async(url,options)=>{
    if(options.method==='POST'){const body=JSON.parse(options.body);remote={...body,id:'vp_patch',version:1};delete remote.operation_id;return response({item:remote});}
    if(options.method==='PATCH'){patches++;const body=JSON.parse(options.body);remote={...body,id:'vp_patch',version:2};delete remote.expected_version;throw new Error('accepted but response lost');}
    if(url.endsWith('/projects/vp_patch'))return response({item:remote});
  });
  await app.rename('initial');await app.field('saveProject').click();
  await app.rename('updated');await app.field('saveProject').click();
  assert.equal(patches,1);assert.match(app.field('saveStatus').textContent,/v2/);
  await app.field('saveProject').click();assert.equal(patches,1);
});

test('a true version conflict preserves unsaved draft and prevents project replacement',async()=>{
  let remote;
  const app=await browser(async(url,options)=>{
    if(options.method==='POST'){const body=JSON.parse(options.body);remote={...body,id:'vp_conflict',version:1};delete remote.operation_id;return response({item:remote});}
    if(options.method==='PATCH'){remote={...remote,name:'someone else edited',version:2};return response({code:'project_version_conflict',error:'项目已被更新'},409);}
    if(url.endsWith('/projects/vp_conflict'))return response({item:remote});
  });
  await app.rename('initial');await app.field('saveProject').click();
  await app.rename('my unsaved draft');await app.field('newProject').click();
  assert.equal(app.field('projectName').value,'my unsaved draft');assert.match(app.field('saveStatus').textContent,/版本冲突/);
});

test('explicit save-copy resolves a conflict without writing over the other saved project',async()=>{
  const projects=new Map();let next=1;
  const app=await browser(async(url,options)=>{
    if(options.method==='POST'){const body=JSON.parse(options.body);const record={...body,id:`vp_copy_${next++}`,version:1};delete record.operation_id;projects.set(record.id,record);return response({item:record});}
    if(options.method==='PATCH'){const remote=projects.get('vp_copy_1');projects.set(remote.id,{...remote,name:'other editor',version:2});return response({code:'project_version_conflict',error:'项目已被更新'},409);}
    if(url.endsWith('/projects/vp_copy_1'))return response({item:projects.get('vp_copy_1')});
  });
  await app.rename('initial');await app.field('saveProject').click();await app.rename('my kept edits');await app.field('saveProject').click();
  await app.field('saveCopy').click();
  assert.equal(projects.size,2);assert.equal(projects.get('vp_copy_1').name,'other editor');
  assert.equal(projects.get('vp_copy_2').name,'my kept edits · 副本');assert.match(app.field('saveStatus').textContent,/v1/);
});

test('multipart resume reconciles COS, renews an expired signature and never stores credentials',async()=>{
  const buffer=Buffer.from('abcdefghijklmnopq');const file={name:'sample.mp4',size:buffer.length,type:'video/mp4',lastModified:123,slice:(start,end)=>new Blob([buffer.subarray(start,end)])};
  const fingerprint=`${file.name}|${file.size}|${file.lastModified}`;
  const writes=[],storage=new Map([['ve-upload-sessions-v1',JSON.stringify({[fingerprint]:{id:'vu_resume',operation_id:'original_operation'}})]]);
  const localStorage={getItem:key=>storage.get(key)||null,setItem(key,value){writes.push(value);storage.set(key,value);}};
  const puts=[],signatures=new Map();let completed=false,active=0,maxActive=0;
  class XHR{
    constructor(){this.upload={};this.headers={};}
    open(method,url){assert.equal(method,'PUT');this.url=url;}
    setRequestHeader(key,value){this.headers[key]=value;}
    getResponseHeader(name){return name==='ETag'?'"part-etag"':null;}
    send(blob){assert.equal(this.withCredentials,false);active++;maxActive=Math.max(maxActive,active);puts.push(this);const part=Number(this.url.split('/').at(-1));const expired=part===2&&puts.filter(put=>put.url===this.url).length===1;
      setImmediate(async()=>{assert.equal(this.headers['Content-MD5'],crypto.createHash('md5').update(Buffer.from(await blob.arrayBuffer())).digest('base64'));this.status=expired?403:200;active--;if(this.upload.onprogress)this.upload.onprogress({lengthComputable:true,loaded:blob.size,total:blob.size});this.onload();});
    }
    abort(){if(this.onabort)this.onabort();}
  }
  const session={id:'vu_resume',asset_id:'va_resume',status:'uploading',part_size:8,part_count:3,size_bytes:file.size,uploaded_parts:[{part_number:1,size_bytes:8,etag:'previous'}]};
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/uploads/vu_resume')||url.endsWith('/reconcile'))return response({item:session});
    const match=url.match(/\/parts\/(\d+)\/sign$/);if(match){const part=Number(match[1]);signatures.set(part,(signatures.get(part)||0)+1);return response({item:{url:`https://cos.example.test/parts/${part}`,headers:{Authorization:'temporary-secret-sentinel','Content-MD5':JSON.parse(options.body).content_md5},expires_at:1890000000}});}
    if(url.endsWith('/complete')){assert.equal(puts.filter(put=>put.status===200).length,2);completed=true;return response({item:{...session,status:'completed'}});}
  },{XMLHttpRequest:XHR,localStorage});
  app.field('fileInput').files=[file];await app.field('fileInput').emit('change');
  for(let i=0;i<40&&!completed;i++)await tick();for(let i=0;i<5;i++)await tick();
  assert.equal(completed,true);assert.equal(maxActive,2);assert.equal(signatures.get(2),2);assert.equal(signatures.get(3),1);
  assert.equal(app.calls.some(call=>call.options.method==='POST'&&call.url.endsWith('/uploads')),false);
  assert.equal(app.calls.some(call=>call.url.includes('/parts/1/sign')),false);
  assert.equal(writes.some(value=>value.includes('temporary-secret')||value.includes('https://')),false);
  assert.deepEqual(JSON.parse(storage.get('ve-upload-sessions-v1')),{});
});
