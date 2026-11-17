const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const fields = new Map();
const node = () => ({children: [], dataset: {}, value: '', appendChild(n) {this.children.push(n);return n;}});
const field = id => {if (!fields.has(id)) fields.set(id,node());return fields.get(id);};
let confirm = true, resolvePost, fail = false;
const calls = [], toasts = [];
const ui = {
  byId: field, clear: n => n.children=[], positiveId: v => String(v||''),
  element: (tag, options={}) => Object.assign(node(),{tag},options),
  appendTextCell: (row,text) => row.appendChild({text}),formatTime:v=>v||'—',
  statusBadge:(text)=>({text}),statusKind:()=>'',objectValue:v=>v||{},numberValue:(v,f)=>Number(v??f),
  setText:(n,text)=>n.text=text,showToast:(...v)=>toasts.push(v),initShell:()=>{},
  confirmAction:async()=>confirm,queryString:()=>'',readItems:p=>p.items,
  API_BASE:'/api/admin/x-tag-auto-publish',
  api:async(url,options)=>{
    calls.push({url,options});
    if(options?.method==='POST') {
      if(fail) throw Error('请求失败');
      await new Promise(r=>resolvePost=r);
      return {ok:true,message:'本次队列执行已终止'};
    }
    return {items:[{id:'123',status:'canceled',task_count:1}],total:1};
  },
};
const context={window:{XAutoPublish:ui,clearTimeout(){},setTimeout(){return 1;}},URLSearchParams,location:{search:''},console};
let source=fs.readFileSync(path.join(__dirname,'../static/x-tag-auto-publish-runs.js'),'utf8');
source=source.replace('  void ui.initShell({','  window.testRuns={state,renderRuns,terminateRun};\n  void ui.initShell({');
vm.runInNewContext(source,context);
const page=context.window.testRuns;
const buttons=()=>field('runRows').children.flatMap(row=>row.children.flatMap(cell=>cell.children||[])).filter(n=>n.tag==='button');
(async()=>{
  page.state.runs=[{id:'123',status:'running',task_count:178},{id:'124',status:'canceled'},{id:'125',status:'completed'}];
  page.renderRuns();
  assert.equal(buttons().filter(n=>n.dataset.terminateRunId).length,1);
  assert.equal(buttons().filter(n=>n.dataset.runId).length,3);
  confirm=false;await page.terminateRun('123');assert.equal(calls.length,0);
  confirm=true;const first=page.terminateRun('123');await new Promise(r=>setImmediate(r));
  assert.equal(buttons().find(n=>n.dataset.terminateRunId).disabled,true);
  await page.terminateRun('123');assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/admin/x-tag-auto-publish/runs/123/terminate');
  assert.equal(calls[0].options.body,'{}');
  resolvePost();await first;assert.equal(buttons().filter(n=>n.dataset.terminateRunId).length,0);
  assert.equal(page.state.terminating.size,0);
  fail=true;page.state.runs=[{id:'126',status:'queued'}];await page.terminateRun('126');
  assert.equal(toasts.at(-1)[1],true);assert.equal(page.state.terminating.size,0);
  console.log('PASS: active/terminal rows, confirm/cancel, duplicate click, success refresh and failure recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});
