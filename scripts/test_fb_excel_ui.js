async (page) => {
  // Isolated browser contract fixture: no real source, Page or Graph writes.
  await page.unroute("**/api/**");
  await page.unroute("**/navigation.json");
  page.removeAllListeners("pageerror");
  await page.evaluate(() => sessionStorage.clear());
  await page.setViewportSize({width:1440,height:1100});
  const checks = [], calls = [], errors = [], operations = new Map();
  const check = (value, name) => { if (!value) throw new Error(name); checks.push(name); };
  const cfg = {page_id:"1000000000000001",page_name:"Page <img src=x>",language:"zh",caption_template:"{name} {url}",config_instance_id:"fixture-config",version:1,eligible_token_count:1};
  let previewCount = 0, submitCount = 0, holdPreview = null, releasePreview, allInvalid = false, expired = false;
  let reject = false, abort = false, hideOperation = false, rejectRetry = false;
  const base = new URL(page.url()).origin;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/navigation.json", route => route.fulfill({contentType:"application/json",body:"[]"}));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/fb-video-publish", ""), method = request.method();
    const body = request.postDataJSON() || {};
    calls.push({path, method, body});
    const reply = (data, status = 200) => route.fulfill({status,contentType:"application/json",body:JSON.stringify({ok:status < 400,...data})});
    if (url.pathname === "/api/ui/topbar") return reply({authenticated:true,user:{tenant_key:"excel-test",user_id:"excel-owner",name:"Excel 测试用户",permissions:{fb_video_publish:true}}});
    if (path === "/pages") return reply({items:[cfg]});
    if (path === "/languages") return reply({items:[{value:"zh",label:"中文"}]});
    if (path === "/excel/preview") {
      previewCount++;
      if (holdPreview) await holdPreview;
      const items = allInvalid ? [] : [
        {row_number:2,page_id:cfg.page_id,material_id:"201",page_name:cfg.page_name,material_name:"素材 <script>bad()</script>",caption:"剧名\n独立短链占位",processing_mode:"original",credential_mode:"operator",title:"标题 <b>文本</b>",planned_at_utc:"2026-11-01T01:00:00Z",schedule_kind:"scheduled",repeat_required:false},
        {row_number:3,page_id:cfg.page_id,material_id:"202",page_name:cfg.page_name,material_name:"素材 202",caption:"第二条",processing_mode:"random_overlay",credential_mode:"page_pool",title:"",planned_at_utc:"2026-11-01T01:00:00Z",schedule_kind:"immediate",repeat_required:true},
      ];
      const skipped = [{row_number:4,page_id:"1000000000000002",material_id:"203",code:"page_not_configured",message:"请先配置该 Page，再重新提交这些行。"}];
      return reply({request:{schema_version:3,operation_id:body.operation_id,rows:items,filename:body.filename},preview_token:"fixture-preview",expires_at_utc:new Date(Date.now() + (expired ? -60000 : 900000)).toISOString(),items,skipped,summary:{total:items.length+1,valid:items.length,skipped:1},repeat_required:items.some(item=>item.repeat_required)});
    }
    if (path === "/excel/batches" && method === "POST") {
      submitCount++;
      if (reject || rejectRetry) return reply({accepted:false,code:"preview_changed",message:"配置已改变"},409);
      const op = body.request.operation_id;
      if (operations.has(op)) return reply(operations.get(op));
      const rows = body.request.rows.filter(row=>body.allow_repeat || !row.repeat_required);
      const batch = {batch_id:"batch-"+submitCount,items:rows.map(row=>({...row,task_id:"task-"+row.row_number,status:"queued"})),excel:{filename:body.request.filename,skipped:[{row_number:4,page_id:"1000000000000002",material_id:"203",message:"请先配置该 Page，再重新提交这些行。"},...body.request.rows.filter(row=>!body.allow_repeat&&row.repeat_required).map(row=>({...row,message:"未确认重复发布，已跳过"}))]}};
      const result = {batch_id:batch.batch_id,operation_id:op,batch};operations.set(op,result);
      if (abort) { abort = false; return route.abort("failed"); }
      return reply(result);
    }
    if (path.startsWith("/excel/operations/")) {
      const found = operations.get(path.split("/").pop());
      return found&&!hideOperation ? reply(found) : reply({message:"未查询到原操作"},404);
    }
    if (path.startsWith("/excel/batches/")) {
      const found = [...operations.values()].find(value=>value.batch_id===path.split("/").pop());
      return found ? reply(found) : reply({message:"批次不存在"},404);
    }
    return reply({message:"Unexpected fixture path "+path},404);
  });
  await page.goto(base + "/fb-pages.html");
  await page.locator("#excelPublishButton:not([disabled])").waitFor();
  await page.evaluate(()=>{const b=document.querySelector("#excelPublishButton");b.click();b.click();});
  check(await page.locator("#excelPublish").count()===1,"double open creates one Excel dialog");
  check(await page.locator('#excelPublish a[download]').getAttribute("href")==="/fb-excel-template.xlsx","template download stays on own static origin");
  const upload = (name="fixture.xlsx", size=8) => page.locator("#excelFile").setInputFiles({name,mimeType:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",buffer:Buffer.alloc(size,65)});
  await upload("bad.xls");await page.locator("#excelPreview").click();
  await page.locator("#excelError:not([hidden])").waitFor();
  check(previewCount===0,"legacy XLS rejected before upload");
  await upload("large.xlsx",2*1024*1024+1);await page.locator("#excelPreview").click();
  check(previewCount===0,"oversize workbook rejected before upload");
  await upload();holdPreview=new Promise(resolve=>releasePreview=resolve);
  await page.evaluate(()=>{const b=document.querySelector("#excelPreview");b.click();b.click();});
  await page.waitForFunction(()=>document.querySelector("#excelUploadFields").disabled);
  check(await page.locator("#excelPreview").isDisabled(),"preview locks file controls");
  releasePreview();holdPreview=null;
  await page.locator("#excelPreviewRows .task-card").first().waitFor();
  check(previewCount===1,"duplicate preview click produces one request");
  check(await page.locator("#excelPreviewRows .task-card").count()===2,"valid rows retain exact source row numbers");
  check(await page.locator("#excelPreviewRows script,#excelPreviewRows img,#excelPreviewRows b").count()===0,"row names and titles cannot execute markup");
  check((await page.locator("#excelPreviewRows").innerText()).includes("当前操作人 Token"),"personal credential choice is visible");
  check((await page.locator("#excelSkipped").innerText()).includes("先配置"),"unconfigured Page reminder is visible without auto creation");
  check((await page.locator("#excelSubmit").innerText()).includes("1 条"),"history rows are excluded until explicit acknowledgement");
  await page.screenshot({path:"output/playwright/fb-excel-preview-1440.png"});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:"output/playwright/fb-excel-preview-390.png"});
  await page.setViewportSize({width:1440,height:1100});
  await page.locator("#excelSubmit").click();await page.locator("#excelResult .task-card").waitFor();
  check(submitCount===1&&await page.locator("#excelResult .task-card").count()===1,"only valid nonrepeat rows are submitted");
  check(calls.find(call=>call.path==="/excel/batches").body.allow_repeat===false,"unchecked history confirmation is sent as false");
  check(!calls.some(call=>call.path==="/pages"&&call.method==="POST"),"import never auto creates Page configuration");
  check((await page.locator("#excelResult").innerText()).includes("已入队不代表发布成功"),"queue acceptance is distinct from platform publication");
  await page.locator("#excelRefreshBatch").click();
  await page.waitForFunction(()=>!document.querySelector("#excelNextBatch").disabled);
  check(calls.some(call=>call.path.startsWith("/excel/batches/")),"result refresh uses Page-management Excel read route");
  await page.locator("#excelNextBatch").click();await upload();await page.locator("#excelPreview").click();
  await page.locator("#excelPreviewRows .task-card").first().waitFor();
  await page.locator("#excelAllowRepeat").check();await page.locator("#excelSubmit").click();
  await page.waitForFunction(()=>document.querySelectorAll("#excelResult .task-card").length===2);
  check(submitCount===2,"explicit repeat acknowledgement includes both rows");
  await page.locator("#excelNextBatch").click();allInvalid=true;await upload();await page.locator("#excelPreview").click();
  await page.locator("#excelPreviewPanel:not([hidden])").waitFor();
  check(await page.locator("#excelSubmit").isDisabled(),"all invalid workbook cannot submit");
  allInvalid=false;expired=true;await upload("expired.xlsx");await page.locator("#excelPreview").click();
  await page.locator("#excelPreviewRows .task-card").first().waitFor();await page.locator("#excelSubmit").click();
  check(submitCount===2,"expired preview creates no request");
  expired=false;reject=true;await page.locator("#excelPreview").click();await page.locator("#excelPreviewRows .task-card").first().waitFor();
  await page.locator("#excelSubmit").click();await page.locator("#excelError:not([hidden])").waitFor();
  check(await page.locator("#excelPendingPanel").isHidden(),"definitive first-attempt rejection releases pending state");
  check(await page.locator("#excelFile").evaluate(el=>el.files.length)===1,"definitive rejection retains uploaded file");
  reject=false;await page.locator("#excelPreview").click();await page.locator("#excelPreviewRows .task-card").first().waitFor();
  await page.evaluate(()=>{window.fixtureSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(){throw new Error("quota");};});
  const beforeStorageFailure=submitCount;
  await page.locator("#excelSubmit").click();await page.locator("#excelError:not([hidden])").waitFor();
  check(submitCount===beforeStorageFailure,"storage failure prevents untracked submission");
  await page.evaluate(()=>{Storage.prototype.setItem=window.fixtureSetItem;delete window.fixtureSetItem;});
  abort=true;hideOperation=true;
  await page.locator("#excelSubmit").click();await page.locator("#excelPendingPanel:not([hidden])").waitFor();
  await page.waitForFunction(()=>!document.querySelector("#excelQueryOperation").disabled);
  const frozen=await page.evaluate(()=>JSON.parse(sessionStorage.getItem("fb-excel-pending:excel-test:excel-owner")));
  check(!!frozen?.request?.operation_id,"ambiguous submission persists exact operation under tenant and user");
  check(await page.locator("#excelFile").isDisabled(),"ambiguous submission blocks replacement workbook");
  await page.locator("#excelPublish .dialog-foot [data-close]").click();await page.locator("#excelPublishButton").click();
  await page.waitForFunction(()=>!document.querySelector("#excelQueryOperation").disabled);
  check(await page.locator("#excelPendingPanel").isVisible(),"reopening restores pending operation");
  rejectRetry=true;await page.locator("#excelRetryOperation").click();
  await page.waitForFunction(()=>!document.querySelector("#excelQueryOperation").disabled);
  check(await page.locator("#excelPendingPanel").isVisible(),"definitive retry rejection cannot erase earlier unknown outcome");
  const retries=calls.filter(call=>call.path==="/excel/batches");
  check(JSON.stringify(retries.at(-1).body)===JSON.stringify(frozen),"retry sends identical frozen operation and preview");
  rejectRetry=false;hideOperation=false;await page.locator("#excelQueryOperation").click();
  await page.locator("#excelResult .task-card").waitFor();
  check(await page.evaluate(()=>sessionStorage.getItem("fb-excel-pending:excel-test:excel-owner"))===null,"accepted readback clears pending state");
  await page.setViewportSize({width:390,height:844});
  check(await page.locator("#excelPublish").evaluate(el=>el.getBoundingClientRect().right<=innerWidth&&el.getBoundingClientRect().left>=0),"Excel dialog fits mobile viewport");
  check(errors.length===0,"no unhandled browser errors");
  return {passed:checks.length,checks,previewRequests:previewCount,submitRequests:submitCount};
}
