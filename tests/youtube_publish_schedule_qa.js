/* Run with playwright-cli run-code --filename tests/youtube_publish_schedule_qa.js.
 * All APIs and external assets are isolated mocks; no production writes. */
async (page) => {
  const checks = [], calls = [], errors = [];
  const check = (name,value) => { if (!value) throw new Error(name); checks.push(name); };
  const origin = page.url().match(/^http:\/\/127\.0\.0\.1:\d+/)?.[0] || 'http://127.0.0.1:8877';
  const reply = (route,data,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  const cover = origin + '/schedule-qa-cover.svg';
  const material = {id:'material-1',name:'预约测试素材',macro_name:'测试剧名',macro_desc:'测试描述',drama_cover_status:'available',drama_cover_url:cover,thumbnail_url:cover,language:'英语',url:''};
  const channel = {id:'channel-1',name:'测试频道',eligible:true,comment_eligible:true};
  const future = '2099-02-12T20:30', futureIso = '2099-02-12T12:30:00.000Z';
  const changed = '2099-07-01T00:15', changedIso = '2099-06-30T16:15:00.000Z';
  const record = (id,extra={}) => ({id,title:'测试视频 '+id,title_template:'测试视频',description:'说明',comment:'首评',material,channel,cover_source:'ai',requirements:'横版封面',current_version:1,versions:[{number:1,url:cover}],cover_preview:{url:cover,version:1,is_current:true},created_at:'2026-09-11T04:00:00Z',status:'review',can_review:true,can_retry:false,publish_at:futureIso,schedule_version:1,schedule_state:'requested',can_schedule:true,schedule_control:{},steps:[],...extra});
  let tasks = [record('review'),record('scheduled',{status:'scheduled',can_review:false,schedule_state:'scheduled'}),record('missed',{publish_at:'2020-01-01T12:00:00Z',schedule_state:'missed'}),record('published',{status:'published',can_review:false,can_schedule:false}),record('unknown',{status:'schedule_pending',can_review:false,can_schedule:false,schedule_control:{action:'reschedule',state:'unknown',publish_at:changedIso,message:'正在核对平台结果'}})];
  let createFailure = false, controlMode = 'pending', creates = 0;
  const controls = () => calls.filter(c => c.path.endsWith('/schedule'));
  const createCalls = () => calls.filter(c => c.path.endsWith('/tasks') && c.method === 'POST');
  page.on('pageerror',e => errors.push(e.message));
  await page.unrouteAll({behavior:'ignoreErrors'});
  await page.route('**/*',route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  await page.route('**/schedule-qa-cover.svg',route => route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#2457ff"/></svg>'}));
  await page.route('**/navigation.json',route => reply(route,[]));
  await page.route('**/api/**',route => {
    const request = route.request(), path = request.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0], body = request.postDataJSON();
    calls.push({path,method:request.method(),body});
    if (path === '/api/ui/topbar') return reply(route,{authenticated:true,user:{name:'测试用户',role:'admin',is_admin:true,permissions:{youtube_auto_publish:true}}});
    if (path.endsWith('/bootstrap')) return reply(route,{enabled:true,settings:{default_description:'默认描述'},source:{configured:true},channels_loaded:false});
    if (path.endsWith('/channels')) return reply(route,{channels:[channel],checking:false});
    if (path.endsWith('/materials')) return reply(route,{configured:true,items:[material]});
    if (path.endsWith('/tasks') && request.method() === 'GET') return reply(route,{items:tasks,total:tasks.length,counts:{all:tasks.length,review:2,running:2,published:1,failed:1}});
    if (path.endsWith('/tasks') && request.method() === 'POST') {
      if (createFailure) { createFailure = false; return reply(route,{message:'服务器校验失败，请修改后重试'},422); }
      const t = record('created-'+(++creates),{status:'generating',can_review:false,publish_at:body.publish_at,title:body.title_template});
      tasks.unshift(t); return reply(route,{task:t});
    }
    const match = path.match(/\/tasks\/([^/]+)(?:\/(schedule|review))?$/);
    if (match) {
      const t = tasks.find(t => t.id === match[1]); if (!t) return reply(route,{message:'任务不存在'},404);
      if (match[2] === 'schedule') {
        if (controlMode === 'uncertain') return reply(route,{message:'平台结果待确认'},503);
        if (controlMode === 'conflict') { t.schedule_version += 1; controlMode = 'confirmed'; return reply(route,{message:'版本冲突'},409); }
        if (controlMode === 'pending') { t.schedule_control = {action:body.action,state:'pending',publish_at:body.publish_at || '',message:'平台正在确认'}; t.can_schedule = false; t.schedule_version += 1; }
        else { t.schedule_control = {action:body.action,state:'confirmed'}; t.publish_at = body.action === 'reschedule' ? body.publish_at : ''; t.schedule_version += 1; t.status = body.action === 'cancel' ? 'cancelled' : body.action === 'immediate' ? 'processing' : 'scheduled'; t.can_review = false; t.can_schedule = body.action !== 'cancel'; t.schedule_state = body.action === 'reschedule' ? 'scheduled' : ''; }
        return reply(route,{task:t});
      }
      if (match[2] === 'review') { t.can_review = false; t.status = t.schedule_state === 'missed' ? 'schedule_missed' : 'processing'; return reply(route,{task:t}); }
      return reply(route,{task:t});
    }
    return reply(route,{message:'Unexpected route '+path},404);
  });
  // Verify the same Beijing digits on a browser observing US daylight saving.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setTimezoneOverride',{timezoneId:'America/Los_Angeles'});
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/youtube-publish.html');
  await page.locator('#task-table [data-id="scheduled"]').first().waitFor();
  check('test browser uses non-Beijing timezone',await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone) === 'America/Los_Angeles');
  check('list includes explicit Beijing target time',await page.locator('tr[data-task-id="scheduled"]').innerText().then(t => t.includes('2099-02-12 20:30（北京时间）')));
  check('missed review task displays overdue schedule without hiding cover review',await page.locator('tr[data-task-id="missed"]').innerText().then(t => t.includes('已错过预约时间') && t.includes('审核封面')));
  check('status filters expose scheduled, missed, pending and cancelled',await page.locator('#status-filter option').evaluateAll(options => ['scheduled','schedule_missed','schedule_pending','cancelled'].every(value => options.some(o => o.value === value))));
  const closeTop = () => page.locator('.modal-overlay').last().locator('[data-action="close-modal"]').first().click();
  const openDetails = async id => { await page.locator(`tr[data-task-id="${id}"] [data-action="details"]`).first().click(); await page.locator('.modal-title').filter({hasText:'发布任务详情'}).waitFor(); };
  const prepare = async () => {
    await page.locator('#new-publish').click();
    await page.locator('#draft-channel').selectOption(channel.id);
    await page.locator('[data-action="choose-material"]').click();
    await page.locator('[data-action="select-material"]').click();
    await page.locator('[data-action="confirm-material"]').click();
    await page.locator('#draft-title').fill('测试视频');
    await page.locator('#draft-requirements').fill('横版人物封面');
  };
  await prepare();
  check('new schedule is optional and initially blank',await page.locator('#draft-publish-at').inputValue() === '' && !await page.locator('#draft-publish-at').getAttribute('required'));
  check('immediate summary retains AI approval semantics',await page.locator('.modal-footer').innerText().then(t => t.includes('审核后立即发布') && t.includes('提交并生成封面')));
  check('schedule follows description and comment',await page.locator('#draft-publish-at').evaluate(el => Boolean(document.querySelector('#draft-comment').compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)));
  check('desktop schedule spans the row below description and comment',await page.locator('.publish-schedule-field').evaluate(el => { const rect = el.getBoundingClientRect(), description = document.querySelector('.copy-field-description').getBoundingClientRect(), comment = document.querySelector('.copy-field-comment').getBoundingClientRect(); return rect.top >= Math.max(description.bottom,comment.bottom) && Math.abs(description.top-comment.top)<2 && rect.width > description.width*1.8; }));
  await page.locator('#draft-publish-at').fill(future);
  check('future selection updates summary in Beijing time',await page.locator('.modal-footer').innerText().then(t => t.includes('预约公开：2099-02-12 20:30（北京时间）')));
  await page.locator('#draft-publish-at').evaluate(el => { window.scheduleInput = el; window.scheduleDialog = el.closest('.modal'); });
  await page.locator('[data-action="retry-channels"]').click();
  await page.waitForTimeout(100);
  check('channel refresh preserves date control and modal identity',await page.locator('#draft-publish-at').evaluate(el => window.scheduleInput === el && window.scheduleDialog === el.closest('.modal')));
  check('channel refresh preserves selected future date',await page.locator('#draft-publish-at').inputValue() === future);
  await page.locator('[data-action="clear-publish-at"]').click();
  check('clear returns to immediate summary',await page.locator('#draft-publish-at').inputValue() === '' && await page.locator('.modal-footer').innerText().then(t => t.includes('审核后立即发布')));
  await page.locator('[data-action="submit-publish"]').click();
  await page.getByRole('heading',{name:'发布任务详情'}).waitFor();
  check('blank create sends empty publish_at',createCalls().at(-1).body.publish_at === '');
  await closeTop();
  await prepare();
  await page.locator('#draft-publish-at').fill('2020-01-01T08:00');
  const beforePast = createCalls().length;
  await page.locator('[data-action="submit-publish"]').click();
  check('past schedule blocks submission with useful timezone error',createCalls().length === beforePast && await page.locator('.error-message').innerText().then(t => t.includes('晚于当前北京时间')));
  await page.locator('#draft-publish-at').fill(future);
  createFailure = true;
  await page.locator('[data-action="submit-publish"]').click();
  await page.locator('.inline-error').waitFor();
  check('failed request preserves future date and title',await page.locator('#draft-publish-at').inputValue() === future && await page.locator('#draft-title').inputValue() === '测试视频');
  await page.locator('[data-action="submit-publish"]').click();
  await page.getByRole('heading',{name:'发布任务详情'}).waitFor();
  check('future create converts Beijing digits to UTC independent of US timezone',createCalls().at(-1).body.publish_at === futureIso);
  check('new task shows schedule without claiming platform confirmed',await page.locator('.schedule-card').innerText().then(t => t.includes('预约公开：2099-02-12 20:30') && !t.includes('已预约')));
  await closeTop();
  await page.locator('tr[data-task-id="review"] [data-action="review"]').click();
  check('review shows selected public time and preparation semantics',await page.locator('.review-side .schedule-card').innerText().then(t => t.includes('20:30')) && await page.locator('.review-layout .note-blue').first().innerText().then(t => t.includes('等待预约时间公开')));
  await closeTop();
  await page.locator('tr[data-task-id="missed"] [data-action="review"]').click();
  check('missed cover review explains explicit reschedule or immediate requirement',await page.locator('.schedule-card').innerText().then(t => t.includes('完成封面审核后') && t.includes('重新定时')));
  await closeTop();
  await openDetails('scheduled');
  await page.locator('[data-action="schedule-reschedule"]').click();
  check('edit prepopulates current Beijing time',await page.locator('#schedule-publish-at').inputValue() === future);
  await page.locator('#schedule-publish-at').fill('');
  await page.locator('[data-action="save-schedule"]').click();
  check('reschedule requires date and does not silently publish immediately',controls().length === 0 && await page.locator('.error-message').innerText().then(t => t.includes('请选择新的预约')));
  await page.locator('#schedule-publish-at').fill(changed);
  controlMode = 'conflict';
  await page.locator('[data-action="save-schedule"]').click();
  await page.locator('.modal-overlay').last().locator('.inline-error').waitFor();
  check('schedule CAS conflict retains operator date edit',await page.locator('#schedule-publish-at').inputValue() === changed && await page.locator('.modal-overlay').last().innerText().then(t => t.includes('已加载最新状态')));
  const firstOperation = controls().at(-1).body.operation_id;
  controlMode = 'pending';
  await page.locator('[data-action="save-schedule"]').click();
  await page.waitForFunction(() => document.querySelectorAll('.modal-overlay').length === 1);
  check('reschedule sends refreshed CAS, new operation ID and correct summer UTC rollover',controls().at(-1).body.schedule_version === 2 && controls().at(-1).body.operation_id !== firstOperation && controls().at(-1).body.publish_at === changedIso);
  check('accepted reschedule shows pending target alongside confirmed original time',await page.locator('.schedule-card').innerText().then(t => t.includes('预约公开：2099-02-12 20:30') && t.includes('申请改为：2099-07-01 00:15') && t.includes('待确认')));
  check('unconfirmed reschedule does not claim success or enable duplicate writes',await page.locator('[data-action="schedule-reschedule"]').count() === 0 && await page.locator('#toast-root').innerText().then(t => t.includes('待确认') && !t.includes('已更新')));
  const pending = tasks.find(t => t.id === 'scheduled');
  pending.schedule_control = {action:'reschedule',state:'confirmed'}; pending.can_schedule = true; pending.publish_at = changedIso; pending.schedule_version += 1;
  await page.locator('.schedule-card').evaluate(el => { window.scheduleCardNode = el; window.detailsNode = el.closest('.modal'); });
  await page.waitForFunction(() => document.querySelector('.schedule-card')?.textContent.includes('预约公开：2099-07-01 00:15') && !document.querySelector('.schedule-card')?.textContent.includes('申请改为'),{timeout:9000});
  check('confirmed readback updates schedule without remounting modal or card',await page.locator('.schedule-card').evaluate(el => window.scheduleCardNode === el && window.detailsNode === el.closest('.modal')));
  await page.locator('[data-action="schedule-immediate"]').click();
  check('immediate action explicitly retains cover approval',await page.locator('.schedule-explanation').innerText().then(t => t.includes('AI 封面仍需审核')));
  controlMode = 'confirmed';
  await page.locator('[data-action="save-schedule"]').click();
  await page.waitForFunction(() => document.querySelectorAll('.modal-overlay').length === 1);
  check('immediate action sends explicit command',controls().at(-1).body.action === 'immediate' && !Object.prototype.hasOwnProperty.call(controls().at(-1).body,'publish_at'));
  check('immediate readback removes reservation after confirmation',await page.locator('.schedule-card').innerText().then(t => t.includes('审核后立即发布') && !t.includes('预约公开：')));
  await page.locator('[data-action="schedule-cancel"]').click();
  controlMode = 'pending';
  await page.locator('[data-action="save-schedule"]').click();
  await page.waitForFunction(() => document.querySelectorAll('.modal-overlay').length === 1);
  check('cancel request awaits confirmation rather than displaying cancelled',await page.locator('.schedule-card').innerText().then(t => t.includes('取消发布请求待确认') && !t.includes('已取消本次发布')));
  pending.schedule_control = {action:'cancel',state:'confirmed'}; pending.status = 'cancelled'; pending.can_schedule = false;
  await closeTop();
  await openDetails('scheduled');
  check('cancelled readback displays final state and hides controls',await page.locator('.schedule-card').innerText().then(t => t.includes('已取消本次发布')) && await page.locator('.schedule-actions').count() === 0);
  await closeTop();
  await openDetails('unknown');
  check('unknown platform outcome hides all mutation controls',await page.locator('.schedule-card').innerText().then(t => t.includes('待确认')) && await page.locator('.schedule-actions').count() === 0);
  await closeTop();
  await openDetails('published');
  check('published tasks cannot change schedule or cancel',await page.locator('.schedule-actions').count() === 0);
  await closeTop();
  await openDetails('review');
  await page.locator('[data-action="schedule-reschedule"]').click();
  await page.locator('#schedule-publish-at').fill(changed);
  controlMode = 'uncertain';
  const beforeUnknown = controls().length;
  await page.locator('[data-action="save-schedule"]').click();
  await page.locator('.schedule-uncertain').waitFor();
  check('uncertain POST freezes original edit and offers read-only reconciliation',await page.locator('#schedule-publish-at').isDisabled() && await page.locator('#schedule-publish-at').inputValue() === changed && await page.locator('[data-action="save-schedule"]').count() === 0);
  await page.locator('[data-action="refresh-schedule"]').click();
  await page.waitForTimeout(150);
  check('uncertain reconciliation never repeats schedule mutation',controls().length === beforeUnknown + 1);
  await closeTop(); await closeTop();
  await page.setViewportSize({width:390,height:844});
  await page.locator('#new-publish').click();
  await page.locator('#draft-publish-at').fill(future);
  await page.locator('#draft-publish-at').scrollIntoViewIfNeeded();
  check('mobile datetime row fits modal width',await page.locator('.schedule-input-row').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
  check('mobile dialog does not overflow viewport',await page.locator('.modal').evaluate(el => el.getBoundingClientRect().right <= innerWidth && el.getBoundingClientRect().left >= 0));
  await cdp.send('Emulation.setTimezoneOverride',{timezoneId:''});
  check('no browser runtime errors',errors.length === 0);
  return {passed:checks.length,checks,errors,mutations:createCalls().length+controls().length};
}
