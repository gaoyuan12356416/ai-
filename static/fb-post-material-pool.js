(() => {
  "use strict";
  const BASE = "/api/fb-video-publish", POOL = BASE + "/material-pool";
  const $ = id => document.getElementById(id);
  const state = {auth:{},pool:null,groups:[],languages:new Map(),selectedGroups:new Set(),selectedItems:new Set(),items:[],dirty:false,ready:false,featureEnabled:true,recordAccess:false,writing:false,reading:false,pending:null,itemCursor:"",itemStack:[],itemNext:"",runCursor:"",runStack:[],runNext:"",itemGeneration:0,runGeneration:0,previewGeneration:0,hasPreview:false};
  const labels = {pending:"待发布",bound:"已绑定任务",queued:"待处理",preparing:"加工中",processing:"处理中",ready:"待发布",running:"发布中",publishing:"发布中",submitted:"平台处理中",published:"已发布",completed:"已完成",partial:"部分完成",partial_failed:"部分失败",failed:"失败",cancelled:"已取消",unknown:"结果待核对",unknown_outcome:"结果待核对",removed:"已移除",blocked:"已阻止",skipped:"已跳过",frozen:"已安排",created:"已安排",empty:"无可发布素材",enabled:"已启用",paused:"已暂停"};
  const reasons = {groups_changed:"Page 组已变更，请重新启用计划。",operator_paused:"已手动暂停；已生成的任务继续保留。",no_eligible_pairs:"本批没有可用的语言配对。",configuration_changed:"发布设置已变化，本时点不再执行。",slot_expired:"已错过计划时点，本批不补发。",group_not_found:"部分 Page 组不存在或发布人已无访问权限，请重新选择后启用。",group_access_denied:"Page 组访问权限已失效，请重新核对后启用。",identity_inactive:"发布人身份已失效，请由有权限的账号重新启用。",identity_attribution_invalid:"发布人归因身份不完整，请联系管理员核对。",identity_unavailable:"暂时无法复核发布人身份，请稍后重试。",invalid_actor:"发布人身份无效，请重新登录后核对。",fb_auto_owner_mapping_missing:"当前账号未关联到唯一的 Page 组负责人，请联系管理员核对。",group_source_unavailable:"Page 组或素材查询暂不可用，请稍后重试。",group_pool_source_unavailable:"Page 组或素材查询暂不可用，请稍后重试。",group_source_invalid:"Page 组数据发生变化，请刷新后核对。",group_pool_source_too_large:"所选范围过大，请减少 Page 组后重试。",source_scan_budget_exceeded:"本轮素材检查已到时间上限，其余素材继续留池。",language_material_unavailable:"没有可用的同语言素材。",language_page_unavailable:"所选组没有同语言可用 Page，素材继续留池。",daily_capacity_exceeded:"本日发布额度不足。",material_previously_bound:"素材已有 FB 任务历史，不能自动重复发布。",material_identity_changed:"入池后素材身份或语言改变，请移除后重新导入。",fb_video_page_unknown:"Page 存在结果待核对的任务，请先完成核对。",permission_denied:"发布权限已失效，请联系管理员核对。",group_pool_gate_closed:"组素材池维护中，暂时无法修改。"};
  const reasonText=(value,fallback="当前条件不满足，请查看配对与缺口详情。")=>{const text=String(value||"");return reasons[text] || (/^[a-z][a-z0-9_:-]*$/i.test(text)?fallback:text);};
  const language = value => state.languages.get(value) || value || "未设置语言";
  const bj = value => { if (!value) return "—"; const date=new Date(value); return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(date) : "—"; };
  const node = (tag, text, className) => { const el=document.createElement(tag); if(text!==undefined)el.textContent=String(text??""); if(className)el.className=className; return el; };
  const errorAt = (id,error) => { $(id).textContent=error?.message || String(error || ""); $(id).hidden=!$(id).textContent; };
  let toastTimer;
  function toast(message){ $("toast").textContent=message;$("toast").hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("toast").hidden=true,5000); }
  class ApiError extends Error { constructor(status,data){super(data.message || data.error || `请求失败（${status}）`);this.status=status;this.data=data;} }
  async function api(path,options={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),45000);
    try{
      const response=await fetch(path,{credentials:"same-origin",cache:"no-store",...options,signal:controller.signal,headers:{...(options.body?{"Content-Type":"application/json"}:{}),...(options.headers||{})}});
      let data;try{data=await response.json();}catch(_){throw new Error("服务返回内容无效，请保留输入并查询操作结果。");}
      if(!response.ok || data.ok===false)throw new ApiError(response.status,data);
      if(!data || typeof data!=="object")throw new Error("服务返回内容无效。");
      return data;
    }finally{clearTimeout(timer);}
  }
  const post=(path,data)=>api(path,{method:"POST",body:JSON.stringify(data)});
  async function loadRecordAccess(){
    // Use the actual server configuration, never QuickNav's cached/default
    // additions: a pool-only permission must not produce inaccessible links.
    try{
      const config=await api("/navigation.json"),user=state.auth.user||{};
      const visible=item=>item&&item.enabled!==false&&(!item.adminOnly||user.is_admin)&&(!item.module||user.is_admin||user.permissions?.[item.module]);
      return Array.isArray(config)&&config.some(group=>visible(group)&&Array.isArray(group.items)&&group.items.some(item=>visible(item)&&["fbVideoPublishRecords","fbVideoPublish"].includes(item.key)&&(item.module==="fb_video_publish"||group.module==="fb_video_publish")));
    }catch(_){return false;}
  }
  const storageKey=()=>`fb-group-pool-pending:${state.auth.user?.tenant_key || ""}:${state.auth.user?.user_id || ""}`;
  function storePending(operation){
    // Persist before sending. A rejected storage write must never send a mutation.
    if(operation)sessionStorage.setItem(storageKey(),JSON.stringify(operation));else sessionStorage.removeItem(storageKey());
    state.pending=operation;paintLocks();
  }
  function paintLocks(){
    const locked=!state.ready||!state.featureEnabled||state.writing||state.reading||!!state.pending;
    $("settingsControls").disabled=locked;$("admissionControls").disabled=locked;
    $("enableSchedule").disabled=locked||state.dirty||state.pool?.status==="enabled"||!state.selectedGroups.size;
    $("pauseSchedule").disabled=locked||state.pool?.status!=="enabled";
    $("removeItems").disabled=locked||!state.selectedItems.size;
    $("previewPairs").disabled=!state.ready||state.writing||state.reading||!state.selectedGroups.size;
    $("queryOperation").disabled=state.writing||state.reading;$("retryOperation").disabled=!state.ready||!state.featureEnabled||state.writing||state.reading;
    $("refreshData").disabled=state.writing||state.reading;$("pendingPanel").hidden=!state.pending;
    if(state.pending)$("pendingText").textContent=`“${state.pending.label}”的结果尚未确认。已保存原操作，可查询结果或原样重试。`;
    $("selectedCount").textContent=state.selectedItems.size?`已选择 ${state.selectedItems.size} 条`:"";
    $("materialList").querySelectorAll("input").forEach(el=>el.disabled=locked||el.dataset.modifiable!=="yes");
    $("itemsPrev").disabled=state.reading||!state.itemStack.length;$("itemsNext").disabled=state.reading||!state.itemNext;
    $("runsPrev").disabled=state.reading||!state.runStack.length;$("runsNext").disabled=state.reading||!state.runNext;
    $("dirtyHint").hidden=!state.dirty;
    $("settingsHint").textContent=state.dirty?"请先保存修改，再启用计划。":"启用后从未来计划时点执行；暂停不撤销已生成的任务。";
  }
  function invalidatePreview(){++state.previewGeneration;$("previewStale").hidden=!state.hasPreview;}
  function markDirty(){state.dirty=true;invalidatePreview();paintLocks();}
  function modeChanged(){const random=$("scheduleMode").value==="random";$("randomSettings").hidden=!random;$("fixedSettings").hidden=random;}
  function paintGroups(){
    const search=$("groupSearch").value.trim().toLowerCase(),fragment=document.createDocumentFragment();
    const missing=[...state.selectedGroups].filter(id=>!state.groups.some(g=>String(g.id)===id));
    const visible=[...state.groups,...missing.map(id=>({id,name:"当前不可访问的组（取消选择后保存）"}))].filter(g=>(String(g.name||"")+String(g.id)).toLowerCase().includes(search));
    for(const group of visible){const label=node("label",undefined,"group-choice"),input=node("input"),body=node("span");input.type="checkbox";input.value=String(group.id);input.checked=state.selectedGroups.has(String(group.id));input.dataset.group=String(group.id);body.append(node("strong",group.name || group.id),node("small",`组 ${group.id}${Number.isFinite(group.total_pages)?` · ${group.total_pages} 个 Page`:""}`));label.append(input,body);fragment.append(label);}
    if(!visible.length)fragment.append(node("div",search?"没有匹配的 Page 组":"当前账号没有可用的 Page 组","empty"));
    $("groupChoices").replaceChildren(fragment);
    $("groupSummary").textContent=`已选 ${state.selectedGroups.size} 个组${missing.length?`；其中 ${missing.length} 个组当前不可访问，请重新选择后保存。`:"；组内重复 Page 自动去重。"}`;
  }
  function applyPool(pool,replaceDraft){
    if(!pool || !Number.isInteger(pool.version))throw new Error("未能读取发布设置，请刷新后再操作。");
    state.pool=pool;
    $("scheduleState").textContent=pool.status==="enabled"?"计划已启用":"计划已暂停";$("scheduleState").classList.toggle("paused",pool.status!=="enabled");
    $("scheduleOwner").textContent=pool.publisher_name?`发布人：${pool.publisher_name}`:"尚未启用发布";
    $("scheduleEffective").textContent=pool.pause_reason?reasonText(pool.pause_reason,"计划已暂停，请核对发布人和 Page 组后重新启用。"):pool.effective_at_utc?`生效时间：${bj(pool.effective_at_utc)}（北京）`:"保存设置后启用，按计划自动分配。";
    if(replaceDraft){state.selectedGroups=new Set((pool.group_ids||[]).map(String));$("scheduleMode").value=pool.schedule_mode || "fixed";$("fixedTimes").value=(pool.times||[]).join(", ");$("dailyBatches").value=pool.daily_batches || 1;$("captionTemplate").value=pool.caption_template ?? "{name}\n{desc}\n{url}";state.dirty=false;modeChanged();paintGroups();}
    paintLocks();
  }
  function paintStats(counts){
    const active=["queued","preparing","ready","running","publishing","submitted"].reduce((n,k)=>n+Number(counts[k]||0),0);
    const problem=["failed","cancelled","unknown","unknown_outcome","blocked"].reduce((n,k)=>n+Number(counts[k]||0),0);
    $("poolStats").replaceChildren(...[[Number(counts.pending||0),"待发布素材"],[active,"处理中"],[Number(counts.published||0),"已发布"],[problem,"需关注"]].map(([count,label])=>{const card=node("div",undefined,"pool-stat");card.append(node("strong",count),node("span",label));return card;}));
  }
  function safeRecordLink(batchId,text="查看发布记录"){const a=node("a",text);a.href="/fb-video-publish-records.html?batch="+encodeURIComponent(batchId);return a;}
  function paintItems(){
    const fragment=document.createDocumentFragment();
    for(const item of state.items){const card=node("article",undefined,"group-material"),input=node("input"),info=node("div",undefined,"material-info"),title=node("div",undefined,"material-title");input.type="checkbox";input.dataset.item=String(item.id);input.dataset.modifiable=item.can_modify?"yes":"no";input.setAttribute("aria-label",`选择素材 ${item.material_id}`);input.checked=state.selectedItems.has(String(item.id));
      title.append(node("strong",item.material_name||item.material_id),node("span",labels[item.status]||item.status,"badge"));info.append(title,node("p",`${item.material_id} · ${language(item.language)} · ${item.drama_name || "—"}`),node("p",`入池 ${bj(item.created_at_utc)}（北京）`));
      if(item.error_message)info.append(node("p",item.error_message,"material-error"));if(item.task_id){const p=node("p",`任务 ${item.task_id} `);if(state.recordAccess&&item.can_view_task&&item.batch_id)p.append(safeRecordLink(item.batch_id));info.append(p);}card.append(input,info);fragment.append(card);
    }
    if(!state.items.length)fragment.append(node("div","当前筛选下没有素材","empty"));$("materialList").replaceChildren(fragment);paintLocks();
  }
  async function loadItems(){
    const generation=++state.itemGeneration,query=new URLSearchParams({state:$("itemState").value,language:$("itemLanguage").value,limit:"50",cursor:state.itemCursor});
    const result=await api(POOL+"?"+query);if(generation!==state.itemGeneration)return;
    state.items=result.items||[];state.itemNext=result.next_cursor||"";state.selectedItems.clear();paintStats(result.counts||{});paintItems();
  }
  function exclusions(rows){const list=node("ul",undefined,"excluded-list");for(const row of rows||[])list.append(node("li",`${row.page_id?`Page ${row.page_id}：`:row.material_id?`素材 ${row.material_id}：`:""}${reasonText(row.message||row.reason||row.code||"暂不可用")}`));return list;}
  function pairNode(pair){const card=node("article",undefined,"preview-pair");card.append(node("strong",`${pair.material_name||pair.material_id} → ${pair.page_name||pair.page_id}`),node("p",`${language(pair.language)} · 素材 ${pair.material_id} · Page ${pair.page_id}`,"helper"));if(pair.caption){const detail=node("details");detail.append(node("summary","查看文案"),node("pre",pair.caption));card.append(detail);}return card;}
  async function preview(){
    errorAt("previewError","");const generation=++state.previewGeneration;$("previewPairs").disabled=true;
    try{const result=await post(POOL+"/preview",{group_ids:[...state.selectedGroups],caption_template:$("captionTemplate").value});if(generation!==state.previewGeneration)return;
      const fragment=document.createDocumentFragment(),pairs=result.pairs||[],counts=new Map();for(const pair of pairs)counts.set(pair.language,(counts.get(pair.language)||0)+1);
      fragment.append(node("p",`本批可配对 ${pairs.length} 条${Number.isFinite(result.quota_remaining)?` · 剩余额度 ${result.quota_remaining} 条`:""}`,"helper"));
      const capacities=node("div",undefined,"capacity-list");if(Array.isArray(result.language_capacity)){for(const entry of result.language_capacity)capacities.append(node("span",`${language(entry.language)}：可用 Page ${entry.page_count} · 池内待发 ${entry.material_count} · 配对 ${entry.matched_count}`,"capacity"));}else{for(const [lang,count] of counts)capacities.append(node("span",`${language(lang)}：${count} 个 Page / ${count} 条素材`,"capacity"));}fragment.append(capacities);
      const list=node("div",undefined,"pair-list");for(const pair of pairs)list.append(pairNode(pair));fragment.append(list);
      if(!pairs.length)fragment.append(node("p","当前没有可分配的素材，请查看下方原因或添加相应语言的素材。","preview-empty"));
      if(result.excluded?.length){const notice=node("div",undefined,"notice warning");notice.append(node("strong",`未参与配对 ${result.excluded.length} 项`),exclusions(result.excluded));fragment.append(notice);}
      $("previewResult").className="";$("previewResult").replaceChildren(fragment);state.hasPreview=true;$("previewStale").hidden=true;
    }catch(error){if(generation===state.previewGeneration)errorAt("previewError",error);}finally{paintLocks();}
  }
  async function loadRuns(){
    const generation=++state.runGeneration,result=await api(POOL+"/runs?"+new URLSearchParams({limit:"20",cursor:state.runCursor}));if(generation!==state.runGeneration)return;state.runNext=result.next_cursor||"";
    const fragment=document.createDocumentFragment();for(const run of result.items||[]){const card=node("article",undefined,"run-card"),head=node("div",undefined,"row spread");head.append(node("strong",`${bj(run.slot_at_utc)}（北京）`),node("span",labels[run.status]||run.status,"badge"),node("span",`批次 ${run.id}`,"mono"));card.append(head);
      const counts=Object.entries(run.task_counts||{}).map(([k,v])=>`${labels[k]||k} ${v}`).join(" · ");if(counts)card.append(node("p",counts,"task-counts"));if(run.reason)card.append(node("p",reasonText(run.reason),"helper"));
      if(run.pairs?.length||run.excluded?.length){const details=node("details");details.append(node("summary",`查看 ${run.pairs?.length||0} 条冻结配对与缺口`));for(const pair of run.pairs||[])details.append(node("p",`${pair.material_id} → ${pair.page_name||pair.page_id} · ${language(pair.language)}`,"run-pair"));if(run.excluded?.length)details.append(exclusions(run.excluded));card.append(details);}
      if(state.recordAccess&&run.can_view_tasks&&run.batch_id)card.append(safeRecordLink(run.batch_id));fragment.append(card);
    }$("runList").replaceChildren(fragment);paintLocks();
  }
  async function refresh(){
    if(state.reading)return;state.reading=true;state.ready=false;paintLocks();errorAt("pageError","");
    try{
      const [schedule,groups,recordAccess]=await Promise.all([api(POOL+"/schedule"),api(BASE+"/page-groups"),loadRecordAccess()]);state.recordAccess=recordAccess;state.featureEnabled=schedule.feature_enabled!==false;$("featureNotice").hidden=state.featureEnabled;state.groups=(groups.items||[]).map(g=>({...g,id:String(g.id??g.group_id)}));applyPool(schedule.pool,!state.dirty);paintGroups();
      $("dayPlan").textContent=(schedule.day_plan||[]).map(day=>`${day.bj_date}：${(day.times||[]).join("、")}（北京）`).join("\n");
      await Promise.all([loadItems(),loadRuns()]);state.ready=true;
    }catch(error){errorAt("pageError",error);}finally{state.reading=false;paintLocks();}
  }
  function materialIds(){const values=$("materialIds").value.trim().split(/[\s,，;；]+/).filter(Boolean);if(!values.length)throw new Error("请先填写素材 ID。");if(values.length>200)throw new Error("每次最多添加 200 条素材 ID，请分批操作。");if(values.some(id=>!/^\d+$/.test(id)))throw new Error("素材 ID 应为数字，请检查输入。");return [...new Set(values)];}
  function showAdmission(items,errors,verb){const result=$("admissionResult");result.hidden=false;result.replaceChildren(node("strong",`${verb} ${items.length} 条，未通过 ${errors.length} 条`));for(const item of items)result.append(node("div",`${item.material_id} · ${item.material_name||item.drama_name||"素材"} · ${language(item.language)}`,"admission-row"));for(const error of errors)result.append(node("div",`${error.material_id || "—"} · ${error.message||error.reason||error.code||"无法加入"}`,"admission-row"));}
  function validateMutation(result,operation){
    if(result.operation_id!==operation.payload.operation_id)throw new Error("操作返回标识不一致，请查询原操作。");
    if(!result.pool || !Number.isInteger(result.pool.version))throw new Error("操作结果未完整返回，请查询原操作。");
    if(operation.path==="/items"&&(!Array.isArray(result.added)||!Array.isArray(result.duplicates)||!Array.isArray(result.errors)))throw new Error("入池结果未完整返回，请查询原操作。");
  }
  async function finishOperation(result,operation){
    validateMutation(result,operation);
    if(operation.path==="/items"){
      const accepted=new Set([...result.added.map(item=>String(item.material_id)),...result.duplicates.map(String)]),failed=(operation.input_ids||operation.payload.material_ids).filter(id=>!accepted.has(String(id)));
      const added=result.added.map(row=>({...((operation.resolved_items||[]).find(item=>String(item.material_id)===String(row.material_id))||{}),...row}));
      $("materialIds").value=failed.join("\n");showAdmission(added,[...(operation.resolve_errors||[]),...result.errors],"入池成功");
      if(result.duplicates.length)$("admissionResult").append(node("p",`已在池内，跳过 ${result.duplicates.length} 条：${result.duplicates.join("、")}`));
    }
    applyPool(result.pool,operation.path==="/schedule");storePending(null);invalidatePreview();state.itemCursor="";state.itemStack=[];state.selectedItems.clear();toast(`${operation.label}成功`);await refresh();
  }
  async function submitOperation(operation,isReplay=false){
    if(state.writing)return;state.writing=true;paintLocks();errorAt("pageError","");
    try{const result=await post(POOL+operation.path,operation.payload);await finishOperation(result,operation);}
    catch(error){
      // A new rejected request has no uncertain predecessor. Replayed requests
      // retain their original hold even when the replay itself is rejected.
      if(!isReplay && error instanceof ApiError && error.status>=400 && error.status<500 && ![408,429].includes(error.status)){storePending(null);if(error.status===409)await refresh();}
      errorAt("pageError",state.pending?new Error(`${error.message} 请查询原操作结果；当前输入已保留。`):error);
    }finally{state.writing=false;paintLocks();}
  }
  async function mutate(path,payload,label,extra={}){
    if(!state.ready||!state.featureEnabled||state.reading||state.writing||state.pending)return;
    const operation={path,label,payload:{...payload,expected_version:state.pool.version,operation_id:crypto.randomUUID()},...extra};storePending(operation);await submitOperation(operation);
  }
  async function resolveMaterials(add){
    if(!state.ready||state.writing||state.pending)return;errorAt("pageError","");let ids;
    try{ids=materialIds();}catch(error){errorAt("pageError",error);return;}
    state.writing=true;paintLocks();let result;
    try{result=await post(POOL+"/items/resolve",{material_ids:ids});if(!Array.isArray(result.items)||!Array.isArray(result.errors))throw new Error("素材校验结果不完整，请重试。");showAdmission(result.items,result.errors,"校验通过");}
    catch(error){errorAt("pageError",error);return;}
    finally{state.writing=false;paintLocks();}
    if(add&&result.items.length)await mutate("/items",{material_ids:result.items.map(item=>String(item.material_id))},"添加入池",{input_ids:ids,resolved_items:result.items,resolve_errors:result.errors});
  }
  function settings(){
    const times=[...new Set($("fixedTimes").value.split(/[\s,，;；]+/).filter(Boolean))].sort(),mode=$("scheduleMode").value,daily=Number($("dailyBatches").value);
    if(!state.selectedGroups.size)throw new Error("请至少选择一个 Page 组。");
    if([...state.selectedGroups].some(id=>!state.groups.some(g=>String(g.id)===id)))throw new Error("所选组中有当前不可访问的 Page 组，请重新选择。");
    if(mode==="fixed"&&(!times.length||times.some(t=>!/^([01]\d|2[0-3]):[0-5]\d$/.test(t))))throw new Error("请填写有效时点，例如 09:00, 18:00。");
    if(!Number.isInteger(daily)||daily<1||daily>24)throw new Error("每日随机批次数必须是 1–24 的整数。");
    return {group_ids:[...state.selectedGroups],schedule_mode:mode,times,daily_batches:daily,caption_template:$("captionTemplate").value,processing_mode:"original"};
  }
  async function queryOperation(){
    if(!state.pending||state.writing)return;const operation=state.pending;state.writing=true;paintLocks();errorAt("pageError","");
    try{await finishOperation(await api(POOL+"/operations/"+encodeURIComponent(operation.payload.operation_id)),operation);}
    catch(error){errorAt("pageError",error.status===404?new Error("暂未查到原操作结果，输入和操作记录已保留；可稍后查询或原样重试。"):error);}finally{state.writing=false;paintLocks();}
  }
  function restorePendingInputs(){
    if(!state.pending)return;const operation=state.pending;
    if(operation.path==="/items")$("materialIds").value=(operation.input_ids||operation.payload.material_ids||[]).join("\n");
    if(operation.path==="/schedule"){
      const draft=operation.payload;state.selectedGroups=new Set((draft.group_ids||[]).map(String));$("scheduleMode").value=draft.schedule_mode;$("fixedTimes").value=(draft.times||[]).join(", ");$("dailyBatches").value=draft.daily_batches;$("captionTemplate").value=draft.caption_template;state.dirty=true;modeChanged();paintGroups();paintLocks();
    }
  }
  function run(fn){return async()=>{try{await fn();}catch(error){errorAt("pageError",error);}};}
  function bind(){
    $("showNav").onclick=()=>$("sidebar").classList.add("open");$("hideNav").onclick=()=>$("sidebar").classList.remove("open");$("refreshData").onclick=run(refresh);
    $("groupSearch").oninput=paintGroups;$("groupChoices").onchange=e=>{const id=e.target.dataset.group;if(!id)return;e.target.checked?state.selectedGroups.add(id):state.selectedGroups.delete(id);paintGroups();markDirty();};
    $("scheduleMode").onchange=()=>{modeChanged();markDirty();};["fixedTimes","dailyBatches","captionTemplate"].forEach(id=>$(id).oninput=markDirty);
    $("saveSchedule").onclick=run(()=>mutate("/schedule",settings(),"保存设置"));$("enableSchedule").onclick=run(()=>mutate("/enable",{},"启用计划"));$("pauseSchedule").onclick=run(()=>mutate("/pause",{},"暂停计划"));
    $("previewPairs").onclick=preview;$("resolveMaterials").onclick=()=>resolveMaterials(false);$("addMaterials").onclick=()=>resolveMaterials(true);
    $("queryOperation").onclick=queryOperation;$("retryOperation").onclick=run(()=>state.pending&&submitOperation(state.pending,true));
    $("materialList").onchange=e=>{const id=e.target.dataset.item;if(!id)return;e.target.checked?state.selectedItems.add(id):state.selectedItems.delete(id);paintLocks();};
    $("removeItems").onclick=run(()=>mutate("/items/remove",{item_ids:[...state.selectedItems]},"移除素材"));
    ["itemState","itemLanguage"].forEach(id=>$(id).onchange=run(async()=>{state.itemCursor="";state.itemStack=[];await loadItems();}));
    $("itemsNext").onclick=run(async()=>{state.itemStack.push(state.itemCursor);state.itemCursor=state.itemNext;await loadItems();});$("itemsPrev").onclick=run(async()=>{state.itemCursor=state.itemStack.pop()||"";await loadItems();});
    $("runsNext").onclick=run(async()=>{state.runStack.push(state.runCursor);state.runCursor=state.runNext;await loadRuns();});$("runsPrev").onclick=run(async()=>{state.runCursor=state.runStack.pop()||"";await loadRuns();});$("refreshRuns").onclick=run(loadRuns);
    window.addEventListener("beforeunload",e=>{if(state.dirty){e.preventDefault();e.returnValue="";}});
  }
  async function boot(){
    bind();
    try{
      state.auth=await api("/api/ui/topbar");Promise.resolve().then(()=>QuickNav.render({container:"#quickNav",auth:state.auth,activeKey:"fbPostMaterialPool"})).catch(()=>{$("quickNav").textContent="导航暂时不可用，请刷新重试。";});
      UiTopbar.render({auth:state.auth,userCard:"#userCard",authButton:"#authButton",refreshButton:"#refreshPage"});$("authButton").onclick=()=>UiTopbar.handleAuthAction({auth:state.auth,api});
      if(!state.auth.authenticated){$("gate").textContent="请先登录 AI 自动后台后使用 FB Post 素材池。";return;}
      if(!(state.auth.user?.is_admin||state.auth.user?.permissions?.fb_video_publish)){$("gate").textContent="当前账号没有 Facebook 视频发布权限，请联系管理员分配。";return;}
      try{state.pending=JSON.parse(sessionStorage.getItem(storageKey())||"null");}catch(_){throw new Error("无法读取上次操作记录，请恢复浏览器存储后重试。");}
      const languages=await api(BASE+"/languages");for(const item of languages.items||[]){state.languages.set(String(item.value),item.label);const option=node("option",item.label);option.value=String(item.value);$("itemLanguage").append(option);}
      $("gate").hidden=true;$("content").hidden=false;await refresh();restorePendingInputs();
    }catch(error){$("gate").hidden=true;errorAt("pageError",error);}
  }
  boot();
})();
