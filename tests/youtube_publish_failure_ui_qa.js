/* Isolated browser QA. All feature APIs and images are mocked, including POSTs. */
async (page) => {
  const origin=page.url().match(/^http:\/\/127\.0\.0\.1:\d+/)?.[0]||'http://127.0.0.1:8881';
  const checks=[],errors=[],calls=[];
  const check=(name,value)=>{if(!value)throw new Error(name);checks.push(name);};
  const reply=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  const cover1=origin+'/qa-cover-v1.svg',cover2=origin+'/qa-cover-v2.svg',materialThumb=origin+'/qa-material.svg';
  const material={id:'source-1',name:'测试发布素材.mp4',thumbnail_url:materialThumb,macro_name:'The Secret Heiress',macro_desc:'测试简介',long_url:'https://example.invalid/drama'};
  const channel={id:'channel-1',name:'DramaWave English',language:'en',eligible:true};
  const notification=(status,extra={})=>({status,message:'',stage:'cover',error_code:'generation_failed',event_id:'event-'+status,is_current:true,...extra});
  const make=(id,extra={})=>({id,title:'测试任务 '+id,title_template:'测试任务 '+id,description:('任务描述与发布文案\n').repeat(45),description_template:'',comment:'',comment_template:'',material,channel,cover_source:'ai',requirements:'16:9 横版封面',status:'generation_failed',phase:'cover',created_at:'2026-09-10T12:00:00Z',current_version:2,cover_url:'',versions:[],cover_preview:null,reference_cover:null,error:{code:'generation_failed',message:'本次封面生成失败，请重试。'},notification:{status:'none',message:'尚未发送审核提醒'},failure_notification:null,steps:[],can_review:false,can_retry:true,...extra});
  const tasks=[
    make('no-cover',{current_version:1,failure_notification:notification('pending')}),
    make('history',{versions:[{number:1,url:cover1}],cover_preview:{url:cover1,version:1,is_current:false},failure_notification:notification('sent')}),
    make('current',{status:'thumbnail_failed',phase:'thumbnail',cover_url:cover2,versions:[{number:1,url:cover1},{number:2,url:cover2}],cover_preview:{url:cover2,version:2,is_current:true},failure_notification:notification('failed',{stage:'thumbnail',message:'飞书服务暂不可用'})}),
    make('unknown',{failure_notification:notification('unknown',{message:'<img src=x onerror="window.qaFailureXss=1">发送结果不确定，请核对飞书。'})}),
    make('recovered',{status:'published',phase:'complete',error:{},cover_url:cover2,cover_preview:{url:cover2,version:2,is_current:true},failure_notification:notification('sent',{is_current:false}),can_retry:false}),
    make('other-event',{status:'publish_failed',phase:'public',error:{code:'publish_failed',message:'公开视频失败'},cover_preview:{url:cover2,version:2,is_current:true},failure_notification:notification('failed',{is_current:false})}),
    make('authoritative-null',{cover_url:cover1,versions:[{number:1,url:cover1}],cover_preview:null}),
    make('unsafe-preview',{cover_preview:{url:'javascript:window.qaFailureXss=1',version:2,is_current:true}}),
    make('legacy-history',{versions:[{number:1,url:cover1},{number:3,url:cover1,status:'failed'},{number:2,url:cover2}],current_version:4}),
    make('legacy-recovered',{status:'review',error:{},cover_url:cover2,versions:[{number:1,url:cover1},{number:2,url:cover2}],notification:{status:'sent',message:'审核通知已发送'},failure_notification:notification('sent'),can_review:true}),
    make('review-missing-current',{status:'review',error:{},versions:[{number:1,url:cover1}],cover_preview:{url:cover1,version:1,is_current:false},can_review:true}),
    make('legacy-cover-only',{status:'uploading',error:{},current_version:1,cover_url:cover1})
  ];
  delete tasks.find(t=>t.id==='legacy-history').cover_preview;
  delete tasks.find(t=>t.id==='legacy-recovered').cover_preview;
  delete tasks.find(t=>t.id==='legacy-recovered').failure_notification.is_current;
  delete tasks.find(t=>t.id==='legacy-cover-only').cover_preview;
  page.on('pageerror',e=>errors.push(e.message));
  await page.unrouteAll({behavior:'ignoreErrors'});
  await page.route('**/qa-*.svg',route=>route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#294d76"/><text x="80" y="340" fill="#f2dbac" font-size="62">Successful cover '+(route.request().url().includes('v2')?'V2':'V1')+'</text><text x="80" y="430" fill="#dce8f5" font-size="30">QA fixture</text></svg>'}));
  await page.route('**/navigation.json',route=>reply(route,[{key:'youtube',label:'YouTube 社媒',items:[{key:'youtubeAutoPublish',label:'YouTube 自动发布',kind:'page',href:'/youtube-publish.html',module:'youtube_auto_publish',enabled:true}]}]));
  await page.route('**/api/**',route=>{
    const request=route.request(),path=request.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0],body=request.postDataJSON();calls.push({path,method:request.method(),body});
    if(path==='/api/ui/topbar')return reply(route,{authenticated:true,user:{name:'异常处理 QA',role:'admin',is_admin:true,tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if(path.endsWith('/bootstrap'))return reply(route,{settings:{default_description:'默认描述'},source:{configured:true},enabled:true,channels:[],channels_loaded:false,can_manage_settings:true});
    if(path.endsWith('/channels'))return reply(route,{channels:[channel]});
    if(path.endsWith('/tasks')&&request.method()==='GET')return reply(route,{items:tasks,total:tasks.length,counts:{all:tasks.length,failed:tasks.filter(t=>t.status.endsWith('_failed')).length,review:tasks.filter(t=>t.can_review).length}});
    const match=path.match(/\/tasks\/([^/]+)(?:\/(review))?$/);
    if(match){const t=tasks.find(t=>t.id===match[1]);if(!t)return reply(route,{error:'unknown task'},404);if(match[2]){t.status='uploading';t.phase='upload';t.can_review=false;t.error={};}return reply(route,{task:t});}
    return reply(route,{error:'Unexpected route '+path},404);
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/youtube-publish.html');
  await page.locator('[data-task-id="history"]').waitFor();
  const row=id=>page.locator('[data-task-id="'+id+'"]');
  const open=async id=>{await row(id).locator('[data-action="details"]').first().click();await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();};
  const close=()=>page.locator('.modal-overlay').last().locator('.close-modal').click();
  const capture=async name=>{await page.waitForTimeout(250);await page.screenshot({path:'output/playwright/youtube-failure-handling/'+name,fullPage:false});};
  check('no-cover list has no thumbnail fallback',await row('no-cover').locator('.task-thumb img').count()===0&&await row('no-cover').innerText().then(t=>t.includes('暂无有效生成封面')));
  check('history list identifies successful prior version',await row('history').locator('.task-thumb img').getAttribute('src')===cover1&&await row('history').innerText().then(t=>t.includes('历史封面 V1（本次生成失败）')));
  check('current failed task retains current valid cover',await row('current').locator('.task-thumb img').getAttribute('src')===cover2&&await row('current').innerText().then(t=>t.includes('当前封面 V2')));
  check('authoritative null ignores stale cover URL and versions',await row('authoritative-null').locator('.task-thumb img').count()===0);
  check('invalid preview URL rejected',await row('unsafe-preview').locator('.task-thumb img').count()===0);
  check('legacy newest successful version selected',await row('legacy-history').locator('.task-thumb img').getAttribute('src')===cover2&&await row('legacy-history').innerText().then(t=>t.includes('历史封面 V2')));
  check('legacy cover-only DTO still supported',await row('legacy-cover-only').locator('.task-thumb img').getAttribute('src')===cover1);
  check('material thumbnail never displayed as generated cover',await page.locator('#task-table img[src="'+materialThumb+'"]').count()===0);
  await open('no-cover');
  check('detail empty state explicit',await page.locator('.task-cover-preview').innerText().then(t=>t.includes('暂无有效生成封面'))&&await page.locator('.task-cover-preview img').count()===0);
  check('pending failure notice separate from review notification',await page.locator('.failure-notification-status').innerText()==='待发送'&&await page.locator('.modal-body').innerText().then(t=>t.includes('异常飞书提醒')&&t.includes('飞书审核提醒')));
  await capture('01-no-cover-pending-1440.png');
  await close();
  await open('history');
  check('history detail caption is explicit',await page.locator('.cover-preview-caption').innerText()==='历史封面 V1（本次生成失败）');
  check('sent notice acknowledged',await page.locator('.failure-notification-status').innerText()==='已发送');
  await capture('02-history-cover-1440.png');
  await page.waitForTimeout(250);
  await page.evaluate(()=>{const body=document.querySelector('.modal-body');body.scrollTop=280;window.qaFailureNodes={overlay:document.querySelector('.modal-overlay'),modal:document.querySelector('.modal'),body,image:document.querySelector('.task-cover-preview img'),row:document.querySelector('[data-task-id="history"]'),rowImage:document.querySelector('[data-task-id="history"] .task-thumb img'),scroll:body.scrollTop,animations:0};document.addEventListener('animationstart',event=>{if(event.target===window.qaFailureNodes.overlay||event.target===window.qaFailureNodes.modal)window.qaFailureNodes.animations++;});});
  const history=tasks.find(t=>t.id==='history');history.status='generating';history.error={};history.failure_notification.is_current=false;
  await page.waitForFunction(()=>document.querySelector('.cover-preview-caption')?.textContent.includes('本次生成未完成'),{timeout:10000});
  check('poll updates historical cover reason after retry',await page.locator('.cover-preview-caption').innerText()==='历史封面 V1（本次生成未完成）');
  check('recovered progress labels old notice as historical',await page.locator('.failure-notification').innerText().then(t=>t.includes('历史异常提醒')&&!t.includes('当前异常提醒')));
  check('changed poll preserves modal, image, row and scroll',await page.evaluate(()=>{const n=window.qaFailureNodes;return n.overlay===document.querySelector('.modal-overlay')&&n.modal===document.querySelector('.modal')&&n.body===document.querySelector('.modal-body')&&n.image===document.querySelector('.task-cover-preview img')&&n.row===document.querySelector('[data-task-id="history"]')&&n.rowImage===document.querySelector('[data-task-id="history"] .task-thumb img')&&Math.abs(n.body.scrollTop-n.scroll)<2&&n.animations===0;}));
  const reads=calls.filter(c=>c.path.endsWith('/tasks/history')).length;
  await page.waitForTimeout(6300);
  check('unchanged poll still fetches current task',calls.filter(c=>c.path.endsWith('/tasks/history')).length>reads);
  check('unchanged poll retains image and avoids entrance animation',await page.evaluate(()=>window.qaFailureNodes.image===document.querySelector('.task-cover-preview img')&&window.qaFailureNodes.animations===0));
  await close();
  for(const [id,text] of [['current','发送失败'],['unknown','发送结果待核对']]){await open(id);check(id+' notification status clear',await page.locator('.failure-notification-status').innerText()===text);await close();}
  await open('unknown');
  check('notification message remains escaped text',await page.locator('.failure-notification').innerText().then(t=>t.includes('<img src=x'))&&await page.evaluate(()=>!window.qaFailureXss));
  await close();
  await open('recovered');
  check('published status not replaced by historical error',await page.locator('.review-task-heading .badge').innerText()==='已发布'&&await page.locator('.failure-notification').innerText().then(t=>t.includes('历史异常提醒')&&t.includes('当前任务状态以上方状态为准')));
  await capture('03-published-history-notice-1440.png');
  await close();
  await open('other-event');
  check('is_current false honored even when another failure exists',await page.locator('.failure-notification').innerText().then(t=>t.includes('历史异常提醒')&&!t.includes('当前异常提醒')));
  await close();
  await row('legacy-recovered').locator('[data-action="review"]').click();await page.getByRole('heading',{name:'审核封面',exact:true}).waitFor();
  check('legacy non-failed task notice marked historical',await page.locator('.failure-notification').innerText().then(t=>t.includes('历史异常提醒')));
  await page.locator('[data-action="view-version"][data-id="1"]').click();
  check('historical version remains unapprovable',await page.locator('[data-action="approve"]').isDisabled());
  check('review historical image shown by selected version',await page.locator('.review-image>img').getAttribute('src')===cover1);
  await page.locator('[data-action="view-version"][data-id="2"]').click();
  check('valid current version remains approvable',!(await page.locator('[data-action="approve"]').isDisabled()));
  await page.locator('[data-action="approve"]').click();await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  check('current-version review contract preserved',calls.some(c=>c.body?.action==='approve'&&c.body.version===2));
  await close();
  await row('review-missing-current').locator('[data-action="review"]').click();await page.getByRole('heading',{name:'审核封面',exact:true}).waitFor();
  check('review does not label historical fallback as current image',await page.locator('.review-image img').count()===0&&await page.locator('.review-image').innerText().then(t=>t.includes('暂无有效生成封面')));
  check('missing current image cannot be approved',await page.locator('[data-action="approve"]').isDisabled());
  check('no failure notice invented for task without one',await page.locator('.failure-notification').count()===0);
  check('no JavaScript exceptions',errors.length===0);
  return {passed:checks.length,checks,errors,requestCount:calls.length,screenshots:3};
}
