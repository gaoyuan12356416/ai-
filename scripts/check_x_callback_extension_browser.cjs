/* Real Chromium extension test. DNS fences production; only fake OAuth data is used. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const args = process.argv.slice(2);
const option = name => args[args.indexOf(name) + 1];
async function main() {
  const dataDir = option('--data-dir');
  const moduleRoot = option('--playwright-root');
  const {chromium} = require(path.join(moduleRoot, 'playwright'));
  const extensionDir = path.resolve(__dirname, '../browser-extension');
  const executablePath = args.includes('--chromium-executable') ? option('--chromium-executable') : undefined;
  const guard = JSON.parse(fs.readFileSync(path.join(dataDir, 'callback-guard.json'), 'utf8')).guard;
  const context = await chromium.launchPersistentContext(path.join(dataDir, 'extension-qa-profile'), {
    headless: false, executablePath, ignoreDefaultArgs: ['--disable-extensions'],
    args: ['--disable-extensions-except=' + extensionDir, '--load-extension=' + extensionDir,
      '--host-resolver-rules=MAP ai.yingliangads.com ~NOTFOUND'],
  });
  try {
    const page = context.pages()[0] || await context.newPage();
    await page.goto('http://127.0.0.1:18880/', {waitUntil: 'networkidle'});
    const result = await page.evaluate(async guard => {
      async function message(kind,payload={}) {
        const id=crypto.randomUUID();
        return await new Promise(resolve=>{
          const listener=event=>{if(event.source===window&&event.data?.source==='x-sandbox-extension'&&event.data.id===id){window.removeEventListener('message',listener);resolve(event.data.result);}};
          window.addEventListener('message',listener);
          window.postMessage({source:'x-sandbox-page',id,kind,payload},location.origin);
          setTimeout(()=>{window.removeEventListener('message',listener);resolve({ok:false});},8000);
        });
      }
      if(!(await message('PING')).ok)return {ready:false};
      const state=Array.from(crypto.getRandomValues(new Uint8Array(24)),v=>v.toString(16).padStart(2,'0')).join('');
      const auth=new URL('https://x.com/i/oauth2/authorize');
      auth.searchParams.set('state',state);
      auth.searchParams.set('redirect_uri','https://ai.yingliangads.com/x-oauth/callback');
      const registered=await message('REGISTER',{authorization_url:auth.href,callback_guard:guard,expires_at:new Date(Date.now()+60000).toISOString()});
      return {ready:true,registered:registered.ok,state};
    }, guard);
    if(!result.ready||!result.registered)throw new Error('extension_registration_failed');
    let localCallbackRejected = false;
    page.on('response', response => {
      if(response.url().startsWith('http://127.0.0.1:18880/x-oauth/callback?')
          &&response.status()===302&&String(response.headers()['location']).includes('reason=invalid_request'))localCallbackRejected=true;
    });
    await page.goto('https://ai.yingliangads.com/x-oauth/callback?code=fake-code&state='+result.state,{waitUntil:'networkidle'});
    if(!page.url().startsWith('http://127.0.0.1:18880/')||!localCallbackRejected)throw new Error('local_callback_not_confirmed');
    const worker=context.serviceWorkers()[0];
    if(!worker)throw new Error('extension_worker_missing');
    const remaining=await worker.evaluate(async()=> (await chrome.declarativeNetRequest.getSessionRules()).length);
    if(remaining!==0)throw new Error('rules_not_cleared_after_callback');
    const evidence={default_browser_extension_ready:true,production_dns_fenced:true,official_callback_redirected_locally:true,fake_state_rejected_locally:true,session_rules_cleared:true,real_x_login_attempted:false,checked_at:new Date().toISOString()};
    fs.writeFileSync(path.join(dataDir,'default-extension-self-test.json'),JSON.stringify(evidence));
    console.log(JSON.stringify(evidence));
  } finally {await context.close();}
}
main().catch(error=>{console.error('Extension browser check failed: '+String(error.message).split('\n')[0].replace(/https?:\/\/\S+/g,'[URL]'));process.exit(1);});
