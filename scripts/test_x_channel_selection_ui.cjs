const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.value = ''; this.handlers = {}; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  focus() {}
  querySelectorAll() { return this.children.flatMap(c => c.tag === 'input' ? [c] : c.querySelectorAll()); }
}
function fn(source, name) {
  const pattern = new RegExp(`^( +)(?:async )?function ${name}\\([^]*?^\\1}\\r?$`, 'm');
  const match = source.match(pattern);
  assert(match, `missing function ${name}`);
  return match[0];
}
function listener(source, prefix) {
  const start = source.indexOf(prefix);
  assert(start >= 0, `missing listener ${prefix}`);
  const indent = source.slice(source.lastIndexOf('\n', start) + 1, start);
  const closing = '\n' + indent + '});';
  const end = source.indexOf(closing, start);
  assert(end >= 0);
  return source.slice(start, end + closing.length);
}
function context() {
  const nodes = {};
  const byId = id => nodes[id] ||= new Element();
  byId('accountMembershipFilter').value = 'all';
  byId('scheduleEnabled').checked = true;
  byId('randomDailyCount').value = '1';
  byId('materialPostTemplate').value = '{{drama_name}} {{desc}}';
  byId('dramaPostTemplate').value = '{{drama_name}} {{episode_number}} {{desc}}';
  const accounts = [{id: 101, publish_eligible: true, status: 'active'},
                    {id: 102, publish_eligible: false, status: 'error'}];
  const state = {accounts, selectedAccountIds: new Set(), accountOptions: accounts,
    schedule: {accountIds: new Set(['101', '102']), savedAccountIds: [],
               publishTimes: ['12:00'], version: 1}};
  const calls = [];
  const c = {state, byId, calls, console, document: {createElement: tag => new Element(tag),
    querySelector: () => ({value: 'fixed'})}, window: {confirm: () => true},
    ui: {byId, clear: n => n.replaceChildren(), element: (tag, props = {}) => Object.assign(new Element(tag), props)},
    accountId: a => String(a.id), accountName: a => String(a.id), accountTitle: a => String(a.id),
    accountEligible: a => a.publish_eligible, accountSubscription: () => 'none',
    accountLanguage: () => 'en', accountEligibilityText: () => '受限',
    accountMembershipLabel: () => '', subscriptionLabel: () => '',
    updateSummary() {}, updateAccountRefreshButton() {}, renderSelectedAccountSummary() {},
    renderScheduleSummary() {}, applySchedule() {}, showToast() {},
    setScheduleMessage() {}, setMessage() {}, setText(n, value) { n.text = value; },
    api: async (url, options) => { calls.push(JSON.parse(options.body)); return {item: {}}; }};
  return vm.createContext(c);
}

(async () => {
  let count = 0;
  const source = fs.readFileSync(path.join(root, 'static/x-auto-publish-template.js'), 'utf8');
  const c = context();
  vm.runInContext(fn(source, 'renderAccounts') + '\nrenderAccounts();', c);
  const boxes = c.byId('accountList').querySelectorAll();
  assert.equal(boxes.length, 2);
  assert(boxes.every(box => !box.disabled));
  vm.runInContext(listener(source, 'ui.byId("selectVisibleAccounts").addEventListener'), c);
  c.byId('selectVisibleAccounts').handlers.click();
  assert.deepEqual([...c.state.selectedAccountIds], ['101', '102']);
  count++;
  for (const filename of ['x-post-material-pool.html', 'x-post-drama-pool.html']) {
    const source = fs.readFileSync(path.join(root, 'static', filename), 'utf8');
    for (const selected of [['101', '102'], ['102']]) {
      const c = context();
      c.state.schedule.accountIds = new Set(selected);
      vm.runInContext(fn(source, 'renderAccountOptions') + '\nrenderAccountOptions();', c);
      assert(c.byId('accountOptions').querySelectorAll().every(box => !box.disabled));
      vm.runInContext(listener(source, 'byId("accountOptions").addEventListener'), c);
      c.state.schedule.accountIds.delete('102');
      const box = {checked: true, dataset: {accountId: '102'}};
      c.byId('accountOptions').handlers.change({target: {closest: () => box}});
      assert(c.state.schedule.accountIds.has('102'));
      vm.runInContext(fn(source, 'saveSchedule'), c);
      await vm.runInContext('saveSchedule()', c);
      assert.equal(c.calls.length, 1, `save must reach API: ${filename}`);
      assert.equal(c.calls[0].enabled, true);
      assert.deepEqual(c.calls[0].account_ids.sort(), selected.map(Number).sort());
      count++;
    }
  }
  console.log(`${count} channel-selection UI scenarios passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
