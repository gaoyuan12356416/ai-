async (page) => {
  // Local mocked API only. No production queue or platform writes.
  await page.unroute("**/api/**");await page.unroute("**/navigation.json");
  page.removeAllListeners("pageerror");page.removeAllListeners("dialog");
  await page.evaluate(()=>sessionStorage.clear());
  await page.setViewportSize({width:1440,height:900});
  const checks=[],calls=[],errors=[],operations=new Map();
  const check=(value,name)=>{if(!value)throw new Error(name);checks.push(name);};
  const cfg={page_id:"101",page_name:"ID 入池验收 Page",language:"zh",caption_template:"{name} {url}",
    config_instance_id:"ids-fixture",version:9,eligible_token_count:1,created_by_name:"Fixture"};
  let pool={config_instance_id:cfg.config_instance_id,version:0,status:"paused",times:["09:00"],processing_mode:"original"};
  let rows=[],counter=0,hideOperations=false,abortAdmission=false,rejectAdmission=false,rejectReplay=false,failRead=false,malformedAdmission=false;
  let resolveGate=null,resolveStarted=null;
  const pendingKey="fb-pool-pending:tenant-ids:user-ids:101:ids-fixture";
  const repeatIds=new Set(["301","303","305"]);
  const endpoint="/api/fb-video-publish/pages/101/pool";
  const material=id=>({material_id:id,material_name:id==="301"?"<img src=x onerror=alert(1)>":"素材 "+id,language:"zh",drama_name:"Fixture drama"});
  const addCalls=()=>calls.filter(c=>c.path===endpoint+"/items");
  const resolveCalls=()=>calls.filter(c=>c.path===endpoint+"/items/resolve");
  const previewCalls=()=>calls.filter(c=>c.path===endpoint+"/items/preview");
  const waitReady=()=>page.waitForFunction(()=>!document.getElementById("poolControls").disabled);
  const waitSummary=text=>page.waitForFunction(text=>!document.getElementById("poolControls").disabled&&!document.getElementById("poolAdmissionResult").hidden&&document.getElementById("poolAdmissionResult").textContent.includes(text),text);
  const enter=async text=>{await page.locator("#poolMaterialIds").fill(text);await page.locator("#poolAddMaterials").click();};
  page.on("pageerror",error=>errors.push(error.message));page.on("dialog",dialog=>dialog.accept());
  await page.route("**/navigation.json",route=>route.fulfill({contentType:"application/json",body:"[]"}));
  await page.route("**/api/**",async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method(),body=request.postDataJSON() || {};
    calls.push({path,method,body});
    const reply=(data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify({ok:status<400,...data})});
    if(path==="/api/ui/topbar")return reply({authenticated:true,user:{user_id:"user-ids",tenant_key:"tenant-ids",name:"Fixture",permissions:{fb_video_publish:true}}});
    if(path==="/api/fb-video-publish/languages")return reply({items:[{value:"zh",label:"中文"}]});
    if(path==="/api/fb-video-publish/pages")return reply({items:[cfg]});
    if(path===endpoint&&method==="GET"){
      if(failRead){failRead=false;return reply({message:"读取暂不可用"},503);}
      const state=url.searchParams.get("state") || "pending";
      const found=rows.filter(item=>state==="failed"?item.status==="removed":item.status===state);
      return reply({pool,items:found,counts:{pending:rows.filter(item=>item.status==="pending").length,removed:rows.filter(item=>item.status==="removed").length},next_cursor:""});
    }
    if(path.startsWith(endpoint+"/operations/")){
      const value=operations.get(path.split("/").at(-1));
      return !hideOperations&&value?reply(value):reply({message:"未查询到原操作"},404);
    }
    if(path.startsWith(endpoint+"/")&&method==="POST"){
      const action=path.slice(endpoint.length+1);
      if(action==="items"&&rejectReplay){rejectReplay=false;return reply({accepted:false,message:"本次重试未受理"},409);}
      if(action==="items"&&operations.has(body.operation_id))return reply(operations.get(body.operation_id));
      if(body.config_instance_id!==cfg.config_instance_id||body.expected_version!==pool.version)return reply({accepted:false,message:"版本已改变，请重新核对",code:"pool_version_conflict"},409);
      if(action==="items/resolve"){
        if(resolveGate){resolveStarted();await resolveGate;resolveGate=null;}
        const seen=new Set(),items=[],skipped=[];
        body.material_ids.forEach((id,index)=>{
          let code="",message="";
          if(seen.has(id)){code="duplicate";message="输入重复，保留首次";}
          else if(id.length>128){code="invalid_material_id";message="素材 ID 格式无效";}
          else if(id==="bad"){code="material_not_found";message="素材不存在";}
          else if(id==="lang"){code="language_mismatch";message="素材语言与 Page 不符";}
          else if(id==="busy"){code="active_task";message="已有活动任务占用";}
          else if(rows.some(item=>item.material_id===id&&item.status==="pending")){code="already_in_pool";message="已有待发布项";}
          seen.add(id);
          if(code)skipped.push({row_number:index+1,material_id:id.slice(0,128),code,message});
          else items.push({...material(id),row_number:index+1,repeat_required:repeatIds.has(id)});
        });
        return reply({items,skipped,material_ids:items.map(item=>item.material_id),preview_token:"resolve-"+items.map(item=>item.material_id).join(","),
          repeat_required:items.some(item=>item.repeat_required),summary:{total:body.material_ids.length,valid:items.length,skipped:skipped.length}});
      }
      if(action==="items/preview")return reply({preview_token:"subset-"+body.material_ids.join(","),items:body.material_ids.map(material),repeat_required:false});
      if(action==="items"){
        if(rejectAdmission){rejectAdmission=false;return reply({accepted:false,message:"素材已变化，请重新核对"},409);}
        const added=body.material_ids.map(id=>({...material(id),id:"item-"+(++counter),status:"pending",created_at_utc:"2026-10-20T01:00:00Z"}));
        rows.push(...added);pool={...pool,version:pool.version+1};
        const result={pool,items:added};operations.set(body.operation_id,result);
        if(abortAdmission){abortAdmission=false;hideOperations=true;return route.abort("failed");}
        if(malformedAdmission){malformedAdmission=false;hideOperations=true;return reply({});}
        return reply(result);
      }
      if(action==="settings"){pool={...pool,times:body.times,processing_mode:body.processing_mode,version:pool.version+1};return reply({pool});}
      if(action==="enable"){pool={...pool,status:"enabled",publisher_user_id:"user-ids",publisher_name:"Fixture",version:pool.version+1};return reply({pool});}
      if(action==="pause"){pool={...pool,status:"paused",version:pool.version+1};return reply({pool});}
      if(action==="move"){
        const index=rows.findIndex(item=>item.id===body.item_id),item=rows.splice(index,1)[0];
        rows.splice(body.direction==="top"?0:body.direction==="up"?Math.max(index-1,0):Math.min(index+1,rows.length),0,item);
        pool={...pool,version:pool.version+1};return reply({pool});
      }
      if(action==="remove"){rows.filter(item=>body.item_ids.includes(item.id)).forEach(item=>item.status="removed");pool={...pool,version:pool.version+1};return reply({pool});}
    }
    return reply({message:"Unexpected fixture request "+path},404);
  });
  await page.goto(new URL(page.url()).origin+"/fb-pages.html");
  await page.locator('[data-pool-page="101"]').click();await waitReady();
  check(await page.locator("#poolMaterialIds").count()===1&&await page.locator("#poolPickMaterials").count()===0,"ID textarea replaces material selector");
  check(!calls.some(call=>call.path==="/api/fb-video-publish/materials"),"opening pool does not list material source");
  await enter("  \n\n ");
  check(resolveCalls().length===0&&await page.locator("#poolError").innerText().then(text=>text.includes("请输入")),"blank lines alone do not submit");
  await enter(Array.from({length:201},(_,index)=>String(index)).join("\n"));
  check(resolveCalls().length===0&&await page.locator("#poolError").innerText().then(text=>text.includes("200")),"over 200 input IDs are rejected before API call");

  let releaseResolve;
  resolveGate=new Promise(resolve=>{releaseResolve=resolve;});
  const started=new Promise(resolve=>{resolveStarted=resolve;});
  const raw=" 201 \n\n bad \n202\n201\nlang\nbusy\n203 ";
  await page.locator("#poolMaterialIds").fill(raw);await page.locator("#poolAddMaterials").click();await started;
  await page.locator("#poolAddMaterials").dispatchEvent("click");
  check(resolveCalls().length===1&&await page.locator("#poolMaterialIds").isDisabled(),"in-flight resolver blocks duplicate clicks and input editing");
  releaseResolve();await waitSummary("成功添加 3 条，跳过 4 条");
  check(resolveCalls()[0].body.material_ids.join(",")==="201,bad,202,201,lang,busy,203","resolver receives trimmed nonempty inputs in original order");
  check(addCalls().length===1&&addCalls()[0].body.material_ids.join(",")==="201,202,203","single click adds valid first occurrences in input order");
  check(await page.locator("#poolAdmission").count()===0,"ordinary valid IDs need no second confirmation dialog");
  check(await page.locator("#poolMaterialIds").inputValue()==="bad\nlang\nbusy","only skipped IDs remain for correction");
  check(await page.locator("#poolAdmissionResult").innerText().then(text=>["输入重复","不存在","语言","占用"].every(term=>text.includes(term))),"partial results show specific duplicate existence language and occupancy reasons");
  await page.locator("#poolPendingActions").screenshot({path:"output/playwright/fb-pool-ids-partial-1440.png"});
  await page.setViewportSize({width:390,height:844});
  await page.locator("#poolPendingActions").screenshot({path:"output/playwright/fb-pool-ids-partial-390.png"});
  await page.setViewportSize({width:1440,height:900});
  check(await page.evaluate(key=>sessionStorage.getItem(key)===null,pendingKey),"confirmed admission removes original pending operation");
  const countBeforeInvalid=addCalls().length;
  await enter("201\nbad\nlang\nbusy");await waitSummary("成功添加 0 条，跳过 4 条");
  check(addCalls().length===countBeforeInvalid&&await page.locator("#poolMaterialIds").inputValue()==="201\nbad\nlang\nbusy","all invalid IDs stay editable without enqueueing");
  check(await page.locator("#poolAdmissionResult").innerText().then(text=>text.includes("已有待发布项")),"existing pending material is explicitly skipped");
  const longInvalid="x".repeat(160);
  await enter(" "+longInvalid+" \n501\n"+longInvalid+"\nbad\n502");await waitSummary("成功添加 2 条，跳过 3 条");
  check(await page.locator("#poolMaterialIds").inputValue()===longInvalid+"\nbad","truncated invalid IDs retain full original input once and in order while successful IDs are removed");

  await enter("301\nbad\n302");await page.locator("#poolAdmission").waitFor();
  check(!await page.locator("#poolRepeatAck").isChecked()&&await page.locator("#poolConfirmAdmission").innerText()==="添加 1 条有效素材","history defaults to skipped while other IDs remain addable");
  check(await page.locator("#poolAdmission img").count()===0,"history material metadata cannot execute markup");
  await page.locator("#poolConfirmAdmission").click();await waitSummary("成功添加 1 条，跳过 2 条");
  check(previewCalls().at(-1).body.material_ids.join(",")==="302"&&addCalls().at(-1).body.preview_token==="subset-302","unchecked history revalidates and signs only nonrepeat subset");
  check(addCalls().at(-1).body.allow_repeat===false&&addCalls().at(-1).body.material_ids.join(",")==="302","unchecked history is never submitted accidentally");
  check(await page.locator("#poolMaterialIds").inputValue()==="301\nbad","historical and other skipped inputs retain original relative order");
  const previewsBeforeAllow=previewCalls().length;
  await enter("303\n304");await page.locator("#poolRepeatAck").check();await page.locator("#poolConfirmAdmission").click();await waitSummary("成功添加 2 条，跳过 0 条");
  check(addCalls().at(-1).body.allow_repeat===true&&addCalls().at(-1).body.material_ids.join(",")==="303,304"&&previewCalls().length===previewsBeforeAllow,"explicit history acknowledgement submits full signed ordered set");
  await enter("305");await page.locator("#poolAdmission").waitFor();
  check(await page.locator("#poolConfirmAdmission").isDisabled(),"all historical items cannot submit without acknowledgement");
  await page.locator("#poolAdmission [data-close]").first().click();
  check(await page.locator("#poolMaterialIds").inputValue()==="305","returning from history review preserves input");

  await page.locator("#poolTimeInput").fill("18:00");await page.locator("#poolAddTime").click();await page.locator("#poolProcessing").selectOption("random_overlay");
  failRead=true;await page.locator("#poolRefresh").click();await page.getByText("读取暂不可用",{exact:true}).waitFor();
  check(await page.locator("#poolControls").evaluate(element=>element.disabled)&&await page.locator("#poolMaterialIds").inputValue()==="305","failed pool read disables writes while preserving ID input");
  await page.locator("#poolRefresh").click();await waitReady();
  check(await page.locator("#poolTimes").innerText().then(text=>text.includes("18:00"))&&await page.locator("#poolProcessing").inputValue()==="random_overlay","refresh preserves dirty schedule and processing settings");
  rejectAdmission=true;await enter("401\nbad");await page.waitForFunction(()=>document.getElementById("poolError").textContent.includes("输入已保留"));
  check(await page.locator("#poolMaterialIds").inputValue()==="401\nbad"&&await page.evaluate(key=>sessionStorage.getItem(key)===null,pendingKey),"definitive first rejection preserves input and clears pending operation");
  const beforeStorage=addCalls().length;
  await page.evaluate(()=>{window._poolSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.startsWith("fb-pool-pending:"))throw new Error("fixture storage unavailable");return window._poolSetItem.call(this,key,value);};});
  await enter("402");await page.waitForFunction(()=>document.getElementById("poolError").textContent.includes("无法保存"));
  check(addCalls().length===beforeStorage&&await page.locator("#poolMaterialIds").inputValue()==="402","storage failure prevents untracked admission");
  await page.evaluate(()=>{Storage.prototype.setItem=window._poolSetItem;delete window._poolSetItem;});

  abortAdmission=true;await enter("403\nbad\n404");await page.waitForFunction(()=>document.getElementById("poolPendingText").textContent.includes("尚未查询"));
  const original=addCalls().at(-1).body,persisted=await page.evaluate(key=>JSON.parse(sessionStorage.getItem(key)),pendingKey);
  check(await page.locator("#poolMaterialIds").isDisabled()&&persisted.request.operation_id===original.operation_id,"ambiguous response freezes original operation under tenant and user");
  check(persisted.input_text==="403\nbad\n404"&&persisted.remaining_text==="bad","pending operation stores original and remaining input for recovery");
  const beforeReopen=addCalls().length;
  await page.locator("#pagePool [data-close]").first().click();await page.locator('[data-pool-page="101"]').click();
  await page.waitForFunction(()=>document.getElementById("poolPendingText").textContent.includes("尚未查询"));
  check(await page.locator("#poolMaterialIds").inputValue()==="403\nbad\n404"&&addCalls().length===beforeReopen,"reopening restores pending input and only queries original operation");
  rejectReplay=true;await page.locator("#poolRetryOperation").click();await page.waitForFunction(()=>document.getElementById("poolError").textContent.includes("本次重试未受理"));
  check(JSON.stringify(addCalls().at(-1).body)===JSON.stringify(original),"retry sends exact original operation request");
  check(await page.locator("#poolMaterialIds").isDisabled()&&await page.evaluate(key=>!!sessionStorage.getItem(key),pendingKey),"rejected retry cannot clear an earlier unknown outcome");
  hideOperations=false;await page.locator("#poolQueryOperation").click();await waitSummary("成功添加 2 条，跳过 1 条");
  check(await page.locator("#poolMaterialIds").inputValue()==="bad"&&rows.filter(item=>["403","404"].includes(item.material_id)).length===2,"operation readback preserves skipped input without duplicate admission");

  malformedAdmission=true;await enter("405");await page.waitForFunction(()=>!document.getElementById("poolPendingPanel").hidden&&!document.getElementById("poolQueryOperation").disabled&&document.getElementById("poolPendingText").textContent.includes("尚未查询"));
  check(await page.locator("#poolMaterialIds").isDisabled()&&await page.evaluate(key=>!!sessionStorage.getItem(key),pendingKey),"incomplete successful response remains pending instead of claiming success");
  hideOperations=false;await page.locator("#poolQueryOperation").click();await waitSummary("成功添加 1 条，跳过 0 条");
  await page.locator("#poolTimeInput").fill("18:00");await page.locator("#poolAddTime").click();await page.locator("#poolProcessing").selectOption("random_overlay");
  await page.locator("#poolSaveSettings").click();await page.waitForFunction(()=>document.getElementById("poolSettingsStatus").textContent==="已保存设置");
  check(pool.times.join(",")==="09:00,18:00"&&pool.processing_mode==="random_overlay","pool plan editing remains available after ID admission");
  await page.locator("#poolEnable").click();await page.locator("#poolConfirmEnable").click();await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("已启用"));
  const last=rows.at(-1).id;
  await page.locator(`[data-pool-move="${last}"][data-direction="top"]`).click();await waitReady();
  check(rows[0].id===last,"existing pool ordering still works");
  await page.locator(`[data-pool-item="${last}"]`).check();await page.locator("#poolRemoveSelected").click();await waitReady();
  check(rows.find(item=>item.id===last).status==="removed","existing selected removal still works");
  await page.locator("#poolPause").click();await page.waitForFunction(()=>document.getElementById("poolStatus").textContent.includes("已暂停"));
  check(pool.status==="paused"&&rows.length>0,"pause preserves queue and history");
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:900});
    check(await page.locator("#pagePool").evaluate(element=>{const rect=element.getBoundingClientRect();return rect.left>=0&&rect.right<=innerWidth+1&&rect.top>=0&&rect.bottom<=innerHeight+1;}),`${width}: ID admission dialog fits viewport`);
    check(await page.locator("#pagePool .dialog-body").evaluate(element=>element.scrollWidth<=element.clientWidth+1),`${width}: ID input content has no horizontal overflow`);
  }
  check(errors.length===0,"no browser runtime errors");
  return {passed:checks.length,checks,resolveRequests:resolveCalls().length,subsetPreviews:previewCalls().length,addRequests:addCalls().length};
}
