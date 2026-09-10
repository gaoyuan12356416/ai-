/* Isolated localhost browser verification. Every API call is mocked. */
async (page) => {
  const checks = [], calls = [], errors = [];
  const check = (name, value) => { if (!value) throw new Error(name); checks.push(name); };
  let missing = false;
  const material = {id:'6617770',name:'AI翻转-zh-TW-24508-26:38-I_Hatched_My_Dragon_Husband__Princes_Regret-clear',
    content_id:'7Bw77v5k3t',language:'zh-TW',macro_name:'我孵化了我的龍丈夫，王子們後悔了',macro_desc:'精靈公主Malyn孵化了一顆神秘的蛋，誕生了命中注定的龍主。',
    macro_url:'',long_url:'',source_job_id:'',source_kind:'',drama_status:'matched',drama_message:'',link_ready:true,
    thumbnail_url:'',url:'',duration:'26:38',app_id:'1479'};
  const channel = {id:'qa-channel',name:'本地测试频道',eligible:true,comment_eligible:true};
  const respond = (route, data, status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  await page.unrouteAll({behavior:'wait'});
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = request.url(), p = url.replace(/^http:\/\/127\.0\.0\.1:8879/, '').split('?')[0];
    if (!url.startsWith('http://127.0.0.1:8879/')) return route.abort();
    if (p === '/navigation.json') return respond(route, []);
    if (!p.startsWith('/api/')) return route.continue();
    const body = request.postDataJSON(); calls.push({path:p,method:request.method(),body});
    if (p === '/api/ui/topbar') return respond(route,{authenticated:true,user:{name:'本地回归验证',role:'admin',is_admin:true,tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if (p.endsWith('/bootstrap')) return respond(route,{settings:{default_description:'{url}\n{desc}'},channels:[channel],source:{configured:true},can_manage_settings:true,enabled:true});
    if (p.endsWith('/channels')) return respond(route,{items:[channel],channels:[channel]});
    if (p.endsWith('/materials')) return respond(route,{configured:true,items:[missing ? {...material,macro_name:'',macro_desc:'',drama_status:'missing',drama_message:'未找到与素材剧集 ID、语言一致的剧集资料。',link_ready:false} : material]});
    if (p.endsWith('/tasks') && request.method() === 'GET') return respond(route,{items:[],total:0,counts:{all:0}});
    if (p.endsWith('/tasks') && request.method() === 'POST') return respond(route,{task:{id:'qa-preparation',title:material.macro_name,...body,material,channel,created_at:'2026-09-10T09:00:00Z',status:'queued_generation',current_version:1,versions:[],steps:[],notification:{status:'none'},can_review:false,can_retry:false}});
    return respond(route,{error:'unexpected_fixture_route'},404);
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('http://127.0.0.1:8879/youtube-publish.html');
  await page.locator('#new-publish').click();
  await page.locator('[data-action="choose-material"]').click();
  await page.locator('[data-action="select-material"]').click();
  await page.locator('[data-action="confirm-material"]').click();
  await page.locator('#draft-channel').selectOption(channel.id);
  await page.locator('#draft-requirements').fill('16:9 横版封面，突出精灵公主和龙。');
  await page.locator('#draft-comment').fill('{url}');
  check('localized drama title instead of video filename', (await page.locator('[data-macro-preview="title"] .macro-preview-body').innerText()) === material.macro_name);
  check('description resolves synopsis', (await page.locator('[data-macro-preview="description"] .macro-preview-body').innerText()).includes(material.macro_desc));
  check('URL preview waits for submit without false source error', await page.locator('.macro-preview.has-warning').count() === 0);
  await page.locator('.macro-link-help summary').click();
  check('standalone association is explained', (await page.locator('.macro-link-help').textContent()).includes('独立素材（无需合成任务）'));
  check('no horizontal overflow at 1440', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path:'output/playwright/youtube-drama-association-1440.png',fullPage:true});
  await page.setViewportSize({width:1280,height:900});
  check('no horizontal overflow at 1280', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.locator('#draft-comment').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/youtube-drama-association-1280.png',fullPage:true});
  await page.locator('[data-action="submit-publish"]').click();
  await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  check('matched standalone material can submit', calls.filter(c => c.path.endsWith('/tasks') && c.method === 'POST').length === 1);
  check('all three templates submitted for server resolution', calls.some(c => c.body?.title_template === '{name}' && c.body.description_template === '{url}\n{desc}' && c.body.comment_template === '{url}'));
  await page.locator('.close-modal').click();
  missing = true;
  await page.locator('#new-publish').click();
  await page.locator('[data-action="choose-material"]').click();
  await page.locator('[data-action="select-material"]').click();
  await page.locator('[data-action="confirm-material"]').click();
  await page.locator('#draft-channel').selectOption(channel.id);
  await page.locator('#draft-requirements').fill('16:9 横版');
  await page.locator('[data-action="submit-publish"]').click();
  check('genuinely missing association is explained', (await page.locator('.modal-overlay').innerText()).includes('未找到与素材剧集 ID、语言一致的剧集资料'));
  check('missing association blocks POST', calls.filter(c => c.path.endsWith('/tasks') && c.method === 'POST').length === 1);
  check('no JavaScript runtime errors', errors.length === 0);
  return {passed:checks.length,checks};
}
