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
  const elements=new Map(),calls=[],timers=new Map(),intervals=[],documentEvents={},episodeChecks=new Map(),createdElements=[];let nextTimer=1;
  class Element{
    constructor(id){this.id=id;this.value='';this.textContent='';this.innerHTML='';this.dataset={};this.style={};this.listeners={};this.files=[];this.classList={add(){},remove(){},toggle(){}};}
    addEventListener(type,fn){(this.listeners[type]||(this.listeners[type]=[])).push(fn);}
    emit(type,event={}){return Promise.all((this.listeners[type]||[]).map(fn=>fn({target:this,...event})));}
    closest(selectors){return selectors.split(',').some(selector=>{const match=selector.match(/^\[data-([a-z-]+)\]$/);return match&&Object.hasOwn(this.dataset,match[1].replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()));})?this:null;}
    setAttribute(){}removeAttribute(){}pause(){}load(){}click(){return this.emit('click');}showModal(){this.open=true;}close(){this.open=false;}play(){return Promise.resolve();}
  }
  const fits=['contain','cover'].map(fit=>{const el=new Element(`fit-${fit}`);el.dataset.fit=fit;return el;});
  const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},querySelectorAll(selector){
    if(selector==='[data-fit]')return fits;
    if(selector==='.episode-select:checked'){
      const markup=elements.get('catalogEpisodes')?.innerHTML||'';
      return Array.from(markup.matchAll(/class="episode-select"[^>]*value="([^"]+)"/g),match=>{if(!episodeChecks.has(match[1])){const el=new Element();el.value=match[1];episodeChecks.set(match[1],el);}return episodeChecks.get(match[1]);}).filter(el=>el.checked);
    }return [];
  },addEventListener(type,fn){(documentEvents[type]||(documentEvents[type]=[])).push(fn);},createElement(){const el=new Element();createdElements.push(el);return el;}};
  const fetch=async(url,options={})=>{calls.push({url,options,body:options.body?JSON.parse(options.body):null});const custom=await handler(url,options,calls);if(custom)return custom;
    if(url==='/api/ui/topbar')return response({authenticated:true,user:{name:'Tester',role:'admin',tenant_key:'tenant'}});
    if(url.endsWith('/config'))return response({item:{enabled:true,intake_enabled:true,worker_configured:true,cos_configured:true}});
    if(url.includes('/assets?')||url.endsWith('/projects')||url.endsWith('/tasks'))return response({items:[],total:0});
    throw new Error(`Unexpected request ${options.method||'GET'} ${url}`);
  };
  const window={document,crypto:crypto.webcrypto,addEventListener(){},QuickNav:{render:async()=>{}},UiTopbar:{render(){},handleAuthAction:async()=>{}}};
  const context={window,document,fetch,URLSearchParams,location:{search:'',pathname:'/video-editing.html'},performance:{now:()=>0},requestAnimationFrame:()=>0,cancelAnimationFrame(){},setInterval(fn){intervals.push(fn);},setTimeout(fn){const id=nextTimer++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},console,...extras};
  vm.runInNewContext(script,context,{filename:'video-editing.js'});for(let i=0;i<6;i++)await tick();
  return {elements,calls,timers,createdElements,field:id=>document.getElementById(id),rename:async name=>{const input=document.getElementById('projectName');input.value=name;await input.emit('change');},response,
    clickData:async(dataset,container)=>{const target=new Element();target.dataset=dataset;return container?document.getElementById(container).emit('click',{target}):Promise.all((documentEvents.click||[]).map(fn=>fn({target})));},
    checkEpisodes(ids){for(const id of ids){if(!episodeChecks.has(id)){const el=new Element();el.value=id;episodeChecks.set(id,el);}}for(const [id,el] of episodeChecks)el.checked=ids.includes(id);},
    fit:fit=>fits.find(el=>el.dataset.fit===fit).click(),poll:()=>Promise.all(intervals.map(fn=>fn())),
  };
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

test('actual GPU metric DTOs show clip time, transferred bytes and steps as stage progress, never total percent',async()=>{
  const tasks=[
    {id:'ve_time',status:'running',stage:'rendering_random',progress:{out_time_seconds:2.4,duration_seconds:9.9,frame:72,fps:30,percent:24.24}},
    {id:'ve_download',status:'running',stage:'downloading',progress:{download_bytes:1048576,total_bytes:4194304}},
    {id:'ve_upload',status:'running',stage:'uploading',progress:{uploaded_bytes:2097152,upload_bytes_total:8388608}},
    {id:'ve_steps',status:'running',stage:'normalizing',progress:{completed_steps:0,total_steps:4}},
    {id:'ve_legacy_download',status:'running',stage:'prefetching',progress:{downloaded_bytes:3145728,total_bytes:6291456}},
  ];
  const app=await browser(async url=>url.endsWith('/tasks')?response({items:tasks,total:tasks.length}):null);
  const markup=app.field('taskList').innerHTML;
  assert.match(markup,/制作中 · 当前阶段：当前片段 2\.4 \/ 9\.9 秒/);
  assert.match(markup,/下载素材 · 当前阶段：已下载 1\.0 MiB \/ 4\.0 MiB/);
  assert.match(markup,/上传中 · 当前阶段：已上传 2\.0 MiB \/ 8\.0 MiB/);
  assert.match(markup,/处理片段 · 当前阶段：已完成 0 \/ 4 步/);
  assert.match(markup,/预下载素材 · 当前阶段：已下载 3\.0 MiB \/ 6\.0 MiB/);
  assert.doesNotMatch(markup,/%|rendering_random|\[object Object\]/);
});

test('runtime stages and waiting states have Chinese labels without exposing unknown technical stage names',async()=>{
  const stages={queued:'排队中',starting:'准备制作',rendering:'制作中',downloading:'下载素材',normalizing:'处理片段',waiting_cover:'等待封面',rendering_intro:'制作片头',concatenating:'拼接制作',removing_bgm:'处理背景音乐',rendering_random:'制作中',uploading:'上传中',verifying:'校验成片',completed:'制作完成',failed:'失败',recovery_required:'等待恢复',prefetching:'预下载素材',prefetched:'素材已就绪',verification:'校验成片',waiting_gpu_capacity:'等待制作资源',future_internal_stage:'处理中'};
  const items=Object.entries(stages).map(([stage],i)=>({id:`ve_stage_${i}`,status:'running',stage,progress:{}}));
  const app=await browser(async url=>url.endsWith('/tasks')?response({items,total:items.length}):url.includes('/assets?')?response({items:[{id:'va_stage',name:'素材',status:'starting',size_bytes:1}],total:1}):null);
  const labels=Array.from(app.field('taskList').innerHTML.matchAll(/<p class="micro">([^<]*)<\/p>/g),match=>match[1]);
  assert.deepEqual(labels,Object.values(stages));
  assert.match(app.field('assetGrid').innerHTML,/准备制作/);
  assert.doesNotMatch(app.field('taskList').innerHTML,/future_internal_stage|rendering_random/);
});

test('numeric task progress stays compatible while invalid or incomplete metric DTOs do not invent progress',async()=>{
  const tasks=[
    {id:'ve_fraction',status:'running',stage:'rendering',progress:0.5},
    {id:'ve_integer',status:'running',stage:'rendering',progress:42},
    {id:'ve_complete',status:'completed',stage:'completed',progress:100},
    {id:'ve_bad',status:'running',stage:'rendering_random',progress:{out_time_seconds:-1,duration_seconds:0,completed_steps:'3',total_steps:5,percent:88}},
    {id:'ve_partial',status:'running',stage:'downloading',progress:{download_bytes:1048576}},
    {id:'ve_unknown',status:'running',stage:'rendering_random',progress:{out_time_seconds:1,duration_seconds:0}},
  ];
  const app=await browser(async url=>url.endsWith('/tasks')?response({items:tasks,total:tasks.length}):null);
  const labels=Array.from(app.field('taskList').innerHTML.matchAll(/<p class="micro">([^<]*)<\/p>/g),match=>match[1]);
  assert.deepEqual(labels,['制作中 · 50%','制作中 · 42%','制作完成 · 100%','制作中','下载素材 · 当前阶段：已下载 1.0 MiB','制作中']);
});

test('real event workflow keeps two dramas independent through probe, editing, batch submit and refreshed output links',async()=>{
  const id=(prefix,n)=>prefix+String(n).padStart(32,'0');
  const asset={id:id('va_',1),source_id:id('va_',1),kind:'asset',name:'共享素材',status:'ready',can_manage:true,metadata:{duration_ms:8000,width:1920,height:1080,has_audio:true},preview_url:'https://cos.example.test/asset',expires_at:Math.floor(Date.now()/1000)+900};
  const dramas=[{app_id:'2310',content_id:'AbCd123456',title:'第一部'},{app_id:'2310',content_id:'XyZ0987654',title:'第二部'}];
  const source=(n,drama)=>({id:id('vep_',n),source_id:id('vep_',n),kind:'episode',...drama,name:`${drama.title}第${n}集`,episode_number:n,status:n===3?'ready':'unprobed',metadata:n===3?{duration_ms:30000,width:720,height:1280,has_audio:true}:{},preview_url:`https://cos.example.test/episode-${n}`,expires_at:Math.floor(Date.now()/1000)+900});
  const sources=new Map([asset,...[1,2,3].map(n=>source(n,dramas[n===3?1:0]))].map(s=>[s.id,s]));
  const projectId=id('vp_',1),submissions=[],batches=new Map();let remote=null,tasks=[];
  const app=await browser(async(url,options)=>{
    const method=options.method||'GET',body=options.body&&JSON.parse(options.body);
    if(url.includes('/assets?'))return response({items:[asset],total:1});
    if(url.endsWith('/catalog/products'))return response({items:[{app_id:'2310',app:'DramaWave',label:'DramaWave · 英语'}]});
    if(url.includes('/catalog/dramas?'))return response({items:dramas});
    if(url.includes('/catalog/episodes?')){const contentId=new URL(url,'https://local.test').searchParams.get('content_id');return response({items:[...sources.values()].filter(s=>s.content_id===contentId)});}
    if(url.endsWith('/catalog/probe')){assert.deepEqual(body.source_ids,[id('vep_',1),id('vep_',2)]);const items=body.source_ids.map(sid=>{const s=sources.get(sid);s.status='probing';s.probe_task_id=id('ve_',100+s.episode_number);return s;});return response({items});}
    if(url.includes('/sources/'))return response({item:sources.get(url.split('/').at(-1))});
    if(url.endsWith('/projects')&&method==='POST'){assert.deepEqual(Object.keys(body).sort(),['asset_id','dramas','name','operation_id']);remote={...body,id:projectId,version:1};delete remote.operation_id;return response({item:remote});}
    if(url.endsWith(`/projects/${projectId}`)&&method==='PATCH'){assert.equal(body.expected_version,remote.version);remote={...body,id:projectId,version:remote.version+1};delete remote.expected_version;return response({item:remote});}
    if(url.endsWith('/projects'))return response({items:remote?[remote]:[],total:remote?1:0});
    if(url.endsWith(`/projects/${projectId}/render`)){assert.equal(body.expected_version,remote.version);submissions.push(body);if(!batches.has(body.operation_id)){const batch_id=id('vb_',batches.size+1),project_version_id=id('vv_',remote.version);tasks=remote.dramas.map((drama,index)=>({id:id('ve_',index+1),project_id:projectId,batch_id,project_version_id,output_id:id('vo_',index+1),drama_title:drama.title,status:'queued',stage:'queued',progress:0,output:null}));batches.set(body.operation_id,JSON.parse(JSON.stringify({batch_id,project_version_id,items:tasks,draft:remote})));}return response(batches.get(body.operation_id));}
    if(url.endsWith('/tasks'))return response({items:tasks,total:tasks.length});
    if(url.includes('/tasks/')){const task=tasks.find(t=>t.id===url.split('/').at(-1));assert.ok(task);return response({item:{...task,status:'completed',stage:'completed',progress:100,output:{id:task.output_id,preview_url:'https://cos.example.test/output-fresh',download_url:'https://cos.example.test/download-fresh',expires_at:Math.floor(Date.now()/1000)+900,metadata:{duration_ms:27000,width:720,height:1280,fps:30,has_audio:true}}}});}
  });
  const clips=()=>Array.from(app.field('timeline').innerHTML.matchAll(/data-clip-id="([^"]+)"/g),match=>match[1]);
  const chooseClip=async index=>{await app.clickData({clipId:clips()[index]},'timeline');await tick();};
  const change=async(id,value)=>{app.field(id).value=value;await app.field(id).emit('change');};
  const seek=async milliseconds=>{app.field('playhead').value=milliseconds;await app.field('playhead').emit('input');await tick();};
  await app.rename('两剧验收');await app.clickData({pickAsset:asset.id});
  await app.field('addDrama').click();app.field('productPicker').value='2310';app.field('dramaSearch').value='AbCd123456';await app.field('searchDramas').click();
  await app.clickData({catalogIndex:'0'},'catalogDramas');app.checkEpisodes([id('vep_',1),id('vep_',2)]);await app.field('addEpisodes').click();
  assert.equal(app.field('dramaCount').textContent,'0 部');assert.match(app.field('catalogStatus').textContent,/媒体校验/);
  for(const n of [1,2])Object.assign(sources.get(id('vep_',n)),{status:'ready',metadata:{duration_ms:30000,width:720,height:1280,has_audio:n===1}});
  await app.poll();await app.field('addEpisodes').click();assert.equal(app.field('dramaCount').textContent,'1 部');
  await app.field('addDrama').click();app.field('productPicker').value='2310';app.field('dramaSearch').value='XyZ0987654';await app.field('searchDramas').click();await app.clickData({catalogIndex:'1'},'catalogDramas');app.checkEpisodes([id('vep_',3)]);await app.field('addEpisodes').click();
  assert.equal(app.field('dramaCount').textContent,'2 部');
  app.field('commonPosition').value='start';app.field('commonTransition').value='cut';await app.field('applyCommon').click();
  await app.clickData({dramaIndex:'0'},'dramaList');await chooseClip(1);await change('clipStart',2);await change('clipEnd',20);
  await seek(12000);await app.field('splitClip').click();await chooseClip(2);await seek(15000);await app.field('splitClip').click();await chooseClip(2);await app.field('removeClip').click();
  await chooseClip(3);await app.field('moveLeft').click();await app.field('removeEpisode').click();assert.equal(clips().length,3);await app.field('undoEdit').click();assert.equal(clips().length,4);
  await chooseClip(1);await app.field('insertAsset').click();app.field('insertStart').value=.5;app.field('insertEnd').value=1.5;app.field('insertPosition').value='after';await app.field('confirmInsert').click();await app.fit('cover');app.field('clipSound').checked=false;await app.field('clipSound').emit('change');
  app.field('commonPosition').value='none';app.field('commonTransition').value='fade';await app.field('applyCommon').click();
  await app.field('saveProject').click();assert.ok(remote);const frozen=JSON.parse(JSON.stringify(remote));
  assert.deepEqual(remote.dramas[0].clips.filter(c=>c.source_id===id('vep_',1)).map(c=>[c.start_ms,c.end_ms]),[[2000,6000],[9000,20000]]);
  assert.deepEqual(remote.dramas[1].clips.map(c=>[c.source_id,c.start_ms,c.end_ms]),[[asset.id,0,8000],[id('vep_',3),0,30000]]);
  assert.equal(remote.dramas[0].clips.filter(c=>c.source_id===asset.id).length,2);
  assert.ok(remote.dramas[0].clips.some(c=>c.source_id===asset.id&&c.fit==='cover'&&c.mute&&c.start_ms===500));
  assert.equal(remote.dramas[0].clips.at(-1).transition,'cut');assert.equal(app.field('validationErrors').textContent,'');
  await app.field('renderProject').click();assert.equal(tasks.length,2);assert.match(app.field('taskList').innerHTML,/第一部/);assert.match(app.field('taskList').innerHTML,/第二部/);
  await app.field('renderProject').click();assert.equal(submissions[1].operation_id,submissions[0].operation_id);assert.equal(batches.size,1);
  await app.clickData({previewTask:tasks[0].id});assert.equal(app.field('mediaPlayer').src,'https://cos.example.test/output-fresh');assert.equal(app.field('mediaDialog').open,true);assert.match(app.field('mediaExpiry').textContent,/链接有效期至/);
  await app.clickData({downloadTask:tasks[0].id});assert.equal(app.createdElements.at(-1).href,'https://cos.example.test/download-fresh');assert.equal(app.createdElements.at(-1).rel,'noopener noreferrer');
  await app.field('undoEdit').click();await app.field('renderProject').click();assert.equal(batches.size,2);assert.notEqual(submissions.at(-1).operation_id,submissions[0].operation_id);assert.ok(submissions.at(-1).expected_version>submissions[0].expected_version);assert.deepEqual(batches.get(submissions[0].operation_id).draft,frozen);
});

test('adding episodes resumes the original probe and keeps tracking even while the source remains failed',async()=>{
  const source={id:'vep_waiting',source_id:'vep_waiting',kind:'episode',app_id:'2310',content_id:'AbCd123456',status:'failed',name:'第1集',metadata:{},probe_task_id:'ve_original'};let resumed=false,probeStatus='unknown';
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/catalog/products'))return response({items:[{app_id:'2310',app:'DramaWave'}]});
    if(url.includes('/catalog/dramas?'))return response({items:[{app_id:'2310',content_id:'AbCd123456',title:'待校验剧'}]});
    if(url.includes('/catalog/episodes?'))return response({items:[source]});
    if(url.endsWith('/tasks/ve_original'))return response({item:{id:'ve_original',status:probeStatus,kind:'probe'}});
    if(url.endsWith('/tasks/ve_original/resume')){assert.equal(options.method,'POST');resumed=true;probeStatus='resume_queued';return response({item:{id:'ve_original',status:probeStatus,kind:'probe'}});}
    if(url.endsWith('/sources/vep_waiting'))return response({item:source});
  });
  await app.field('addDrama').click();app.field('productPicker').value='2310';app.field('dramaSearch').value='AbCd123456';await app.field('searchDramas').click();await app.clickData({catalogIndex:'0'},'catalogDramas');app.checkEpisodes([source.id]);await app.field('addEpisodes').click();
  assert.equal(resumed,true);assert.equal(app.calls.some(call=>call.url.endsWith('/catalog/probe')),false);assert.match(app.field('catalogStatus').textContent,/恢复/);
  await app.poll();assert.equal(app.field('dramaCount').textContent,'0 部');
  probeStatus='completed';source.status='ready';source.metadata={duration_ms:10000,width:720,height:1280};await app.poll();assert.match(app.field('catalogEpisodes').innerHTML,/10 秒/);
  await app.field('addEpisodes').click();assert.equal(app.field('dramaCount').textContent,'1 部');
});

test('actual expires_at refreshes a timeline source and leaving the project rejects its late preview response',async()=>{
  const source={id:'vep_late',source_id:'vep_late',kind:'episode',status:'ready',metadata:{duration_ms:10000},preview_url:'https://cos.example.test/expired',expires_at:Math.floor(Date.now()/1000)-1};
  const project={id:'vp_late',name:'原项目',version:1,asset_id:'',dramas:[{app_id:'2310',content_id:'AbCd123456',title:'原剧',clips:[{id:'clip_late',source_id:source.id,start_ms:0,end_ms:10000,fit:'contain',mute:false,transition:'cut'}]}]};
  let sourceReads=0,finish;const late=new Promise(resolve=>{finish=resolve;});
  const app=await browser(async url=>{
    if(url.endsWith('/projects'))return response({items:[project],total:1});
    if(url.endsWith('/projects/vp_late'))return response({item:project});
    if(url.endsWith('/sources/vep_late')){if(++sourceReads===1)return response({item:source});await late;return response({item:{...source,preview_url:'https://cos.example.test/late',expires_at:Math.floor(Date.now()/1000)+900}});}
  });
  app.field('projectPicker').value=project.id;await app.field('projectPicker').emit('change');await app.clickData({clipId:'clip_late'},'timeline');await tick();assert.equal(sourceReads,2);
  await app.field('newProject').click();finish();for(let i=0;i<4;i++)await tick();
  assert.equal(app.field('projectName').value,'未命名剪辑项目');assert.equal(app.field('previewA').src,undefined);assert.equal(app.field('previewMessage').textContent,'添加素材与剧集后即可预览');
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
