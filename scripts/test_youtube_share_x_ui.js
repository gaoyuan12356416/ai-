// Isolated UI state/event/render contracts. No browser, network or social posts.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const root = path.resolve(__dirname,'..');
const source = fs.readFileSync(path.join(root,'static/youtube-publish.js'),'utf8').replace(/\r\n/g,'\n');
const taskId = 'a'.repeat(32), runId = 'b'.repeat(32), videoId = 'abcDEF123_-';
const sourceContext = {task_id:taskId,title:'A public video',video_id:videoId,youtube_url:'https://www.youtube.com/watch?v=' + videoId,channel_name:'Channel'};
const accounts = [{id:1,name:'First',username:'first',selectable:true},{id:2,name:'Blocked',username:'blocked',selectable:false,block_reason:'授权已失效'},{id:3,name:'Third',username:'third',selectable:true}];
const contextData = () => ({source:sourceContext,accounts,player_card:{eligible:true,state:'ready'},macros:[{key:'title',label:'标题',value:'A public video'},{key:'youtube_url',label:'视频链接',value:sourceContext.youtube_url},{key:'short_url',label:'推广短链',value:'https://example.invalid/short'}],default_template:'{title}\n\n{youtube_url}',history:[],limit:280});
const preview = text => ({text,weighted_length:45,limit:280,errors:[],appended_url:true,valid:true});
const run = extra => ({id:runId,status:'queued',account_ids:[1,3],description_template:'{title}',text:'A public video\n\n' + sourceContext.youtube_url,items:[{account_id:1,username:'first',status:'queued'},{account_id:3,username:'third',status:'queued'}],...extra});
const plain = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let n=0;n<8;n++) await Promise.resolve(); };

function harness() {
  const listeners = new Map(), nodes = new Map(), timers = new Map();
  let nextTimer=0, nextUuid=0;
  const doc = {hidden:false,activeElement:{focus(){}},addEventListener(name,fn){listeners.set(name,[...(listeners.get(name)||[]),fn]);},querySelector(selector){if(!nodes.has(selector))nodes.set(selector,{dataset:{},addEventListener(){},focus(){},value:''});return nodes.get(selector);}};
  const sandbox = {document:doc,window:{addEventListener(){}},location:{origin:'https://local.invalid',search:''},URL,URLSearchParams,TextEncoder,AbortController,Event,crypto:{randomUUID:()=>`00000000-0000-4000-8000-${String(++nextUuid).padStart(12,'0')}`},setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);},console};
  vm.createContext(sandbox);
  assert.ok(source.includes('\n  init();\n'));
  vm.runInContext(source.replace('\n  init();\n',`\n  renderModals = () => {};\n  globalThis.ui = {state,carets,shareXDrafts,shareXBlockReason,shareXButton,shareXSelected,shareXPreviewValid,shareXRunItems,shareXRunView,shareXView,shareXAcceptRun,loadShareX,scheduleShareXPreview,updateShareXPreview,openShareX,checkShareX,submitShareX,newShareX,stopShareX,closeModal,top,rememberCaret,setApi:fn => { api = fn; },setStack:value => { stack = value; }};\n`),sandbox,{filename:'youtube-publish.js'});
  const ui=sandbox.ui;
  function view(extra={}) { const v={type:'shareX',id:taskId,key:1,loaded:true,loading:false,source:sourceContext,playerCard:contextData().player_card,accounts:plain(accounts),macros:contextData().macros,history:[],selected:new Set([1,3]),description:'{title}',operationId:sandbox.crypto.randomUUID(),previewSerial:0,preview:preview('A public video'),previewFor:'{title}',...extra};ui.setStack([v]);return v; }
  function emit(type,target) { for(const fn of listeners.get(type)||[])fn({target}); }
  function click(action,id) { const button={disabled:false,dataset:{action,id},closest(){return this;}};emit('click',button); }
  return {ui,view,emit,click,nodes,timers,doc,sandbox,fire(ms){const pending=[...timers].filter(([,timer])=>timer.ms===ms);for(const [id,timer] of pending){timers.delete(id);timer.fn();}return pending.length;}};
}

test('only confirmed public tasks expose the share action, including first-comment failure',() => {
  const {ui}=harness(), base={id:taskId,video_id:videoId};
  assert.equal(ui.shareXBlockReason({...base,status:'published'}),'');
  assert.equal(ui.shareXBlockReason({...base,status:'comment_failed'}),'');
  assert.equal(ui.shareXBlockReason({...base,status:'uploading',steps:[{key:'public',status:'complete'}]}),'');
  for(const status of ['review','thumbnail_failed','scheduled','schedule_pending','schedule_missed','cancelled','publish_failed'])assert.ok(ui.shareXBlockReason({...base,status}));
  assert.match(ui.shareXBlockReason({...base,status:'scheduled'}),/等待公开/);
  assert.match(ui.shareXBlockReason({...base,status:'thumbnail_failed'}),/私享/);
  assert.ok(ui.shareXBlockReason({status:'published',video_id:''}));
  assert.ok(ui.shareXBlockReason({status:'published',video_id:'javascript:alert(1)'}));
  assert.equal(ui.shareXBlockReason({status:'processing',can_share_x:true}),'');
  assert.ok(ui.shareXBlockReason({...base,status:'published',can_share_x:false}));
  assert.equal(ui.shareXBlockReason({...base,status:'published',can_share_x:false,share_x_block_reason:'平台尚未确认公开'}),'平台尚未确认公开');
  assert.match(ui.shareXButton({...base,status:'review'}),/disabled title=.*aria-describedby/);
  assert.doesNotMatch(ui.shareXButton({...base,status:'comment_failed'}),/ disabled/);
});

test('opening and selecting accounts never submits; disabled accounts cannot enter the payload',async () => {
  const h=harness(), calls=[];h.ui.state.tasks=[{id:taskId,status:'published',video_id:videoId}];
  h.ui.setApi(async (route,options={})=>{calls.push({route,options});return contextData();});
  h.ui.openShareX(taskId);await settle();
  const v=h.ui.top();assert.equal(v.loaded,true);assert.equal(v.description,contextData().default_template);
  assert.equal(calls.length,1);assert.equal(calls[0].options.method,undefined);
  h.emit('change',{dataset:{shareAccount:'2'},checked:true});assert.equal(v.selected.has(2),false);
  h.click('select-all-share-x');assert.deepEqual(plain(h.ui.shareXSelected(v)),[1,3]);
  assert.equal(calls.length,1);
});

test('account selection is capped at 20 and templates never impose Premium membership',() => {
  const h=harness(), v=h.view({accounts:Array.from({length:25},(_,i)=>({id:i+1,name:'Account ' + i,selectable:true,subscription_type:'none'})),selected:new Set()});
  h.click('select-all-share-x');assert.equal(v.selected.size,20);
  h.emit('change',{dataset:{shareAccount:'25'},checked:true});assert.equal(v.selected.has(25),false);
  assert.match(h.ui.shareXView(v),/已选 20 \/ 20 个/);
  assert.match(h.ui.shareXView(v),/data-share-account="25"\s+disabled/);
  assert.doesNotMatch(h.ui.shareXView(v),/Premium/);
});

test('debouncing sends only the latest description and makes stale previews unsubmitable',async () => {
  const h=harness(), v=h.view(), calls=[];
  h.ui.setApi(async(route,options)=>{calls.push(JSON.parse(options.body));return preview('Latest final text');});
  h.emit('input',{id:'x-share-description',dataset:{},value:'first'});
  h.emit('input',{id:'x-share-description',dataset:{},value:'latest'});
  assert.equal(h.ui.shareXPreviewValid(v),false);assert.equal(h.fire(350),1);await settle();
  assert.deepEqual(calls,[{description_template:'latest'}]);
  assert.equal(v.preview.text,'Latest final text');assert.equal(h.ui.shareXPreviewValid(v),true);
});

test('out-of-order preview responses cannot overwrite the current server preview',async () => {
  const h=harness(), v=h.view({description:'older',previewSerial:1}), resolvers=[];
  h.ui.setApi(()=>new Promise(resolve=>resolvers.push(resolve)));
  const first=h.ui.updateShareXPreview(v,1,'older');
  v.description='newer';v.previewSerial=2;
  const second=h.ui.updateShareXPreview(v,2,'newer');
  resolvers[1](preview('Newer final text'));await second;
  resolvers[0](preview('Older final text'));await first;
  assert.equal(v.preview.text,'Newer final text');assert.equal(v.previewFor,'newer');
});

test('server validation, preview failure, and non-current content block submit',async () => {
  const h=harness(), v=h.view(), calls=[];
  h.ui.setApi(async(route,options)=>{calls.push({route,options});return {run:run()};});
  for(const update of [{playerCard:{eligible:false,message:'播放卡片不可用'}},{previewLoading:true},{previewFor:'old'},{contextError:'permissions changed'},{preview:{...preview('too long'),weighted_length:281}},{preview:{...preview('bad macro'),valid:false,errors:['未知宏参数']}},{preview:{...preview('bad format'),weighted_length:NaN}}]) {
    Object.assign(v,{playerCard:{eligible:true},previewLoading:false,previewFor:v.description,contextError:'',preview:preview('Valid')},update);
    assert.equal(h.ui.shareXPreviewValid(v),false);await h.ui.submitShareX(v);
  }
  assert.equal(calls.length,0);
  Object.assign(v,{previewSerial:7,previewLoading:true});h.ui.setApi(async()=>{throw new Error('预览服务失败');});
  await h.ui.updateShareXPreview(v,7,v.description);assert.equal(v.previewError,'预览服务失败');assert.equal(h.ui.shareXPreviewValid(v),false);
});

test('macro insertion replaces the saved selection and restores the caret and input value',() => {
  for (const macro of ['youtube_url','short_url']) {
  const h=harness(), v=h.view({description:'alpha replace omega'});
  assert.ok(h.ui.shareXView(v).includes('data-macro="' + macro + '"'));
  const input={id:'x-share-description',dataset:{},value:v.description,disabled:false,selectionStart:6,selectionEnd:13,matches:()=>true,focus(){h.doc.activeElement=this;},setRangeText(text,start,end){this.value=this.value.slice(0,start)+text+this.value.slice(end);this.selectionStart=this.selectionEnd=start+text.length;},dispatchEvent(event){h.emit(event.type,this);}};
  h.nodes.set('#x-share-description',input);h.ui.rememberCaret(input);
  const button={disabled:false,dataset:{action:'insert-macro',target:'x-share-description',macro},closest(){return this;}};
  h.emit('click',button);
  assert.equal(input.value,'alpha {' + macro + '} omega');assert.equal(v.description,input.value);
  assert.equal(input.selectionStart,8 + macro.length);assert.equal(input.selectionEnd,8 + macro.length);assert.equal(h.doc.activeElement,input);
  }
});

test('uncertain submission freezes the exact operation and payload across close and reopen',async () => {
  const h=harness(), v=h.view(), writes=[];h.ui.shareXDrafts.set(taskId,v);h.ui.state.tasks=[{id:taskId,status:'published',video_id:videoId}];
  h.ui.setApi(async(route,options={})=>{if(options.method==='POST'){writes.push(JSON.parse(options.body));throw Object.assign(new Error('network timeout'),{uncertain:true});}return contextData();});
  const operationId=v.operationId;await h.ui.submitShareX(v);
  assert.equal(v.uncertain,true);assert.equal(Object.isFrozen(v.pendingPayload),true);
  h.emit('input',{id:'x-share-description',dataset:{},value:'changed content'});
  h.emit('change',{dataset:{shareAccount:'1'},checked:false});h.click('clear-share-x');
  assert.equal(v.description,'{title}');assert.equal(v.selected.has(1),true);
  h.ui.closeModal();h.ui.openShareX(taskId);await settle();
  assert.equal(h.ui.top(),v);assert.equal(v.operationId,operationId);assert.equal(writes.length,1);
  h.ui.setApi(async(route,options={})=>{if(options.method==='POST'){writes.push(JSON.parse(options.body));return {run:run({operation_id:operationId})};}return contextData();});
  await h.ui.submitShareX(v);
  assert.deepEqual(writes[1],writes[0]);assert.equal(v.run.id,runId);assert.equal(v.uncertain,false);
});

test('history readback recovers an uncertain operation without issuing another submit',async () => {
  const h=harness(), v=h.view({uncertain:true}), calls=[];
  v.pendingPayload={operation_id:v.operationId,account_ids:[1,3],description_template:v.description};
  const saved=run({operation_id:v.operationId,status:'completed',items:[{account_id:1,status:'published',post_url:'https://x.com/first/status/123'},{account_id:3,status:'unknown_outcome'}]});
  h.ui.setApi(async(route,options={})=>{calls.push({route,options});return route.includes('/runs/')?{run:saved}:{...contextData(),history:[saved]};});
  await h.ui.checkShareX(v);
  assert.equal(v.run.status,'completed');assert.equal(v.uncertain,false);assert.equal(v.preview.text,saved.text);
  assert.ok(calls.every(call=>!call.options.method));assert.equal(calls.length,2);
  assert.match(h.ui.shareXView(v),/已提交内容/);
});

test('a later authorization rejection never releases an earlier uncertain operation',async () => {
  const h=harness(), v=h.view({uncertain:true});
  v.pendingPayload=Object.freeze({operation_id:v.operationId,account_ids:[1,3],description_template:v.description});
  const body=v.pendingPayload, operation=v.operationId;
  h.ui.setApi(async()=>{throw Object.assign(new Error('权限已变更'),{status:403});});
  await h.ui.submitShareX(v);
  assert.equal(v.uncertain,true);assert.equal(v.pendingPayload,body);assert.equal(v.operationId,operation);
  assert.match(h.ui.shareXView(v),/按原内容核对提交/);
});

test('an initial pre-enqueue 409 rejection restores editing and account selection',async () => {
  for(const code of ['x_account_disabled','youtube_not_public','youtube_player_unavailable','youtube_not_embeddable']) {
    const h=harness(), v=h.view(), operation=v.operationId;
    h.ui.setApi(async()=>{throw Object.assign(new Error('尚未入队'),{status:409,code,uncertain:false});});
    await h.ui.submitShareX(v);
    assert.equal(v.uncertain,false);assert.equal(v.pendingPayload,null);assert.notEqual(v.operationId,operation);
    if (['youtube_player_unavailable','youtube_not_embeddable'].includes(code)) {
      assert.equal(v.playerCard.eligible,false);assert.match(h.ui.shareXView(v),/重新检查播放器/);
    }
    h.emit('change',{dataset:{shareAccount:'1'},checked:false});
    h.emit('input',{id:'x-share-description',dataset:{},value:'Corrected text'});
    assert.equal(v.selected.has(1),false);assert.equal(v.description,'Corrected text');
    assert.doesNotMatch(h.ui.shareXView(v),/按原内容核对提交/);
  }
});

test('explicit idempotency conflicts keep the original operation frozen',async () => {
  for(const code of ['operation_conflict','youtube_share_idempotency_conflict','idempotency_conflict']) {
    const h=harness(), v=h.view(), operation=v.operationId;
    h.ui.setApi(async()=>{throw Object.assign(new Error('操作已存在'),{status:409,code,uncertain:false});});
    await h.ui.submitShareX(v);
    assert.equal(v.uncertain,true);assert.equal(v.operationId,operation);assert.equal(v.pendingPayload.operation_id,operation);
  }
});

test('server 500 uncertainty keeps the same payload and operation',async () => {
  const h=harness(), v=h.view(), operation=v.operationId;
  h.ui.setApi(async()=>{throw Object.assign(new Error('提交结果未知'),{status:500,uncertain:true});});
  await h.ui.submitShareX(v);
  assert.equal(v.uncertain,true);assert.equal(v.operationId,operation);assert.equal(v.pendingPayload.operation_id,operation);
});

test('closing cancels polling; reopening resumes readback of the original active run',async () => {
  const h=harness(), v=h.view({run:run()}), calls=[];
  h.ui.shareXDrafts.set(taskId,v);h.ui.state.tasks=[{id:taskId,status:'published',video_id:videoId}];
  h.ui.setApi(async(route,options={})=>{calls.push({route,options});return route.includes('/runs/')?{run:run()}:{...contextData(),history:[run()]};});
  await h.ui.checkShareX(v);assert.ok([...h.timers.values()].some(timer=>timer.ms===2000));
  h.ui.closeModal();assert.ok(![...h.timers.values()].some(timer=>timer.ms===2000));
  h.ui.openShareX(taskId);await settle();assert.ok(calls.filter(call=>call.route.endsWith('/runs/'+runId)).length>=2);
  assert.ok(calls.every(call=>!call.options.method));
});

test('partial and duplicate outcomes are explicit, all returned content is escaped, unsafe links omitted',() => {
  const h=harness(), v=h.view();
  const mixed=run({status:'completed',items:[{account_id:1,username:'<img src=x onerror=alert(1)>',status:'published',duplicate:true,post_url:'https://x.com/first/status/123'},{account_id:2,status:'failed',message:'<script>alert(1)</script>'},{account_id:3,status:'unknown_outcome',post_url:'javascript:alert(1)'}]});
  const html=h.ui.shareXRunView(mixed);
  assert.match(html,/成功 1 · 失败 1 · 待核对 1/);assert.match(html,/沿用已有转发记录/);assert.match(html,/系统不会自动重发/);
  assert.doesNotMatch(html,/<script>|<img src=x|href="javascript:/);assert.match(html,/&lt;script&gt;/);assert.match(html,/rel="noopener noreferrer"/);
  v.source={...sourceContext,title:'<img onerror="x">',youtube_url:'javascript:x'};v.accounts=[{id:2,name:'<script>x</script>',selectable:false,block_reason:'<b>blocked</b>'}];
  const dialog=h.ui.shareXView(v);assert.doesNotMatch(dialog,/<script>|<b>blocked|href="javascript:/);assert.match(dialog,/&lt;b&gt;blocked/);assert.match(dialog,/由 YouTube 和 X 决定/);
});

test('a new operation requires explicit action after terminal results and starts with no accounts',async () => {
  const h=harness(), v=h.view({run:run({status:'completed'})});const oldId=v.operationId;
  h.ui.setApi(async()=>contextData());h.ui.newShareX(v);await settle();
  assert.notEqual(v.operationId,oldId);assert.equal(v.selected.size,0);assert.equal(v.pendingPayload,null);assert.equal(v.run,null);
  v.run=run({status:'running'});const activeId=v.operationId;h.ui.newShareX(v);assert.equal(v.operationId,activeId);
});

test('assets keep uploader selection and add the new modal through the existing focus updater',() => {
  const html=fs.readFileSync(path.join(root,'static/youtube-publish.html'),'utf8');
  const css=fs.readFileSync(path.join(root,'static/youtube-publish.css'),'utf8');
  assert.match(source,/material-uploader/);assert.match(source,/uploader_id/);assert.match(source,/shareX:shareXView/);
  assert.match(source,/listRevision/);assert.match(source,/taskLoadingView\(v\)/);assert.match(source,/compact:'1'/);
  assert.match(source,/retained\.setSelectionRange\(\.\.\.selection\)/);assert.match(source,/shareXButton\(t\)/);
  assert.match(html,/youtube-publish\.js\?v=20260917-share-player-v2/);assert.match(html,/youtube-publish\.css\?v=20260916-share-x-v1/);
  assert.match(css,/@media\(max-width:760px\)\{\.x-share-layout\{grid-template-columns:1fr/);
});
