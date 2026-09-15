const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
let source = fs.readFileSync(require('path').join(__dirname, '../static/quick-nav.js'), 'utf8');
source = source.replace('window.QuickNav = {', 'window.normalizer = normalizeNavConfig; window.defaults = DEFAULT_NAV; window.QuickNav = {');
const sandbox = { window: {}, location: { hash: '' } };
vm.runInNewContext(source, sandbox);
const input = JSON.parse(JSON.stringify(sandbox.window.defaults));
input.push({key:'ad_control_v3', module:'ad_control_v3', items:[]});
const result = sandbox.window.normalizer(input);
for (const key of ['ad_control', 'ad_control_v3', 'ad_material', 'ad_material_test']) {
  assert(!result.some(group => group.key === key));
}
const meta = result.find(group => group.key === 'meta_asset_delete');
assert(meta && meta.items.filter(item => item.key === 'fbPostAdDelete').length === 1);
for (const key of ['drama', 'voiceover', 'tiktok_platform', 'facebook_platform', 'x_platform', 'youtube_platform', 'system']) {
  assert(result.some(group => group.key === key), key);
}
assert.deepStrictEqual(JSON.parse(JSON.stringify(sandbox.window.normalizer(result))), JSON.parse(JSON.stringify(result)));
console.log('PASS: retired navigation, legacy Meta migration, other groups, idempotence');
