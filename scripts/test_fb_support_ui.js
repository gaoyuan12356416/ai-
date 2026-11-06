"use strict";
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const panel = { hidden: true, innerHTML: '' };
const context = { window: {}, document: { getElementById: id => id === 'pmFbSupport' ? panel : null,
  addEventListener() {}, readyState: 'loading' }, URL, location: { origin: 'https://fixture.invalid' } };
vm.createContext(context);
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../static/pipeline-monitor.js'), 'utf8').replace(/\r\n/g, '\n');
assert(source.includes('\n  boot();\n'));
vm.runInContext(source.replace('\n  boot();\n', '\n'), context);
const render = context.window.PipelineMonitor.renderFbSupport;
render(undefined); assert.equal(panel.hidden, true);
render({ available: true, ok: true, services: [{ name: '短链生成与回执核对', ok: true }],
  progress: { due_pending: 0, overdue_pending: 0, future_scheduled: 22, platform_processing: 3, outcome_unknown: 4 } });
assert.equal(panel.hidden, false);
assert.match(panel.innerHTML, /未来预约 22/); assert.match(panel.innerHTML, /平台处理中 3/);
assert.match(panel.innerHTML, /结果待核对 4/); assert.match(panel.innerHTML, /到期待短链 0/);
render({ available: false, alerts: ['监测数据过期'] });
assert.match(panel.innerHTML, /监测数据过期/); assert.doesNotMatch(panel.innerHTML, /到期待短链 0/);
render({ available: true, ok: false, services: [{ name: '<img onerror=bad()>', ok: false }], alerts: ['<script>bad()</script>'] });
assert.match(panel.innerHTML, /role="alert"/); assert.doesNotMatch(panel.innerHTML, /<script>|<img/);
assert.match(panel.innerHTML, /到期待短链 未知/);
render(undefined); assert.equal(panel.innerHTML, ''); assert.equal(panel.hidden, true);
console.log('FB support UI: healthy / alert / stale / scoped hidden / unknown counts / HTML escaping passed');
