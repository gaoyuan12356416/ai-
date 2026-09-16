/* Local regression only: every API and non-local request is intercepted. */
const fs = require('fs'), path = require('path'), http = require('http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const checks = [], errors = [], writes = [], lists = [], pending = [];
  const check = (name, value) => { if (!value) throw Error(name); checks.push(name); };
  const server = http.createServer((req,res) => {
    const file = path.join(process.cwd(),'static',decodeURIComponent(req.url.split('?')[0]));
    if (fs.existsSync(file) && fs.statSync(file).isFile()) {
      res.setHeader('Content-Type',file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(fs.readFileSync(file));
    } else { res.statusCode=404; res.end(); }
  });
  await new Promise(r => server.listen(0,'127.0.0.1',r));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless:true,channel:'chrome'});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e => errors.push(e.message));
  const first = 'a'.repeat(32), second = 'b'.repeat(32), revision = '1'.repeat(64);
  const make = id => ({id,title:id === first ? '待审核测试任务' : '第二个测试任务',description:'完整详情内容',comment:'',status:'review',phase:'cover',created_at:'2026-09-16T10:00:00Z',current_version:2,cover_source:'ai',material:{name:'测试素材'},channel:{name:'测试频道'},requirements:'16:9',can_review:true,can_retry:false,versions:[{number:2,url:'/api/youtube-auto-publish/covers/'+id}],cover_preview:{url:'/api/youtube-auto-publish/covers/'+id,version:2,is_current:true},steps:[],notification:{status:'none'}});
  const rows = [first,second].map(id => ({...make(id),description:undefined,current_version:1,thumbnail_url:'/api/youtube-auto-publish/covers/' + id + '/thumbnail'}));
  let mode = 'hold';
  const reply = (route,data,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)}).catch(()=>{});
  await page.route('**/*',route => {
    const request=route.request(), url=new URL(request.url());
    if (url.origin !== origin) return route.abort();
    if (!url.pathname.startsWith('/api/')) return route.continue();
    if (url.pathname === '/api/ui/topbar') return reply(route,{authenticated:true,user:{name:'QA',role:'admin',is_admin:true,permissions:{youtube_auto_publish:true}}});
    if (url.pathname.endsWith('/bootstrap')) return reply(route,{enabled:true,source:{configured:true},settings:{},channels:[],can_manage_settings:true});
    if (url.pathname.endsWith('/tasks')) {
      lists.push(url.search);
      if (url.searchParams.get('since') === revision) return reply(route,{unchanged:true,revision});
      return reply(route,{items:rows,total:2,counts:{all:2,review:2},revision});
    }
    if (url.pathname.includes('/covers/')) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#3157cf"/></svg>'});
    if (request.method() === 'POST') { writes.push(request.postDataJSON()); return reply(route,{task:{...make(first),status:'enqueue_pending',can_review:false}}); }
    if (url.pathname.includes('/tasks/')) {
      if (mode === 'hold') { pending.push(route); return; }
      if (mode === 'error') return reply(route,{message:'测试读取失败'},503);
      return reply(route,{task:make(url.pathname.split('/').pop())});
    }
    return reply(route,{channels:[],items:[]});
  });
  try {
    await page.goto(origin+'/youtube-publish.html');
    await page.locator('[data-task-id]').first().waitFor();
    check('summary requested on initial load',new URLSearchParams(lists[0]).get('compact') === '1');
    check('list uses dedicated thumbnails',await page.locator('#task-table img').evaluateAll(nodes=>nodes.every(n=>n.src.endsWith('/thumbnail'))));
    await page.waitForTimeout(6300);
    check('unchanged poll sends revision and retains rows',lists.some(q=>new URLSearchParams(q).get('since')===revision) && await page.locator('[data-task-id]').count()===2);
    const latency = await page.evaluate(async () => {
      const start=performance.now(); document.querySelector('[data-task-id] [data-action="review"]').click();
      const immediate=!!document.querySelector('[role="dialog"]');
      await new Promise(requestAnimationFrame);
      return {immediate,ms:performance.now()-start};
    });
    check('dialog appears synchronously before network completes',latency.immediate && latency.ms < 250);
    await page.locator('.task-detail-loading').waitFor();
    check('approval unavailable before fresh detail',await page.locator('[data-action="approve"]').count()===0);
    await page.screenshot({path:'output/playwright/performance-loading-desktop.png',animations:'disabled'});
    const count=lists.length;
    await page.waitForTimeout(6300);
    check('slow detail does not trigger background list downloads',lists.length===count);
    await page.locator('.close-modal').click();
    check('pending request can be closed immediately',await page.locator('[role="dialog"]').count()===0);
    await page.locator('[data-task-id="'+second+'"] [data-action="review"]').click();
    while (pending.length < 2) await page.waitForTimeout(20);
    await reply(pending[1],{task:make(second)});
    await page.locator('[data-action="approve"]').waitFor();
    await reply(pending[0],{task:make(first)});
    check('late closed response cannot reopen or replace task',await page.locator('.modal-overlay').innerText().then(t=>t.includes('第二个测试任务')&&!t.includes('待审核测试任务')));
    await page.locator('.close-modal').click();
    mode='error';
    await page.locator('[data-task-id="'+first+'"] [data-action="review"]').click();
    await page.locator('[data-action="reload-task"]').waitFor();
    check('read failure keeps actionable dialog without approval',await page.locator('.modal-overlay').innerText().then(t=>t.includes('测试读取失败')) && await page.locator('[data-action="approve"]').count()===0);
    mode='ready';
    await page.locator('[data-action="reload-task"]').click();
    await page.locator('[data-action="approve"]').waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'output/playwright/performance-review-mobile.png'});
    check('mobile dialog fits viewport',await page.locator('.modal').evaluate(el=>el.getBoundingClientRect().right<=innerWidth));
    await page.locator('[data-action="approve"]').click();
    await page.getByText('封面已确认，任务进入上传发布流程。',{exact:true}).waitFor();
    check('approval uses fresh version instead of list snapshot',writes.length===1 && writes[0].version===2 && writes[0].action==='approve');
    check('no JS exceptions',errors.length===0);
    console.log(JSON.stringify({passed:checks.length,checks,clickToFrameMs:latency.ms,errors}));
  } finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exitCode=1; });
