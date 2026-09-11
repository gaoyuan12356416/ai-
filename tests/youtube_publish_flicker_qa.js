/* playwright-cli run-code --filename tests/youtube_publish_flicker_qa.js
 * Local mocked API only. Port 8891 is the unchanged baseline, 8890 the fix.
 * Use real 6-second polling so animation/node churn is measured, not disabled. */
async (page) => {
  const origin = page.url().match(/^http:\/\/127\.0\.0\.1:\d+/)[0], baseline = origin.endsWith(':8891');
  const results = [], errors = [], calls = [];
  const check = (name, ok) => { if (!ok) throw new Error(name); results.push(name); };
  const cover = origin + '/qa-flicker-cover.svg';
  const material = {id:'material-1',name:'录屏复现素材',macro_name:'测试剧',macro_desc:'剧情简介',macro_url:'',content_id:'qa',source_job_id:'',link_ready:true,drama_status:'matched',drama_cover_url:cover,drama_cover_status:'available',thumbnail_url:cover,url:origin+'/qa-preview.mp4',language:'zh-TW',duration:'12:30'};
  const makeTask = id => ({id,title:'录屏复现任务',title_template:'{name}',description:('一段保留滚动位置的发布文案。\n').repeat(70),description_template:'{desc}',comment:'',comment_template:'',material,channel:{id:'ch-1',name:'测试频道'},cover_source:'ai',requirements:'横版封面',status:'published',phase:'complete',created_at:'2026-09-10T07:00:00Z',cover_url:cover,current_version:1,versions:[{number:1,url:cover,feedback:'',created_at:'2026-09-10T07:00:00Z'}],steps:[{key:'upload',label:'上传视频',status:'success',message:'上传完成'},{key:'public',label:'公开视频',status:'success',message:'已公开'},{key:'comment',label:'首条评论',status:'skipped',message:'未填写，已跳过'}],notification:{status:'sent',message:'已发送审核提醒'},can_review:false,can_retry:false,reference_cover:{url:cover,drama_name:'测试剧',frozen:true}});
  let tasks=[makeTask('stable-task')], deferMaterials=false, materialGate=null;
  const respond=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  await page.unrouteAll({behavior:'wait'});
  page.on('pageerror', e=>errors.push(e.message));
  await page.route('**/qa-flicker-cover.svg', route=>route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#3154b3"/><text x="80" y="350" fill="white" font-size="90">Stable cover</text></svg>'}));
  await page.route('**/qa-preview.mp4',route=>route.fulfill({status:200,contentType:'video/mp4',body:''}));
  await page.route('**/navigation.json',route=>respond(route,[{key:'youtube',label:'YouTube 社媒',items:[{key:'youtubeAutoPublish',label:'YouTube 自动发布',kind:'page',href:'/youtube-publish.html',module:'youtube_auto_publish',enabled:true}]}]));
  await page.route('**/api/**',async route=>{
    const request=route.request(), path=request.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0];
    calls.push({path,method:request.method()});
    if(request.method()!=='GET')throw new Error('This regression must never POST');
    if(path==='/api/ui/topbar')return respond(route,{authenticated:true,user:{name:'QA',role:'admin',is_admin:true,tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if(path.endsWith('/bootstrap'))return respond(route,{settings:{default_description:'{desc}'},channels:[],channels_loaded:false,source:{configured:true},can_manage_settings:true,enabled:true});
    if(path.endsWith('/channels'))return respond(route,{channels:[{id:'ch-1',name:'测试频道',eligible:true}]});
    if(path.endsWith('/materials')){if(deferMaterials)await new Promise(resolve=>{materialGate=resolve;});return respond(route,{configured:true,items:[material]});}
    if(path.endsWith('/tasks'))return respond(route,{items:tasks,total:tasks.length,counts:{all:tasks.length,published:tasks.filter(t=>t.status==='published').length,review:tasks.filter(t=>t.can_review).length,running:tasks.filter(t=>t.status==='processing').length,failed:0}});
    const match=path.match(/\/tasks\/([^/]+)$/);if(match)return respond(route,{task:tasks.find(t=>t.id===match[1])});
    return respond(route,{error:{message:'Unexpected mock route'}},404);
  });
  await page.setViewportSize({width:1366,height:768});
  await page.goto(origin+'/youtube-publish.html');
  await page.locator('#task-table tr').first().waitFor();
  await page.locator('[data-action="details"][data-id="stable-task"]').last().click();
  await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  await page.waitForTimeout(300);
  await page.evaluate(()=>{
    const body=document.querySelector('.modal-body'); body.scrollTop=320;
    window.__flicker={overlay:document.querySelector('.modal-overlay'),modal:document.querySelector('.modal'),body,cover:document.querySelector('.detail-cover'),row:document.querySelector('#task-table tr'),listImage:document.querySelector('.task-thumb img'),scroll:body.scrollTop,animations:[]};
    document.addEventListener('animationstart',event=>{if(event.target.matches('.modal,.modal-overlay'))window.__flicker.animations.push(event.animationName);});
  });
  const beforePoll=calls.filter(c=>c.path.endsWith('/tasks/stable-task')).length;
  await page.waitForTimeout(12800);
  const stable=await page.evaluate(()=>{const p=window.__flicker;return {overlay:p.overlay===document.querySelector('.modal-overlay'),modal:p.modal===document.querySelector('.modal'),body:p.body===document.querySelector('.modal-body'),cover:p.cover===document.querySelector('.detail-cover'),row:p.row===document.querySelector('#task-table tr'),listImage:p.listImage===document.querySelector('.task-thumb img'),scrollBefore:p.scroll,scrollAfter:document.querySelector('.modal-body').scrollTop,animations:p.animations.length};});
  check('two real automatic detail polls executed',calls.filter(c=>c.path.endsWith('/tasks/stable-task')).length>=beforePoll+2);
  if(baseline){
    check('baseline reproduces modal and cover replacement',!stable.overlay&&!stable.cover);
    check('baseline reproduces repeated entrance animations',stable.animations>=4);
    await page.screenshot({path:'output/playwright/flicker-recording/baseline-detail.png'});
    return {mode:'baseline',results,observation:stable};
  }
  for(const key of ['overlay','modal','body','cover','row','listImage'])check('unchanged polls preserve '+key,stable[key]);
  check('unchanged polls preserve scroll',Math.abs(stable.scrollAfter-stable.scrollBefore)<=1&&stable.scrollBefore>0);
  check('unchanged polls never replay entrance animations',stable.animations===0);

  // A real state transition must update, while retaining the currently read content.
  tasks[0]={...tasks[0],status:'processing',phase:'processing',steps:[{key:'upload',label:'上传视频',status:'success',message:'上传完成'},{key:'processing',label:'等待视频处理',status:'running',message:'新的处理进度'}]};
  await page.locator('.modal .review-task-heading .badge').getByText('视频处理中',{exact:true}).waitFor({timeout:10000});
  await page.waitForTimeout(300);
  const changed=await page.evaluate(()=>{const p=window.__flicker;return{overlay:p.overlay===document.querySelector('.modal-overlay'),modal:p.modal===document.querySelector('.modal'),cover:p.cover===document.querySelector('.detail-cover'),row:p.row===document.querySelector('#task-table tr'),scroll:document.querySelector('.modal-body').scrollTop,animations:p.animations.length};});
  for(const key of ['overlay','modal','cover','row'])check('changed progress preserves '+key,changed[key]);
  check('changed progress is visible',await page.locator('.timeline').innerText().then(x=>x.includes('新的处理进度')));
  check('changed progress preserves scroll',Math.abs(changed.scroll-stable.scrollBefore)<=1);
  check('changed progress does not replay entrance animation',changed.animations===0);
  await page.screenshot({path:'output/playwright/flicker-recording/fixed-detail-1366.png'});
  await page.locator('.close-modal').click();

  // A later-arriving picker search response must not rebuild its parent form.
  await page.locator('#new-publish').click();
  await page.locator('#draft-title').fill('保留文字与输入位置');
  await page.locator('[data-action="choose-material"]').click();
  await page.locator('.material-card').first().waitFor();
  // A prefetched result mounts immediately, before its first animation frame.
  // Measure later search updates after the intentional entrance has completed.
  await page.waitForTimeout(300);
  await page.evaluate(()=>{const p=window.__flicker;p.form=document.querySelector('#draft-title');p.publish=document.querySelectorAll('.modal')[0];p.picker=document.querySelectorAll('.modal')[1];p.search=document.querySelector('#material-search');p.animations=[];});
  await page.locator('#material-search').fill('测试');
  await page.locator('#material-search').evaluate(el=>el.setSelectionRange(0,1));
  await page.waitForTimeout(900);
  const picker=await page.evaluate(()=>{const p=window.__flicker;return{parent:p.publish===document.querySelectorAll('.modal')[0],form:p.form===document.querySelector('#draft-title'),picker:p.picker===document.querySelectorAll('.modal')[1],search:p.search===document.querySelector('#material-search'),focused:document.activeElement===p.search,selection:[p.search.selectionStart,p.search.selectionEnd],animations:p.animations.length};});
  for(const key of ['parent','form','picker','search','focused'])check('picker search preserves '+key,picker[key]);
  check('picker search preserves selection',picker.selection[0]===0&&picker.selection[1]===1);
  check('picker refresh does not replay entrance animation',picker.animations===0);
  check('parent draft retains typed text',await page.locator('#draft-title').inputValue()==='保留文字与输入位置');
  // While a nested preview is open, finish the pending materials fetch.
  deferMaterials=true;
  await page.locator('#material-search').fill('延迟');
  await page.locator('[data-action="preview-material"]').first().click();
  await page.locator('video').waitFor();
  await page.waitForTimeout(300);
  await page.evaluate(()=>{const p=window.__flicker;p.video=document.querySelector('video');p.preview=document.querySelector('.modal-overlay:last-child');p.animations=[];});
  deferMaterials=false;if(materialGate)materialGate();
  await page.waitForTimeout(700);
  const preview=await page.evaluate(()=>{const p=window.__flicker;return{video:p.video===document.querySelector('video'),overlay:p.preview===document.querySelector('.modal-overlay:last-child'),animations:p.animations.length};});
  check('background materials response preserves preview video node',preview.video);
  check('background materials response preserves preview overlay',preview.overlay);
  check('background materials response does not animate preview again',preview.animations===0);
  await page.locator('.modal-overlay:last-child .close-modal').click();
  await page.setViewportSize({width:1920,height:1080});
  await page.screenshot({path:'output/playwright/flicker-recording/fixed-picker-1920.png'});
  check('no JavaScript runtime errors',errors.length===0);
  check('all API verification was read only',calls.every(c=>c.method==='GET'));
  return {mode:'fixed',results,stable,changed,picker,preview};
}
