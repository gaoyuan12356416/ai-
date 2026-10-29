const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../static/x-accounts.html'), 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
  if (match[1].trim()) new vm.Script(match[1]);
}
const source = html.match(/      function showOauthNotice\(\) \{[\s\S]*?\n      \}/)[0];
for (const [query, expected] of [
  ['oauth=success', '账号已归属当前后台用户'],
  ['oauth=error&reason=x_account_suspended', '已被 X 平台暂停'],
  ['oauth=error&reason=x_account_owned_by_other', '旧的账号归属规则'],
  ['oauth=error&reason=oauth_denied', '已取消'],
  ['oauth=error&reason=invalid_request', '已失效'],
  ['oauth=error&reason=x_post_rate_limited', '限流'],
  ['oauth=error&reason=x_upstream_error', 'X API 暂时'],
  ['oauth=error&reason=constructor', '请联系管理员'],
  ['oauth=error&reason=%3Cscript%3Esecret%3C%2Fscript%3E', '请联系管理员'],
]) {
  const nodes = {};
  const context = {
    URLSearchParams,
    location: { search: '?' + query, pathname: '/x-accounts.html' },
    history: { replaceState: (_state, _title, url) => assert.equal(url, '/x-accounts.html') },
    el: id => nodes[id] ||= { classList: { toggle() {}, remove() {} }, textContent: '' },
  };
  vm.runInNewContext(source + '\nshowOauthNotice();', context);
  assert.ok(nodes.oauthNoticeText.textContent.includes(expected), query);
  assert.ok(!nodes.oauthNoticeText.textContent.includes('<script>'));
}
console.log('9 OAuth notice cases and all inline JavaScript syntax passed');
