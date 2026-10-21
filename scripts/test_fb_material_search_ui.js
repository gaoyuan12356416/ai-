/* Exercise the real picker control flow with delayed reads; no network/writes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname,"../static/fb-video-publish.js"),"utf8");
const body = source.slice(source.indexOf("  async function materialPickerDialog("),source.indexOf("  const poolStorageKey="));
const elements = new Map(), pending = [], calls = [];
let current, applied;
const field = id => ({id,disabled:false,value:"",textContent:"",innerHTML:"",dataset:{},setAttribute(k,v){this[k]=v;},focus(){},click(){if(!this.disabled)this.onclick?.();}});
const context = {
  Map,URLSearchParams,console,
  $:id=>id==="materialPicker"?(current?.isConnected?current:null):elements.get(id),
  languages:new Map([["ja","日语"],["en","英语"]]),lang:x=>x,langOptions:()=>"",esc:x=>String(x??"").replaceAll("<","&lt;"),
  errorAt:(id,e)=>{elements.get(id).textContent=e?.message||String(e||"");},
  dialog:()=>{
    for(const id of ["pickerError","materialSearch","materialLanguage","materialSearchButton","pickerCount","pickCurrent","clearCurrent","materialChoices","materialsPrev","materialsNext","applyMaterials"])elements.set(id,field(id));
    current={isConnected:true,close(){this.isConnected=false;},focus(){}};return current;
  },
  api:path=>{calls.push(path);return new Promise((resolve,reject)=>pending.push({resolve,reject}));},
};
vm.createContext(context);vm.runInContext(body,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const material={material_id:"42",material_name:"fixture",language:"ja",duration_seconds:30};
(async()=>{
  const opening=context.materialPickerDialog(new Map(),m=>{applied=m;});
  assert.equal(calls.length,1);
  for(const id of ["materialSearch","materialLanguage","materialSearchButton","materialsNext","pickCurrent"])assert.equal(elements.get(id).disabled,true);
  assert.match(elements.get("materialChoices").innerHTML,/正在搜索/);
  elements.get("materialSearchButton").onclick();elements.get("materialsNext").onclick();
  await context.materialPickerDialog(new Map(),()=>{});
  assert.equal(calls.length,1,"rapid requests and reopening are coalesced");
  pending.shift().resolve({items:[material],next_cursor:"next"});await opening;
  assert.equal(elements.get("materialSearchButton").disabled,false);
  elements.get("materialChoices").onchange({target:{dataset:{material:"42"},checked:true}});
  elements.get("materialSearch").value="3174214";elements.get("materialSearchButton").onclick();
  assert.equal(calls.length,2);assert.match(calls[1],/q=3174214/);
  pending.shift().resolve({items:[],empty_reason:"时长缺失 <unsafe>"});await flush();
  assert.match(elements.get("materialChoices").innerHTML,/时长缺失 &lt;unsafe>/);
  assert.match(elements.get("pickerCount").textContent,/1/);
  elements.get("materialSearchButton").onclick();pending.shift().reject(new Error("temporary"));await flush();
  assert.equal(elements.get("materialSearchButton").disabled,false);
  assert.equal(elements.get("materialsNext").disabled,true);
  assert.match(elements.get("materialChoices").innerHTML,/已选素材已保留/);
  elements.get("applyMaterials").onclick();assert.equal(applied.size,1);
  const old=context.materialPickerDialog(new Map(),()=>{});const oldRequest=pending.shift();current.close();
  const fresh=context.materialPickerDialog(new Map(),()=>{}, {language:"ja"});
  oldRequest.resolve({items:[material]});await old;
  assert.equal(elements.get("materialSearchButton").disabled,true,"closed response must not unlock new dialog");
  pending.shift().resolve({items:[]});await fresh;
  assert.equal(elements.get("materialLanguage").disabled,true,"pool language stays locked");
  assert.equal(elements.get("materialSearchButton").disabled,false);
  console.log("PASS: picker loading, duplicate suppression, exact reason, selection preservation, retry, stale response, locked language");
})().catch(e=>{console.error(e);process.exitCode=1;});
