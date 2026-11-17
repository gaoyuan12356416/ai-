/* Exercise real renderers and click handlers without network or real templates. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag, props = {}) { Object.assign(this, {tag, children: [], value: '', innerHTML: '', dataset: {}}, props); }
  appendChild(child) { this.children.push(child); return child; }
  querySelectorAll(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
  contains() { return true; }
}

async function check(platform) {
  const elements = new Map(), calls = [], confirms = [], toasts = [];
  let accept = true, fail = false;
  const location = {href: ''};
  const ui = {
    API_BASE: '/api/' + platform,
    byId: id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); },
    element: (tag, props) => new Element(tag, props),
    clear: el => { el.children = []; },
    objectValue: value => value || {},
    positiveId: Number, numberValue: (v, fallback) => v == null ? fallback : Number(v), boolValue: Boolean,
    templateVersion: item => item.version,
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    formatTime: value => value || '', statusKind: () => '',
    statusBadge: text => new Element('span', {text}),
    appendTextCell: (row, text) => row.appendChild(new Element('td', {text})),
    setText: (el, text) => { el.text = text; },
    queryString: value => new URLSearchParams(value).toString(),
    readItem: value => value.template, readItems: value => value.items,
    showToast: (...args) => toasts.push(args),
    confirmAction: async (...args) => { confirms.push(args); return accept; },
    api: async (url, options) => {
      calls.push({url, options});
      if (fail) throw new Error('version conflict');
      if (url.endsWith('/copy')) return {template: {id: 99}};
      if (url.endsWith('/delete')) return {ok: true, deleted: true};
      return {items: [], total: 0};
    },
  };
  const window = {FBAutoPublishUI: ui, XAutoPublish: ui, location};
  const filename = platform === 'x' ? 'x-tag-auto-publish-templates.js' : 'fb-auto-publish-templates.js';
  let source = fs.readFileSync(path.join(__dirname, '..', 'static', filename), 'utf8');
  const entry = platform === 'x' ? '  void ui.initShell' : '  void ui.boot';
  source = source.slice(0, source.indexOf(entry)) + '\nwindow.test = {state, renderTemplates, click: ' +
    (platform === 'x' ? 'handleTableAction' : 'handleAction') + '};\n})();';
  vm.runInNewContext(source, {window, location, URLSearchParams, console});
  const api = window.test;
  const item = {id: 7, name: '<template>', version: 3, status: 'enabled', config: {}};
  const reset = () => { api.state.templates = [item]; api.state.total = 1; calls.length = 0; location.href = ''; api.renderTemplates(); };
  const click = action => api.click({target: {closest: () => ({dataset: {templateId: '7', action}})}});
  reset();
  const rows = ui.byId('templateRows');
  if (platform === 'x') {
    assert.deepEqual(rows.querySelectorAll('button').filter(b => ['copy', 'delete'].includes(b.dataset.action)).map(b => b.text), ['复制', '删除']);
  } else {
    assert.match(rows.innerHTML, /data-action="copy"/);
    assert.match(rows.innerHTML, /data-action="delete"/);
    assert.match(rows.innerHTML, /&lt;template>/);
  }
  accept = false;
  await click('delete');
  assert.equal(calls.length, 0, 'cancel must never mutate');
  accept = true;
  await click('copy');
  assert.equal(calls[0].url, ui.API_BASE + '/templates/7/copy');
  assert.deepEqual(JSON.parse(calls[0].options.body), {expected_version: 3});
  assert.match(location.href, /publish-template\.html\?id=99$/);
  assert.equal(api.state.busyIds.size, 0);
  reset();
  await click('delete');
  assert.equal(calls[0].url, ui.API_BASE + '/templates/7/delete');
  assert.deepEqual(JSON.parse(calls[0].options.body), {expected_version: 3});
  assert.equal(api.state.templates.length, 0, 'refresh hides deleted template');
  reset(); fail = true;
  await click('delete');
  assert.equal(api.state.templates.length, 1, 'failed mutation keeps template');
  assert.equal(api.state.busyIds.size, 0);
  assert.equal(toasts.at(-1)[0], 'version conflict');
  console.log(platform + ': render, cancel, copy redirect, delete refresh, conflict passed');
}

(async () => { await check('x'); await check('fb'); })().catch(error => { console.error(error); process.exitCode = 1; });
