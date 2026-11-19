const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

(async () => {
  const requests = [], cell = {dataset:{xTagId:'7'},innerHTML:''}, summary = {innerHTML:''};
  const context = {window:{}, document:{addEventListener(){}, querySelector:()=>null,
    querySelectorAll:()=>[cell], getElementById:()=>summary},
    AbortController, Intl, Date, encodeURIComponent, setTimeout, clearTimeout,
    fetch: url => new Promise(resolve => requests.push({url, resolve}))};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/x-tag-revenue.js'),'utf8'), context);
  const response = (version, name) => ({ok:true, json:async()=>({meta:{version,start:'2026-07-23',end:'2026-11-18',
    generated_at:'2026-11-19T03:20:00Z'},items:{7:{status:'complete',audiences:[{name,share_bp:10000}]}}})});
  const first = context.window.XTagRevenue.refresh('20261119T032000Z');
  const second = context.window.XTagRevenue.refresh('20261119T042000Z');
  assert.match(requests[0].url,/version=20261119T032000Z/);
  assert.match(requests[1].url,/version=20261119T042000Z/);
  requests[1].resolve(response('20261119T042000Z','最新受众')); await second;
  requests[0].resolve(response('20261119T032000Z','旧受众')); await first;
  assert.match(cell.innerHTML,/最新受众/); assert.doesNotMatch(cell.innerHTML,/旧受众/);
  assert.match(summary.innerHTML,/同一批收入/);
  const retry = context.window.XTagRevenue.refresh();
  assert.match(requests[2].url,/version=20261119T042000Z/);
  requests[2].resolve(response('20261119T052000Z','错误批次')); await retry;
  assert.doesNotMatch(cell.innerHTML,/错误批次/);
  assert.match(summary.innerHTML,/收入批次已更新，请刷新列表/);
  console.log('PASS: pinned version, stale-response race, pinned retry, mismatched version rejected');
})().catch(error=>{console.error(error);process.exitCode=1;});
