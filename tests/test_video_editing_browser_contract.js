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
  const window={document,crypto:crypto.webcrypto,sessionStorage:extras.sessionStorage,addEventListener(){},QuickNav:{render:async()=>{}},UiTopbar:{render(){},handleAuthAction:async()=>{}}};
  class ClockDate extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  const context={window,document,fetch,URL,URLSearchParams,AbortController,Date:ClockDate,location:{search:'',pathname:'/video-editing.html'},performance:{now:()=>now},requestAnimationFrame:()=>0,cancelAnimationFrame(){},setInterval(fn){intervals.push(fn);return intervals.length;},clearInterval(id){intervals[id-1]=null;},setTimeout(fn){const id=nextTimer++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},console,...extras};
  vm.runInNewContext(script,context,{filename:'video-editing.js'});for(let i=0;i<6;i++)await tick();
  return {elements,calls,timers,createdElements,field:id=>document.getElementById(id),dataElement:dataset=>nodes().find(el=>Object.entries(dataset).every(([key,value])=>el.dataset[key]===value)),rename:async name=>{const input=document.getElementById('projectName');input.value=name;await input.emit('change');},response,
    clickData:async(dataset,container)=>{const target=nodes().find(el=>Object.entries(dataset).every(([key,value])=>el.dataset[key]===value))||Object.assign(new Element(),{dataset});if(target.disabled)return;await target.click();return container?document.getElementById(container).emit('click',{target}):Promise.all((documentEvents.click||[]).map(fn=>fn({target,currentTarget:document})));},
    checkEpisodes(ids){const inputs=document.querySelectorAll('.episode-select');assert.ok(ids.every(id=>inputs.some(el=>el.value===id)),'selected episode must exist in rendered DOM');for(const el of inputs)el.checked=ids.includes(el.value);},
    async selectRadio(name,value,formId){const inputs=document.querySelectorAll(`[name="${name}"]`);assert.ok(inputs.some(input=>input.value===value),'radio option exists in real markup');for(const input of inputs)input.checked=input.value===value;await document.getElementById(formId).emit('change',{target:inputs.find(input=>input.value===value)});},
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
    if(url.includes('/catalog/resources?'))return response({items:[{series_code:'21481',english_title:'Test English Drama'}]});
    if(url.includes('/catalog/resource-variants?'))return response({items:dramas});
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
  return Object.assign(app,fixture,{async selectDrama(index=0){if(app.field('dramaCount').textContent==='0 部'){await app.field('addDrama').click();app.field('resourceSearch').value='21481';await app.field('searchResources').click();await app.clickData({selectResource:'0'},'resourceResults');}await app.field('onlineSources').click();await app.clickData({catalogIndex:String(index)},'catalogDramas');}});
}

test('one click waits for all three probes and adds them exactly once without another confirmation',async()=>{
  const app=await catalogFixture();await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);
  const first=app.field('addEpisodes').click(),second=app.field('addEpisodes').click();await Promise.all([first,second]);
  assert.equal(app.probes.length,1);assert.equal(app.field('addEpisodes').disabled,true);assert.equal(app.field('addEpisodes').getAttribute('aria-busy'),'true');
  assert.equal(app.field('cancelAddEpisodes').classList.contains('hidden'),false);assert.match(app.field('catalogStatus').textContent,/自动添加/);
  app.ready(1);await app.poll();assert.match(app.field('catalogStatus').textContent,/1\s*\/\s*3/);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  app.ready(2);await app.poll();assert.match(app.field('catalogStatus').textContent,/2\s*\/\s*3/);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  app.ready(3);await app.poll();await settle();assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(app.field('dramaDialog').open,false);
  await app.poll();await app.field('saveProject').click();assert.equal(app.probes.length,1);
  assert.deepEqual(app.saved.at(-1).units[0].clips.map(clip=>clip.source_id),['vep_async_1','vep_async_2','vep_async_3']);
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
  assert.equal(app.field('projectName').value,'未命名剪辑项目');assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.ok(app.saved.every(project=>project.units.every(unit=>unit.clips.length===0)));
  if(action!=='cancel')assert.equal(app.field('dramaDialog').open,false);
});

test('lost probe response requires an explicit retry that reconciles the source without posting another probe',async()=>{
  let posts=0;const app=await catalogFixture(async(url,options,_calls,fixture)=>{
    if(url.endsWith('/catalog/probe')){posts++;for(const id of JSON.parse(options.body).source_ids)Object.assign(fixture.sources.get(id),{status:'probing',probe_task_id:`accepted_${id}`});throw new Error('response lost after server acceptance');}return null;
  });
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();
  for(let i=0;i<3;i++)await app.poll();assert.equal(posts,1);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  assert.match(app.field('catalogStatus').textContent,/核实|确认|未知|等待|稍后/);
  app.ready(1,2,3);await app.poll();await settle();assert.equal(posts,1);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  await app.field('addEpisodes').click();await settle();assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'1 部');
});

test('probe polling stays single-flight and one failed episode never causes partial auto-add or automatic retry',async()=>{
  let delay=false,active=0,maxActive=0;const gates=[];
  const app=await catalogFixture(async(url,_options,_calls,fixture)=>{if(delay&&url.includes('/sources/')){active++;maxActive=Math.max(maxActive,active);const gate=deferred();gates.push({gate,id:url.split('/').at(-1)});await gate.promise;active--;return response({item:fixture.sources.get(url.split('/').at(-1))});}return null;});
  await app.selectDrama();app.checkEpisodes(['vep_async_1','vep_async_2','vep_async_3']);await app.field('addEpisodes').click();delay=true;
  const first=app.poll(),second=app.poll();await settle();assert.ok(gates.length<=3);assert.ok(maxActive<=3);
  app.ready(1,2);Object.assign(app.sources.get('vep_async_3'),{status:'failed',error_message:'无法读取视频信息'});for(const {gate}of gates)gate.resolve();await Promise.all([first,second]);delay=false;await app.poll();
  assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.equal(app.probes.length,1);assert.equal(app.resumes.length,0);
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
  await app.poll();assert.equal(posts,1);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  assert.equal(app.field('addEpisodes').disabled,false,'an incomplete write response must stop automatic continuation');
  const reads=app.calls.filter(call=>call.url.includes('/sources/')).length;
  await app.field('addEpisodes').click();await app.poll();
  assert.ok(app.calls.filter(call=>call.url.includes('/sources/')).length>reads,'explicit retry must read back uncertain sources');
  assert.equal(posts,1);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.match(app.field('catalogStatus').textContent,/核实|未知|暂不重复|确认/);
});

test('re-adding the same source after cancellation joins its outstanding probe write instead of submitting again',async()=>{
  const gate=deferred();let posts=0,secondFinished=false;
  const app=await catalogFixture(async url=>url.endsWith('/catalog/probe')?(posts++,gate.promise):null);
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);const first=app.field('addEpisodes').click();await settle();
  await app.field('cancelAddEpisodes').click();app.checkEpisodes(['vep_async_1']);
  const second=app.field('addEpisodes').click().then(()=>{secondFinished=true;});await settle();
  try{assert.equal(posts,1);assert.equal(secondFinished,false);assert.equal(app.field('addEpisodes').disabled,true);assert.equal(app.field('cancelAddEpisodes').classList.contains('hidden'),false);}
  finally{Object.assign(app.sources.get('vep_async_1'),{status:'probing',probe_task_id:'ve_original_submission'});gate.resolve(response({items:[app.sources.get('vep_async_1')]}));await Promise.all([first,second]);}
  assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.equal(app.field('dramaDialog').open,true);
  app.ready(1);await app.poll();await app.field('saveProject').click();
  assert.equal(posts,1);assert.equal(app.field('dramaCount').textContent,'1 部');assert.deepEqual(app.saved.at(-1).units[0].clips.map(clip=>clip.source_id),['vep_async_1']);
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
  await app.poll();assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.equal(app.field('addEpisodes').disabled,true);
  assert.equal(app.field('cancelAddEpisodes').classList.contains('hidden'),false);assert.match(app.field('catalogStatus').textContent,/核实|稍后|网络|暂|等待/);
  fail=false;app.ready(1,2,3);await app.poll();await settle();assert.equal(app.probes.length,1);assert.equal(app.field('dramaCount').textContent,'1 部');
});

test('unknown probe submission stays fenced across dialog cancellation even if the refreshed source is still unprobed',async()=>{
  let posts=0;const app=await catalogFixture(async url=>{if(url.endsWith('/catalog/probe')){posts++;throw new Error('request outcome unknown');}return null;});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();assert.equal(posts,1);
  await app.closeDialog('dramaDialog');await app.selectDrama();app.checkEpisodes(['vep_async_1']);
  const reads=app.calls.filter(call=>call.url.endsWith('/sources/vep_async_1')).length;await app.field('addEpisodes').click();await app.poll();
  assert.ok(app.calls.filter(call=>call.url.endsWith('/sources/vep_async_1')).length>reads);
  assert.equal(posts,1);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.match(app.field('catalogStatus').textContent,/核实|确认|未知|发起人|管理员/);
});

for(const status of [403,404])test(`an existing probe hidden by HTTP ${status} never permits a replacement probe`,async()=>{
  let reads=0;const app=await catalogFixture(async url=>{if(url.endsWith('/tasks/ve_private')){reads++;return response({code:'task_not_found',error:'该校验任务不可见'},status);}return null;});
  Object.assign(app.sources.get('vep_async_1'),{status:'failed',probe_task_id:'ve_private'});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();await app.poll();
  assert.ok(reads>0);assert.equal(app.probes.length,0);assert.equal(app.resumes.length,0);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  assert.match(app.field('catalogStatus').textContent,/权限|发起人|管理员|不可见|核实/);
});

for(const status of [403,404])test(`a shared probing episode tolerates its hidden HTTP ${status} task and keeps observing the readable source`,async()=>{
  let privateReads=0;const app=await catalogFixture(async url=>{if(url.includes('/tasks/')){privateReads++;return response({code:'task_not_found',error:'不可见'},status);}return null;});
  Object.assign(app.sources.get('vep_async_1'),{status:'probing',probe_task_id:'ve_someone_else'});
  await app.selectDrama();app.checkEpisodes(['vep_async_1']);await app.field('addEpisodes').click();await app.poll();
  assert.ok(privateReads>0);assert.equal(app.probes.length,0);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);assert.equal(app.field('addEpisodes').disabled,true);
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
  await app.poll();await app.poll();assert.equal(resumes,0);assert.equal(app.probes.length,0);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  const first=app.field('addEpisodes').click(),second=app.field('addEpisodes').click();await Promise.all([first,second]);
  assert.equal(resumes,1);assert.equal(app.field('addEpisodes').disabled,true);assert.equal(app.probes.length,0);
  taskStatus='running';await app.poll();assert.equal(resumes,1);assert.doesNotMatch(app.field('timeline').innerHTML,/data-clip-id/);
  taskStatus='completed';app.ready(1);await app.poll();await settle();assert.equal(app.field('dramaCount').textContent,'1 部');assert.equal(resumes,1);assert.equal(app.probes.length,0);
});

test('opening a project leaves editing and saving available while source metadata loads',async()=>{
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
    assert.match(app.field('saveStatus').textContent,/已打开.*媒体信息/);assert.equal(app.field('trimStart').disabled,false);assert.equal(app.field('saveProject').disabled,false);assert.equal(app.field('renderProject').disabled,false);
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

async function resourceFixture(override=async()=>null){
  const resources=[{series_code:'{v3404/l144250/}',english_title:'Love & Revenge',english_title_status:'matched'},{series_code:'21482',english_title:'Her Second Life',english_title_status:'matched'}];
  const assets=[{id:'va_free',source_id:'va_free',kind:'asset',library_kind:'free_episode',series_code:resources[0].series_code,english_title:resources[0].english_title,name:'Free compilation',status:'ready',can_manage:true,size_bytes:2000,metadata:{duration_ms:15000,width:720,height:1280,has_audio:true}},{id:'va_material',source_id:'va_material',kind:'asset',library_kind:'material',series_code:resources[0].series_code,english_title:resources[0].english_title,name:'Hook',status:'ready',can_manage:true,size_bytes:1000,metadata:{duration_ms:5000,width:1920,height:1080,has_audio:true}}];
  const writes=[],renderRequests=[];let remote=null;
  const app=await browser(async(url,options,calls)=>{
    const custom=await override(url,options,calls,{resources,assets,remote,writes,renderRequests});if(custom)return custom;
    const body=options.body&&JSON.parse(options.body),method=options.method||'GET';
    if(url.includes('/catalog/resources?'))return response({items:resources});
    if(url.includes('/catalog/resource-variants?'))return response({items:[{app_id:'1479',content_id:'EnDrama',language:'en',country:'US',title:'English drama'}]});
    if(url.includes('/assets?')){const query=new URL(url,'https://local').searchParams;return response({items:assets.filter(asset=>(!query.get('library_kind')||asset.library_kind===query.get('library_kind'))&&(!query.get('series_code')||asset.series_code===query.get('series_code'))),total:1});}
    if(url.includes('/sources/'))return response({item:assets.find(asset=>asset.id===url.split('/').at(-1))});
    if(url.endsWith('/projects')&&method==='POST'){remote={...body,id:'vp_resources',version:1};delete remote.operation_id;writes.push(body);return response({item:remote});}
    if(url.endsWith('/projects/vp_resources')&&method==='PATCH'){remote={...body,id:'vp_resources',version:remote.version+1};delete remote.expected_version;writes.push(body);return response({item:remote});}
    if(url.endsWith('/projects/vp_resources'))return response({item:remote});
    if(url.endsWith('/projects'))return response({items:remote?[remote]:[]});
    if(url.endsWith('/render')){renderRequests.push(body);return response({items:body.unit_ids.map(id=>({id:'ve_'+id,unit_id:id})),results:body.unit_ids.map(id=>({unit_id:id,status:'accepted',task_id:'ve_'+id})),issues:[]});}
  });
  return Object.assign(app,{resources,assets,writes,renderRequests,async addUnit(index=0){await app.field('addDrama').click();app.field('resourceSearch').value=resources[index].series_code;await app.field('searchResources').click();await app.clickData({selectResource:String(index)},'resourceResults');},checkSources(ids){const inputs=app.field('assetPicker').querySelectorAll('.source-pick');assert.ok(ids.every(id=>inputs.some(input=>input.value===id)));for(const input of inputs)input.checked=ids.includes(input.value);}});
}

test('resource search takes newest intent, encodes opaque IDs and ignores closed-dialog responses',async()=>{
  const gates=new Map();const app=await browser(async url=>{if(!url.includes('/catalog/resources?'))return null;const q=new URL(url,'https://test').searchParams.get('q'),gate=deferred();gates.set(q,gate);return gate.promise;});
  await app.field('addDrama').click();app.field('resourceSearch').value='Old';const first=app.field('searchResources').click();await settle();app.field('resourceSearch').value='{v3404/l144250/}';const second=app.field('searchResources').click();await settle();
  gates.get('{v3404/l144250/}').resolve(response({items:[{series_code:'{v3404/l144250/}',english_title:'Newest'}]}));await second;gates.get('Old').resolve(response({items:[{series_code:'1',english_title:'Stale'}]}));await first;
  assert.match(app.field('resourceResults').innerHTML,/Newest/);assert.doesNotMatch(app.field('resourceResults').innerHTML,/Stale/);assert.ok(app.calls.some(call=>call.url.includes('%7Bv3404%2Fl144250%2F%7D')));
  app.field('resourceSearch').value='Closed';const last=app.field('searchResources').click();await settle();await app.closeDialog('resourceDialog');gates.get('Closed').resolve(response({items:[{series_code:'2',english_title:'Closed result'}]}));await last;assert.doesNotMatch(app.field('resourceResults').innerHTML,/Closed result/);
});

test('resource lookup failure stays local and an empty project still saves',async()=>{
  const app=await resourceFixture(async url=>url.includes('/catalog/resources?')?response({error:'目录暂不可用'},503):null);
  await app.field('addDrama').click();app.field('resourceSearch').value='Love';await app.field('searchResources').click();assert.equal(app.field('resourceDialog').open,true);assert.match(app.field('resourceStatus').textContent,/目录暂不可用/);await app.closeDialog('resourceDialog');await app.rename('Saved without directory');await app.field('saveProject').click();assert.equal(app.writes.at(-1).schema_version,2);assert.deepEqual(app.writes.at(-1).units,[]);
});

test('four tabs isolate free videos from materials and resource-first chooser permits uploaded-only output',async()=>{
  const app=await resourceFixture();assert.match(html,/data-view="editor"[^>]*>工作台/);assert.match(html,/data-view="free"/);
  await app.clickData({view:'free'});await settle();assert.match(app.field('assetGrid').innerHTML,/Free compilation/);assert.doesNotMatch(app.field('assetGrid').innerHTML,/>Hook</);await app.clickData({view:'library'});await settle();assert.match(app.field('assetGrid').innerHTML,/>Hook</);assert.doesNotMatch(app.field('assetGrid').innerHTML,/Free compilation/);
  await app.addUnit();await app.field('chooseAsset').click();app.checkSources(['va_free']);await app.field('insertSelectedSources').click();await app.field('saveProject').click();
  assert.equal(app.writes.at(-1).units.length,1);assert.deepEqual(app.writes.at(-1).units[0].clips.map(clip=>clip.source_id),['va_free']);assert.equal(app.writes.at(-1).asset_id,undefined);
  await app.field('renderProject').click();assert.equal(app.renderRequests.length,1);assert.equal(app.renderRequests[0].unit_ids.length,1);
});

test('one resource unit accepts repeated free and material video references with independent trims',async()=>{
  const app=await resourceFixture();await app.addUnit();await app.field('chooseAsset').click();app.checkSources(['va_free']);await app.field('insertSelectedSources').click();await app.field('chooseAsset').click();await app.clickData({sourceKind:'material'});app.checkSources(['va_material']);await app.field('insertSelectedSources').click();
  await app.field('chooseAsset').click();app.checkSources(['va_material']);await app.field('insertSelectedSources').click();await app.field('saveProject').click();const clips=app.writes.at(-1).units[0].clips;
  assert.deepEqual(clips.map(clip=>clip.source_id),['va_free','va_material','va_material']);assert.equal(new Set(clips.map(clip=>clip.id)).size,3);
});

test('changing resource preserves existing clips and mirror split/undo settings in v2 drafts',async()=>{
  const app=await resourceFixture();await app.addUnit();await app.field('chooseAsset').click();app.checkSources(['va_free']);await app.field('insertSelectedSources').click();
  app.field('clipMirror').checked=true;await app.field('clipMirror').emit('change');app.field('subtitleTop').value=800;app.field('subtitleBottom').value=1100;await app.field('subtitleTop').emit('change');app.field('playhead').value=5000;await app.field('playhead').emit('input');await app.field('splitClip').click();await app.field('saveProject').click();
  assert.ok(app.writes.at(-1).units[0].clips.every(clip=>clip.mirror&&clip.subtitle_band.top===800));assert.equal(app.writes.at(-1).units[0].clips.length,2);
  await app.field('undoEdit').click();await app.field('changeUnitResource').click();app.field('resourceSearch').value='21482';await app.field('searchResources').click();await app.clickData({selectResource:'1'},'resourceResults');await app.field('saveProject').click();
  const unit=app.writes.at(-1).units[0];assert.equal(unit.series_code,'21482');assert.equal(unit.clips.length,1);assert.equal(unit.clips[0].source_id,'va_free');assert.match(app.field('validationErrors').textContent,/其他资源/);
});

test('invalid unit does not block accepted unit and next explicit submission selects only invalid units',async()=>{
  let attempts=0;const app=await resourceFixture(async(url,options,_calls,fixture)=>{if(!url.endsWith('/render'))return null;const body=JSON.parse(options.body);fixture.renderRequests.push(body);attempts++;return response({items:[{id:'ve_valid',unit_id:body.unit_ids[0]}],results:[],issues:attempts===1?[{unit_id:body.unit_ids[1],code:'unit_incomplete',message:'第二个单元暂无片段'}]:[]});});
  await app.addUnit(0);await app.addUnit(1);await app.field('renderProject').click();assert.equal(app.renderRequests[0].unit_ids.length,2);assert.match(app.field('validationErrors').textContent,/暂无片段/);await app.field('renderProject').click();assert.deepEqual(app.renderRequests[1].unit_ids,[app.renderRequests[0].unit_ids[1]]);assert.notEqual(app.renderRequests[0].operation_id,app.renderRequests[1].operation_id);
});

test('lost render response keeps original operation and selected units until reconciled',async()=>{
  let attempts=0;const app=await resourceFixture(async(url,options,_calls,fixture)=>{if(!url.endsWith('/render'))return null;const body=JSON.parse(options.body);fixture.renderRequests.push(body);if(++attempts===1)throw new Error('response lost');return response({items:[],results:[],issues:[]});});
  await app.addUnit();await app.field('renderProject').click();await app.field('renderProject').click();assert.equal(attempts,2);assert.deepEqual(app.renderRequests[0],app.renderRequests[1]);
});

test('resource-task material intake asks for product and language only at intake click',async()=>{
  const task={id:'ve_resource',series_code:'{v3404/l144250/}',unit_id:'u1',status:'completed',drama_title:'English display title',output:{id:'vo1',metadata:{duration_ms:3000}}};let posted;
  const app=await browser(async(url,options)=>{if(url.endsWith('/tasks'))return response({items:[task]});if(url.includes('/catalog/resource-variants?'))return response({items:[{app_id:'1479',content_id:'SpanishDrama',language:'es',country:'MX'}]});if(url.endsWith('/material-source')){posted=JSON.parse(options.body);return response({item:{status:'queued'}});}});
  assert.equal(app.calls.some(call=>call.url.includes('resource-variants')),false);await app.clickData({uploadMaterialTask:task.id});assert.equal(app.field('variantDialog').open,true);assert.equal(posted,undefined);app.field('variantSelect').value='0';await app.field('variantSelect').emit('change');await app.field('confirmVariant').click();assert.deepEqual(posted,{app_id:'1479',content_id:'SpanishDrama'});
});

test('unknown render replays the frozen request before submitting an edited project version',async()=>{
  let attempts=0;const app=await resourceFixture(async(url,options,_calls,fixture)=>{if(!url.endsWith('/render'))return null;const body=JSON.parse(options.body);fixture.renderRequests.push(body);if(++attempts===1)throw new Error('response lost');return response({items:[],results:[],issues:[]});});
  await app.addUnit();await app.field('renderProject').click();await app.addUnit(1);await app.field('renderProject').click();
  assert.equal(attempts,3);assert.deepEqual(app.renderRequests[1],app.renderRequests[0]);assert.equal(app.renderRequests[2].unit_ids.length,2);assert.notEqual(app.renderRequests[2].operation_id,app.renderRequests[0].operation_id);assert.ok(app.renderRequests[2].expected_version>app.renderRequests[0].expected_version);
});

test('new free upload waits for a real resource selection and freezes its category and opaque ID',async()=>{
  let created;const app=await resourceFixture(async(url,options)=>{if(url.endsWith('/uploads')&&options.method==='POST'){created=JSON.parse(options.body);return response({item:{id:'vu_new',status:'completed'}});}});
  await app.clickData({view:'free'});const file=new Blob(['sample'],{type:'video/mp4'});Object.assign(file,{name:'Free compilation.mp4',lastModified:123});app.field('fileInput').files=[file];await app.field('fileInput').emit('change');await settle();
  assert.equal(created,undefined);assert.equal(app.field('resourceDialog').open,true);
  app.field('resourceSearch').value='{v3404/l144250/}';await app.field('searchResources').click();await app.clickData({selectResource:'0'},'resourceResults');await settle();
  assert.equal(created.library_kind,'free_episode');assert.equal(created.series_code,'{v3404/l144250/}');assert.equal(created.name,'Free compilation.mp4');assert.ok(created.operation_id);
});

test('explicit pre-write project version conflict releases only the rejected render request after reopening',async()=>{
  let attempts=0;const app=await resourceFixture(async(url,options,_calls,fixture)=>{if(!url.endsWith('/render'))return null;const body=JSON.parse(options.body);fixture.renderRequests.push(body);if(++attempts===1){fixture.remote.version++;return response({code:'project_version_conflict',error:'项目版本冲突'},409);}return response({items:[],results:[],issues:[]});});
  await app.addUnit();await app.field('renderProject').click();assert.match(app.field('toast').textContent,/重新打开项目/);
  app.field('projectPicker').value='vp_resources';await app.field('projectPicker').emit('change');await app.field('renderProject').click();
  assert.equal(attempts,2);assert.ok(app.renderRequests[1].expected_version>app.renderRequests[0].expected_version);assert.notEqual(app.renderRequests[1].operation_id,app.renderRequests[0].operation_id);
});

async function replicationFixture(override=async()=>null,admin=true,extras={}){
  const fixture={history:[],posts:[],contextReads:0,historyReads:0};
  fixture.task={id:'ve_replication',drama_title:'Saved original',status:'completed',stage:'completed',progress:1,output:{metadata:{duration_ms:20000}},material_upload:{status:'succeeded',material_id:52},material_replication:{enabled:true,count:0,summary:null}};
  fixture.context={enabled:true,source:{material_id:52,name:'Saved original',resource_id:'resource-abc',source_language:'en',duration_ms:20000},default_operator:'owner_from_server',can_change_operator:admin,target_languages:[{code:'en',name:'英语'},{code:'es',name:'西语'},{code:'pt',name:'葡语'}]};
  const app=await browser(async(url,options,calls)=>{
    if(url.includes('/material-replication')){
      if(options.method==='POST')fixture.posts.push(JSON.parse(options.body));
      else if(url.includes('history_only=1'))fixture.historyReads++;else fixture.contextReads++;
      const custom=await override(url,options,fixture,calls);if(custom)return custom;
      if(options.method==='POST'){
        const body=JSON.parse(options.body),record={...body,id:'receipt-1',operator:body.operator||fixture.context.default_operator,status:'accepted',upstream_status:'waiting_download',b_task_id:'remote-17',effective_langs:body.target_langs,unsupported_langs:[]};fixture.history=[record];fixture.task.material_replication={enabled:true,count:1,summary:record};return response({item:record});
      }
      return response({item:{...fixture.context,history:fixture.history,summary:fixture.history[0]||null}});
    }
    if(url.endsWith('/tasks'))return response({items:[fixture.task,{id:'unfinished',status:'running'},{id:'not-in-library',status:'completed',output:{metadata:{duration_ms:1000}},material_replication:{enabled:false,disabled_reason:'请先入库'}}]});
  },extras);
  return Object.assign(app,fixture,{async open(){await app.clickData({replicateTask:'ve_replication'});},async selectLanguage(code){const input=app.dataElement({replicationLanguage:code});assert.ok(input,'language option comes from context response');input.checked=true;await app.field('replicationForm').emit('change',{target:input});},submit(){return app.field('replicationForm').emit('submit');}});
}

test('replication opens only on eligible finished output, defaults to empty targets, and reads context only on demand',async()=>{
  const app=await replicationFixture();assert.equal(app.contextReads,0);assert.equal(app.dataElement({replicateTask:'unfinished'}).disabled,true);assert.equal(app.dataElement({replicateTask:'not-in-library'}).disabled,true);assert.match(app.field('taskList').innerHTML,/请先入库/);
  await app.open();assert.equal(app.field('replicationLanguageCount').textContent,'0');assert.equal(app.field('replicationOperator').value,'owner_from_server');assert.equal(app.field('replicationOperator').readOnly,false);
  await app.submit();assert.equal(app.posts.length,0);assert.match(app.field('replicationError').textContent,/至少选择/);
  await app.selectLanguage('en');await app.closeDialog('replicationDialog');await app.open();assert.equal(app.field('replicationLanguageCount').textContent,'0');
  const reads=app.calls.filter(call=>call.url.endsWith('/material-replication')).length;assert.equal(reads,2);await app.poll();await app.poll();assert.equal(app.calls.filter(call=>call.url.endsWith('/material-replication')).length,reads);
});

test('replication single-flight freezes owner and allows source language while original completed status remains separate',async()=>{
  const gate=deferred();const app=await replicationFixture(async(_url,options,fixture)=>{if(options.method==='POST'){await gate.promise;return response({item:{...fixture.posts[0],id:'receipt',status:'accepted',operator:fixture.posts[0].operator,effective_langs:['en'],unsupported_langs:[],b_task_id:'B-1',upstream_status:'waiting_download'}});}});
  await app.open();await app.selectLanguage('en');app.field('replicationOperator').value='admin_changed';const first=app.submit();await settle();const second=app.submit();await second;
  assert.equal(app.posts.length,1);assert.equal(app.field('replicationSubmit').disabled,true);assert.deepEqual(app.posts[0].target_langs,['en']);assert.equal(app.posts[0].operator,'admin_changed');assert.equal(app.posts[0].task_type,1);assert.equal(app.posts[0].audio_type,0);assert.equal(app.posts[0].target_material_type,1);assert.equal(app.posts[0].no_bgm,false);
  gate.resolve();await first;assert.match(app.field('replicationReceipt').innerHTML,/待转存/);assert.match(app.field('replicationReceipt').innerHTML,/尚未查询/);assert.match(app.field('taskList').innerHTML,/制作完成 · 100%/);
});

test('member ownership is read-only and never sends a client operator override',async()=>{
  const app=await replicationFixture(async()=>null,false);await app.open();assert.equal(app.field('replicationOperator').readOnly,true);app.field('replicationOperator').value='tampered';await app.selectLanguage('es');await app.submit();assert.equal(Object.hasOwn(app.posts[0],'operator'),false);
});

test('lost replication response never auto-posts and explicit replay uses exact original payload and operation',async()=>{
  let attempts=0;const app=await replicationFixture(async(_url,options)=>{if(options.method==='POST'&&++attempts===1)throw new Error('connection lost');});await app.open();await app.selectLanguage('es');await app.submit();
  assert.match(app.field('replicationReceipt').innerHTML,/结果待核实/);assert.equal(app.field('replicationSubmit').disabled,true);await app.poll();await app.poll();await app.field('replicationVerify').click();assert.equal(app.posts.length,1);
  app.field('replicationOperator').value='new value must not apply';await app.submit();assert.equal(app.posts.length,1);await app.field('replicationReplay').click();assert.equal(app.posts.length,2);assert.deepEqual(app.posts[1],app.posts[0]);assert.equal(app.posts[1].operator,'owner_from_server');
});

test('read-only history resolves a lost reused operation through operation_ids without another POST',async()=>{
  const app=await replicationFixture(async(_url,options,fixture)=>{if(options.method==='POST'){const request=fixture.posts[0];fixture.history=[{...request,id:'old-receipt',operation_id:'older-operation',operation_ids:['older-operation',request.operation_id],status:'accepted',operator:request.operator,effective_langs:['es'],unsupported_langs:[],b_task_id:'B-old'}];throw new Error('reply lost');}});
  await app.open();await app.selectLanguage('es');await app.submit();await app.field('replicationVerify').click();assert.equal(app.posts.length,1);assert.equal(app.field('replicationPending').classList.contains('hidden'),true);assert.match(app.field('replicationReceipt').innerHTML,/复用已有任务/);assert.equal(app.calls.filter(call=>call.url.endsWith('/material-replication?history_only=1')).length,1);
});

test('replication 409 keeps the draft and frozen operation, and prevents new submit or replay',async()=>{
  const app=await replicationFixture(async(_url,options)=>options.method==='POST'?response({error:'operation payload conflict'},409):null);await app.open();await app.selectLanguage('es');app.field('replicationOperator').value='draft_owner';await app.submit();
  assert.match(app.field('replicationError').textContent,/草稿和原请求均已保留/);assert.equal(app.field('replicationOperator').value,'draft_owner');assert.equal(app.field('replicationReplay').disabled,true);await app.submit();await app.poll();assert.equal(app.posts.length,1);
});

test('reused conflicting receipt shows original task and parameter differences without claiming new settings applied',async()=>{
  const app=await replicationFixture(async(_url,options,fixture)=>options.method==='POST'?response({item:{...fixture.posts[0],id:'old',task_type:3,operator:'original-owner',status:'accepted',b_task_id:'B-old',effective_langs:['es'],unsupported_langs:[]},reused:true,conflict:true,conflict_fields:[{field:'operator',previous:'original-owner',requested:'owner_from_server'}]}):null);
  await app.open();await app.selectLanguage('es');await app.submit();assert.match(app.field('replicationReceipt').innerHTML,/本次新参数未生效/);assert.match(app.field('replicationReceipt').innerHTML,/original-owner/);assert.match(app.field('replicationReceipt').innerHTML,/B-old/);
});

test('narration range is validated before POST, and partial and rejected receipts remain explicit',async()=>{
  const app=await replicationFixture(async(_url,options,fixture)=>options.method==='POST'?response({item:{...fixture.posts.at(-1),id:'partial',operator:'owner',status:'partial',effective_langs:['es'],unsupported_langs:['pt'],b_task_id:'B-partial'}}):null);await app.open();await app.selectLanguage('es');await app.selectLanguage('pt');await app.selectRadio('replicationMaterialType','2','replicationForm');app.field('replicationStart').value='00:00:00';app.field('replicationEnd').value='00:00:21';await app.submit();assert.equal(app.posts.length,0);assert.match(app.field('replicationError').textContent,/时长/);app.field('replicationEnd').value='00:00:20';await app.submit();assert.equal(app.posts[0].descant_end_time,'00:00:20');assert.match(app.field('replicationReceipt').innerHTML,/部分语种已受理/);assert.match(app.field('replicationReceipt').innerHTML,/不支持/);
  const rejected=await replicationFixture(async(_url,options,fixture)=>options.method==='POST'?response({item:{...fixture.posts[0],id:'rejected',status:'rejected',operator:'owner',error:'账号未绑定',effective_langs:[],unsupported_langs:[]}}):null);await rejected.open();await rejected.selectLanguage('es');await rejected.submit();assert.match(rejected.field('replicationReceipt').innerHTML,/提交失败/);assert.match(rejected.field('replicationReceipt').innerHTML,/账号未绑定/);
});

test('pending replication is reconciled from existing task-list polling without context queries or a second POST',async()=>{
  const app=await replicationFixture(async(_url,options,fixture)=>{if(options.method==='POST'){const record={...fixture.posts[0],id:'pending-receipt',status:'pending',operator:'owner',effective_langs:[],unsupported_langs:[]};fixture.task.material_replication={enabled:true,count:1,summary:record};fixture.history=[record];return response({item:record});}});await app.open();await app.selectLanguage('es');await app.submit();assert.equal(app.field('replicationReplay').disabled,true);assert.match(app.field('replicationPendingText').textContent,/请求已登记/);
  const reads=app.calls.filter(call=>call.url.includes('/material-replication')&&!call.options.method).length;Object.assign(app.task.material_replication.summary,{status:'accepted',effective_langs:['es'],b_task_id:'B-finished-receipt'});await app.field('refreshTasks').click();assert.equal(app.field('replicationPending').classList.contains('hidden'),true);assert.equal(app.field('replicationSubmit').disabled,false);assert.equal(app.posts.length,1);assert.equal(app.calls.filter(call=>call.url.includes('/material-replication')&&!call.options.method).length,reads);assert.match(app.field('replicationReceipt').innerHTML,/B-finished-receipt/);
});

test('closing and reopening during an in-flight replication cannot leave stale busy controls after completion',async()=>{
  const gate=deferred();const app=await replicationFixture(async(_url,options,fixture)=>{if(options.method==='POST'){await gate.promise;return response({item:{...fixture.posts[0],id:'receipt-after-reopen',status:'accepted',operator:'owner',effective_langs:['es'],unsupported_langs:[]}});}});await app.open();await app.selectLanguage('es');const submitting=app.submit();await settle();await app.closeDialog('replicationDialog');await app.open();assert.equal(app.field('replicationSubmit').disabled,true);assert.equal(app.field('replicationLanguageCount').textContent,'0');gate.resolve();await submitting;assert.equal(app.field('replicationSubmit').disabled,false);assert.equal(app.field('replicationPending').classList.contains('hidden'),true);assert.equal(app.posts.length,1);
});

for(const outcome of ['rejected','unknown_outcome'])test(`a newer ${outcome} replication remains visible over an older accepted historical task and updates its pending controls`,async()=>{
  const historical={id:'history-1732014',operation_id:'older-history-operation',operation_ids:['older-history-operation'],historical:true,status:'accepted',operator:'owner',task_type:1,audio_type:0,target_material_type:1,no_bgm:false,target_langs:['es'],effective_langs:['es'],unsupported_langs:[],upstream_status:'waiting_download',b_task_id:'1732014'};
  const app=await replicationFixture(async(_url,options,fixture)=>{if(options.method==='POST'){const record={...fixture.posts[0],id:'newer-different-target',historical:false,status:'pending',operator:'owner',effective_langs:[],unsupported_langs:[]};fixture.history=[record,...fixture.history];fixture.task.material_replication={enabled:true,count:fixture.history.length,summary:record};return response({item:record});}});
  app.history.push(historical);app.task.material_replication={enabled:true,count:1,summary:historical};await app.field('refreshTasks').click();assert.match(app.field('taskList').innerHTML,/受理时：待转存/);
  await app.open();await app.selectLanguage('pt');await app.submit();assert.equal(app.posts.length,1);assert.deepEqual(app.posts[0].target_langs,['pt']);assert.equal(app.field('replicationReplay').disabled,true);assert.match(app.field('taskList').innerHTML,/等待提交/);
  Object.assign(app.task.material_replication.summary,{status:outcome,error:outcome==='rejected'?'归属账号未绑定':'接口响应中断'});await app.field('refreshTasks').click();
  assert.match(app.field('taskList').innerHTML,outcome==='rejected'?/提交失败/:/结果待核实/);assert.match(app.field('taskList').innerHTML,/制作完成 · 100%/);assert.doesNotMatch(app.field('taskList').innerHTML,/受理时：待转存/);assert.equal(app.posts.length,1);
  if(outcome==='rejected'){assert.equal(app.field('replicationPending').classList.contains('hidden'),true);assert.equal(app.field('replicationSubmit').disabled,false);assert.match(app.field('replicationReceipt').innerHTML,/归属账号未绑定/);}
  else{assert.equal(app.field('replicationPending').classList.contains('hidden'),false);assert.equal(app.field('replicationSubmit').disabled,true);assert.equal(app.field('replicationReplay').disabled,false);assert.match(app.field('replicationReceipt').innerHTML,/接口响应中断/);}
});

const categoryValues=['解说','二创','混剪','精剪','切片','image','Dramawave Post'];
const replicationStorageKey='video-editing:replication:tenant:Tester:ve_replication';
function memorySessionStorage(){const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value)),removeItem:key=>values.delete(key)};}
async function chooseCategory(app,value){app.field('replicationCategory').value=value;await app.field('replicationForm').emit('change',{target:app.field('replicationCategory')});}

test('material category offers all seven documented values, defaults to mixed editing, and resets on every opening',async()=>{
  const app=await replicationFixture();await app.open();assert.equal(app.field('replicationCategory').value,'混剪');assert.match(app.field('replicationSummaryDetail').textContent,/素材分类：混剪/);assert.match(app.field('replicationCategory').innerHTML,/value="image">图片（image）/);
  for(const category of categoryValues){await app.open();assert.equal(app.field('replicationCategory').value,'混剪');await app.selectLanguage('es');await chooseCategory(app,category);await app.submit();assert.equal(app.posts.at(-1).material_category,category);assert.match(app.field('replicationReceipt').textContent,/素材分类：/);}
  await app.closeDialog('replicationDialog');await app.open();assert.equal(app.field('replicationCategory').value,'混剪');assert.equal(app.field('replicationLanguageCount').textContent,'0');
  await app.selectLanguage('es');await chooseCategory(app,'invalid-category');const count=app.posts.length;await app.submit();assert.equal(app.posts.length,count);assert.match(app.field('replicationError').textContent,/素材分类/);
});

test('material category does not toggle narration or change member ownership permissions',async()=>{
  const app=await replicationFixture(async()=>null,false);await app.open();await app.selectLanguage('es');await chooseCategory(app,'解说');assert.equal(app.field('replicationNarration').classList.contains('hidden'),true);await app.submit();assert.equal(app.posts[0].material_category,'解说');assert.equal(app.posts[0].target_material_type,1);assert.equal(Object.hasOwn(app.posts[0],'descant_start_time'),false);assert.equal(Object.hasOwn(app.posts[0],'operator'),false);
  await app.open();await app.selectLanguage('es');await app.selectRadio('replicationMaterialType','2','replicationForm');assert.equal(app.field('replicationCategory').value,'混剪');app.field('replicationStart').value='00:00:00';app.field('replicationEnd').value='00:00:10';await app.submit();assert.equal(app.posts[1].material_category,'混剪');assert.equal(app.posts[1].target_material_type,2);assert.equal(app.posts[1].descant_end_time,'00:00:10');
});

test('a frozen legacy session request replays without adding material_category despite the new form default',async()=>{
  const storage=memorySessionStorage(),payload={operation_id:'legacy-operation-123',task_type:1,audio_type:0,target_material_type:1,target_langs:['es'],operator:'legacy-owner',no_bgm:false};storage.setItem(replicationStorageKey,JSON.stringify({taskId:'ve_replication',phase:'unknown',payload}));
  const app=await replicationFixture(async()=>null,true,{sessionStorage:storage});await app.open();assert.equal(app.field('replicationCategory').value,'混剪');assert.match(app.field('replicationPendingText').textContent,/素材分类：未指定（旧记录）/);assert.equal(app.field('replicationSubmit').disabled,true);await app.field('replicationReplay').click();assert.deepEqual(app.posts[0],payload);assert.equal(Object.hasOwn(app.posts[0],'material_category'),false);assert.match(app.field('replicationReceipt').textContent,/素材分类：未指定（旧记录）/);assert.equal(storage.getItem(replicationStorageKey),null);
});

test('a new pending category remains frozen across refresh and replay instead of following the reopened form default',async()=>{
  const storage=memorySessionStorage();const first=await replicationFixture(async(_url,options)=>{if(options.method==='POST')throw new Error('reply lost');},true,{sessionStorage:storage});await first.open();await first.selectLanguage('es');await chooseCategory(first,'image');await first.submit();const frozen=JSON.parse(storage.getItem(replicationStorageKey));assert.equal(frozen.payload.material_category,'image');
  const reopened=await replicationFixture(async()=>null,true,{sessionStorage:storage});await reopened.open();assert.equal(reopened.field('replicationCategory').value,'混剪');assert.match(reopened.field('replicationPendingText').textContent,/素材分类：图片（image）/);await reopened.field('replicationReplay').click();assert.deepEqual(reopened.posts[0],first.posts[0]);assert.equal(reopened.posts[0].material_category,'image');
});

test('category conflicts preserve an unspecified legacy task and show the explicit new value without claiming it applied',async()=>{
  const app=await replicationFixture(async(_url,options,fixture)=>options.method==='POST'?response({item:{...fixture.posts[0],id:'legacy-category',material_category:null,status:'accepted',operator:'owner',effective_langs:['es'],unsupported_langs:[],b_task_id:'B-old'},reused:true,conflict:true,conflict_fields:[{field:'material_category',previous:null,requested:'混剪'}]}):null);await app.open();await app.selectLanguage('es');await app.submit();assert.match(app.field('replicationReceipt').textContent,/素材分类：原值 未指定（旧记录） → 本次 混剪/);assert.match(app.field('replicationReceipt').textContent,/本次新参数未生效/);assert.match(app.field('replicationReceipt').textContent,/素材分类：未指定（旧记录）/);
});

test('legacy history is not reinterpreted as mixed editing and readback detects a new explicit category mismatch',async()=>{
  const app=await replicationFixture(async(_url,options,fixture)=>{if(options.method==='POST'){const request=fixture.posts[0];fixture.history=[{...request,id:'legacy-category-history',operation_id:'older-operation',operation_ids:['older-operation',request.operation_id],material_category:null,status:'accepted',operator:request.operator,effective_langs:['es'],unsupported_langs:[],b_task_id:'B-old'}];throw new Error('reply lost');}});await app.open();await app.selectLanguage('es');await chooseCategory(app,'Dramawave Post');await app.submit();await app.field('replicationVerify').click();assert.match(app.field('replicationReceipt').textContent,/素材分类：原值 未指定（旧记录） → 本次 Dramawave Post/);assert.match(app.field('replicationReceipt').textContent,/本次新参数未生效/);await app.field('replicationViewHistory').click();assert.match(app.field('replicationHistoryList').textContent,/素材分类：未指定（旧记录）/);assert.equal(app.posts.length,1);
});
