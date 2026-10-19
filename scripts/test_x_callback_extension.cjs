const assert = require('node:assert/strict');
const {sandboxCallbackRules} = require('../browser-extension/rules.js');
const now = Date.now();
const state = 'a'.repeat(43);
const guard = 'b'.repeat(43);
const auth = new URL('https://x.com/i/oauth2/authorize');
auth.searchParams.set('state', state);
auth.searchParams.set('redirect_uri', 'https://ai.yingliangads.com/x-oauth/callback');
const payload = {authorization_url: auth.href, callback_guard: guard, expires_at: new Date(now + 600000).toISOString()};
const plan = sandboxCallbackRules(payload, now);
const regex = new RegExp(plan.rules[0].condition.regexFilter);
const base = 'https://ai.yingliangads.com/x-oauth/callback?';
for (const query of ['state=' + state + '&code=fake', 'code=fake&state=' + state, 'error=access_denied&state=' + state]) {
  const match = regex.exec(base + query);
  assert.ok(match);
  assert.equal(match[1], query);
}
for (const bad of [
  base + 'state=' + state + 'X&code=fake', base + 'xstate=' + state,
  base + 'state=other&code=fake', base.replace('.com/', '.com.evil/') + 'state=' + state,
  base.replace('/callback?', '/callback-evil?') + 'state=' + state,
]) assert.equal(regex.test(bad), false);
assert.equal(plan.rules[0].action.redirect.regexSubstitution, 'http://127.0.0.1:18880/x-oauth/callback?\\1');
assert.equal(plan.rules[1].action.requestHeaders[0].value, guard);
assert.equal(new RegExp(plan.rules[1].condition.regexFilter).test('http://127.0.0.1:18880/x-oauth/callback?state=' + state), true);
assert.equal(new RegExp(plan.rules[1].condition.regexFilter).test('http://127.0.0.1:18881/x-oauth/callback?state=' + state), false);
for (const change of [{callback_guard: 'bad'}, {expires_at: new Date(now - 1).toISOString()}, {expires_at: new Date(now + 900000).toISOString()}, {authorization_url: auth.href.replace('x.com/', 'evil.example/')}]) {
  assert.throws(() => sandboxCallbackRules({...payload, ...change}, now));
}
console.log('Extension rule scope, query preservation, expiry and guard checks: PASS');
