/* Dedicated interactive browser. Routes only this sandbox's OAuth state locally. */
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
const option = (name) => args[args.indexOf(name) + 1];
const dataDir = option('--data-dir');
const moduleRoot = option('--playwright-root');
const localBase = 'http://127.0.0.1:18880';
const callback = 'https://ai.yingliangads.com/x-oauth/callback';

async function main() {
  if (!args.includes('--data-dir') || !args.includes('--playwright-root')) throw new Error('data directory and Playwright package root are required');
  const { chromium } = require(path.join(moduleRoot, 'playwright'));
  const guard = JSON.parse(fs.readFileSync(path.join(dataDir, 'callback-guard.json'), 'utf8')).guard;
  const context = await chromium.launchPersistentContext(path.join(dataDir, 'browser-profile'), {
    channel: 'chrome', headless: false, viewport: null, serviceWorkers: 'block',
    args: ['--no-default-browser-check'],
  });
  const expectedStates = new Map();
  let forwarded = 0;
  let lastReceiverOutcome = null;
  const bridgeStatus = (active) => fs.writeFileSync(path.join(dataDir, 'browser-bridge-status.json'), JSON.stringify({active, pid: process.pid, forwarded_callbacks: forwarded, checked_at: new Date().toISOString()}));
  await context.route(localBase + '/**', async route => {
    await route.continue({ headers: {...route.request().headers(), 'X-Sandbox-Callback-Guard': guard} });
  });
  context.on('response', async response => {
    if (response.url() !== localBase + '/api/authorize' || response.status() !== 200) return;
    try {
      const payload = await response.json();
      const state = new URL(payload.authorization_url).searchParams.get('state');
      if (state) {expectedStates.clear(); expectedStates.set(state, Date.parse(payload.expires_at));}
    } catch (_) { /* Missing state fails closed in the callback route. */ }
  });
  // The whole backend domain is fenced in this browser, including malformed callbacks.
  // Unmatched requests are blocked locally; they are never sent to production.
  await context.route(/^https?:\/\/ai\.yingliangads\.com(?::\d+)?\//, async route => {
    const url = new URL(route.request().url());
    const state = url.searchParams.get('state');
    if (route.request().method() === 'GET' && route.request().isNavigationRequest()
        && url.origin + url.pathname === callback && state && expectedStates.get(state) > Date.now()) {
      expectedStates.delete(state);
      forwarded += 1;
      bridgeStatus(true);
      // Use the context's cookie jar and explicit guard. Browser redirect requests
      // do not reliably pass through a second Playwright route handler.
      let received;
      try {
        received = await context.request.get(localBase + '/x-oauth/callback' + url.search, {
          headers: {'X-Sandbox-Callback-Guard': guard}, maxRedirects: 0, timeout: 75000,
        });
      } catch (_) {
        await route.fulfill({status: 503, contentType: 'text/plain; charset=utf-8', body: '本机隔离回调连接中断，请返回测试页核对账号状态。'});
        return;
      }
      const location = received.headers()['location'] || '';
      lastReceiverOutcome = {status: received.status(), invalid_state_rejected: location.includes('reason=invalid_request')};
      if (received.status() !== 302 || !location.startsWith(localBase + '/')) {
        await route.fulfill({status: 503, contentType: 'text/plain; charset=utf-8', body: '本机隔离回调未完成，请返回测试页检查账号状态。'});
        return;
      }
      await route.fulfill({status: 302, headers: {'Location': location, 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}, body: ''});
      return;
    }
    await route.fulfill({status: 400, contentType: 'text/plain; charset=utf-8', body: '专用测试浏览器已阻止未匹配的生产请求。请返回本机测试页重新开始。'});
  });
  const pages = context.pages();
  const page = pages[0] || await context.newPage();
  await page.goto(localBase + '/', {waitUntil: 'domcontentloaded'});
  if (args.includes('--self-test')) {
    const check = await page.evaluate(async () => (await fetch('/api/status')).json());
    if (!check.callback_guard_active || check.callback_url !== callback) throw new Error('Browser callback guard check failed');
    const probe = await context.newPage();
    const blocked = await probe.goto(callback + '?code=fake-code&state=not-a-sandbox-state');
    if (blocked.status() !== 400) throw new Error('Unmatched production callback was not blocked');
    const fakeState = require('node:crypto').randomBytes(24).toString('hex');
    expectedStates.set(fakeState, Date.now() + 30000);
    await probe.goto(callback + '?code=fake-code&state=' + fakeState);
    if (!probe.url().startsWith(localBase + '/') || forwarded !== 1 || !lastReceiverOutcome?.invalid_state_rejected) throw new Error('Matched callback did not reach the isolated receiver');
    await probe.close();
    forwarded = 0;
    fs.writeFileSync(path.join(dataDir, 'browser-self-test.json'), JSON.stringify({guard_active: true, production_callback: callback, unmatched_callback_blocked: true, matched_callback_relayed: true, fake_code_rejected_by_local_receiver: true, real_authorization_attempted: false, checked_at: new Date().toISOString()}));
  }
  bridgeStatus(true);
  await page.bringToFront();
  console.log('Dedicated sandbox browser ready; production requests fenced.');
  context.on('close', () => {bridgeStatus(false); process.exit(0);});
}
main().catch(error => {
  const safeMessage = String(error.message || '').split('\n')[0].replace(/https?:\/\/\S+/g, '[URL]');
  console.error('Sandbox browser failed: ' + error.name + ': ' + safeMessage);
  process.exit(1);
});
