'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const node = {dataset:{},addEventListener(){}};
const context = {document:{querySelector:()=>node,addEventListener(){}},
  window:{addEventListener(){}},location:{origin:'https://ai.yingliangads.com'},
  URL,URLSearchParams,TextEncoder,console,setTimeout:()=>1,clearTimeout(){}};
vm.createContext(context);
let source = fs.readFileSync(path.join(__dirname,'../static/youtube-publish.js'),'utf8');
source = source.replace(/  init\(\);\s*\}\)\(\);\s*$/, `
  renderModals=()=>{}; showSource=()=>{};
  globalThis.uiTest={loadMaterials,pickerView,readMaterials,
    setup(view,request){stack=[view];state.source={configured:true};api=request;},
    cache:(key,data)=>materialCache.set(key,{data,at:Date.now()})};
})();`);
vm.runInContext(source, context);
const ui = context.uiTest;

(async()=>{
  const view = {type:'picker',search:'',uploaderId:'947',items:[]};
  ui.setup(view,async()=>({items:[],uploaders:[],configured:true,cache:{refreshing:true}}));
  await ui.loadMaterials(view);
  assert.match(ui.pickerView(view),/正在加载素材/);
  ui.setup(view,async()=>{throw new Error('素材更新失败');});
  await ui.loadMaterials(view,false,true);
  assert.equal(view.loading,false);
  assert.equal(view.cache.refreshing,false);
  const failed = ui.pickerView(view);
  assert.ok(!failed.includes('正在加载素材'));
  assert.ok(!failed.includes('素材正在更新'));
  assert.match(failed,/重新加载/);

  const item = {id:'3',name:'Video',uploader_id:'947'};
  ui.setup(view,async()=>({items:[item],uploaders:[],configured:true,cache:{refreshing:false}}));
  await ui.loadMaterials(view,true);
  assert.equal(view.error,'');
  assert.match(ui.pickerView(view),/Video/);
  view.selected=item;
  ui.setup(view,async()=>{throw new Error('network failure');});
  await ui.loadMaterials(view,true);
  assert.equal(view.items[0],item);
  assert.equal(view.selected,item);
  assert.match(ui.pickerView(view),/Video/);

  let rejectOld;
  view.uploaderId='904';
  ui.setup(view,()=>new Promise((_,reject)=>{rejectOld=reject;}));
  const old=ui.loadMaterials(view);
  view.uploaderId='824';
  ui.setup(view,async()=>({items:[{id:'4',name:'New uploader'}],configured:true,cache:{refreshing:false}}));
  await ui.loadMaterials(view);
  rejectOld(new Error('old request failure')); await old;
  assert.equal(view.error,'');
  assert.equal(view.items[0].name,'New uploader');

  let calls=0;
  ui.cache(JSON.stringify(['','1']),{items:[],cache:{refreshing:false,stale:false,error:'stale failure',age_seconds:0,ttl_seconds:300}});
  ui.setup(view,async()=>{calls++;return {items:[item]};});
  await ui.readMaterials('',false,'1');
  assert.equal(calls,1);
  console.log('Passed: polling failure, explicit retry, retained material/selection, stale response isolation, failed cache refresh.');
})().catch(e=>{console.error(e);process.exitCode=1;});
