const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function editor(lane) {
  const fields = new Map();
  const field = id => { if (!fields.has(id)) fields.set(id, {value:'', checked:false}); return fields.get(id); };
  const ui = {byId:field,pageIdFromQuery:()=>'',positiveId:()=>'',initShell:()=>{},init:()=>{}};
  const context = {window:{XAutoPublish:ui,FBAutoPublishUI:ui,location:{search:''}},document:{getElementById:field,querySelectorAll:()=>[]},URLSearchParams,console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/drama-launch-window.js'),'utf8'),context);
  const name = lane==='x' ? 'x-tag-auto-publish-template.js' : 'fb-auto-publish-template.js';
  let source = fs.readFileSync(path.join(__dirname,'../static',name),'utf8');
  const point = source.lastIndexOf('  void ui.initShell(');
  if (point>=0) source=source.slice(0,point)+'  window.testEditor={state,buildPayload};\n})();';
  else {
    const pos=source.lastIndexOf('  ui.init(');
    const init=pos>=0 ? pos : source.lastIndexOf('  void ui.boot(');
    assert.ok(init>=0,'Find FB initialization boundary');
    source=source.slice(0,init)+'  window.testEditor={state,buildPayload};\n})();';
  }
  vm.runInNewContext(source,context);
  const e=context.window.testEditor;
  if(lane==='x') {
    e.state.selectedAccountIds.add('101');
    Object.entries({templateName:'Test',bodyTemplate:'{{drama_name}} {{desc}}',metricWindowDays:'1',cooldownDays:'0',materialDurationMin:'60',materialDurationMax:'',randomDailyCount:'2',platform:'0',dramaSortBy:'spend',dramaSortDirection:'desc',materialSortBy:'spend',materialSortDirection:'desc'}).forEach(([k,v])=>field(k).value=v);
    field('scheduleModeRandom').checked=true;
  }
  const id=lane==='x'?'dramaLaunchWindowDays':'launchDays';
  for(const [v,want] of [['-1~0','-1~0'],['【-2～-1】','-2~-1'],['0',0],['7',7]]) {
    field(id).value=v;
    assert.equal(e.buildPayload().drama_launch_window_days,want);
  }
  field(id).value='0~-1';assert.throws(()=>e.buildPayload());
}
editor('x');editor('fb');console.log('PASS: X and FB real editor payloads preserve signed ranges and legacy integers');
