const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function element() {
  return { children: [], dataset: {}, style: {}, value: '', checked: false,
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; } };
}
for (const lane of ['x-tag', 'fb']) {
  const fields = new Map();
  const field = id => { if (!fields.has(id)) fields.set(id, element()); return fields.get(id); };
  field('materialProducts').dataset.sourceOption = lane === 'fb' ? 'true' : 'false';
  const allInputs = () => field('materialProducts').children.map(label => label.children[0]);
  const doc = { getElementById: field, createElement: element,
    querySelectorAll: selector => selector.includes('materialProducts') ? allInputs().filter(i => i.checked) : [] };
  const ui = { byId: field, positiveId: () => '9', pageIdFromQuery: () => '9', initShell() {}, boot() {} };
  const ctx = { window: { XAutoPublish: ui, FBAutoPublishUI: ui, location: { search: '?id=9' },
    normalizeDramaLaunchWindow: value => Number(value) }, document: doc, URLSearchParams };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/material-product-picker.js'),'utf8'),ctx);
  const picker = ctx.window.MaterialProductPicker;
  picker.render();
  assert.deepEqual(Array.from(picker.read()), ['Dramawave']);
  for (const values of [['Moboreels','Moboshort'],['Dramawave','Moboreels','Moboshort']]) {
    picker.render({ material_products: values });
    assert.deepEqual(Array.from(picker.read()),values);
  }
  picker.render({material_product:'SavedCustomProduct'});
  assert.deepEqual(Array.from(picker.read()),['SavedCustomProduct']);
  allInputs().forEach(i=>i.checked=false);
  assert.throws(()=>picker.read(),/至少选择/);
  picker.render({material_products:['Moboreels','Moboshort']});
  let source = fs.readFileSync(path.join(__dirname,`../static/${lane}-auto-publish-template.js`),'utf8');
  const hook = lane === 'fb' ? '  void ui.boot({' : '  void ui.initShell({';
  source = source.replace(hook,'  window.editor = { state, buildPayload };\n'+hook);
  vm.runInNewContext(source,ctx);
  const editor=ctx.window.editor;
  if (lane==='x-tag') {
    editor.state.selectedAccountIds.add('101');
    Object.entries({templateName:'Products',bodyTemplate:'{{drama_name}}\n{{desc}}',metricWindowDays:'7',
      dramaLaunchWindowDays:'0',cooldownDays:'0',randomDailyCount:'2',platform:'0',
      dramaSortBy:'spend',dramaSortDirection:'desc',materialSortBy:'spend',materialSortDirection:'desc'})
      .forEach(([id,value])=>field(id).value=value);
    field('scheduleModeRandom').checked=true;
  } else {
    Object.entries({name:'Products',message:'{{drama_name}}',videoTemplate:'random_overlay',dataSource:'6',
      metricDays:'7',launchDays:'0',cooldown:'0',scheduleMode:'fixed',fixedTimes:'10:30'})
      .forEach(([id,value])=>field(id).value=value);
  }
  assert.deepEqual(Array.from(editor.buildPayload().material_products),['Moboreels','Moboshort']);
  assert.match(source,/MaterialProductPicker\.render\(config\)/);
  console.log(`PASS ${lane}: defaults, multi-select, legacy values, empty selection, real editor payload`);
}
