'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const crypto=require('node:crypto');
const test=require('node:test');
const script=fs.readFileSync(path.join(__dirname,'../static/video-editing.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../static/video-editing.html'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const response=(payload,status=200)=>{const wire=JSON.stringify(payload);return {ok:status>=200&&status<300,status,json:async()=>JSON.parse(wire)};};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const settle=async()=>{for(let i=0;i<6;i++)await tick();};

// A minimal browser contract harness exercises real event handlers and asynchronous API calls.
// It intentionally does not reproduce the editing or save implementation.
async function browser(handler,extras={}){
  const elements=new Map(),calls=[],timers=new Map(),intervals=[],documentEvents={},createdElements=[],staticNodes=[];let nextTimer=1,now=Date.now();
  const dataKey=name=>name.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
  const attrs=markup=>Object.fromEntries(Array.from(markup.matchAll(/([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g),m=>[m[1],m[2]??m[3]??m[4]??'']));
  function matches(el,selector){
    return selector.split(',').some(raw=>{let s=raw.trim();const checked=s.endsWith(':checked');if(checked)s=s.slice(0,-8);
      const attr=s.match(/^\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]$/);
      const found=attr?el.hasAttribute(attr[1])&&(attr[2]===undefined||el.getAttribute(attr[1])===attr[2]):s[0]==='.'?el.classList.contains(s.slice(1)):s[0]==='#'?el.id===s.slice(1):el.tagName.toLowerCase()===s;
      return found&&(!checked||el.checked);});
  }
  class Element{
    constructor(id,tag='div'){this.id=id;this.tagName=tag.toUpperCase();this.value='';this.textContent='';this._html='';this.dataset={};this.style={};this.listeners={};this.files=[];this.attributes=new Map();this.children=[];this.disabled=false;this.checked=false;this.open=false;this._classes=new Set();
      this.classList={add:(...names)=>names.forEach(n=>this._classes.add(n)),remove:(...names)=>names.forEach(n=>this._classes.delete(n)),contains:name=>this._classes.has(name),toggle:(name,force)=>{const add=force===undefined?!this._classes.has(name):Boolean(force);if(add)this._classes.add(name);else this._classes.delete(name);return add;}};}
    get className(){return [...this._classes].join(' ');}set className(value){this._classes=new Set(String(value).split(/\s+/).filter(Boolean));}
    get textContent(){return this._text;}set textContent(value){this._text=String(value);this._html=this._text.replaceAll('&','&amp;').replaceAll('<','&lt;');this.children=[];}
    get innerHTML(){return this._html;}set innerHTML(value){this._html=String(value);this._text=this._html.replace(/<[^>]+>/g,'');this.children=parseNodes(this._html,this);}
    addEventListener(type,fn){(this.listeners[type]||(this.listeners[type]=[])).push(fn);}
    emit(type,event={}){event.target=event.target||this;event.currentTarget=this;event.defaultPrevented=Boolean(event.defaultPrevented);event.preventDefault=event.preventDefault||function(){this.defaultPrevented=true;};return Promise.all((this.listeners[type]||[]).map(fn=>fn(event)));}
    closest(selectors){for(let el=this;el;el=el.parentElement)if(matches(el,selectors))return el;return null;}
    setAttribute(name,value){value=String(value);this.attributes.set(name,value);if(name==='class')this.className=value;else if(name==='disabled')this.disabled=true;else if(name==='checked')this.checked=true;else if(name==='value')this.value=value;else if(name.startsWith('data-'))this.dataset[dataKey(name.slice(5))]=value;}
    getAttribute(name){if(name==='class')return this.className;if(name==='disabled')return this.disabled?'':null;if(name.startsWith('data-'))return this.dataset[dataKey(name.slice(5))]??null;return this.attributes.get(name)??null;}
    hasAttribute(name){return this.getAttribute(name)!==null;}
    removeAttribute(name){this.attributes.delete(name);if(name==='disabled')this.disabled=false;else if(name==='checked')this.checked=false;else if(name==='src')delete this.src;else if(name.startsWith('data-'))delete this.dataset[dataKey(name.slice(5))];}
    querySelectorAll(selector){return this.children.filter(el=>matches(el,selector));}
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
    pause(){}load(){}focus(){}click(){return this.disabled?Promise.resolve():this.emit('click');}showModal(){this.open=true;}close(){if(!this.open)return Promise.resolve();this.open=false;return this.emit('close');}play(){return Promise.resolve();}
  }
  function parseNodes(markup,parent){return Array.from(markup.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi),m=>{const a=attrs(m[2]);const el=new Element(a.id,m[1]);el.parentElement=parent||null;for(const [name,value]of Object.entries(a))el.setAttribute(name,value);const tail=markup.slice(m.index+m[0].length),end=tail.indexOf(`</${m[1]}>`);if(end>=0){el._html=tail.slice(0,end);el._text=el._html.replace(/<[^>]+>/g,'');}return el;});}
  for(const el of parseNodes(html)){staticNodes.push(el);if(el.id)elements.set(el.id,el);}
  const nodes=()=>[...staticNodes,...[...elements.values()].flatMap(el=>el.children)];
  const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},querySelectorAll:selector=>nodes().filter(el=>matches(el,selector)),
    querySelector:selector=>nodes().find(el=>matches(el,selector))||null,addEventListener(type,fn){(documentEvents[type]||(documentEvents[type]=[])).push(fn);},createElement(tag){const el=new Element(undefined,tag);createdElements.push(el);return el;}};
  const fetch=async(url,options={})=>{calls.push({url,options,body:options.body?JSON.parse(options.body):null});const custom=await handler(url,options,calls);if(custom)return custom;
    if(url==='/api/ui/topbar')return response({authenticated:true,user:{name:'Tester',role:'admin',tenant_key:'tenant'}});
    if(url.endsWith('/config'))return response({item:{enabled:true,intake_enabled:true,worker_configured:true,cos_configured:true}});
    if(url.includes('/assets?')||url.endsWith('/projects')||url.endsWith('/tasks'))return response({items:[],total:0});
    throw new Error(`Unexpected request ${options.method||'GET'} ${url}`);
  };
  const window={document,crypto:crypto.webcrypto,addEventListener(){},QuickNav:{render:async()=>{}},UiTopbar:{render(){},handleAuthAction:async()=>{}}};
  class ClockDate extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  const context={window,document,fetch,URL,URLSearchParams,AbortController,Date:ClockDate,location:{search:'',pathname:'/video-editing.html'},performance:{now:()=>now},requestAnimationFrame:()=>0,cancelAnimationFrame(){},setInterval(fn){intervals.push(fn);return intervals.length;},clearInterval(id){intervals[id-1]=null;},setTimeout(fn){const id=nextTimer++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},console,...extras};
  vm.runInNewContext(script,context,{filename:'video-editing.js'});for(let i=0;i<6;i++)await tick();
  return {elements,calls,timers,createdElements,field:id=>document.getElementById(id),dataElement:dataset=>nodes().find(el=>Object.entries(dataset).every(([key,value])=>el.dataset[key]===value)),rename:async name=>{const input=document.getElementById('projectName');input.value=name;await input.emit('change');},response,
    clickData:async(dataset,container)=>{const target=nodes().find(el=>Object.entries(dataset).every(([key,value])=>el.dataset[key]===value))||Object.assign(new Element(),{dataset});if(target.disabled)return;await target.click();return container?document.getElementById(container).emit('click',{target}):Promise.all((documentEvents.click||[]).map(fn=>fn({target,currentTarget:document})));},
    checkEpisodes(ids){const inputs=document.querySelectorAll('.episode-select');assert.ok(ids.every(id=>inputs.some(el=>el.value===id)),'selected episode must exist in rendered DOM');for(const el of inputs)el.checked=ids.includes(el.value);},
    closeDialog:async(id,escape=false)=>{const dialog=document.getElementById(id);if(escape){const event={defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};await dialog.emit('cancel',event);if(!event.defaultPrevented)await dialog.close();}else await nodes().find(el=>el.dataset.close===id).click();await settle();},
    advance:milliseconds=>{now+=milliseconds;},fit:fit=>document.querySelectorAll('[data-fit]').find(el=>el.dataset.fit===fit).click(),poll:()=>Promise.all(intervals.filter(Boolean).map(fn=>fn())),
  };
}

async function catalogFixture(override=async()=>null){
  const dramas=[{app_id:'2310',content_id:'DramaAlpha',title:'第一部'},{app_id:'2310',content_id:'DramaBeta',title:'第二部'}];
  const sources=new Map([1,2,3,4].map(n=>{const drama=dramas[n===4?1:0];const source={id:`vep_async_${n}`,source_id:`vep_async_${n}`,kind:'episode',...drama,name:`第${n}集`,episode_number:n,status:'unprobed',metadata:{}};return [source.id,source];}));
  const probes=[],resumes=[],saved=[];let project=null;
  const fixture={dramas,sources,probes,resumes,saved,ready(...numbers){for(const n of numbers)Object.assign(sources.get(`vep_async_${n}`),{status:'ready',metadata:{duration_ms:10000,width:720,height:1280,has_audio:true}});}};
  const app=await browser(async(url,options,calls)=>{
    const result=await override(url,options,calls,fixture);if(result)return result;
    const method=options.method||'GET',body=options.body&&JSON.parse(options.body);
    if(url.endsWith('/catalog/products'))return response({items:[{app_id:'2310',label:'DramaWave'}]});
    if(url.includes('/catalog/dramas?'))return response({items:dramas});
    if(url.includes('/catalog/episodes?'))return response({items:[...sources.values()].filter(source=>source.content_id===new URL(url,'https://test').searchParams.get('content_id'))});
    if(url.endsWith('/catalog/probe')){probes.push(body.source_ids);for(const id of body.source_ids)Object.assign(sources.get(id),{status:'probing',probe_task_id:`probe_${id}`});return response({items:body.source_ids.map(id=>sources.get(id))});}
    if(url.includes('/sources/'))return response({item:sources.get(url.split('/').at(-1))});
    if(url.includes('/tasks/')&&url.endsWith('/resume')){resumes.push(url);return response({item:{id:url.split('/').at(-2),status:'resume_queued'}});}
    if(url.includes('/tasks/')){const id=url.split('/').at(-1),source=[...sources.values()].find(item=>item.probe_task_id===id);return response({item:{id,kind:'probe',status:source?.status==='failed'?'failed':source?.status==='ready'?'completed':'running',error_message:source?.error_message}});}
    if(url.endsWith('/projects')&&method==='POST'){project={...body,id:'vp_async',version:1};delete project.operation_id;saved.push(JSON.parse(JSON.stringify(project)));return response({item:project});}
    if(url.endsWith('/projects/vp_async')&&method==='PATCH'){project={...body,id:'vp_async',version:project.version+1};delete project.expected_version;saved.push(JSON.parse(JSON.stringify(project)));return response({item:project});}
    if(url.endsWith('/projects/vp_async'))return response({item:project});
    if(url.endsWith('/projects'))return response({items:project?[project]:[]});
  });
  return Object.assign(app,fixture,{async selectDrama(index=0){await app.field('addDrama').click();app.field('productPicker').value='2310';app.field('dramaSearch').value='DramaAlpha';await app.field('searchDramas').click();await app.clickData({catalogIndex:String(index)},'catalogDramas');}});
}

test('asset chooser opens with visible feedback before its delayed fetch and suppresses a second click',async()=>{
  const gate=deferred();let reads=0;
  const app=await browser(async url=>url.includes('/assets?')&&++reads===2?gate.promise:null);
  const opening=app.field('chooseAsset').click();const repeated=app.field('chooseAsset').click();await settle();
  try{
    assert.equal(app.field('assetDialog').open,true);
    assert.match(app.field('assetPicker').innerHTML,/加载|读取/);
    assert.equal(app.field('chooseAsset').getAttribute('aria-busy'),'true');
    assert.equal(app.field('chooseAsset').disabled,true);assert.equal(reads,2);
  }finally{gate.resolve(response({items:[],total:0}));await Promise.all([opening,repeated]);}
  assert.notEqual(app.field('chooseAsset').getAttribute('aria-busy'),'true');assert.equal(app.field('chooseAsset').disabled,false);
});

test('catalog product failure keeps the open dialog actionable and releases its immediate busy state',async()=>{
  const gate=deferred();let reads=0;
  const app=await browser(async url=>url.endsWith('/catalog/products')?(reads++,gate.promise):null);
  const opening=app.field('addDrama').click();const repeated=app.field('addDramaBottom').click();await settle();
  try{
    assert.equal(app.field('dramaDialog').open,true);assert.equal(reads,1);
    assert.equal(app.field('addDrama').getAttribute('aria-busy'),'true');assert.equal(app.field('addDrama').disabled,true);
    assert.match(app.field('catalogStatus').textContent,/加载|读取/);
  }finally{gate.resolve(response({code:'catalog_unavailable',error:'产品目录暂不可用'},503));await Promise.all([opening,repeated]);}
  assert.equal(app.field('dramaDialog').open,true);assert.equal(app.field('addDrama').disabled,false);
  assert.notEqual(app.field('addDrama').getAttribute('aria-busy'),'true');assert.equal(app.field('toast').classList.contains('hidden'),false);
  assert.match(app.field('toast').textContent,/产品目录暂不可用/);
});

test('newer search wins out of order and clearing the product invalidates a still-running search',async()=>{
  const gates=new Map();const app=await catalogFixture(async url=>{if(!url.includes('/catalog/dramas?'))return null;const query=new URL(url,'https://test').searchParams.get('q');const gate=deferred();gates.set(query,gate);return gate.promise;});
  await app.field('addDrama').click();app.field('productPicker').value='2310';
  app.field('dramaSearch').value='Alpha';const first=app.field('dramaSearch').emit('keydown',{key:'Enter'});await settle();
  assert.match(app.field('catalogDramas').innerHTML,/正在|加载|搜索|查找/);
  app.field('dramaSearch').value='Beta';const second=app.field('dramaSearch').emit('keydown',{key:'Enter'});await settle();
  gates.get('Beta').resolve(response({items:[{app_id:'2310',content_id:'Beta',title:'较新结果'}]}));await second;
  gates.get('Alpha').resolve(response({items:[{app_id:'2310',content_id:'Alpha',title:'过时结果'}]}));await first;
  assert.match(app.field('catalogDramas').innerHTML,/较新结果/);assert.doesNotMatch(app.field('catalogDramas').innerHTML,/过时结果/);
  app.field('dramaSearch').value='Gamma';const last=app.field('dramaSearch').emit('keydown',{key:'Enter'});await settle();
  app.field('productPicker').value='';await app.field('productPicker').emit('change');
  gates.get('Gamma').resolve(response({items:[{app_id:'2310',content_id:'Gamma',title:'已清空产品的结果'}]}));await last;
  assert.doesNotMatch(app.field('catalogDramas').innerHTML,/已清空产品的结果/);assert.equal(app.field('addEpisodes').disabled,true);
});

test('newer selected drama wins when its episode response arrives before the old drama',async()=>{
  const alpha=deferred(),beta=deferred();const app=await catalogFixture(async url=>url.includes('/catalog/episodes?')?(url.includes('DramaAlpha')?alpha.promise:beta.promise):null);
  await app.field('addDrama').click();app.field('productPicker').value='2310';app.field('dramaSearch').value='Drama';await app.field('searchDramas').click();
  const first=app.clickData({catalogIndex:'0'},'catalogDramas');await settle();
  assert.match(app.field('catalogEpisodes').innerHTML,/正在|加载/);assert.equal(app.field('addEpisodes').disabled,true);
  const second=app.clickData({catalogIndex:'1'},'catalogDramas');await settle();
  beta.resolve(response({items:[app.sources.get('vep_async_4')]}));await second;
  alpha.resolve(response({items:[app.sources.get('vep_async_1')]}));await first;
  assert.match(app.field('catalogEpisodes').innerHTML,/vep_async_4/);assert.doesNotMatch(app.field('catalogEpisodes').innerHTML,/vep_async_1/);
});

test('returning to an in-flight search reuses its read but makes that query the newest user intent',async()=>{
  const gates={Alpha:deferred(),Beta:deferred()},reads={Alpha:0,Beta:0};
  const app=await catalogFixture(async url=>{if(!url.includes('/catalog/dramas?'))return null;const query=new URL(url,'https://test').searchParams.get('q');reads[query]++;return gates[query].promise;});
  await app.field('addDrama').click();app.field('productPicker').value='2310';
  app.field('dramaSearch').value='Alpha';const first=app.field('dramaSearch').emit('keydown',{key:'Enter'});await settle();
  app.field('dramaSearch').value='Beta';const second=app.field('dramaSearch').emit('keydown',{key:'Enter'});await settle();
  app.field('dramaSearch').value='Alpha';const third=app.field('dramaSearch').emit('keydown',{key:'Enter'});await settle();
  gates.Beta.resolve(response({items:[{app_id:'2310',content_id:'Beta',title:'已离开的乙查询'}]}));await second;
  assert.doesNotMatch(app.field('catalogDramas').innerHTML,/已离开的乙查询/);
  gates.Alpha.resolve(response({items:[{app_id:'2310',content_id:'Alpha',title:'最终选择的甲查询'}]}));await Promise.all([first,third]);
  assert.deepEqual(reads,{Alpha:1,Beta:1});assert.match(app.field('catalogDramas').innerHTML,/最终选择的甲查询/);
  assert.doesNotMatch(app.field('catalogDramas').innerHTML,/已离开的乙查询/);
});

test('one click waits for all three probes and adds them exactly once without another confirmation',async()=>{
  const app=await catalogFixture();await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);
  const first=app.field('addEpisodes').click(),second=app.field('addEpisodes').click();await Promise.all([first,second]);
  assert.equal(app.probes.length,1);assert.equal(app.field('addEpisodes').disabled,true);assert.equal(app.field('addEpisodes').getAttribute('aria-busy'),'true');
  assert.equal(app.field('cancelAddEpisodes').classList.contains('hidden'),false);assert.match(app.field('catalogStatus').textContent,/自动添加/);
  app.ready(1);await app.poll();assert.match(app.field('catalogStatus').textContent,/1\s*\/\s*3/);assert.equal(app.field('dramaCount').textContent,'0 部');
  app.ready(2);await app.poll();assert.match(app.field('catalogStatus').textContent,/2\s*\/\s*3/);assert.equal(app.field('dramaCount').textContent,'0 部');
  app.ready(3);await app.poll();await settle();assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(app.field('dramaDialog').open,false);
  await app.poll();await app.field('saveProject').click();assert.equal(app.probes.length,1);
  assert.deepEqual(app.saved.at(-1).dramas[0].clips.map(clip=>clip.source_id),['vep_async_1','vep_async_2','vep_async_3']);
});

for(const action of ['close','escape','cancel'])test(`pending episode addition cannot mutate ${action==='cancel'?'the current':'a new'} project after ${action}`,async()=>{
  const gate=deferred();let probeBody;
  const app=await catalogFixture(async(url,options)=>{if(url.endsWith('/catalog/probe')){probeBody=JSON.parse(options.body);return gate.promise;}return null;});
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);const adding=app.field('addEpisodes').click();await settle();
  assert.deepEqual(probeBody.source_ids,['vep_async_1','vep_async_2','vep_async_3']);
  if(action==='cancel')await app.field('cancelAddEpisodes').click();else await app.closeDialog('dramaDialog',action==='escape');
  assert.notEqual(app.field('addEpisodes').getAttribute('aria-busy'),'true');
  if(action!=='cancel')await app.field('newProject').click();app.ready(1,2,3);
  gate.resolve(response({items:[1,2,3].map(n=>app.sources.get(`vep_async_${n}`))}));await adding;await app.poll();await settle();
  assert.equal(app.field('projectName').value,'未命名剪辑项目');assert.equal(app.field('dramaCount').textContent,'0 部');
  assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.equal(app.saved.length,0);
  if(action!=='cancel')assert.equal(app.field('dramaDialog').open,false);
});

test('lost probe response requires an explicit retry that reconciles the source without posting another probe',async()=>{
  let posts=0;const app=await catalogFixture(async(url,options,_calls,fixture)=>{
    if(url.endsWith('/catalog/probe')){posts++;for(const id of JSON.parse(options.body).source_ids)Object.assign(fixture.sources.get(id),{status:'probing',probe_task_id:`accepted_${id}`});throw new Error('response lost after server acceptance');}return null;
  });
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();
  for(let i=0;i<3;i++)await app.poll();assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'0 部');
  assert.match(app.field('catalogStatus').textContent,/核实|确认|未知|等待|稍后/);
  app.ready(1,2,3);await app.poll();await settle();assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'0 部');
  await app.field('addEpisodes').click();await settle();assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'1 部');
});

test('probe polling stays single-flight and one failed episode never causes partial auto-add or automatic retry',async()=>{
  let delay=false,active=0,maxActive=0;const gates=[];
  const app=await catalogFixture(async(url,_options,_calls,fixture)=>{if(delay&&url.includes('/sources/')){active++;maxActive=Math.max(maxActive,active);const gate=deferred();gates.push({gate,id:url.split('/').at(-1)});await gate.promise;active--;return response({item:fixture.sources.get(url.split('/').at(-1))});}return null;});
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();delay=true;
  const first=app.poll(),second=app.poll();await settle();assert.ok(gates.length<=3);assert.ok(maxActive<=3);
  app.ready(1,2);Object.assign(app.sources.get('vep_async_3'),{status:'failed',error_message:'无法读取视频信息'});for(const {gate}of gates)gate.resolve();await Promise.all([first,second]);delay=false;await app.poll();
  assert.equal(app.field('dramaCount').textContent,'0 部');assert.equal(app.probes.length,1);assert.equal(app.resumes.length,0);
  assert.match(app.field('catalogStatus').textContent,/3|第3集/);assert.match(app.field('catalogStatus').textContent,/失败|无法读取/);
});

test('one failed source read does not release the polling lock while its sibling reads are still in flight',async()=>{
  const gates=new Map([1,2,3].map(n=>[`vep_async_${n}`,deferred()]));let delayed=false,reads=0,finished=false;
  const app=await catalogFixture(async(url,_options,_calls,fixture)=>{if(!delayed||!url.includes('/sources/'))return null;reads++;const id=url.split('/').at(-1);await gates.get(id).promise;return response({item:fixture.sources.get(id)});});
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();delayed=true;
  const first=app.poll().then(()=>{finished=true;});await settle();assert.equal(reads,3);
  gates.get('vep_async_1').reject(new Error('first lookup failed'));await settle();
  let second;
  try{
    assert.equal(finished,false,'poll must retain ownership until every started read settles');
    second=app.poll();await settle();assert.equal(reads,3,'a second interval must join the still-running poll');
  }finally{gates.get('vep_async_2').resolve();gates.get('vep_async_3').resolve();await Promise.all([first,second]);delayed=false;}
  app.ready(1,2,3);await app.poll();assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(app.probes.length,1);
});

for(const outcome of ['partial-4xx','malformed-2xx'])test(`a ${outcome} probe result remains unknown until explicit source reconciliation and never re-posts the batch`,async()=>{
  let posts=0;const app=await catalogFixture(async(url,_options,_calls,fixture)=>{
    if(!url.endsWith('/catalog/probe'))return null;posts++;
    Object.assign(fixture.sources.get('vep_async_1'),{status:'probing',probe_task_id:'ve_partially_accepted'});
    return outcome==='partial-4xx'?response({code:'source_not_available',error:'后续来源暂不可用'},400):response({items:[fixture.sources.get('vep_async_1')]});
  });
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();
  await app.poll();assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'0 部');
  assert.equal(app.field('addEpisodes').disabled,false,'an incomplete write response must stop automatic continuation');
  const reads=app.calls.filter(call=>call.url.includes('/sources/')).length;
  await app.field('addEpisodes').click();await app.poll();
  assert.ok(app.calls.filter(call=>call.url.includes('/sources/')).length>reads,'explicit retry must read back uncertain sources');
  assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'0 部');assert.match(app.field('catalogStatus').textContent,/核实|未知|暂不重复|确认/);
});

test('re-adding the same source after cancellation joins its outstanding probe write instead of submitting again',async()=>{
  const gate=deferred();let posts=0,secondFinished=false;
  const app=await catalogFixture(async url=>url.endsWith('/catalog/probe')?(posts++,gate.promise):null);
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);const first=app.field('addEpisodes').click();await settle();
  await app.field('cancelAddEpisodes').click();app.checkEpisodes(['vep_async_1']);
  const second=app.field('addEpisodes').click().then(()=>{secondFinished=true;});await settle();
  try{assert.equal(posts,1);assert.equal(secondFinished,false);assert.equal(app.field('addEpisodes').disabled,true);assert.equal(app.field('cancelAddEpisodes').classList.contains('hidden'),false);}
  finally{Object.assign(app.sources.get('vep_async_1'),{status:'probing',probe_task_id:'ve_original_submission'});gate.resolve(response({items:[app.sources.get('vep_async_1')]}));await Promise.all([first,second]);}
  assert.equal(app.field('dramaCount').textContent,'0 部');assert.equal(app.field('dramaDialog').open,true);
  app.ready(1);await app.poll();await app.field('saveProject').click();
  assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'1 部');assert.deepEqual(app.saved.at(-1).dramas[0].clips.map(clip=>clip.source_id),['vep_async_1']);
});

for(const phase of ['search','episodes'])test(`closing and reopening the catalog rejects the previous dialog's delayed ${phase}`,async()=>{
  const gate=deferred();const app=await catalogFixture(async url=>phase==='search'&&url.includes('/catalog/dramas?')?gate.promise:phase==='episodes'&&url.includes('/catalog/episodes?')?gate.promise:null);
  await app.field('addDrama').click();app.field('productPicker').value='2310';app.field('dramaSearch').value='DramaAlpha';
  let old;
  if(phase==='search')old=app.field('searchDramas').click();
  else{await app.field('searchDramas').click();old=app.clickData({catalogIndex:'0'},'catalogDramas');}
  await settle();await app.closeDialog('dramaDialog');await app.field('addDrama').click();
  gate.resolve(response({items:phase==='search'?[{app_id:'2310',content_id:'stale',title:'旧弹窗结果'}]:[{...app.sources.get('vep_async_1'),name:'旧弹窗剧集'}]}));await old;await settle();
  assert.equal(app.field('dramaDialog').open,true);assert.equal(app.field('addEpisodes').disabled,true);
  assert.doesNotMatch(app.field('catalogDramas').innerHTML,/旧弹窗结果/);assert.doesNotMatch(app.field('catalogEpisodes').innerHTML,/旧弹窗剧集|vep_async_1/);
});

test('task resume gives immediate feedback and a response loss never triggers an automatic retry',async()=>{
  const gate=deferred();let posts=0;const task={id:'ve_resume_once',status:'failed',drama_title:'待恢复成片',stage:'failed'};
  const app=await browser(async(url,options)=>{if(url.endsWith('/tasks'))return response({items:[task]});if(url.endsWith('/tasks/ve_resume_once/resume')){posts++;return gate.promise;}return null;});
  const first=app.clickData({resumeTask:task.id}),second=app.clickData({resumeTask:task.id});await settle();
  const button=app.dataElement({resumeTask:task.id});
  try{assert.equal(posts,1);assert.equal(button.disabled,true);assert.equal(button.getAttribute('aria-busy'),'true');assert.match(button.textContent,/正在|核实|恢复/);}
  finally{gate.reject(new Error('connection lost after acceptance'));await Promise.all([first,second]);}
  await app.poll();await app.poll();assert.equal(posts,1);assert.equal(button.disabled,false);assert.notEqual(button.getAttribute('aria-busy'),'true');
  assert.equal(app.field('toast').classList.contains('hidden'),false);assert.match(app.field('toast').textContent,/connection lost|核实|失败|异常/);
});

test('transient source reads keep a selected pending addition intact without resubmitting probes',async()=>{
  let fail=false;const app=await catalogFixture(async url=>{if(fail&&url.includes('/sources/'))throw new Error('temporary source lookup outage');return null;});
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();fail=true;
  await app.poll();assert.equal(app.field('dramaCount').textContent,'0 部');assert.equal(app.field('addEpisodes').disabled,true);
  assert.equal(app.field('cancelAddEpisodes').classList.contains('hidden'),false);assert.match(app.field('catalogStatus').textContent,/核实|稍后|网络|暂|等待/);
  fail=false;app.ready(1,2,3);await app.poll();await settle();assert.equal(app.probes.length,1);assert.equal(app.field('dramaCount').textContent,'1 部');
});

test('unknown probe submission stays fenced across dialog cancellation even if the refreshed source is still unprobed',async()=>{
  let posts=0;const app=await catalogFixture(async url=>{if(url.endsWith('/catalog/probe')){posts++;throw new Error('request outcome unknown');}return null;});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();assert.equal(posts,1);
  await app.closeDialog('dramaDialog');await app.selectDrama();app.checkEpisodes(['vep_async_1']);
  const reads=app.calls.filter(call=>call.url.endsWith('/sources/vep_async_1')).length;await app.field('addEpisodes').click();await app.poll();
  assert.ok(app.calls.filter(call=>call.url.endsWith('/sources/vep_async_1')).length>reads);
  assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'0 部');assert.match(app.field('catalogStatus').textContent,/核实|确认|未知|发起人|管理员/);
});

for(const status of [403,404])test(`an existing probe hidden by HTTP ${status} never permits a replacement probe`,async()=>{
  let reads=0;const app=await catalogFixture(async url=>{if(url.endsWith('/tasks/ve_private')){reads++;return response({code:'task_not_found',error:'该校验任务不可见'},status);}return null;});
  Object.assign(app.sources.get('vep_async_1'),{status:'failed',probe_task_id:'ve_private'});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();await app.poll();
  assert.ok(reads>0);assert.equal(app.probes.length,0);assert.equal(app.resumes.length,0);assert.equal(app.field('dramaCount').textContent,'0 部');
  assert.match(app.field('catalogStatus').textContent,/权限|发起人|管理员|不可见|核实/);
});

for(const status of [403,404])test(`a shared probing episode tolerates its hidden HTTP ${status} task and keeps observing the readable source`,async()=>{
  let privateReads=0;const app=await catalogFixture(async url=>{if(url.includes('/tasks/')){privateReads++;return response({code:'task_not_found',error:'不可见'},status);}return null;});
  Object.assign(app.sources.get('vep_async_1'),{status:'probing',probe_task_id:'ve_someone_else'});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();await app.poll();
  assert.ok(privateReads>0);assert.equal(app.probes.length,0);assert.equal(app.field('dramaCount').textContent,'0 部');assert.equal(app.field('addEpisodes').disabled,true);
  const hiddenReads=privateReads;await app.poll();await app.poll();assert.equal(privateReads,hiddenReads,'a known hidden task should not be queried on every source poll');
  app.ready(1);await app.poll();await settle();assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(app.probes.length,0);assert.equal(app.resumes.length,0);
});

for(const terminal of ['unknown','recovery_required'])test(`a readable original probe entering ${terminal} stops waiting and only an explicit retry resumes that same task once`,async()=>{
  let taskStatus='running',resumes=0;const taskId='ve_owner_original';
  const app=await catalogFixture(async(url,options)=>{
    if(url.endsWith(`/tasks/${taskId}`))return response({item:{id:taskId,kind:'probe',status:taskStatus,error_message:'原任务需要核实恢复'}});
    if(url.endsWith(`/tasks/${taskId}/resume`)){assert.equal(options.method,'POST');resumes++;taskStatus='resume_queued';return response({item:{id:taskId,kind:'probe',status:taskStatus}});}
    return null;
  });
  Object.assign(app.sources.get('vep_async_1'),{status:'probing',probe_task_id:taskId});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();assert.equal(resumes,0);
  taskStatus=terminal;await app.poll();assert.equal(app.field('addEpisodes').disabled,false);assert.match(app.field('catalogStatus').textContent,/核实|恢复/);
  await app.poll();await app.poll();assert.equal(resumes,0);assert.equal(app.probes.length,0);assert.equal(app.field('dramaCount').textContent,'0 部');
  const first=app.field('addEpisodes').click(),second=app.field('addEpisodes').click();await Promise.all([first,second]);
  assert.equal(resumes,1);assert.equal(app.field('addEpisodes').disabled,true);assert.equal(app.probes.length,0);
  taskStatus='running';await app.poll();assert.equal(resumes,1);assert.equal(app.field('dramaCount').textContent,'0 部');
  taskStatus='completed';app.ready(1);await app.poll();await settle();assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(resumes,1);assert.equal(app.probes.length,0);
});

test('opening a project shows its timeline before slow source reads while keeping editing and rendering locked',async()=>{
  const projectGate=deferred(),sourceGate=deferred();let renders=0;
  const project={id:'vp_source_loading',name:'媒体加载中的项目',version:3,asset_id:'va_loading',dramas:[{app_id:'2310',content_id:'DramaAlpha',title:'加载中的短剧',clips:[{id:'clip_loading',source_id:'vep_loading',start_ms:0,end_ms:10000,fit:'contain',mute:false,transition:'cut'}]}]};
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/projects'))return response({items:[project]});
    if(url.endsWith('/projects/vp_source_loading'))return projectGate.promise;
    if(url.includes('/sources/')){await sourceGate.promise;const id=url.split('/').at(-1);return response({item:{id,source_id:id,kind:id.startsWith('va_')?'asset':'episode',status:'ready',metadata:{duration_ms:10000,width:720,height:1280}}});}
    if(url.endsWith('/render')&&options.method==='POST'){renders++;return response({items:[]});}
    return null;
  });
  app.field('projectPicker').value=project.id;const opening=app.field('projectPicker').emit('change');await settle();
  assert.equal(app.field('projectPicker').getAttribute('aria-busy'),'true');assert.match(app.field('saveStatus').textContent,/正在打开/);
  projectGate.resolve(response({item:project}));await settle();
  try{
    assert.equal(app.field('projectName').value,project.name);assert.match(app.field('timeline').innerHTML,/clip_loading/);
    assert.match(app.field('saveStatus').textContent,/已打开.*媒体信息/);assert.equal(app.field('trimStart').disabled,true);assert.equal(app.field('renderProject').disabled,true);
    await app.field('renderProject').click();assert.equal(renders,0);
  }finally{sourceGate.resolve();await opening;}
  assert.equal(app.field('trimStart').disabled,false);assert.equal(app.field('renderProject').disabled,false);
  assert.notEqual(app.field('projectPicker').getAttribute('aria-busy'),'true');assert.match(app.field('saveStatus').textContent,/v3/);
});

test('a successful save releases draft flushing before a delayed project-list refresh completes',async()=>{
  const listGate=deferred();let lists=0,remote,saveFinished=false,leaveFinished=false;
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/projects')&&options.method==='POST'){const body=JSON.parse(options.body);remote={...body,id:'vp_saved_slow_list',version:1};delete remote.operation_id;return response({item:remote});}
    if(url.endsWith('/projects'))return ++lists===1?response({items:[]}):listGate.promise;
    return null;
  });
  await app.rename('已写入的草稿');const save=app.field('saveProject').click().then(()=>{saveFinished=true;});await settle();
  try{
    assert.equal(remote.name,'已写入的草稿');assert.equal(lists,2);assert.equal(saveFinished,true);
    assert.equal(app.field('saveProject').disabled,false);assert.notEqual(app.field('saveProject').getAttribute('aria-busy'),'true');
    const leave=app.field('newProject').click().then(()=>{leaveFinished=true;});await settle();
    assert.equal(leaveFinished,true);assert.equal(app.field('projectName').value,'未命名剪辑项目');await leave;
  }finally{listGate.resolve(response({code:'list_unavailable',error:'列表暂不可用'},503));await save;await settle();}
  assert.equal(app.field('projectName').value,'未命名剪辑项目');assert.match(app.field('toast').textContent,/草稿已保存.*列表刷新失败/);
  assert.equal(app.calls.filter(call=>call.options.method==='POST').length,1);
});

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
  await app.poll();await settle();assert.equal(app.field('dramaCount').textContent,'1 部');
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
  probeStatus='completed';source.status='ready';source.metadata={duration_ms:10000,width:720,height:1280};await app.poll();await settle();
  assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(app.field('dramaDialog').open,false);
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
test('selected episodes show independent download and queue progress without losing checks or scroll',async()=>{
  const app=await catalogFixture();await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2']);
  await app.field('addEpisodes').click();
  app.sources.get('vep_async_1').probe_progress={status:'running',stage:'downloading',download_bytes:10485760,total_bytes:41943040};
  app.sources.get('vep_async_2').probe_progress={status:'queued',stage:'queued'};
  app.field('catalogEpisodes').scrollTop=240;await app.poll();
  assert.match(app.field('catalogEpisodes').innerHTML,/下载中 25%/);
  assert.match(app.field('catalogEpisodes').innerHTML,/排队中/);
  assert.match(app.field('catalogEpisodes').innerHTML,/<progress/);
  assert.equal(app.field('catalogEpisodes').scrollTop,240);
  assert.equal(app.field('addEpisodes').disabled,true);
  app.ready(1,2);await app.poll();assert.equal(app.field('dramaDialog').open,false);
  assert.equal(app.probes.length,1);
});

test('completed task submits one material-source request and shows the confirmed material ID after refresh',async()=>{
  const gate=deferred(),task={id:'ve_completed',drama_title:'风起时，遇见你',status:'completed',output:{metadata:{duration_ms:43800}}};let posts=0;
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/tasks'))return response({items:[task]});
    if(url.endsWith('/tasks/ve_completed/material-source')){posts++;assert.equal(options.method,'POST');assert.equal(options.body,'{}');return gate.promise;}
  });
  assert.match(app.field('taskList').innerHTML,/上传至素材库/);
  const button=app.dataElement({uploadMaterialTask:'ve_completed'});
  const first=app.clickData({uploadMaterialTask:'ve_completed'});await settle();
  assert.equal(posts,1);assert.equal(button.disabled,true);
  assert.match(app.field('taskList').innerHTML,/等待上传/);
  assert.doesNotMatch(app.field('taskList').innerHTML,/data-upload-material-task/);
  await button.click();assert.equal(posts,1);
  gate.resolve(response({item:{status:'queued'}}));await first;
  assert.match(app.field('taskList').innerHTML,/等待上传/);
  task.material_upload={status:'succeeded',material_id:56789};await app.field('refreshTasks').click();
  assert.match(app.field('taskList').innerHTML,/素材 ID：56789/);
  assert.doesNotMatch(app.field('taskList').innerHTML,/data-upload-material-task/);
});

test('unknown material outcome offers read-only refresh and never submits a second upload',async()=>{
  const task={id:'ve_unknown',drama_title:'待核实',status:'completed',output:{metadata:{duration_ms:1000}},material_upload:{status:'unknown_outcome',error:'等待素材库回读'}};let posts=0,reads=0;
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/tasks')){reads++;return response({items:[task]});}
    if(url.includes('/material-source')&&options.method==='POST'){posts++;return response({item:{status:'unknown_outcome'}});}
  });
  assert.match(app.field('taskList').innerHTML,/结果待核实/);
  assert.match(app.field('taskList').innerHTML,/等待素材库回读/);
  await app.clickData({refreshMaterialTask:'ve_unknown'});
  assert.equal(posts,0);assert.ok(reads>=2);
  assert.doesNotMatch(app.field('taskList').innerHTML,/data-upload-material-task/);
});

test('lost material-source response stays in reconciliation state after the server records it',async()=>{
  const task={id:'ve_lost',status:'completed',output:{metadata:{duration_ms:1000}}};let posts=0;
  const app=await browser(async(url,options)=>{
    if(url.endsWith('/tasks'))return response({items:[task]});
    if(url.endsWith('/tasks/ve_lost/material-source')&&options.method==='POST'){
      posts++;task.material_upload={status:'unknown_outcome',error:'等待回读'};
      return response({error:'提交回执丢失'},502);
    }
  });
  await app.clickData({uploadMaterialTask:'ve_lost'});
  assert.equal(posts,1);assert.match(app.field('taskList').innerHTML,/结果待核实/);
  assert.doesNotMatch(app.field('taskList').innerHTML,/data-upload-material-task/);
  await app.clickData({refreshMaterialTask:'ve_lost'});
  assert.equal(posts,1);assert.match(app.field('taskList').innerHTML,/等待回读/);
});

test('demo completed task shows upload action but cannot call the business API',async()=>{
  const app=await browser(async()=>{throw new Error('demo must not call the business API');},{location:{search:'?demo=1',pathname:'/video-editing.html'}});
  assert.match(app.field('taskList').innerHTML,/data-upload-material-task="demo-completed-task"/);
  await app.clickData({uploadMaterialTask:'demo-completed-task'});
  assert.match(app.field('toast').textContent,/模拟预览不会上传至素材库/);
  assert.equal(app.calls.some(call=>call.url.includes('/material-source')),false);
});
