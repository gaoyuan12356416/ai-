'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../static/quick-nav.js'), 'utf8');

function client(config, cached) {
  const storage = new Map();
  if (cached) storage.set('quickNavConfigCache:v6', JSON.stringify({items: cached, updatedAt: Date.now()}));
  const root = {classList: {add() {}}, querySelectorAll: () => [], querySelector: () => null};
  const context = {
    window: {location: {pathname: '/x-material-effects.html'}},
    document: {createElement: () => ({}), head: {appendChild() {}}},
    localStorage: {getItem: key => storage.get(key), setItem: (key, data) => storage.set(key, data),
      removeItem: key => storage.delete(key)},
    fetch: async () => ({ok: !!config, json: async () => config}),
  };
  vm.runInNewContext(code, context);
  const nav = context.window.QuickNav;
  return {nav, async links(permissions, admin = false) {
    await nav.render({container: root, auth: {authenticated: true, user: {is_admin: admin, permissions}}});
    return [...root.innerHTML.matchAll(/data-quick-nav-key="([^"]+)"/g)].map(match => match[1]);
  }};
}

const legacy = [
  {key: 'x_platform', module: 'x_accounts', items: [
    {key: 'xMaterialEffects', module: 'x_material_effects', href: '/x-material-effects.html'},
  ]},
  {key: 'social_reports', label: '数据报表', module: 'social_analytics', items: [
    {key: 'socialAnalytics', module: 'social_analytics'}, {key: 'moboreelsReport', module: 'moboreels_report'},
  ]},
  {key: 'socialkit_reports', label: '数据报表 · SocialKit', items: [
    {key: 'socialkitAnalytics', module: 'socialkit_analytics', href: '/socialkit-analytics.html'},
  ]},
];

async function main() {
  const fresh = client(legacy);
  const config = await fresh.nav.loadConfig();
  for (const app of [fresh, client(null, legacy), client(null, config), client(null)]) {
    const groups = await app.nav.loadConfig();
    assert.equal(groups.filter(g => g.label === '数据报表').length, 1);
    assert(!groups.some(g => g.key === 'socialkit_reports'));
    for (const key of ['xMaterialEffects', 'socialkitAnalytics']) {
      assert.deepEqual(Array.from(groups.filter(g => g.items.some(i => i.key === key)), g => g.key), ['social_reports']);
      assert.equal(groups.flatMap(g => g.items).filter(i => i.key === key).length, 1);
    }
    const x = await app.links({x_material_effects: true});
    const sk = await app.links({socialkit_analytics: true});
    assert(x.includes('xMaterialEffects') && !x.includes('socialkitAnalytics') && !x.includes('xAccounts'));
    assert(sk.includes('socialkitAnalytics') && !sk.includes('xMaterialEffects') && !sk.includes('socialAnalytics'));
    assert(!(await app.links({x_accounts: true})).includes('xMaterialEffects'));
    assert(!(await app.links({})).includes('socialkitAnalytics'));
    assert((await app.links({}, true)).includes('xMaterialEffects'));
  }
  assert(!(await fresh.links({moboreels_report: true})).includes('moboreelsReport'));
  assert((await fresh.links({moboreels_report: true, social_analytics: true})).includes('moboreelsReport'));
  const restricted = structuredClone(legacy);
  restricted[2].module = 'custom_module';
  restricted[0].items[0].enabled = false;
  const app = client(restricted);
  assert(!(await app.links({socialkit_analytics: true})).includes('socialkitAnalytics'));
  assert((await app.links({socialkit_analytics: true, custom_module: true})).includes('socialkitAnalytics'));
  assert(!(await app.links({}, true)).includes('xMaterialEffects'));
  console.log('Report placement, cached/default navigation, and independent/inherited permissions passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
