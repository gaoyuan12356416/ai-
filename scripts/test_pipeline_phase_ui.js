"use strict";
// Execute the real rendering functions with a DOM stub; no network or platform calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/pipeline-monitor.js'), 'utf8');
const elements = new Map();
const document = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, { value: '', innerHTML: '', textContent: '', hidden: false, selectedOptions: [{ textContent: '今天' }], classList: { toggle() {} } });
  return elements.get(id);
} };
const context = { document, window: {}, location: { origin: 'https://fixture.invalid' }, URL, URLSearchParams, console };
assert(source.includes('  boot();'));
vm.runInNewContext(source.replace('  boot();', ''), context);
const channels = ['fb', 'tt', 'youtube'].map(channel => ({ channel, stage: 'publish', throughput_per_hour: 1 }));
const efficiency = [
  { channel: 'fb', stage: 'publish', phase: 'platform_submit', samples: 1, p50_seconds: 17, p95_seconds: 17 },
  { channel: 'fb', stage: 'publish', phase: 'platform_upload', samples: 0, applicable: false },
  { channel: 'fb', stage: 'publish', phase: 'platform_confirmation_wait', samples: 1, p50_seconds: 250, p95_seconds: 250 },
  { channel: 'tt', stage: 'publish', phase: 'platform_processing', samples: 1, p50_seconds: 75, p95_seconds: 75 },
];
context.window.PipelineMonitor.renderSummary({ channels, efficiency, allowed_channels: ['fb', 'tt', 'youtube'] });
const rows = document.getElementById('pmEfficiencyRows').innerHTML.match(/<tr>.*?<\/tr>/g);
assert.equal(rows.length, 3);
assert.match(rows[0], /17秒 \/ 17秒[\s\S]*不适用[\s\S]*未记录[\s\S]*4.2分 \/ 4.2分/);
assert.match(rows[1], /1.3分 \/ 1.3分/);
assert.match(rows[2], /未记录/);
assert.equal((rows[0].match(/<td>/g) || []).length, 11);
context.window.PipelineMonitor.renderSummary({ channels: [], allowed_channels: [] });
assert.match(document.getElementById('pmEfficiencyRows').innerHTML, /colspan="11"/);
const html = fs.readFileSync(path.join(root, 'static/pipeline-monitor.html'), 'utf8');
assert.match(html, /平台提交[\s\S]*平台确认等待/);
assert.match(html, /平台确认等待包含拉取、处理与轮询间隔/);
console.log('pipeline phase UI: actual renderer distinguishes submission, confirmation, missing and not-applicable; retained processing passed');
