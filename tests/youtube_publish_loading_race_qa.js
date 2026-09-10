/* Isolated regressions for independent initial reads and late poll responses. */
async (page) => {
  const checks=[],errors=[];
  const check=(name,value)=>{if(!value)throw new Error(name);checks.push(name);};
  const reply=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  const origin=page.url().match(/^http:\/\/127\.0\.0\.1:\d+/)?.[0] || 'http://127.0.0.1:8877',id='b'.repeat(32);
  let initialList,oldDetail,postList,detailCalls=0,listCalls=0,reviewed=false;
  const reviewTask={id,title:'任务不在最近200条列表中',description:'测试详情缓存',comment:'',title_template:'任务不在最近200条列表中',description_template:'测试详情缓存',comment_template:'',material:{id:'1',name:'已冻结素材',thumbnail_url:'',macro_name:'测试',macro_desc:'简介'},channel:{id:'1',name:'测试频道'},cover_source:'ai',requirements:'16:9横版',status:'review',phase:'cover',created_at:'2026-09-10T10:00:00Z',current_version:1,cover_url:'',versions:[{number:1,url:'',feedback:'首次生成'}],steps:[],notification:{status:'sent',message:'已发送提醒'},can_review:true,can_retry:false};
  const publishedTask={...reviewTask,status:'enqueue_pending',can_review:false,phase:'upload'};
  const empty={items:[],total:0,counts:{all:0,review:0,running:0,published:0,failed:0}};
  page.on('pageerror',e=>errors.push(e.message));
  await page.unrouteAll({behavior:'ignoreErrors'});
  await page.route('**/navigation.json',route=>reply(route,[]));
  await page.route('**/api/**',route=>{
    const request=route.request(),path=request.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0];
    if(path==='/api/ui/topbar')return reply(route,{authenticated:true,user:{name:'测试用户',role:'admin',is_admin:true,tenant_key:'test',permissions:{youtube_auto_publish:true}}});
    if(path.endsWith('/bootstrap'))return reply(route,{settings:{default_description:'默认描述'},source:{configured:true},channels:[],channels_loaded:false,can_manage_settings:true,enabled:true});
    if(path.endsWith('/channels'))return;
    if(path.endsWith('/tasks')&&request.method()==='GET'){
      listCalls++;
      if(listCalls===1){initialList=route;return;}
      if(reviewed){postList=route;return;}
      return reply(route,empty);
    }
    if(path.endsWith('/tasks/'+id+'/review')){reviewed=true;check('deep link approval retains frozen version',request.postDataJSON().version===1);return reply(route,{task:publishedTask});}
    if(path.endsWith('/tasks/'+id)){
      detailCalls++;
      if(detailCalls===2&&!reviewed){oldDetail=route;return;}
      return reply(route,{task:reviewed?publishedTask:reviewTask});
    }
    return reply(route,{error:'unexpected'},404);
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/youtube-publish.html?task_id='+id+'&version=1',{waitUntil:'domcontentloaded'});
  await page.getByRole('heading',{name:'审核封面',exact:true}).waitFor();
  check('detail renders before initial list finishes',Boolean(initialList)&&!reviewed);
  await reply(initialList,empty);
  await page.locator('#task-count').getByText('共 0 条任务',{exact:true}).waitFor();
  check('detail remains available when absent from list',await page.locator('.modal-overlay').innerText().then(t=>t.includes(reviewTask.title)));
  await page.waitForTimeout(6500);
  check('old detail poll is deliberately pending',Boolean(oldDetail));
  await page.locator('[data-action="approve"]').click();
  await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  check('review success releases dialog before list refresh',await page.locator('.close-modal').isEnabled());
  await reply(oldDetail,{task:reviewTask});
  await page.waitForTimeout(200);
  check('late pre-approval detail cannot overwrite POST result',await page.locator('.modal-overlay').innerText().then(t=>t.includes('等待上传发布')&&!t.includes('待审核封面')));
  check('post-review list request still pending',Boolean(postList));
  await reply(postList,{error:'temporary_failure',message:'刷新暂不可用'},503);
  await page.locator('#task-count').getByText('自动刷新失败，稍后重试（保留上次数据）',{exact:true}).waitFor();
  await page.waitForTimeout(200);
  check('quiet refresh failure remains visible after render',await page.locator('#task-count').innerText().then(t=>t.includes('自动刷新失败')));
  check('failed list refresh preserves task detail',await page.locator('.modal-overlay').innerText().then(t=>t.includes('等待上传发布')));
  await page.locator('.close-modal').click();
  check('no JavaScript exceptions',errors.length===0);
  return {passed:checks.length,checks,errors};
}
