async (page) => {
  // Isolated local UI fixture. This script never contacts production or Graph.
  await page.unroute("**/api/**");
  page.removeAllListeners("dialog");page.removeAllListeners("pageerror");
  await page.evaluate(()=>sessionStorage.clear());
  const checks=[],calls=[],errors=[];
  const check=(value,name)=>{if(!value)throw new Error(name);checks.push(name);};
  const cfg={page_id:"101",page_name:"共享 Page",language:"zh",caption_template:"{name} {url}",config_instance_id:"instance-pool",version:17,eligible_token_count:1,created_by:"former-id",created_by_name:"原创建人 <b>不可执行</b>",updated_at_utc:new Date().toISOString()};
  const materials=Array.from({length:5},(_,i)=>({material_id:String(201+i),material_name:"池素材 "+(i+1),language:i===4?"en":"zh",drama_name:"测试剧 "+(i+1),duration_seconds:60}));
  let pool={config_instance_id:cfg.config_instance_id,version:0,status:"paused",times:[],processing_mode:"original",publisher_user_id:"",publisher_name:""};
  let rows=[],operations=new Map(),failSettings=false,rejectAdmission=false,rejectReplay=false,abortAdmission=false,hideOperation=false,repeatRequired=true,previews=0,admissions=0,counter=0;
  page.on("pageerror",e=>errors.push(e.message));page.on("dialog",d=>d.accept());
  await page.route("**/api/**",async route=>{
    const r=route.request(),u=new URL(r.url()),path=u.pathname.replace("/api/fb-video-publish",""),method=r.method(),body=r.postDataJSON() || {};
    calls.push({path,method,body,query:u.search});
    const respond=(data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify({ok:status<400,...data})});
    if(u.pathname==="/api/ui/topbar")return respond({authenticated:true,user:{user_id:"current-user",tenant_key:"tenant-test",name:"当前用户",is_admin:false,permissions:{fb_video_publish:true}}});
    if(path==="/languages")return respond({items:[{value:"zh",label:"中文"},{value:"en",label:"英语"}]});
    if(path==="/pages")return respond({items:[cfg]});
    if(path==="/materials"){
      const q=u.searchParams.get("q") || "",language=u.searchParams.get("language"),cursor=Number(u.searchParams.get("cursor") || 0);
      const found=materials.filter(m=>(!language||m.language===language)&&(m.material_name+m.material_id).includes(q));
      return respond({items:found.slice(cursor,cursor+2),next_cursor:cursor+2<found.length?String(cursor+2):"",has_more:cursor+2<found.length});
    }
    const endpoint="/pages/101/pool";
    if(path===endpoint&&method==="GET"){
      const state=u.searchParams.get("state"),rawCursor=u.searchParams.get("cursor") || "",cursor=Number(rawCursor.split(":")[0] || 0);
      if(rawCursor&&Number(rawCursor.split(":")[1])!==pool.version)return respond({code:"pool_version_conflict",message:"队列顺序已改变，请刷新"},409);
      const found=state==="slots"?[{slot_at_utc:"2026-10-20T01:00:00Z",status:"skipped",reason:"空池不补发"}]:rows.filter(x=>state==="failed"?["failed","cancelled","removed"].includes(x.status):x.status===state);
      const counts=Object.fromEntries(["pending","processing","published","failed","cancelled","removed"].map(s=>[s,rows.filter(x=>x.status===s).length]));counts.slots=1;
      return respond({pool,items:found.slice(cursor,cursor+2),counts,next_cursor:cursor+2<found.length?`${cursor+2}:${pool.version}`:"",has_more:cursor+2<found.length});
    }
    if(path.startsWith(endpoint+"/operations/")){
      if(hideOperation)return respond({code:"not_found",message:"未查询到"},404);
      const result=operations.get(path.split("/").pop());return result?respond(result):respond({code:"not_found",message:"未查询到"},404);
    }
    if(path.startsWith(endpoint+"/")&&method==="POST"){
      const action=path.slice(endpoint.length+1);
      if(action==="items"&&rejectReplay){rejectReplay=false;admissions++;return respond({code:"temporary_rejection",message:"本次重试未受理",accepted:false},409);}
      if(action==="items"&&operations.has(body.operation_id))return respond(operations.get(body.operation_id));
      if(body.config_instance_id!==cfg.config_instance_id||body.expected_version!==pool.version||action==="settings"&&failSettings){failSettings=false;return respond({message:"素材池版本已更新",code:"pool_conflict",accepted:false},409);}
      if(action==="settings"){pool={...pool,version:pool.version+1,times:body.times,processing_mode:body.processing_mode};return respond({pool});}
      if(action==="enable"){pool={...pool,version:pool.version+1,status:"enabled",publisher_user_id:"current-user",publisher_name:"当前用户"};return respond({pool});}
      if(action==="pause"){pool={...pool,version:pool.version+1,status:"paused"};return respond({pool});}
      if(action==="items/preview"){previews++;return respond({preview_token:"pool-preview",repeat_required:repeatRequired,items:body.material_ids.map(id=>materials.find(m=>m.material_id===id))});}
      if(action==="items"){
        admissions++;if(rejectAdmission){rejectAdmission=false;return respond({code:"material_changed",message:"素材发生变化，请重新核对",accepted:false},409);}
        const added=body.material_ids.map(id=>({...materials.find(m=>m.material_id===id),id:"item-"+(++counter),status:"pending",position:counter,created_at_utc:new Date().toISOString(),can_view_task:false}));
        rows.push(...added);pool={...pool,version:pool.version+1};const result={pool,items:added};operations.set(body.operation_id,result);
        if(abortAdmission){abortAdmission=false;hideOperation=true;return route.abort("failed");}return respond(result);
      }
      if(action==="remove"){rows.filter(x=>body.item_ids.includes(x.id)).forEach(x=>x.status="removed");pool={...pool,version:pool.version+1};return respond({pool});}
      if(action==="move"){const i=rows.findIndex(x=>x.id===body.item_id),item=rows.splice(i,1)[0];rows.splice(body.direction==="top"?0:body.direction==="up"?Math.max(0,i-1):Math.min(rows.length,i+1),0,item);pool={...pool,version:pool.version+1};return respond({pool});}
    }
    return respond({message:"Unexpected fixture route "+path},404);
  });
  const base=new URL(page.url()).origin;
  await page.goto(base+"/fb-pages.html");await page.locator('[data-pool-page="101"]').waitFor();
  check(await page.locator("#pageRows").innerText().then(t=>t.includes(cfg.created_by_name)),"creator displays trusted original creator name as plain text");
  check(await page.locator("#pageRows td:nth-child(5) b").count()===0,"creator HTML cannot execute");
  await page.locator('[data-pool-page="101"]').click();await page.locator("#poolControls").waitFor();
  await page.waitForFunction(()=>!document.getElementById("poolControls").disabled);
  check(await page.locator("#poolDailyCount").innerText().then(t=>t.includes("每天发布 0 次")),"empty schedule shows zero daily publications");
  check(pool.version===0&&calls.filter(x=>x.method==="POST").length===0,"opening missing pool is read only");
  check(await page.locator("#poolEnable").isDisabled(),"empty daily schedule cannot enable");
  await page.locator("#poolSaveSettings").click();
  check(await page.locator("#poolError").innerText().then(t=>t.includes("至少")),"at least one daily time required");
  await page.locator("#poolTimeInput").fill("09:30");await page.locator("#poolAddTime").click();
  await page.locator("#poolTimeInput").fill("09:30");await page.locator("#poolAddTime").click();
  check(await page.locator("#poolTimes .time-chip").count()===1&&await page.locator("#poolError").innerText().then(t=>t.includes("已存在")),"duplicate daily time rejected without duplicate chips");
  await page.locator("#poolTimeInput").fill("18:00");await page.locator("#poolAddTime").click();
  check(await page.locator("#poolDailyCount").innerText().then(t=>t.includes("每天发布 2 次")),"daily publication count follows unique time chips");
  await page.locator("#poolProcessing").selectOption("random_overlay");
  failSettings=true;await page.locator("#poolSaveSettings").click();
  await page.waitForFunction(()=>document.getElementById("poolError").textContent.includes("版本"));
  check(await page.locator("#poolTimes .time-chip").count()===2&&await page.locator("#poolProcessing").inputValue()==="random_overlay","CAS failure preserves daily times and processing choice");
  pool.version=3;await page.locator("#poolRefresh").click();await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("v3"));
  check(await page.locator("#poolTimes .time-chip").count()===2&&await page.locator("#poolEnable").isDisabled(),"version refresh retains unsaved input and prevents silent enable");
  await page.locator("#poolSaveSettings").click();await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("v4"));
  check(pool.status==="paused"&&pool.times.join(",")==="09:30,18:00"&&!pool.publisher_user_id,"initial settings save stays paused without assigning publisher");
  check(calls.filter(x=>x.path.endsWith("/settings")).at(-1).body.expected_version===3,"settings CAS uses pool version instead of Page template version");
  await page.locator("#poolEnable").click();await page.locator("#poolEnableConfirm").waitFor();
  check(await page.locator("#poolEnableConfirm").innerText().then(t=>t.includes("当前用户")&&t.includes("归属")),"explicit enable explains current publisher ownership");
  check(pool.status==="paused","enable dialog does not mutate plan");
  await page.locator("#poolConfirmEnable").click();await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("已启用"));
  check(pool.status==="enabled"&&pool.publisher_user_id==="current-user","only confirmed enable binds current publisher");
  await page.locator("#poolTimeInput").fill("12:00");await page.locator("#poolAddTime").click();await page.locator("#poolSaveSettings").click();
  await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("v6"));
  check(pool.publisher_user_id==="current-user"&&pool.status==="enabled","normal settings save retains active publisher");
  await page.locator("#poolPickMaterials").click();await page.locator('[data-material="201"]').check();await page.locator("#materialsNext").click();await page.locator('[data-material="203"]').check();
  check(await page.locator("#materialLanguage").isDisabled()&&await page.locator("#materialLanguage").inputValue()==="zh","pool material picker locks Page drama language");
  await page.locator("#materialsPrev").click();await page.locator('[data-material="201"]').waitFor();
  check(await page.locator('[data-material="201"]').isChecked()&&await page.locator("#pickerCount").innerText()==="已选择 2 条素材","pool picker retains selections across pages");
  await page.locator("#applyMaterials").click();await page.locator("#poolReviewStaged").click();await page.locator("#poolAdmission").waitFor();
  check(previews===1&&admissions===0&&rows.length===0,"admission preview creates neither pool items nor tasks");
  check(await page.locator("#poolConfirmAdmission").isDisabled(),"previous successful publish needs explicit admission acknowledgement");
  await page.locator("#poolRepeatAck").check();rejectAdmission=true;await page.locator("#poolConfirmAdmission").click();
  await page.waitForFunction(()=>document.getElementById("poolError").textContent.includes("所选素材已保留"));
  check(await page.locator("#poolStaged").innerText().then(t=>t.includes("2 条"))&&!(await page.locator("#poolPickMaterials").isDisabled()),"definitive rejected admission retains selection and unlocks editor");
  await page.locator("#poolReviewStaged").click();await page.locator("#poolRepeatAck").check();abortAdmission=true;await page.locator("#poolConfirmAdmission").click();
  await page.waitForFunction(()=>document.getElementById("poolPendingText").textContent.includes("尚未查询"));
  check(await page.locator("#poolPickMaterials").isDisabled()&&rows.length===2&&admissions===2,"ambiguous accepted admission locks controls without repeating POST");
  const originalOperation=calls.filter(x=>x.path.endsWith("/items")&&x.method==="POST").at(-1).body.operation_id;
  check(await page.evaluate(op=>Object.values(sessionStorage).some(v=>v.includes(op)),originalOperation),"original pool operation retained before submit");
  rejectReplay=true;await page.locator("#poolRetryOperation").click();
  await page.waitForFunction(()=>document.getElementById("poolError").textContent.includes("本次重试未受理"));
  check(await page.locator("#poolPickMaterials").isDisabled()&&rows.length===2&&admissions===3,"replay accepted:false cannot unlock the earlier ambiguous pool operation");
  check(calls.filter(x=>x.path.endsWith("/items")&&x.method==="POST").at(-1).body.operation_id===originalOperation,"pool retry preserves exact original operation identifier");
  await page.locator("#pagePool [data-close]").first().click();
  await page.waitForFunction(()=>document.querySelector('[data-pool-page="101"]').textContent.includes("待核对"));
  check(await page.locator('[data-pool-page="101"]').innerText().then(t=>t.includes("待核对")),"Page list marks unresolved pool admission");
  hideOperation=false;await page.reload();await page.locator('[data-pool-page="101"]').click();
  await page.waitForFunction(()=>document.getElementById("poolPendingPanel").hidden&&document.querySelectorAll("#poolList .pool-item").length===2);
  check(admissions===3&&rows.length===2&&await page.evaluate(()=>sessionStorage.length===0),"reopening reconciles original admission without duplicate POST");
  repeatRequired=false;await page.locator("#poolPickMaterials").click();await page.locator('[data-material="202"]').check();await page.locator("#applyMaterials").click();await page.locator("#poolReviewStaged").click();await page.locator("#poolConfirmAdmission").click();
  await page.waitForFunction(()=>!document.getElementById("poolNext").disabled);
  check(rows.length===3&&await page.locator("#poolList .pool-item").count()===2,"pool list follows cursor pagination");
  await page.locator("#poolNext").click();await page.locator('[data-pool-item="item-3"]').waitFor();
  await page.locator('[data-pool-move="item-3"][data-direction="top"]').click();
  await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("v9"));
  await page.waitForFunction(()=>document.querySelector("#poolList [data-pool-item]")?.dataset.poolItem==="item-3");
  check(await page.locator("#poolPrev").isDisabled()&&await page.locator("#poolError").isHidden(),"successful mutation resets stale version-bound cursor to first page");
  check(rows[0].id==="item-3","top move uses server order across page boundaries");
  await page.locator('[data-pool-move="item-3"][data-direction="down"]').click();
  await page.waitForFunction(()=>document.querySelector("#poolList [data-pool-item]")?.dataset.poolItem==="item-1");
  check(rows[1].id==="item-3","down move persists server order");
  await page.locator('[data-pool-move="item-3"][data-direction="up"]').click();
  await page.waitForFunction(()=>document.querySelector("#poolList [data-pool-item]")?.dataset.poolItem==="item-3");
  check(rows[0].id==="item-3","up move persists server order");
  await page.locator('[data-pool-item="item-3"]').check();await page.locator('[data-pool-item="item-1"]').check();await page.locator("#poolRemoveSelected").click();
  await page.waitForFunction(()=>document.querySelectorAll("#poolList .pool-item").length===1);
  check(rows.filter(x=>x.status==="removed").length===2,"bulk removal affects selected unbound items only");
  rows.push({...rows[0],id:"private-task",status:"published",can_view_task:false});
  await page.locator('[data-pool-tab="published"]').click();await page.locator('[data-pool-item="private-task"]').waitFor();
  check(await page.locator('#poolList a[href*="records"]').count()===0&&await page.locator('[data-pool-item="private-task"]').isDisabled(),"shared status reveals no private task link or removal controls");
  await page.locator('[data-pool-tab="failed"]').click();await page.waitForFunction(()=>document.querySelectorAll("#poolList .pool-item").length===2);
  check(await page.locator("#poolList").innerText().then(t=>t.includes("已移除")),"exception history retains removed items");
  await page.locator('[data-pool-tab="slots"]').click();await page.waitForFunction(()=>document.getElementById("poolList").textContent.includes("空池不补发"));
  check(await page.locator("#poolList").innerText().then(t=>t.includes("北京时间")),"slot history labels Beijing time and skip reason");
  await page.locator("#poolPause").click();await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("已暂停"));
  check(pool.status==="paused"&&rows.some(x=>x.id==="private-task"),"pause preserves existing tasks and history");
  await page.locator('[data-pool-tab="pending"]').click();
  for(const width of [1440,820,390]){
    await page.setViewportSize({width,height:900});
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${width}: Page management has no outer overflow`);
    check(await page.locator("#pagePool").evaluate(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1;}),`${width}: material pool fits viewport`);
    check(await page.locator("#pagePool .dialog-body").evaluate(el=>el.scrollWidth<=el.clientWidth+1),`${width}: material pool content has no horizontal overflow`);
    await page.screenshot({path:`output/fb-page-pool-ui/pool-${width}.png`});
  }
  check(errors.length===0,"no browser runtime errors");
  return {passed:checks.length,checks,api_requests:calls.length,previews,admissions,errors};
}
