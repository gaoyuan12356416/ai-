const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/x-post-material-pool.html'), 'utf8');
const save = html.slice(html.indexOf('      async function saveSchedule()'), html.indexOf('      function badge('));
async function scenario({ missing = false, allUnavailable = false } = {}) {
  const ids = allUnavailable ? ['2'] : ['1', '2'];
  const nodes = {
    scheduleEnabled: { checked: true }, randomDailyCount: { value: '1' },
    materialPostTemplate: { value: '{{drama_name}} {{desc}}' },
    scheduleRandomTemplate: { checked: true }, saveSchedule: {},
  };
  const calls = [], messages = [];
  const ctx = {
    state: { scheduleBusy: false, schedule: { accountIds: new Set(ids), version: 31, publishTimes: [] },
      accountOptions: missing ? [{ id: 1 }] : [{ id: 1, publish_eligible: true }, { id: 2, publish_eligible: false }] },
    byId: id => nodes[id], document: { querySelector: () => ({ value: 'random' }) },
    accountId: item => String(item.id), accountEligible: item => item.publish_eligible === true,
    setScheduleMessage: (message, error) => messages.push({ message, error }),
    setText: (node, text) => { node.text = text; }, showToast: () => {},
    scheduleItem: result => result.item,
    api: async (url, options) => {
      calls.push(JSON.parse(options.body));
      return { item: { account_ids: allUnavailable ? [] : [1], enabled: !allUnavailable, skipped_account_ids: [2] } };
    },
    applySchedule: result => { ctx.state.schedule.accountIds = new Set(result.item.account_ids.map(String)); },
  };
  vm.createContext(ctx);
  await vm.runInContext(save + '; saveSchedule()', ctx);
  if (missing) {
    assert.equal(calls.length, 0);
    assert.equal(messages.at(-1).error, true);
  } else {
    assert.deepEqual(calls[0].account_ids, ids.map(Number));
    assert.equal(messages.at(-1).error, false);
    assert.match(messages.at(-1).message, /已跳过 1 个异常账号/);
    if (allUnavailable) assert.match(messages.at(-1).message, /已保存并关闭自动发布/);
    assert.equal(ctx.state.scheduleBusy, false);
    assert.equal(nodes.saveSchedule.disabled, false);
  }
}
(async () => {
  await scenario(); await scenario({ missing: true }); await scenario({ allUnavailable: true });
  console.log('3 material-pool save UI scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
