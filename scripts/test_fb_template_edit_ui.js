/* Exercise the actual save handler; no live template or publishing requests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function check({fail = false, confirmed = true, id = 7} = {}) {
  const elements = new Map(), requests = [];
  const defaults = {name: 'Edited', scheduleMode: 'random', randomCount: '1',
    randomStart: '00:15', randomEnd: '23:55', dataSource: '6', metricDays: '7',
    launchDays: '4', durationMin: '300', durationMax: '6000', videoTemplate: 'original'};
  const byId = key => {
    if (!elements.has(key)) elements.set(key, {value: defaults[key] || '', textContent: '', className: ''});
    return elements.get(key);
  };
  const location = {search: id ? '?id=' + id : '', href: ''};
  const ui = {byId, positiveId: Number, templateVersion: item => item.version,
    readItem: data => data.template || {}, API_BASE: '/api/admin/fb-auto-publish', showToast() {},
    async api(url, options) {
      requests.push({url, options});
      if (fail) throw new Error('version conflict');
      return {template: {id: id || 8, version: 4, status: confirmed ? 'disabled' : 'enabled'}};
    }};
  const window = {FBAutoPublishUI: ui, location,
    normalizeDramaLaunchWindow: Number, MaterialProductPicker: {read: () => ['DramaWave']}};
  let source = fs.readFileSync(path.join(__dirname, '../static/fb-auto-publish-template.js'), 'utf8');
  source = source.slice(0, source.indexOf('  void ui.boot')) + '\nwindow.test = {state, saveTemplate};\n})();';
  vm.runInNewContext(source, {window, URLSearchParams, document: {
    querySelectorAll: () => [{value: '6'}], title: ''}});
  const api = window.test;
  api.state.template = {id: 7, version: 3, status: 'enabled', config: {}};
  api.state.templateReady = api.state.groupsLoaded = true;
  await api.saveTemplate({preventDefault() {}});
  assert.equal(requests.length, 1, 'save is one atomic request, never disable-then-save');
  assert.equal(requests[0].url, ui.API_BASE + '/templates' + (id ? '/' + id : ''));
  assert.equal(JSON.parse(requests[0].options.body).expected_version, id ? 3 : undefined);
  assert.equal(location.href, fail || !confirmed ? '' : '/fb-auto-publish-templates.html');
  assert.equal(api.state.busy, false);
  assert.equal(byId('saveTemplate').disabled, false);
  if (fail) assert.equal(byId('formMessage').textContent, 'version conflict');
  if (!confirmed) assert.match(byId('formMessage').textContent, /未确认/);
  if (!fail && confirmed) assert.match(byId('formMessage').textContent, /自动发布已关闭/);
}

(async () => {
  await check();
  await check({fail: true});
  await check({confirmed: false});
  await check({id: 0});
  console.log('FB editor: enabled save, disabled receipt, redirect, failure retention and creation passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
