const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../features/gpu_offload/campaign/index.html'), 'utf8');
const source = Array.from(html.matchAll(/<script>([\s\S]*?)<\/script>/g), item => item[1]);
for (const script of source) new vm.Script(script);
function actual(name) {
  const pattern = new RegExp('^  function ' + name + '\\([^]*?(?=^  function |^  async function |^  const )', 'm');
  return html.match(pattern)[0];
}

function page() {
  const fields = new Map();
  const get = id => {
    if (!fields.has(id)) fields.set(id, {replaceChildren(){this.cleared=true;}});
    return fields.get(id);
  };
  const c = {
    meta: {stat_start:'2026-11-14',stat_end:'2026-11-24',refreshed_through:'2026-11-24',
      pending_refresh_dates:['2026-11-22','2026-11-23','2026-11-21'],
      date_refreshed_at:{'2026-11-21':'2026-11-22 10:00:00','2026-11-24':'2026-11-24 17:07:26'}},
    get, dailyRows:[], state:{currentExportRows:[]}, filteredRows:()=>[], finalize:a=>a, emptyAgg:()=>({}),
    renderKpis(){get('kpis').rendered=true;}, renderTrend(){}, renderRankings(){}, renderTable(){}, renderStatus(){},
  };
  vm.createContext(c);
  vm.runInContext(actual('coverageForDates')+'\n'+actual('render'),c);
  c.range = (start,end) => {get('statStartDate').value=start;get('statEndDate').value=end;c.render();};
  return c;
}

test('missing-only range never renders financial zeroes or exports them', () => {
  const p=page();
  p.range('2026-11-22','2026-11-23');
  assert.match(p.get('coverageHint').textContent,/暂无可核实/);
  assert.equal(p.get('kpis').cleared,true);
  assert.equal(p.get('exportBtn').disabled,true);
  p.range('2026-11-24','2026-11-24');
  assert.equal(p.get('coverageHint').hidden,true);
  assert.equal(p.get('kpis').rendered,true);
  assert.equal(p.get('exportBtn').disabled,false);
});
test('preserve existing facts, partial ranges and previously checked dates awaiting routine rotation', () => {
  const p=page();
  p.range('2026-11-21','2026-11-24');
  assert.match(p.get('coverageHint').textContent,/已有统计行的小计/);
  assert.doesNotMatch(p.get('coverageHint').textContent,/2026-11-21/);
  p.dailyRows.push({dt:'2026-11-23'});
  p.range('2026-11-23','2026-11-23');
  assert.equal(p.get('exportBtn').disabled,false);
  p.range('2026-11-25','2026-11-27');
  assert.match(p.get('coverageHint').textContent,/2026-11-25 至 2026-11-27/);
  assert.equal(p.get('exportBtn').disabled,true);
});
