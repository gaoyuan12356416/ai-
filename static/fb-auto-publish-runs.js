(() => {
  "use strict";
  const {api, API_BASE, escapeHtml: esc, byId: $} = window.FBAutoPublishUI;
  const summary = window.FBRunSummary;
  const states = {running:"执行中", waiting:"等待执行", scheduled:"等待排期", paused:"已暂停", stopped:"已停止",
    attention_required:"待处理", completed:"已完成", partial_failed:"部分失败", failed:"失败", cancelled:"已取消"};
  const taskStates = {planned:"待制作", preparing:"制作中", late_preparing:"制作迟到", queued:"已入队", ready:"已备好",
    waiting_publish:"已备好，等待发布", ready_to_publish:"已备好，等待执行", running:"提交中", submitted:"已提交 Meta",
    unknown:"结果待确认", published:"已发布", failed:"失败", failed_without_retry:"失败（禁止重试）", skipped:"已跳过", cancelled:"已取消"};
  const attemptStates = {accepted:"Meta 已接受", success:"已接受", unknown:"待核对", hold:"待核对",
    definite_failure:"明确失败", credential_failure:"授权失败", started:"已开始"};
  const urlFields = {template_id:"filterTemplateId", trigger_type:"filterTriggerType", status:"filterStatus",
    from:"filterFrom", to:"filterTo"};
  const state = {runs:[], templates:[], selected:[], page:1, detail:null, attempts:new Map(), taskPage:1, loadId:0, detailId:0};
  const initial = new URLSearchParams(location.search);
  const pageSize = 20, taskSize = 50;
  const clock = new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",dateStyle:"short",timeStyle:"medium",hour12:false});
  const day = new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"});
  const format = (v, fmt=clock) => v && Number.isFinite(Date.parse(v)) ? fmt.format(new Date(v)) : "—";
  const secondary = value => value ? '<div class="secondary">'+esc(value)+'</div>' : "";
  const empty = (cols, text) => '<tr><td class="empty" colspan="'+cols+'">'+esc(text)+'</td></tr>';
  const trigger = t => ({auto:"自动定时",manual:"手动执行"})[t] || t || "—";

  function writeFilters() {
    const url = new URL(location.href);
    for (const [name,id] of Object.entries(urlFields)) {
      const value = $(id).value;
      if (value) url.searchParams.set(name,value); else url.searchParams.delete(name);
    }
    history.replaceState(null,"",url);
  }

  function templateOptions(runs) {
    const select = $("filterTemplateId"), selected = select.value;
    const names = new Map(runs.map(r => [String(r.template_id),r.name || "未命名模板"]));
    for (const t of state.templates) names.set(String(t.id || t.template_id),t.name || "未命名模板");
    if (selected && !names.has(selected)) names.set(selected,"模板");
    select.replaceChildren(new Option("全部模板",""));
    for (const [id,name] of [...names].sort((a,b) => a[1].localeCompare(b[1],"zh-CN"))) {
      select.add(new Option(name+" (#"+id+")",id));
    }
    select.value=selected;
  }

  function renderList() {
    const all=state.selected, pages=Math.max(1,Math.ceil(all.length/pageSize));
    state.page=Math.min(pages,Math.max(1,state.page));
    const items=all.slice((state.page-1)*pageSize,state.page*pageSize);
    $("totalCount").textContent=all.length;
    $("activeCount").textContent=all.filter(r=>r.display_status==="running").length;
    $("completedCount").textContent=all.filter(r=>r.display_status==="completed").length;
    $("attentionCount").textContent=all.filter(r=>["attention_required","partial_failed","failed"].includes(r.display_status)).length;
    $("runRows").innerHTML=items.map(r=>{
      const c=r.task_counts;
      const planned=r.schedule_policy==="target_midnight_v1" ? "各 Page 独立排期" : "计划 "+format(r.planned_publish_at_utc);
      return '<tr><td>'+esc(format(r.created_at_utc))+secondary(planned)+'</td><td><strong>'+esc(r.name)+'</strong>'+
        secondary("批次 #"+r.id+" · v"+r.template_version)+'</td><td>'+esc(trigger(r.trigger_type))+'</td><td><strong>'+
        (c ? "已发布 "+esc(c.published)+" / "+esc(c.total) : "—")+'</strong>'+
        secondary("创建时 "+r.total_pages+" 个 Page，可发 "+r.publishable_pages+"，缺授权 "+r.missing_token_pages)+'</td><td>'+
        summary.status(r)+'</td><td>'+summary.counts(r)+'</td><td>'+esc(format(summary.completed(r)))+
        '</td><td><button type="button" class="button small" data-run="'+esc(r.id)+'">查看详情</button></td></tr>';
    }).join("") || empty(8,"没有符合条件的运行记录");
    $("pageInfo").textContent="第 "+state.page+" / "+pages+" 页，共 "+all.length+" 条";
    $("prevPage").disabled=state.page===1;
    $("nextPage").disabled=state.page===pages;
  }

  function filterRuns() {
    const template=$("filterTemplateId").value, method=$("filterTriggerType").value, status=$("filterStatus").value;
    const start=$("filterFrom").value, end=$("filterTo").value;
    $("filterTo").setCustomValidity(start && end && start>end ? "结束日期须大于或等于开始日期" : "");
    if (!$("runFilters").reportValidity()) return;
    state.selected=state.runs.filter(r=>{
      if ((template && String(r.template_id)!==template) || (method && r.trigger_type!==method) ||
          (status && r.display_status!==status)) return false;
      const date=format(r.created_at_utc,day);
      return (!start || date>=start) && (!end || date<=end);
    });
    state.page=1;
    writeFilters();
    renderList();
  }

  async function loadTemplateNames() {
    const names=[];
    for (let offset=0;;) {
      const data=await api(API_BASE+"/templates?limit=200&offset="+offset);
      const items=Array.isArray(data.items) ? data.items : [];
      for (const t of items) names.push({id:t.id || t.template_id,name:t.name});
      offset+=items.length;
      if (items.length<200 || offset>=data.total) break;
    }
    state.templates=names;
    templateOptions(state.runs);
  }

  async function refresh() {
    const token=++state.loadId, unique=new Map();
    $("runRows").innerHTML=empty(8,"正在读取运行记录…");
    $("prevPage").disabled=$("nextPage").disabled=true;
    try {
      // The authorized FB endpoint pages newest-first. Keep all permitted run summaries
      // for accurate template options, counters and filters, even beyond its 200-row limit.
      let offset=0, firstId;
      while (true) {
        const response=await api(API_BASE+"/runs?limit=200&offset="+offset);
        if (token!==state.loadId) return;
        if (!Array.isArray(response.items)) throw new Error("运行列表返回格式异常");
        const items=response.items;
        if (!items.length) break;
        if (firstId==null) firstId=Number(items[0].id);
        for (const r of items) if (Number(r.id)<=firstId) unique.set(String(r.id),r);
        offset+=items.length;
        if (offset>=Number(response.total) || items.length<200) break;
      }
      state.runs=[...unique.values()];
      templateOptions(state.runs);
      filterRuns();
    } catch (err) {
      if (token!==state.loadId) return;
      state.runs=[]; state.selected=[]; renderList();
      $("runRows").innerHTML=empty(8,"读取失败："+err.message);
      for (const id of ["totalCount","activeCount","completedCount","attentionCount"]) $(id).textContent="—";
      $("pageInfo").textContent="读取失败";
    }
  }

  function taskResult(t) {
    const objectId=t.graph_object_id || t.graph_post_id;
    const address=window.FBPostAddress(t.permalink_url || t.post_url,objectId);
    const attempts=state.attempts.get(String(t.id)) || [];
    const history=attempts.map(a=>esc(format(a.created_at_utc)+" · "+(attemptStates[a.result_kind] || a.result_kind)+
      (a.error_code ? " · "+a.error_code : ""))).join("<br>");
    return (address ? '<a target="_blank" rel="noopener noreferrer" href="'+esc(address)+'">打开 Facebook 帖子</a>' : "暂无帖子地址")+
      secondary(objectId && "平台编号 "+objectId)+secondary([t.error_code||t.skip_reason,t.error_message].filter(Boolean).join(" · "))+
      (history ? '<details><summary>发布尝试（'+attempts.length+'）</summary>'+history+'</details>' : "");
  }

  function renderTasks() {
    const tasks=state.detail.tasks || [], pages=Math.max(1,Math.ceil(tasks.length/taskSize));
    state.taskPage=Math.max(1,Math.min(pages,state.taskPage));
    $("runTaskRows").innerHTML=tasks.slice((state.taskPage-1)*taskSize,state.taskPage*taskSize).map(t=>{
      let selection={};
      try { selection=JSON.parse(t.selection_json || "{}") || {}; } catch (_) {}
      const story=selection.story_copy || {};
      return '<tr><td>'+esc(t.page_id)+secondary("任务 #"+t.id)+'</td><td>'+esc(story.title || t.content_id || "—")+
        secondary("剧 ID "+(t.content_id || "—")+" · 素材 "+(t.material_id || "—"))+
        secondary(selection.reason)+'</td><td>'+esc(format(t.planned_publish_at_utc))+'</td><td>'+
        esc(t.prepared_at_utc ? "已备好 "+format(t.prepared_at_utc) : t.prepared_profile || "—")+
        secondary(t.completed_at_utc && "任务结束 "+format(t.completed_at_utc))+'</td><td>'+
        esc(t.unknown_outcome ? "结果待确认" : taskStates[t.schedule_state] || taskStates[t.status] || t.status)+'</td><td>'+
        taskResult(t)+'</td></tr>';
    }).join("") || empty(6,"此批次没有生成 Page 任务");
    $("taskPageInfo").textContent="第 "+state.taskPage+" / "+pages+" 页，共 "+tasks.length+" 条任务";
    $("taskPrev").disabled=state.taskPage===1; $("taskNext").disabled=state.taskPage===pages;
  }

  async function showRun(id) {
    if (!/^[1-9]\d*$/.test(id)) return;
    const token=++state.detailId, dialog=$("runDetailDialog");
    state.detail=null;
    $("runDetailTitle").textContent="运行详情 · #"+id;
    $("runDetailSubtitle").textContent="正在读取…";
    $("runDetailFacts").replaceChildren();
    $("runTaskRows").innerHTML=empty(6,"正在读取 Page 任务…");
    $("runSnapshot").textContent="—"; $("taskPageInfo").textContent="";
    $("extraPages").hidden=true;
    dialog.querySelectorAll("details[open]").forEach(el=>el.open=false);
    $("taskPrev").disabled=$("taskNext").disabled=true;
    if (!dialog.open) dialog.showModal();
    try {
      const {run}=await api(API_BASE+"/runs/"+encodeURIComponent(id));
      if (token!==state.detailId || !dialog.open) return;
      state.detail=run; state.taskPage=1; state.attempts=new Map();
      for (const a of run.attempts || []) {
        const key=String(a.task_id);
        if (!state.attempts.has(key)) state.attempts.set(key,[]);
        state.attempts.get(key).push(a);
      }
      $("runDetailSubtitle").textContent=(run.name || "模板")+" · v"+run.template_version+" · "+trigger(run.trigger_type);
      $("runDetailFacts").innerHTML=summary.status(run)+summary.counts(run)+secondary("触发 "+format(run.created_at_utc)+
        " · 完成 "+format(summary.completed(run)));
      $("runSnapshot").textContent=JSON.stringify(run.config || {},null,2);
      const notes=[...(run.waiting_pages || []).map(p=>"Page "+p.page_id+" · 等待素材 · "+format(p.next_check_at_utc)),
        ...(run.skipped_pages || []).map(p=>"Page "+p.page_id+" · 跳过 · "+p.reason)];
      $("extraPages").hidden=!notes.length; $("pageNotes").textContent=notes.join("\n");
      renderTasks();
    } catch (err) {
      if (token!==state.detailId) return;
      $("runDetailSubtitle").textContent="读取失败："+err.message; $("runTaskRows").innerHTML=empty(6,"详情暂不可用");
    }
  }

  async function init() {
    let auth;
    try { auth=await api("/api/ui/topbar"); } catch (_) { auth={authenticated:false}; }
    void QuickNav.render({container:"#quickNav",auth,activeKey:"fbAutoPublishRuns"}).catch(()=>{});
    UiTopbar.render({auth,userCard:"#userCard",authButton:"#authButton",refreshButton:"#refreshPage"});
    $("authButton").onclick=()=>UiTopbar.handleAuthAction({auth,api});
    if (!auth.authenticated) { $("loginGate").classList.remove("hidden"); return; }
    if (!(auth.user?.is_admin || auth.user?.permissions?.fb_page_posts)) { $("permissionGate").classList.remove("hidden"); return; }
    $("pageRoot").classList.remove("hidden");
    for (const [key,label] of Object.entries(states)) $("filterStatus").add(new Option(label,key));
    const id=initial.get("template_id");
    if (/^[1-9]\d*$/.test(id || "")) $("filterTemplateId").add(new Option("模板 #"+id,id,false,true));
    for (const [key,field] of Object.entries(urlFields)) if (key!=="template_id" && initial.has(key)) $(field).value=initial.get(key);
    void loadTemplateNames().catch(()=>{}); // Runs remain available to users with only history access.
    await refresh();
    if (initial.has("run_id")) void showRun(initial.get("run_id"));
  }
  $("runFilters").onsubmit=e=>{e.preventDefault();filterRuns();};
  $("filterFrom").oninput=$("filterTo").oninput=()=>$("filterTo").setCustomValidity("");
  $("resetFilters").onclick=()=>{for (const field of Object.values(urlFields)) $(field).value="";filterRuns();};
  $("reloadRuns").onclick=refresh;
  $("prevPage").onclick=()=>{state.page--;renderList();}; $("nextPage").onclick=()=>{state.page++;renderList();};
  $("runRows").onclick=e=>{const button=e.target.closest("[data-run]");if(button) void showRun(button.dataset.run);};
  $("closeRunDetail").onclick=()=>$("runDetailDialog").close();
  $("runDetailDialog").onclose=()=>{state.detailId++;state.detail=null;const u=new URL(location.href);u.searchParams.delete("run_id");history.replaceState(null,"",u);};
  $("taskPrev").onclick=()=>{state.taskPage--;renderTasks();}; $("taskNext").onclick=()=>{state.taskPage++;renderTasks();};
  void init();
})();
