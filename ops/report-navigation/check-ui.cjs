const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/ai-image-daily.js'), 'utf8');
async function check(auth, expected) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {hidden:false, events:{},
      addEventListener(event, fn) { this.events[event] = fn; },
      removeAttribute(key) { delete this[key]; }, getAttribute(key) { return this[key]; }});
    return elements.get(id);
  };
  const context = {document:{getElementById:get}, fetch:async()=>({ok:true,json:async()=>auth}),
    UiTopbar:{render(){},handleAuthAction(){}},QuickNav:{render:async()=>{}},
    setTimeout:()=>1,clearTimeout(){}};
  vm.runInNewContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(!!get('reportFrame').src, expected);
  assert.equal(get('reportPanel').hidden, !expected);
  if (expected) {
    assert.match(get('reportFrame').src, /\/ai-image-daily\/index.html$/);
    await get('authButton').events.click();
    assert.equal(get('reportFrame').src, undefined);
  }
}
(async () => {
  await check({authenticated:false}, false);
  await check({authenticated:true,user:{permissions:{}}}, false);
  await check({authenticated:true,user:{permissions:{social_analytics:true}}}, true);
  await check({authenticated:true,user:{is_admin:true}}, true);
  const old = [{key:'social_reports',label:'社媒数据报表',enabled:false,module:'social_analytics',items:[
    {key:'socialAnalytics',label:'发布效果分析',enabled:false,module:'social_analytics'}]}];
  const context = {window:{},localStorage:{getItem:()=>null,setItem(){}},
    fetch:async()=>({ok:true,json:async()=>old}),setTimeout,clearTimeout,Date};
  vm.runInNewContext(fs.readFileSync(path.join(root,'static/quick-nav.js'),'utf8'),context);
  const config = await context.window.QuickNav.loadConfig();
  const report = config.find(g=>g.key==='social_reports');
  assert.equal(report.label,'数据报表');
  assert.equal(report.enabled,false);
  assert.equal(report.items[0].enabled,false);
  assert.equal(report.items[0].label,'社媒效果分析');
  assert.equal(report.items.filter(i=>i.key==='aiImageDaily').length,1);
  console.log('PASS: anonymous, denied, allowed, admin, logout, legacy navigation and disabled flags');
})().catch(error=>{console.error(error);process.exitCode=1;});
