async (page) => {
  // Isolated mock API: no production service, queue, or Meta request is used.
  const checks=[],calls=[],errors=[],operations=new Map();
  const check=(value,name)=>{if(!value)throw new Error(name);checks.push(name);};
  const BASE="/api/fb-video-publish",POOL=BASE+"/material-pool";
  let pool={version:0,status:"paused",group_ids:[],schedule_mode:"fixed",times:[],daily_batches:1,caption_template:"{name}\n{desc}\n{url}",processing_mode:"original",publisher_name:"",effective_at_utc:""};
  let rows=[],counter=0,abortNext=false,hideOperation=false,failGroups=false,rejectSave=false,featureEnabled=true,navMode="full";
  const material=id=>({material_id:id,material_name:id==="201"?"<img src=x onerror=alert(1)>":"素材 "+id,language:id==="202"?"ja":"en",drama_name:"测试剧"});
  const ready=()=>page.waitForFunction(()=>!document.getElementById("settingsControls").disabled);
  const done=()=>page.waitForFunction(()=>document.getElementById("pendingPanel").hidden&&!document.getElementById("settingsControls").disabled);
  page.on("pageerror",error=>errors.push(error.message));
  page.on("dialog",dialog=>dialog.accept());
  await page.route("**/navigation.json",route=>navMode==="unavailable"?route.fulfill({status:503,contentType:"application/json",body:'{"message":"导航暂不可用"}'}):route.fulfill({contentType:"application/json",body:JSON.stringify([{key:"facebook_platform",label:"Facebook 社媒",items:[{key:"fbPostMaterialPool",label:"FB Post 素材池",module:"fb_video_publish",enabled:true,href:"/fb-post-material-pool.html"},{key:"fbVideoPublishRecords",label:"批量发布记录",module:navMode==="unscoped"?"":"fb_video_publish",enabled:navMode!=="pool-only",href:"/fb-video-publish-records.html"},{key:"fbVideoPublish",label:"视频批量发布",module:"fb_video_publish",enabled:navMode==="full",href:"/fb-video-publish.html"}]}])}));
  await page.route("**/api/**",async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname,body=request.postDataJSON()||{},method=request.method();calls.push({path,method,body,query:url.search});
    const reply=(data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify({ok:status<400,...data})});
    if(path==="/api/ui/topbar")return reply({authenticated:true,user:{user_id:"fixture-user",tenant_key:"fixture-tenant",name:"验收用户",permissions:{fb_video_publish:true}}});
    if(path===BASE+"/languages")return reply({items:[{value:"en",label:"英语"},{value:"ja",label:"日语"},{value:"zh-cn",label:"简体中文"}]});
    if(path===BASE+"/page-groups")return failGroups?reply({message:"Page 组读取暂不可用"},503):reply({items:[{id:"11",name:"英语 Page 组",total_pages:3},{id:"12",name:"日语 Page 组",total_pages:2}]});
    if(path===POOL+"/schedule"&&method==="GET")return reply({pool,feature_enabled:featureEnabled,day_plan:pool.schedule_mode==="random"?[{bj_date:"2026-10-27",times:["00:15","01:15"],mode:"random"}]:[]});
    if(path===POOL&&method==="GET"){
      const lang=url.searchParams.get("language"),state=url.searchParams.get("state"),cursor=Number(url.searchParams.get("cursor")||0),filtered=rows.filter(row=>(!lang||row.language===lang)&&(state==="all"||state===row.status||state==="bound"&&!!row.task_id));
      const counts={bound:0};for(const row of rows){counts[row.status]=(counts[row.status]||0)+1;if(row.task_id)counts.bound++;}
      return reply({pool,items:filtered.slice(cursor,cursor+2),counts,next_cursor:cursor+2<filtered.length?String(cursor+2):"",has_more:cursor+2<filtered.length});
    }
    if(path===POOL+"/runs")return reply({items:[{id:"run-fixture",slot_at_utc:"2026-10-26T01:00:00Z",status:"processing",task_counts:{published:1,unknown:1},pairs:[{material_id:"800",page_id:"701",page_name:"测试 Page",language:"en"}],excluded:[{page_id:"702",message:"语言冲突"}],batch_id:"batch-fixture",can_view_tasks:true},{id:"empty-fixture",slot_at_utc:"2026-10-26T00:00:00Z",status:"empty",reason:"no_eligible_pairs",task_counts:{},pairs:[],excluded:[]},{id:"created-fixture",slot_at_utc:"2026-10-25T23:00:00Z",status:"created",reason:"",task_counts:{},pairs:[],excluded:[]}],next_cursor:""});
    if(path===POOL+"/items/resolve")return reply({items:body.material_ids.filter(id=>id!=="999").map(material),errors:body.material_ids.includes("999")?[{material_id:"999",code:"mapping_missing",message:"剧目映射不唯一"}]:[]});
    if(path===POOL+"/preview")return reply({pairs:rows.filter(r=>r.status==="pending").slice(0,2).map((r,i)=>({...r,item_id:r.id,page_id:String(700+i),page_name:i?"日语 Page":"英语 Page",caption:"<script>不可执行</script>"})),excluded:[{page_id:"999",message:"组成员语言冲突"},{page_id:"998",message:"Page 暂无授权"}],language_capacity:[{language:"en",page_count:3,material_count:2,matched_count:1},{language:"ja",page_count:1,material_count:1,matched_count:1}],quota_remaining:9});
    if(path.startsWith(POOL+"/operations/"))return !hideOperation&&operations.has(path.split("/").pop())?reply(operations.get(path.split("/").pop())):reply({message:"未查询到原操作"},404);
    if(method==="POST"&&path.startsWith(POOL+"/")){
      if(operations.has(body.operation_id))return reply(operations.get(body.operation_id));
      if(path===POOL+"/schedule"&&rejectSave){rejectSave=false;pool.version++;return reply({message:"素材池版本已变化",code:"pool_version_conflict"},409);}
      if(body.expected_version!==pool.version)return reply({message:"素材池版本已变化",code:"pool_version_conflict"},409);
      let result={};
      if(path===POOL+"/schedule"){const changed=JSON.stringify(pool.group_ids)!==JSON.stringify(body.group_ids);pool={...pool,group_ids:body.group_ids,schedule_mode:body.schedule_mode,times:body.times,daily_batches:body.daily_batches,caption_template:body.caption_template,status:changed?"paused":pool.status,pause_reason:changed?"groups_changed":pool.pause_reason,effective_at_utc:body.schedule_mode==="random"?"2026-10-26T16:00:00Z":"2026-10-26T02:00:00Z"};}
      else if(path===POOL+"/enable")pool={...pool,status:"enabled",publisher_name:"验收用户",pause_reason:""};
      else if(path===POOL+"/pause")pool={...pool,status:"paused",pause_reason:"operator_paused"};
      else if(path===POOL+"/items"){const added=[],duplicates=[];for(const id of body.material_ids){if(rows.some(row=>row.material_id===id)){duplicates.push(id);continue;}const row={...material(id),id:String(++counter),status:"pending",can_modify:true,created_at_utc:"2026-10-26T01:00:00Z"};rows.unshift(row);added.push({id:row.id,material_id:id});}result={added,duplicates,errors:[]};}
      else if(path===POOL+"/items/remove"){for(const row of rows)if(body.item_ids.includes(row.id)&&row.can_modify){row.status="removed";row.can_modify=false;}result={removed:body.item_ids};}
      else return reply({message:"Unexpected mutation "+path},400);
      pool={...pool,version:pool.version+1};result={...result,pool,operation_id:body.operation_id,idempotent:false};operations.set(body.operation_id,JSON.parse(JSON.stringify(result)));
      if(abortNext){abortNext=false;return route.abort("failed");}return reply(result);
    }
    return reply({message:"Unexpected request "+path},404);
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto("/fb-post-material-pool.html");await ready();
  check((await page.locator("#scheduleState").textContent()).includes("已暂停"),"首次进入暂停");
  check(calls.every(call=>call.method==="GET"),"页面加载仅只读请求");
  await page.locator('[data-group="11"]').check();await page.locator("#fixedTimes").fill("09:00, 18:00");await page.locator("#saveSchedule").click();await done();
  check(pool.status==="paused"&&pool.times.length===2,"首次保存保留暂停并保存固定时点");
  check((await page.locator("#scheduleEffective").textContent()).includes("Page 组已变更"),"组选集变更原因显示中文");
  check(calls.filter(c=>c.path===POOL+"/schedule"&&c.method==="POST").every(c=>Number.isInteger(c.body.expected_version)&&c.body.operation_id),"所有设置写入带版本与幂等标识");
  await page.locator("#enableSchedule").click();await done();check(pool.status==="enabled","单独启用已保存计划");
  await page.locator("#pauseSchedule").click();await done();check((await page.locator("#scheduleEffective").textContent()).includes("已手动暂停"),"手动暂停原因显示中文");await page.locator("#enableSchedule").click();await done();
  await page.locator('[data-group="12"]').check();check(await page.locator("#enableSchedule").isDisabled(),"修改后阻止直接启用未保存配置");await page.locator("#saveSchedule").click();await done();check(pool.status==="paused"&&pool.group_ids.length===2,"修改组选集后自动暂停");
  await page.locator("#scheduleMode").selectOption("random");await page.locator("#dailyBatches").fill("25");const beforeInvalid=calls.length;await page.locator("#saveSchedule").click();await page.waitForFunction(()=>document.getElementById("pageError").textContent.includes("1–24"));check(calls.length===beforeInvalid,"随机批次数超限在本地阻止");
  await page.locator("#dailyBatches").fill("24");await page.locator("#saveSchedule").click();await done();check(pool.daily_batches===24&&pool.schedule_mode==="random","随机 24 批可保存");check((await page.locator("#dayPlan").textContent()).includes("2026-10-27"),"显示保存的随机日期计划");
  await page.locator("#materialIds").fill("201\n202\n999\n201");await page.locator("#addMaterials").click();await done();await page.waitForFunction(()=>document.getElementById("materialIds").value==="999");check(rows.length===2,"批量入池对输入去重");check((await page.locator("#admissionResult").textContent()).includes("剧目映射不唯一"),"保留失败 ID 并显示逐项原因");check(await page.locator("#materialList img").count()===0,"素材名称作为文字显示阻止 XSS");
  await page.locator("#materialIds").fill("201");await page.locator("#addMaterials").click();await done();await page.waitForFunction(()=>document.getElementById("admissionResult").textContent.includes("已在池内"));check(rows.length===2,"重复素材显示跳过而不重复入池");
  const writesBefore=calls.filter(c=>c.method==="POST"&&c.body.operation_id).length;await page.locator("#previewPairs").click();await page.waitForFunction(()=>document.getElementById("previewResult").textContent.includes("组成员语言冲突"));check(calls.filter(c=>c.method==="POST"&&c.body.operation_id).length===writesBefore,"配对预览不创建任务或写入计划");check((await page.locator("#previewResult").textContent()).includes("可用 Page 3"),"预览显示语言容量、授权和语言冲突");check(await page.locator("#previewResult script").count()===0,"预览文案安全显示");check((await page.locator("#runList").textContent()).includes("09:00"),"批次按北京时间展示");
  check((await page.locator("#runList").textContent()).includes("无可发布素材")&&(await page.locator("#runList").textContent()).includes("本批没有可用的语言配对")&&(await page.locator("#runList").textContent()).includes("已安排"),"空批次和已安排状态及原因显示中文");
  await page.locator("#itemLanguage").selectOption("ja");await page.waitForFunction(()=>document.querySelectorAll(".group-material").length===1);check((await page.locator("#materialList").textContent()).includes("202"),"语言筛选正确");await page.locator("#itemLanguage").selectOption("");await page.waitForFunction(()=>document.querySelectorAll(".group-material").length===2);
  await page.locator("#materialIds").fill("203\n204");abortNext=true;hideOperation=true;await page.locator("#addMaterials").click();await page.waitForFunction(()=>!document.getElementById("pendingPanel").hidden&&!document.getElementById("queryOperation").disabled);check(await page.locator("#addMaterials").isDisabled(),"未知入池结果锁定后续写入");const original=calls.filter(c=>c.path===POOL+"/items").at(-1).body;
  await page.reload();await page.waitForFunction(()=>!document.getElementById("pendingPanel").hidden&&!document.getElementById("queryOperation").disabled);check(await page.locator("#materialIds").inputValue()==="203\n204","刷新后恢复未确认的素材输入");await page.locator("#queryOperation").click();await page.waitForFunction(()=>document.getElementById("pageError").textContent.includes("暂未查到"));check(await page.locator("#addMaterials").isDisabled(),"查询 404 保留未知结果锁定");
  await page.locator("#retryOperation").click();await done();check(JSON.stringify(calls.filter(c=>c.path===POOL+"/items").at(-1).body)===JSON.stringify(original),"刷新后原样重试相同幂等操作");check(rows.length===4,"原操作重试不会重复入池");hideOperation=false;
  await page.locator("#captionTemplate").fill("新的文案 {name} {url}");rejectSave=true;await page.locator("#saveSchedule").click();await done();check(await page.locator("#captionTemplate").inputValue()==="新的文案 {name} {url}","版本冲突刷新后保留文案输入");check(!(await page.locator("#dirtyHint").isHidden()),"版本冲突保留未保存状态");await page.locator("#saveSchedule").click();await done();
  failGroups=true;await page.locator("#refreshData").click();await page.waitForFunction(()=>document.getElementById("pageError").textContent.includes("Page 组读取"));check(await page.locator("#saveSchedule").isDisabled(),"Page 组读取失败时禁止写入");check(await page.locator('[data-group="11"]').isChecked(),"读取失败保留已选组");failGroups=false;await page.locator("#refreshData").click();await ready();
  await page.locator("#itemsNext").click();await page.waitForFunction(()=>document.getElementById("materialList").textContent.includes("201"));check(!(await page.locator("#itemsPrev").isDisabled()),"素材分页可前后切换");await page.locator("#itemsPrev").click();await page.waitForFunction(()=>document.getElementById("materialList").textContent.includes("204"));
  await page.locator('#materialList input').first().check();await page.locator("#removeItems").click();await done();check(rows.some(row=>row.status==="removed"),"可移除未绑定素材");
  rows.push({...material("800"),id:"800",status:"unknown",can_modify:false,task_id:"task-unknown",batch_id:"batch-fixture",can_view_task:true,created_at_utc:"2026-10-26T01:00:00Z"});await page.locator("#itemState").selectOption("unknown");await page.waitForFunction(()=>document.getElementById("materialList").textContent.includes("800"));check(await page.locator("#materialList input").isDisabled(),"未知发布结果不可移除素材占用");
  navMode="pool-only";await page.locator("#refreshData").click();await ready();check(await page.locator("#materialList a, #runList a").count()===0,"仅素材池导航权限时不展示无法访问的记录链接");check((await page.locator("#runList").textContent()).includes("已发布 1")&&await page.locator("#runList details").count()>0,"仅素材池导航仍保留内联批次计数与配对详情");
  navMode="records-only";await page.locator("#refreshData").click();await ready();check(await page.locator("#materialList a").count()===1&&await page.locator("#runList a").count()===1,"已授权旧记录导航时展示素材和批次详情链接");
  navMode="unscoped";await page.locator("#refreshData").click();await ready();check(await page.locator("#materialList a, #runList a").count()===0,"旧导航未归属 Facebook 视频模块时不展示记录链接");
  navMode="unavailable";await page.locator("#refreshData").click();await ready();check(await page.locator("#materialList a, #runList a").count()===0,"导航读取失败不采用缓存权限开放记录链接");navMode="full";await page.locator("#refreshData").click();await ready();
  await page.locator("#itemState").selectOption("pending");await page.waitForFunction(()=>!document.getElementById("materialList").textContent.includes("800"));await page.locator("#previewPairs").click();await page.waitForFunction(()=>document.getElementById("previewStale").hidden);
  featureEnabled=false;await page.locator("#refreshData").click();await page.waitForFunction(()=>!document.getElementById("featureNotice").hidden&&!document.getElementById("refreshData").disabled);check(await page.locator("#addMaterials").isDisabled()&&await page.locator("#saveSchedule").isDisabled(),"维护开关关闭后禁止修改");check(!(await page.locator("#previewPairs").isDisabled()),"维护中仍可只读配对预览");featureEnabled=true;await page.locator("#refreshData").click();await ready();
  await page.screenshot({path:"output/playwright/fb-group-pool-desktop.png",fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:"output/playwright/fb-group-pool-mobile.png",fullPage:true});check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),"390px 无横向溢出");
  await page.locator("#showNav").click();check(await page.locator("#sidebar").isVisible(),"手机导航可展开");await page.locator("#hideNav").click();
  check(calls.every(c=>!c.path.includes("/pages/")&&!c.path.includes("/batches")&&!c.path.includes("/execute")),"页面未调用旧 Page 池配置或即时发布接口");check(errors.length===0,"浏览器无未捕获 JavaScript 错误");
  return {checks,request_count:calls.length,screenshots:["output/playwright/fb-group-pool-desktop.png","output/playwright/fb-group-pool-mobile.png"]};
}
