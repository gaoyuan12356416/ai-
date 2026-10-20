async (page) => {
  // UI contract fixture only. Run in an isolated browser on the local test server.
  await page.unroute("**/api/**");
  page.removeAllListeners("dialog");page.removeAllListeners("pageerror");
  await page.evaluate(()=>sessionStorage.clear());
  const checks = [], calls = [], errors = [];
  const check = (value, name) => { if (!value) throw new Error(name); checks.push(name); };
  const languageRows = [{value:"zh",label:"中文"},{value:"en",label:"英语"},{value:"es",label:"西班牙语"}];
  const sourcePages = [
    {page_id:"101",page_name:"中文 Page 一",eligible_token_count:1,blocked_reason:""},
    {page_id:"102",page_name:"中文 Page 二",eligible_token_count:1,blocked_reason:""},
    {page_id:"103",page_name:"English Page",eligible_token_count:1,blocked_reason:""},
    {page_id:"104",page_name:"异常授权 Page",eligible_token_count:0,blocked_reason:"授权不可用"},
  ];
  const materials = [
    {material_id:"201",material_name:"中文素材 A",language:"zh",drama_name:"中文剧 A",drama_description:"简介 A",duration_seconds:50},
    {material_id:"202",material_name:"中文素材 B",language:"zh",drama_name:"中文剧 B",drama_description:"简介 B",duration_seconds:60},
    {material_id:"203",material_name:"English material",language:"en",drama_name:"English Drama",drama_description:"English description",duration_seconds:70},
    {material_id:"204",material_name:"Spanish material",language:"es",drama_name:"Spanish Drama",drama_description:"Spanish description",duration_seconds:80},
  ];
  let configs=[],draft=null,batches=[],counter=0,preflights=0,submits=0,blockEdit=false,abortSubmit=false,rejectSubmit=false,hideOperationOnce=false,repeatRequired=false,unauthorized=false,failMaterials=false,failDraftOnce=false;
  page.on("pageerror",error=>errors.push(error.message));
  page.on("dialog",d=>d.accept());
  await page.route("**/api/**",async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname.replace("/api/fb-video-publish",""),method=request.method(),body=request.postDataJSON() || {};
    calls.push({method,path,body});
    const respond=(data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify({ok:status<400,...data})});
    if(url.pathname==="/api/ui/topbar")return respond({authenticated:true,user:{is_admin:false,user_id:"ui-owner",tenant_key:"tenant-ui",name:"UI 测试用户",permissions:{fb_video_publish:!unauthorized}}});
    if(path==="/languages")return respond({items:languageRows});
    if(path==="/pages/available")return respond({items:sourcePages.filter(p=>(p.page_name+p.page_id).includes(url.searchParams.get("q") || ""))});
    if(path==="/pages"&&method==="GET")return respond({items:configs});
    if(path==="/pages"&&method==="POST"){
      if(body.page_ids.some(id=>configs.some(p=>p.page_id===id)))return respond({code:"page_config_exists",message:"Page 已配置",accepted:false},409);
      configs.push(...body.page_ids.map(id=>({...sourcePages.find(p=>p.page_id===id),language:body.language,caption_template:body.caption_template,version:1,config_instance_id:`config-${++counter}`,updated_at_utc:new Date().toISOString()})));return respond({items:configs});
    }
    const pm=path.match(/^\/pages\/(\d+)(\/delete)?$/);
    if(pm&&method==="POST"){const item=configs.find(p=>p.page_id===pm[1]);if(blockEdit||!item||item.version!==body.expected_version||item.config_instance_id!==body.config_instance_id)return respond({code:"config_conflict",message:"配置已被其他人修改，请保留当前输入并重新核对",accepted:false},409);if(pm[2])configs=configs.filter(p=>p!==item);else Object.assign(item,{version:item.version+1,language:body.language,caption_template:body.caption_template});return respond({});}
    if(path==="/materials"){
      if(failMaterials)return respond({code:"source_unavailable",message:"素材服务暂不可用"},503);
      const q=url.searchParams.get("q") || "",l=url.searchParams.get("language") || "",offset=Number(url.searchParams.get("cursor") || 0);
      const rows=materials.filter(m=>(m.material_name+m.material_id).includes(q)&&(!l||m.language===l));
      return respond({items:rows.slice(offset,offset+2),next_cursor:offset+2<rows.length?String(offset+2):"",has_more:offset+2<rows.length});
    }
    if(path==="/draft"){if(method==="POST"){if(failDraftOnce){failDraftOnce=false;return respond({code:"draft_unavailable",message:"草稿服务暂不可用"},503);}draft=body.draft;}return respond({draft});}
    if(path==="/preflight"){
      preflights++;return respond({
        preview_token:"signed-preview",expires_at_utc:new Date(Date.now()+900000).toISOString(),
        repeat_required:repeatRequired,repeat_fingerprint:repeatRequired?"repeat":null,
        items:body.pairs.map(p=>{
          const m=materials.find(m=>m.material_id===p.material_id),c=configs.find(c=>c.page_id===p.page_id);
          return {...p,material_name:m.material_name,page_name:c.page_name,
            caption:p.caption_template.replace(/\{name\}/g,m.drama_name).replace(/\{desc\}/g,m.drama_description).replace(/\{url\}/g,"[提交后生成独立短链]"),
            short_url:"[提交后生成独立短链]",schedule_kind:body.material_schedules?.[p.material_id]?"scheduled":"immediate",planned_at_utc:body.material_schedules?.[p.material_id] || "",repeat_required:repeatRequired,
            long_url_parameters:{c:"yingliang_post_CLV_VL_facebook_"+c.page_id+"*[提交时间与编号]",af_adset:c.page_name,af_adset_id:c.page_id,af_ad:m.material_name+"_contentid[fixture]",af_ad_id:m.material_id,af_channel:"fixture-only",af_c_id:"[提交时分配任务编号]",af_dp:"fixture-content"}};
        }),
      });
    }
    if(path==="/batches"&&method==="POST"){
      submits++;if(rejectSubmit){rejectSubmit=false;return respond({code:"preflight_changed",message:"配置已变化，请重新核对",accepted:false},409);}const old=batches.find(b=>b.operation_id===body.operation_id);if(old)return respond({batch_id:old.batch_id,operation_id:old.operation_id,batch:old,idempotent:true});
      const b={batch_id:"a".repeat(31)+String(batches.length+1),operation_id:body.operation_id,owner_user_id:"ui-owner",created_at_utc:new Date().toISOString(),status:"queued",processing_mode:body.processing_mode,items:body.pairs.map((p,i)=>({...p,task_id:"b".repeat(30)+String(i+10),ledger_id:300+i,material_name:materials.find(m=>m.material_id===p.material_id).material_name,page_name:configs.find(c=>c.page_id===p.page_id).page_name,caption:p.caption_template.replace(/\{name\}/g,"冻结剧名").replace(/\{url\}/g,`https://example.invalid/s2l/fb/${300+i}.html`),template_version:p.config_version,page_sequence:i+1,planned_at_utc:body.material_schedules?.[p.material_id] || new Date().toISOString(),short_url:`https://example.invalid/s2l/fb/${300+i}.html`,long_url:"https://www.dramawavew2a.com/ads/101/2310/view?af_ad_id="+p.material_id,status:"queued",can_cancel:true}))};batches.push(b);
      if(abortSubmit){abortSubmit=false;hideOperationOnce=true;return route.abort("failed");}return respond({batch_id:b.batch_id,batch:b,operation_id:b.operation_id});
    }
    if(path.startsWith("/operations/")){if(hideOperationOnce){hideOperationOnce=false;return respond({code:"operation_not_found",message:"未找到"},404);}const b=batches.find(b=>b.operation_id===path.split("/").pop());return b?respond({batch_id:b.batch_id,batch:b,operation_id:b.operation_id}):respond({code:"operation_not_found",message:"未找到"},404);}
    if(path==="/batches"){const offset=Number(url.searchParams.get("cursor") || 0),rows=batches.slice(offset,offset+1).map(b=>({...b,items:undefined,task_count:b.items.length}));return respond({items:rows,next_cursor:offset+1<batches.length?String(offset+1):""});}
    if(path.startsWith("/batches/"))return respond({batch:batches.find(b=>b.batch_id===path.split("/").pop())});
    const tm=path.match(/^\/tasks\/([^/]+)\/cancel$/);if(tm){const t=batches.flatMap(b=>b.items).find(t=>t.task_id===tm[1]);Object.assign(t,{status:"cancelled",can_cancel:false});return respond({task:t});}
    return respond({code:"unexpected_test_route",message:url.pathname},404);
  });
  const base=new URL(page.url()).origin;
  await page.goto(base+"/fb-pages.html");
  await page.locator("#createPage").waitFor();
  check(await page.locator("#pageRows").innerText().then(t=>t.includes("尚未配置")),"initial Page list is empty");
  await page.locator("#createPage").click();
  await page.locator("#savePage").click();
  check(await page.locator("#editorError").innerText().then(t=>t.includes("不能为空")),"empty caption blocked");
  await page.locator("#editCaption").fill("{bad}");
  await page.locator("#savePage").click();
  check(await page.locator("#editorError").innerText().then(t=>t.includes("仅支持")),"unknown macro blocked");
  await page.locator("#editCaption").fill("{name}\n{desc}\n{url}");
  await page.locator("#editLanguage").selectOption("zh");
  await page.locator('#availablePages input[value="101"]').check();
  await page.locator('#availablePages input[value="102"]').check();
  await page.locator('#availablePages input[value="104"]').check();
  await page.locator("#savePage").click();
  await page.locator("#pageEditor").waitFor({state:"detached"});
  check(configs.length===3,"batch create makes one config per Page");
  await page.locator("#createPage").click();
  check(await page.locator('#availablePages input[value="101"]').isDisabled(),"existing Page cannot be re-created");
  await page.locator("#editCaption").fill("{name}\n{url}");
  await page.locator("#editLanguage").selectOption("en");
  await page.locator('#availablePages input[value="103"]').check();
  await page.locator("#savePage").click();
  await page.locator("#pageEditor").waitFor({state:"detached"});
  blockEdit=true;
  await page.locator('[data-edit-page="101"]').click();
  await page.locator("#editCaption").fill("UNSAVED {name} {url}");
  await page.locator("#savePage").click();
  await page.waitForFunction(()=>document.getElementById("editorError").textContent.includes("其他人"));
  check(await page.locator("#editorError").innerText().then(t=>t.includes("其他人")),"CAS conflict visible");
  check(await page.locator("#editCaption").inputValue()==="UNSAVED {name} {url}","CAS conflict preserves input");
  await page.locator("#pageEditor [data-close]").first().click();blockEdit=false;
  await page.reload();await page.locator('[data-edit-page="103"]').waitFor();
  check(await page.locator("#pageRows tr").count()===4,"Page configs survive refresh through API");
  const originalConfigs=[...configs];
  configs.push(...Array.from({length:19},(_,i)=>({...configs[0],page_id:String(800+i),page_name:`分页 Page ${i}`,config_instance_id:`pagination-${i}`})));
  await page.locator("#reloadPages").click();await page.waitForFunction(()=>document.querySelectorAll("#pageRows tr").length===20);
  check(await page.locator("#pageRows tr").count()===20,"Page list uses twenty rows per page");
  await page.locator("#pageNext").click();
  check(await page.locator("#pageRows tr").count()===3,"Page list next page shows remaining rows");
  configs=originalConfigs;
  await page.goto(base+"/fb-video-publish.html");await page.locator("#chooseMaterials").waitFor();
  check(await page.locator("#targetPages input:checked").count()===0,"Pages start unselected");
  check(await page.locator('#targetPages input[data-page="104"]').isDisabled(),"authorization error Page disabled");
  await page.locator("#chooseMaterials").click();await page.locator('[data-material="201"]').check();await page.locator('[data-material="202"]').check();
  await page.locator("#materialsNext").click();await page.locator('[data-material="203"]').check();
  await page.locator("#materialSearch").fill("201");await page.locator("#materialSearchButton").click();await page.locator('[data-material="201"]').waitFor();
  check(await page.locator('[data-material="201"]').isChecked(),"material selection survives pagination and search");
  check(await page.locator("#pickerCount").innerText()==="已选择 3 条素材","cross-page selected count retained");
  await page.locator("#applyMaterials").click();
  await page.locator("#selectPages").click();
  check(await page.locator("#targetPages input:checked").count()===3,"select all covers usable configured Pages only");
  await page.locator("#matchPairs").click();
  check(await page.locator("#pairRows input:checked").count()===5,"language matching makes 2x2 plus 1x1 tasks");
  check(await page.locator("#pairRows input:disabled").count()===4,"cross-language pairing disabled");
  await page.locator('[data-pair-material="203"][data-pair-page="103"]').uncheck();
  await page.locator("#preflight").click();
  check(await page.locator("#pageError").innerText().then(t=>t.includes("尚未分配")),"unmatched material blocks confirmation");
  await page.locator('[data-pair-material="203"][data-pair-page="103"]').check();
  await page.locator("#captionPair").selectOption("201::101");await page.locator("#pairCaption").fill("独立文案 {name} {url}");
  await page.locator("#captionPair").selectOption("202::101");
  check(await page.locator("#pairCaption").inputValue()==="{name}\n{desc}\n{url}","same Page different material has independent caption");
  await page.locator("#captionPair").selectOption("201::102");
  check(await page.locator("#pairCaption").inputValue()==="{name}\n{desc}\n{url}","same material different Page has independent caption");
  await page.locator("#captionPair").selectOption("201::101");await page.locator("#pairCaption").fill("{broken");
  await page.locator("#preflight").click();
  check(await page.locator("#pageError").innerText().then(t=>t.includes("仅支持")),"invalid pair macro blocks preflight");
  await page.locator("#pairCaption").fill("独立文案 {name} {url}");
  check(await page.locator("#intervalMinutes,#scheduleMode,#scheduledAt").count()===0,"global schedule and interval removed");
  await page.locator('[name="processing"][value="random_overlay"]').check();
  await page.locator('[data-material-time="201"]').fill("2020-01-01T10:00");await page.locator("#preflight").click();
  check(await page.locator("#pageError").innerText().then(t=>t.includes("未来")),"past per-material Beijing schedule blocked");
  const future=new Date(Date.now()+86400000+8*3600000).toISOString().slice(0,16);
  await page.locator('[data-material-time="201"]').fill(future);
  await page.locator('[data-material-time="202"]').fill(future);await page.locator('[data-clear-time="202"]').click();
  check(await page.locator('[data-material-time="202"]').inputValue()==="","clearing a material time restores immediate publishing");
  await page.locator('[data-move="202"][data-direction="-1"]').click();
  await page.locator("#saveDraft").click();await page.waitForFunction(()=>document.getElementById("draftStatus").textContent.includes("已保存"));
  check(draft.materials[0].material_id==="202"&&draft.pairs.length===5,"server draft stores order and five independent pairs");
  await page.reload();await page.locator("#chooseMaterials").waitFor();
  check(await page.locator("#pairRows input:checked").count()===5,"draft restores material and Page assignments");
  await page.locator("#captionPair").selectOption("201::101");
  check(await page.locator("#pairCaption").inputValue()==="独立文案 {name} {url}","draft restores edited caption");
  check(await page.locator('[data-material-time="201"]').inputValue()===future,"per-material Beijing time restores without timezone drift");
  check(draft.schema===2 && draft.material_schedules_local["201"]===future && !draft.material_schedules_local["202"],"schema 2 draft stores independent material times");
  check(await page.locator('[name="processing"][value="random_overlay"]').isChecked(),"processing mode survives refresh");
  failMaterials=true;await page.locator("#chooseMaterials").click();await page.locator("#pickerError").waitFor({state:"visible"});
  check(await page.locator("#pickerError").innerText().then(t=>t.includes("暂不可用")),"material load failure is explicit");
  await page.locator("#materialPicker [data-close]").first().click();failMaterials=false;
  check(await page.locator("#selectedMaterials .material-row").count()===3,"failed picker retains selected materials");
  repeatRequired=true;
  for(const width of [1440,820,390]){
    await page.setViewportSize({width,height:900});
    await page.locator("#selectedMaterials").scrollIntoViewIfNeeded();
    await page.screenshot({path:`output/fb-page-pool-ui/material-time-${width}.png`});
    await page.locator("#preflight").click();await page.locator("#publishConfirmation").waitFor();
    await page.locator("#publishConfirmation details summary").first().click();
    check(await page.locator("#publishConfirmation .task-card").count()===5,`${width}: five final previews`);
    check(await page.locator("#confirmSubmit").isDisabled(),`${width}: repeated success requires acknowledgement`);
    check(await page.locator("#publishConfirmation").innerText().then(t=>t.includes("占位")&&t.includes("/ads/101/2310/view")),`${width}: placeholder and fixed 2310 destination`);
    check(await page.locator("#publishConfirmation details").first().innerText().then(t=>t.includes("af_adset_id")&&t.includes("af_channel")&&t.includes("af_c_id")),`${width}: attribution parameters rendered from preflight DTO`);
    check(await page.locator("#publishConfirmation").innerText().then(t=>t.includes("立即发布")&&t.includes("预约")),`${width}: confirmation distinguishes immediate and scheduled pairs`);
    check(submits===0&&batches.length===0,`${width}: confirmation creates no real tasks or links`);
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${width}: no page horizontal overflow`);
    check(await page.locator("#publishConfirmation").evaluate(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1;}),`${width}: confirmation fits viewport`);
    await page.screenshot({path:`output/fb-video-ui/confirm-${width}.png`});
    await page.locator("#publishConfirmation [data-close]").first().click();
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.locator("#preflight").click();await page.locator("#allowRepeat").check();
  check(!(await page.locator("#confirmSubmit").isDisabled()),"acknowledgement enables submit");
  rejectSubmit=true;await page.locator("#confirmSubmit").click();
  await page.waitForFunction(()=>document.getElementById("pageError").textContent.includes("配置已变化"));
  check(!(await page.locator("#chooseMaterials").isDisabled()),"explicit unaccepted rejection unlocks editor");
  check(await page.locator("#pairRows input:checked").count()===5&&batches.length===0,"explicit rejection retains all inputs without creating a batch");
  await page.locator("#preflight").click();await page.locator("#allowRepeat").check();
  abortSubmit=true;await page.locator("#confirmSubmit").click();
  await page.locator("#pendingNotice").waitFor({state:"visible"});
  await page.waitForFunction(()=>document.getElementById("pendingText").textContent.includes("尚未查询到"));
  check(await page.locator("#chooseMaterials").isDisabled() && await page.locator("#pairCaption").isDisabled(),"ambiguous submit locks edits");
  check(submits===2&&batches.length===1,"ambiguous accepted POST creates one batch only after rejected attempt");
  const originalOperation=batches[0].operation_id;
  check(draft.pending.request.operation_id===originalOperation,"operation identity saved before submission");
  hideOperationOnce=true;failDraftOnce=true;await page.locator("#retryOperation").click();
  await page.waitForFunction(()=>document.getElementById("pageError").textContent.includes("原操作仍待核对"));
  check(await page.locator("#chooseMaterials").isDisabled(),"draft failure during recovery keeps ambiguous operation locked");
  check(submits===2&&draft.pending.request.operation_id===originalOperation,"draft failure cannot replace or resubmit ambiguous operation");
  hideOperationOnce=true;rejectSubmit=true;await page.locator("#retryOperation").click();
  await page.waitForURL("**/fb-video-publish-records.html?batch=*");
  check(submits===3&&batches.length===1,"retry rejection reconciles earlier ambiguous original instead of enabling a new batch");
  await page.goto(base+"/fb-video-publish.html");
  draft={schema:1,pending:{request:{operation_id:originalOperation},preview_token:"original",allow_repeat:true}};
  await page.reload();await page.waitForURL("**/fb-video-publish-records.html?batch=*");
  await page.locator("#batchDetail .task-card").first().waitFor();
  check(submits===3&&batches.length===1,"refresh reconciles original operation without another POST");
  check(await page.locator("#batchDetail .task-card").count()===5,"records show every frozen pair");
  check(await page.locator("#batchDetail").innerText().then(t=>t.includes("真实短链")&&t.includes("独立文案")),"record detail shows real short links and frozen copy");
  const submitted=calls.find(c=>c.path==="/batches"&&c.method==="POST").body;
  check(submitted.material_schedules["201"]===new Date(future+"+08:00").toISOString()&&submitted.material_schedules["202"]===null&&submitted.material_schedules["203"]===null,"per-material Beijing schedule converted to UTC and immediate is null");
  check(submitted.schema_version===2&&!("interval_minutes" in submitted)&&!("schedule_mode" in submitted)&&!("scheduled_at_utc" in submitted),"V2 request excludes every obsolete global scheduling field");
  await page.locator("[data-cancel-task]").first().click();await page.waitForFunction(()=>document.getElementById("batchDetail").textContent.includes("已取消"));
  check(batches[0].items.filter(t=>t.status==="cancelled").length===1,"cancel affects only one unsent task");
  batches.push({...batches[0],batch_id:"c".repeat(32),operation_id:"other-operation",items:[]});
  await page.locator("#reloadRecords").click();await page.waitForFunction(()=>!document.getElementById("recordsNext").disabled);
  await page.locator("#recordsNext").click();await page.locator(`[data-batch="${"c".repeat(32)}"]`).first().waitFor();
  check(await page.locator("#recordRows tr").count()===1,"batch records follow server cursor pagination");
  await page.locator("#recordsPrev").click();await page.locator(`[data-batch="${batches[0].batch_id}"]`).first().waitFor();
  check(await page.locator("#recordsPrev").isDisabled(),"batch previous cursor restores first page");
  const frozen=JSON.stringify(batches);
  await page.goto(base+"/fb-pages.html");await page.locator('[data-delete-page="102"]').click();await page.waitForFunction(()=>!document.querySelector('[data-delete-page="102"]'));
  check(configs.length===3,"delete removes saved Page configuration");
  check(JSON.stringify(batches)===frozen,"Page deletion leaves frozen records unchanged");
  draft={schema:1,materials:[materials[0]],page_ids:["101"],pairs:[{material_id:"201",page_id:"101",config_instance_id:configs[0].config_instance_id,config_version:configs[0].version,caption_template:"旧草稿 {name} {url}"}],processing_mode:"original",schedule_mode:"scheduled",scheduled_at_local:future,interval_minutes:30};
  await page.goto(base+"/fb-video-publish.html");await page.locator("#legacyDraftNotice").waitFor({state:"visible"});
  check(await page.locator("#chooseMaterials").isDisabled()&&await page.locator("#preflight").isDisabled(),"legacy draft requires explicit time conversion before editing or publishing");
  await page.locator("#convertDraft").click();
  check(!(await page.locator("#chooseMaterials").isDisabled())&&await page.locator('[data-material-time="201"]').inputValue()==="","legacy conversion clears the obsolete global time");
  await page.locator("#saveDraft").click();await page.waitForFunction(()=>document.getElementById("draftStatus").textContent.includes("已保存"));
  check(draft.schema===2&&draft.materials.length===1&&draft.pairs[0].caption_template==="旧草稿 {name} {url}"&&Object.keys(draft.material_schedules_local).length===0,"explicit conversion preserves materials and captions in V2 draft");
  unauthorized=true;await page.goto(base+"/fb-video-publish.html");await page.locator("#gate").waitFor();
  await page.waitForFunction(()=>document.getElementById("gate").textContent.includes("没有"));
  check(await page.locator("#content").isHidden(),"unassigned new permission blocks UI");
  check(errors.length===0,"no browser runtime errors");
  return {passed:checks.length,checks,preflights,submits,api_requests:calls.length,errors};
}
