'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const node = {dataset:{},addEventListener(){}};
let routed;
const context = {document:{querySelector:()=>node,addEventListener(){}},
  window:{addEventListener(){},createYoutubeRegular:()=>({start(rows){routed=rows;}})},location:{origin:'https://ai.yingliangads.com'},
  URL,URLSearchParams,TextEncoder,AbortController,console,setTimeout,clearTimeout};
vm.createContext(context);
let source = fs.readFileSync(path.join(__dirname,'../static/youtube-publish.js'),'utf8');
source = source.replace(/  init\(\);\s*\}\)\(\);\s*$/, `
  renderModals=()=>{}; showSource=()=>{}; closeModal=()=>{stack.pop();};
  globalThis.uiTest={selectMaterial,confirmMaterial,pickerView,publicWatchUrl,
    setup(view,request){stack=[view];state.source={configured:true};api=request;draft={title:'',errors:{},material:null};},
    applied:()=>draft.material, close:()=>{stack=[];}};
})();`);
vm.runInContext(source,context);
const ui=context.uiTest;
const card = id => ({id,name:'Video '+id,metadata_pending:true});
const full = id => ({id,name:'Video '+id,macro_name:'Drama '+id,link_ready:true,drama_cover_status:'available'});

(async()=>{
  const receipt={regular_media:{},video_id:'Az01_-bcDeF',video_url:'https://example.org/private',can_share_x:false};
  assert.equal(ui.publicWatchUrl(receipt),'');
  assert.equal(ui.publicWatchUrl({...receipt,can_share_x:true}),'https://www.youtube.com/watch?v=Az01_-bcDeF');
  assert.equal(ui.publicWatchUrl({...receipt,can_share_x:true,video_id:'invalid'}),'');
  assert.equal(ui.publicWatchUrl({video_url:'https://example.org/legacy'}),'https://example.org/legacy');
  const editorFinal={...full('42'),url:'https://example.org/ai-video-editing/youtube-final/v1/at_'+'a'.repeat(32)+'-'+'2'.repeat(64)+'.mp4'};
  const legacyPicker={type:'picker',selected:editorFinal};
  ui.setup(legacyPicker,()=>{});ui.confirmMaterial(legacyPicker);
  assert.equal(routed[0].id,'42');assert.equal(ui.applied(),null);
  const view={type:'picker',items:[card('1'),card('2')]};
  let finishFirst;
  ui.setup(view,()=>new Promise(resolve=>{finishFirst=resolve;}));
  const first=ui.selectMaterial(view,view.items[0]);
  ui.confirmMaterial(view);
  assert.equal(ui.applied(),null);
  assert.match(ui.pickerView(view),/正在读取素材详情/);
  ui.setup(view,async()=>({items:[full('2')]}));
  await ui.selectMaterial(view,view.items[1]);
  finishFirst({items:[full('1')]}); await first;
  assert.equal(view.selected.id,'2');
  assert.equal(view.detailLoading,false);
  ui.confirmMaterial(view); assert.equal(ui.applied().macro_name,'Drama 2');

  ui.setup(view,async()=>{throw new Error('network failure');});
  await ui.selectMaterial(view,card('1'));
  assert.equal(view.detailLoading,false);
  assert.match(view.detailError,/network failure/);
  ui.confirmMaterial(view);assert.equal(ui.applied(),null);
  ui.setup(view,async()=>({items:[],cache:{refreshing:false}}));
  await ui.selectMaterial(view,card('1'));
  assert.match(view.detailError,/不在可发布范围/);

  let finishClosed;
  ui.setup(view,()=>new Promise(resolve=>{finishClosed=resolve;}));
  const closed=ui.selectMaterial(view,card('1'));
  ui.close(); finishClosed({items:[full('1')]});await closed;
  assert.equal(view.selected.metadata_pending,true);
  assert.equal(ui.applied(),null);

  let calls=0;
  ui.setup(view,async url=>{assert.match(url,/\/materials\?search=1$/);return ++calls===1 ? {items:[],cache:{refreshing:true}} : {items:[full('1')]};});
  await ui.selectMaterial(view,card('1'));
  assert.equal(calls,2);assert.equal(view.selected.macro_name,'Drama 1');
  console.log('Passed: pending guard, selection race, error/retry, removed material, closed modal and exact-ID polling.');
})().catch(e=>{console.error(e);process.exitCode=1;});
