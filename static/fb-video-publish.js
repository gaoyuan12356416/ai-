(() => {
  "use strict";
  const BASE = "/api/fb-video-publish";
  const LANDING = "https://www.dramawavew2a.com/ads/101/2310/view";
  const LINK_PLACEHOLDER = "【提交后生成独立短链】";
  const view = document.body.dataset.fbView;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const uid = () => crypto.randomUUID();
  const pairKey = (m, p) => `${m}::${p}`;
  const languages = new Map();
  const lang = code => languages.get(code) || code || "未设置语言";
  const atBJ = value => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value)) : "—";
  const byteLength = value => new TextEncoder().encode(value).length;
  const statuses = {accepted:"已受理",queued:"待处理",preparing:"加工中",ready:"待发布",running:"发布中",submitted:"平台处理中",published:"已发布",partial:"部分完成",partial_failed:"部分失败",completed:"已完成",failed:"失败",unknown:"结果待核对",cancelled:"已取消",blocked:"已阻止",skipped:"已跳过",links_pending:"链接准备中",pending:"待处理"};
  let auth = {}, toastTimer = 0;
  class ApiError extends Error { constructor(status, data) { super(data.message || data.error || `请求失败（${status}）`); this.status=status; this.data=data; this.code=data.code || data.error || ""; } }
  async function api(path, options={}) {
    const response = await fetch(path.startsWith("/api/") ? path : BASE + path, {
      credentials:"same-origin",cache:"no-store",...options,
      headers:{...(options.body ? {"Content-Type":"application/json"} : {}),...(options.headers || {})},
    });
    let data;
    try { data=await response.json(); } catch (_) { throw new ApiError(response.status || 502,{message:"服务返回内容无效，请保留当前输入后重试。"}); }
    if (!response.ok || data.ok === false) throw new ApiError(response.status, data);
    return data;
  }
  const post = (path, data) => api(path,{method:"POST",body:JSON.stringify(data)});
  function toast(message) { $("toast").textContent=message; $("toast").hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$("toast").hidden=true,4500); }
  function errorAt(id, error) { const el=$(id); if (el) { el.textContent=error?.message || String(error || ""); el.hidden=!el.textContent; } }
  async function busy(button, fn) { if (button.disabled) return; button.disabled=true; try { return await fn(); } finally { if (button.isConnected) button.disabled=false; } }
  function langOptions(emptyLabel="请选择剧语言", selected="") { return `<option value="">${esc(emptyLabel)}</option>`+[...languages].map(([v,l])=>`<option value="${esc(v)}"${v===selected?" selected":""}>${esc(l)}</option>`).join(""); }
  function validateTemplate(text) {
    if (!String(text || "").trim()) throw new Error("发布文案不能为空。");
    const rest=text.replace(/\{(?:name|desc|url)\}/g,"");
    if (/[{}]/.test(rest)) throw new Error("仅支持完整的 {name}、{desc}、{url} 宏参数，请检查括号。");
    if (byteLength(text)>5000) throw new Error("文案模板超过 5,000 UTF-8 字节。");
  }
  function expand(template, material) { return template.replace(/\{(name|desc|url)\}/g,(_,k)=>({name:material.drama_name || "【缺少剧名】",desc:material.drama_description || "【简介将在服务端预检时核对】",url:LINK_PLACEHOLDER}[k])); }
  const macroButtons = target => `<div class="macro-list"><span class="helper">插入宏参数</span>${[["name","剧名"],["desc","简介"],["url","短链"]].map(([k,l])=>`<button type="button" class="macro" data-macro="${k}" data-target="${target}">{${k}} ${l}</button>`).join("")}</div>`;
  function insertMacro(button) { const input=$(button.dataset.target); if (!input || input.disabled) return; const start=input.selectionStart ?? input.value.length,end=input.selectionEnd ?? start; input.setRangeText(`{${button.dataset.macro}}`,start,end,"end"); input.focus(); input.dispatchEvent(new Event("input",{bubbles:true})); }
  function dialog(id, title, body, footer) {
    const el=document.createElement("dialog"); el.id=id;
    el.innerHTML=`<header class="dialog-head"><h2>${esc(title)}</h2><button type="button" class="close" data-close aria-label="关闭">×</button></header><div class="dialog-body">${body}</div><footer class="dialog-foot">${footer}</footer>`;
    document.body.appendChild(el); el.addEventListener("close",()=>el.remove()); el.addEventListener("click",e=>{if(e.target.closest("[data-close]"))el.close();const macro=e.target.closest("[data-macro]");if(macro)insertMacro(macro);});el.showModal();return el;
  }
  function shell() {
    const info={pages:["Page 管理","同租户共用 Page 文案和剧语言；仅显示当前账号有权访问的 Page。","fbPageManagement"],publish:["Facebook 视频发布","多选素材与 Page，按剧语言建立对应关系，逐条核对后提交。","fbVideoPublish"],records:["批量发布记录","查看本模块发布批次、冻结文案、真实链接和执行结果。","fbVideoPublishRecords"]}[view];
    $("fbApp").innerHTML=`<div class="fb-shell"><aside class="fb-sidebar" id="sidebar"><div class="fb-brand"><b>AI</b><div><strong>AI自动后台</strong><small>ai.yingliangads.com</small></div></div><nav id="quickNav"></nav><button class="button mobile-nav" id="hideNav" type="button">收起导航</button></aside><main class="fb-main"><header class="topbar"><div><button type="button" class="button small mobile-nav" id="showNav">☰ 导航</button><h1>${info[0]}</h1><p class="muted">${info[1]}</p></div><div class="top-actions"><div id="userCard" class="user-card"></div><button class="button" id="refreshPage">刷新</button><button class="button" id="authButton">登录</button></div></header><nav class="fb-tabs"><a class="${view==="pages"?"active":""}" href="/fb-pages.html">Page 管理</a><a class="${view==="publish"?"active":""}" href="/fb-video-publish.html">视频发布</a><a class="${view==="records"?"active":""}" href="/fb-video-publish-records.html">批量发布记录</a></nav><div id="gate" class="panel gate">正在读取登录状态…</div><div id="pageError" class="error" role="alert" hidden></div><div id="content" hidden></div></main></div><div id="toast" class="fb-toast" role="status" hidden></div>`;
    $("showNav").onclick=()=>$("sidebar").classList.add("open");$("hideNav").onclick=()=>$("sidebar").classList.remove("open");
    return info[2];
  }
  async function boot() {
    const navKey=shell();
    try {
      auth=await api("/api/ui/topbar");
      // QuickNav paints its local fallback first; its config refresh must not delay page data.
      Promise.resolve().then(()=>QuickNav.render({container:"#quickNav",auth,activeKey:navKey})).catch(()=>{
        if (!$("quickNav").textContent.trim()) $("quickNav").textContent="导航暂时无法加载，可使用顶部入口。";
      });
      UiTopbar.render({auth,userCard:"#userCard",authButton:"#authButton",refreshButton:"#refreshPage"});
      $("authButton").onclick=()=>UiTopbar.handleAuthAction({auth,api});
      if (!auth.authenticated) { $("gate").innerHTML="<strong>请先登录 AI 自动后台</strong><p>登录后可查看 Page 配置和发布记录。</p>"; return; }
      if (!(auth.user?.is_admin || auth.user?.permissions?.fb_video_publish)) { $("gate").innerHTML="<strong>当前账号没有 Facebook 视频发布权限</strong><p>请联系管理员在用户管理中分配“Facebook 视频发布”。</p>"; return; }
      if(view==="publish")await loadLanguages();
      $("gate").hidden=true;$("content").hidden=false;
      if(view==="pages")await pageManagement();else if(view==="publish")await publishing();else await records();
    } catch(error) { $("gate").hidden=true; errorAt("pageError",error); }
  }
  async function loadLanguages() {
    const result=await api("/languages");
    for(const item of result.items || []) languages.set(String(item.value),item.label);
  }

  async function materialPickerDialog(initial, apply, options={}) {
    if($("materialPicker")){$("materialPicker").focus();return;}
    let staged=new Map(initial),items=[],cursor="",next="",stack=[],generation=0,loading=false,emptyReason="";
    const languageChoices=langOptions("全部剧语言",options.language || "")+(options.language&&!languages.has(options.language)?`<option value="${esc(options.language)}" selected>${esc(lang(options.language))}</option>`:"");
    const modal=dialog("materialPicker",options.title || "选择素材（可多选）",
      `<div id="pickerError" class="error" role="alert" hidden></div><div class="filters"><input id="materialSearch" placeholder="素材完整ID（精确）、素材名称或剧名" aria-label="搜索素材"><select id="materialLanguage" aria-label="素材语言">${languageChoices}</select><button id="materialSearchButton" class="button">搜索</button></div><div class="row spread"><span class="helper" id="pickerCount" role="status"></span><div class="actions"><button id="pickCurrent" class="button small">全选当前页</button><button id="clearCurrent" class="button small">取消当前页</button></div></div><div id="materialChoices" class="pick-list"></div><div class="pager"><span class="helper">纯数字按完整素材ID精确查询。跨页和搜索保留已选项，最多 200 条。</span><div class="actions"><button id="materialsPrev" class="button small">上一页</button><button id="materialsNext" class="button small">下一页</button></div></div>`,
      `<button class="button" data-close>取消</button><button id="applyMaterials" class="button primary">${esc(options.applyLabel || "使用所选素材")}</button>`);
    if(options.language)$("materialLanguage").disabled=true;
    const eligible=m=>!options.language || m.language===options.language;
    function lock(){
      if(!modal.isConnected)return;
      $("materialSearch").disabled=loading;$("materialLanguage").disabled=loading||!!options.language;
      $("materialSearchButton").disabled=loading;$("materialSearchButton").textContent=loading?"搜索中…":"搜索";
      $("materialsPrev").disabled=loading||!stack.length;$("materialsNext").disabled=loading||!next;
      $("pickCurrent").disabled=loading||!items.length;$("clearCurrent").disabled=loading||!items.length;
      $("materialChoices").setAttribute("aria-busy",String(loading));
    }
    function paint(){
      $("materialChoices").innerHTML=items.map(m=>`<label class="pick"><input type="checkbox" data-material="${esc(m.material_id)}"${staged.has(m.material_id)?" checked":""}${eligible(m)?"":" disabled"}><span><strong>${esc(m.material_name)}</strong><small>${esc(m.material_id)} · ${esc(lang(m.language))} · ${Number(m.duration_seconds || 0)} 秒</small><small>${esc(m.drama_name || "")}${eligible(m)?"":" · 剧语言不匹配"}</small></span></label>`).join("") || `<div class="empty">${esc(emptyReason || "没有符合条件的素材")}</div>`;
      $("pickerCount").textContent=`已选择 ${staged.size} 条素材`;lock();
    }
    async function load(){
      if(loading||!modal.isConnected)return;
      const gen=++generation;errorAt("pickerError","");
      const query=new URLSearchParams({q:$("materialSearch").value,language:options.language || $("materialLanguage").value,cursor,limit:"50"});
      loading=true;items=[];next="";emptyReason="";lock();
      $("materialChoices").innerHTML='<div class="empty" role="status">正在搜索素材，请稍候…</div>';
      $("pickerCount").textContent=`已选择 ${staged.size} 条素材，正在查询…`;
      try{const data=await api("/materials?"+query);if(!modal.isConnected||gen!==generation)return;items=data.items || [];next=data.next_cursor || "";emptyReason=data.empty_reason || "";}
      catch(err){if(modal.isConnected&&gen===generation){emptyReason="搜索未完成，请点击搜索重试。已选素材已保留。";errorAt("pickerError",err);}}
      finally{if(modal.isConnected&&gen===generation){loading=false;paint();}}
    }
    $("materialChoices").onchange=e=>{const id=e.target.dataset.material;if(id){e.target.checked?staged.set(id,items.find(m=>m.material_id===id)):staged.delete(id);$("pickerCount").textContent=`已选择 ${staged.size} 条素材`;}};
    $("materialSearchButton").onclick=()=>{if(loading)return;cursor="";stack=[];load();};$("materialLanguage").onchange=()=>{if(loading)return;cursor="";stack=[];load();};
    $("materialSearch").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();$("materialSearchButton").click();}};
    $("materialsNext").onclick=()=>{if(loading||!next)return;stack.push(cursor);cursor=next;load();};$("materialsPrev").onclick=()=>{if(loading||!stack.length)return;cursor=stack.pop() || "";load();};
    $("pickCurrent").onclick=()=>{items.filter(eligible).forEach(m=>staged.set(m.material_id,m));paint();};
    $("clearCurrent").onclick=()=>{items.forEach(m=>staged.delete(m.material_id));paint();};
    $("applyMaterials").onclick=()=>{if(staged.size>200){errorAt("pickerError",new Error("一次最多选择 200 条素材。"));return;}modal.close();apply(staged);};
    await load();
  }

  const poolStorageKey=p=>`fb-pool-pending:${auth.user?.tenant_key || ""}:${auth.user?.user_id || ""}:${p.page_id}:${p.config_instance_id}`;
  function poolPending(p){try{return JSON.parse(sessionStorage.getItem(poolStorageKey(p)) || "null");}catch(_){return null;}}
  async function openPool(page, onClose) {
    if($("pagePool")){$("pagePool").focus();return;}
    let pool=null,items=[],counts={},tab="pending",cursor="",next="",stack=[],generation=0,writing=false;
    let reading=false,readFailed=false,readPromise=null;
    let selected=new Set(),staged=new Map(),times=[],settingsDirty=false,pending=poolPending(page);
    const endpoint="/pages/"+encodeURIComponent(page.page_id)+"/pool";
    const stateLabels={pending:"待发布",processing:"处理中",published:"已发布",failed:"异常 / 历史",slots:"时点记录"};
    const itemLabels={...statuses,pending:"待发布",processing:"处理中",removed:"已移除",paused:"已暂停",enabled:"已启用",archived:"已归档"};
    const modal=dialog("pagePool",page.page_name+" · 素材池",
      `<div class="notice">同租户中有权访问此 Page 的用户共用素材池。每个时点发布一条；空池和错过的时点不补发。生成任务时使用最新 Page 文案，随后冻结。</div><div id="poolError" class="error" role="alert" hidden></div><div id="poolPendingPanel" class="notice warning" hidden><strong>入池结果待核对</strong><p id="poolPendingText">保留原操作查询，避免重复入池。</p><div class="actions"><button id="poolQueryOperation" class="button">查询原操作</button><button id="poolRetryOperation" class="button">按原操作重试</button></div></div><div class="row spread"><div id="poolStatus" class="helper">正在加载…</div><button id="poolRefresh" class="button small">刷新版本并保留输入</button></div><fieldset id="poolControls" disabled><section class="pool-settings"><div class="form-grid"><div class="field"><span>每天的发布时间（北京时间）</span><div id="poolTimes" class="time-chips"></div><span id="poolDailyCount" class="helper"></span><div class="row"><input id="poolTimeInput" type="time" aria-label="新增每日时点"><button id="poolAddTime" class="button small">添加时点</button></div><span class="helper">至少设置一个 HH:mm 时点；每个时点取队首一条。</span></div><label class="field"><span>视频加工</span><select id="poolProcessing"><option value="original">不去重（原片发布）</option><option value="random_overlay">随机加工</option></select><span class="helper">普通保存不改变启用人；首次保存保持暂停。</span></label></div><div class="row spread"><span id="poolSettingsStatus" class="helper"></span><div class="actions"><button id="poolSaveSettings" class="button">保存设置</button><button id="poolEnable" class="button primary">启用计划</button><button id="poolPause" class="button">暂停计划</button></div></div></section><nav id="poolTabs" class="fb-tabs pool-tabs">${Object.entries(stateLabels).map(([key,label])=>`<button class="button small" data-pool-tab="${key}">${label}<span data-pool-count="${key}"></span></button>`).join("")}</nav><div id="poolPendingActions" class="row spread"><div class="actions"><button id="poolPickMaterials" class="button primary">选择素材入池</button><button id="poolRemoveSelected" class="button danger">移除选中项</button></div><span id="poolSelectionCount" class="helper"></span></div><div id="poolStaged" class="notice" hidden></div><div id="poolList" class="pool-list"></div><div class="pager"><span class="helper">每页最多 50 条；已生成任务不随移除或暂停而改变。</span><div class="actions"><button id="poolPrev" class="button small">上一页</button><button id="poolNext" class="button small">下一页</button></div></div></fieldset>`,
      `<button class="button" data-close>关闭</button>`);
    modal.addEventListener("close",()=>onClose?.());modal.addEventListener("cancel",e=>{if(writing)e.preventDefault();});
    function remember(){try{pending?sessionStorage.setItem(poolStorageKey(page),JSON.stringify(pending)):sessionStorage.removeItem(poolStorageKey(page));}catch(_){}}
    function identity(){if(!pool||reading||readFailed)throw new Error("请先成功读取素材池最新版本。");return {config_instance_id:page.config_instance_id,expected_version:pool.version};}
    function markSettings(){settingsDirty=true;$("poolSettingsStatus").textContent="设置尚未保存";lock();}
    function paintTimes(){$("poolDailyCount").textContent=`每天发布 ${times.length} 次（每个时点一条）` ;$("poolTimes").innerHTML=times.map(t=>`<span class="time-chip">${esc(t)}<button type="button" data-remove-time="${esc(t)}" aria-label="移除时点 ${esc(t)}">×</button></span>`).join("") || '<span class="helper">尚未设置时点</span>';}
    function lock(){
      if(!modal.isConnected)return;
      modal.querySelectorAll("[data-close]").forEach(el=>el.disabled=writing);
      $("poolControls").disabled=writing||reading||readFailed||!!pending||!pool||pool.status==="archived";
      $("poolControls").setAttribute("aria-busy",String(reading));
      $("poolRefresh").disabled=writing||reading;$("poolRefresh").textContent=reading?"正在读取最新版本…":readFailed?"重试读取，保留输入":"刷新版本并保留输入";
      $("poolQueryOperation").disabled=writing||reading;$("poolRetryOperation").disabled=writing||reading;
      $("poolPendingPanel").hidden=!pending;$("poolPrev").disabled=!stack.length;$("poolNext").disabled=!next;
      $("poolEnable").disabled=!pool||pool.status==="enabled"||settingsDirty||!times.length;
      $("poolPause").disabled=!pool||pool.status!=="enabled";
      $("poolRemoveSelected").disabled=!selected.size;
    }
    function paint(){
      if(!modal.isConnected)return;
      $("poolStatus").innerHTML=`<span class="badge ${pool.status==="enabled"?"good":""}">${esc(itemLabels[pool.status] || pool.status)}</span> · v${pool.version} · 剧语言 ${esc(lang(page.language))}${pool.publisher_user_id?` · 启用人 ${esc(pool.publisher_name || pool.publisher_user_id)}`:""}${pool.pause_reason?`<p class="notice warning">${esc(pool.pause_reason)}</p>`:""}`;
      document.querySelectorAll("#poolTabs [data-pool-tab]").forEach(el=>el.classList.toggle("primary",el.dataset.poolTab===tab));
      document.querySelectorAll("#poolTabs [data-pool-count]").forEach(el=>{const key=el.dataset.poolCount;el.textContent=" "+Number(key==="failed"?Number(counts.failed || 0)+Number(counts.cancelled || 0)+Number(counts.removed || 0):counts[key] || 0);});
      $("poolPendingActions").hidden=tab!=="pending";$("poolSelectionCount").textContent=`已选 ${selected.size} 条待发布素材`;
      $("poolStaged").hidden=!staged.size;$("poolStaged").innerHTML=staged.size?`<span>已选择 ${staged.size} 条素材，尚未入池。</span> <button id="poolReviewStaged" class="button small">核对并入池</button> <button id="poolChangeStaged" class="button small">继续选择</button>`:"";
      if(staged.size){$("poolReviewStaged").onclick=reviewAdmission;$("poolChangeStaged").onclick=pickMaterials;}
      $("poolList").innerHTML=tab==="slots"?
        items.map(item=>`<article class="pool-item"><div class="row spread"><strong>${esc(atBJ(item.slot_at_utc))} 北京时间</strong><span class="badge">${esc(itemLabels[item.status] || item.status)}</span></div><p class="helper">${esc(item.reason || "—")}${item.material_id?` · 素材 ${esc(item.material_id)}`:""}</p></article>`).join("") || '<div class="empty">暂无时点记录</div>':
        items.map(item=>`<article class="pool-item"><div class="row spread"><label class="check"><input type="checkbox" data-pool-item="${esc(item.id)}"${selected.has(String(item.id))?" checked":""}${(item.can_modify ?? (item.status==="pending"&&!item.task_id))?"":" disabled"}><strong>${esc(item.material_name || item.material_id)}</strong></label><span class="badge ${item.language_mismatch?"bad":""}">${esc(itemLabels[item.status] || item.status)}</span></div><p class="helper">${esc(item.material_id)} · ${esc(lang(item.language))} · ${esc(item.drama_name || "")}</p>${item.language_mismatch?'<p class="notice warning">剧语言与 Page 不匹配，请移除后重新选择。</p>':""}${item.error_message?`<p class="error">${esc(item.error_message)}</p>`:""}<div class="row spread"><span class="helper">入池 ${esc(atBJ(item.created_at_utc))}</span><div class="actions">${(item.can_modify ?? (item.status==="pending"&&!item.task_id))?`<button class="button small" data-pool-move="${esc(item.id)}" data-direction="top">置顶</button><button class="button small" data-pool-move="${esc(item.id)}" data-direction="up" aria-label="上移 ${esc(item.material_name)}">↑</button><button class="button small" data-pool-move="${esc(item.id)}" data-direction="down" aria-label="下移 ${esc(item.material_name)}">↓</button>`:""}${item.can_view_task&&item.batch_id?`<a class="button small" href="/fb-video-publish-records.html?batch=${encodeURIComponent(item.batch_id)}">查看任务 / 取消</a>`:item.task_id||item.status!=="pending"?'<span class="helper">任务详情遵循原发布记录权限</span>':""}</div></div></article>`).join("") || '<div class="empty">暂无素材</div>';
      lock();
    }
    function load(){
      if(readPromise)return readPromise;
      const gen=++generation;
      reading=true;lock();errorAt("poolError","");
      if(!pool){$("poolStatus").textContent="正在读取素材池设置和队列…";$("poolList").innerHTML='<div class="empty">正在加载素材池…</div>';}
      readPromise=(async()=>{
        try{
          const data=await api(endpoint+"?"+new URLSearchParams({state:tab,cursor,limit:"50"}));
          if(!modal.isConnected||gen!==generation)return;
          if(data.pool.config_instance_id!==page.config_instance_id)throw new Error("Page 配置实例已改变，请关闭素材池并刷新 Page 列表。");
          pool=data.pool;items=data.items || [];counts=data.counts || {};next=data.next_cursor || "";readFailed=false;
          if(!settingsDirty){times=[...(pool.times || [])];$("poolProcessing").value=pool.processing_mode || "original";paintTimes();$("poolSettingsStatus").textContent="已保存设置";}
          paint();
        }catch(err){
          if(modal.isConnected&&gen===generation){
            readFailed=true;errorAt("poolError",err);
            if(!pool){$("poolStatus").textContent="素材池暂时无法加载";$("poolList").innerHTML='<div class="empty">读取失败，请点击“重试读取，保留输入”。</div>';}
          }
        }finally{
          readPromise=null;reading=false;if(modal.isConnected&&gen===generation)lock();
        }
      })();
      return readPromise;
    }
    async function change(action,extra={}){
      if(writing||reading||readFailed||pending)return;
      writing=true;lock();errorAt("poolError","");
      try{const result=await post(endpoint+"/"+action,{...identity(),...extra});pool=result.pool;if(action==="settings")settingsDirty=false;if(action==="remove")selected.clear();cursor="";stack=[];await load();toast("素材池已更新");}
      catch(err){errorAt("poolError",new Error(err.message+(err.status===409?"；输入已保留，请刷新版本后重新核对。":"")));}
      finally{writing=false;lock();}
    }
    function pickMaterials(){materialPickerDialog(staged,chosen=>{staged=chosen;paint();},{title:"选择入池素材",language:page.language,applyLabel:"核对所选素材"});}
    async function admitted(){
      pending=null;remember();staged.clear();selected.clear();cursor="";stack=[];await load();toast("素材已入池");
    }
    async function queryAdmission(){
      if(!pending)return false;
      try{await api(endpoint+"/operations/"+encodeURIComponent(pending.request.operation_id));await admitted();return true;}
      catch(err){if(modal.isConnected)$("poolPendingText").textContent=err.status===404?"尚未查询到原入池操作，请保留此页并按原操作重试；不要重复创建。":"暂时无法查询原操作："+err.message;return false;}
    }
    async function enqueue(request,recovering=false){
      if(writing)return;writing=true;
      pending={request:structuredClone(request)};remember();lock();
      try{await post(endpoint+"/items",pending.request);await admitted();}
      catch(err){
        if(err.data?.accepted===false&&!recovering){pending=null;remember();errorAt("poolError",new Error(err.message+"；所选素材已保留，请刷新版本后重新预检。"));}
        else{errorAt("poolError",new Error("入池结果需要核对，已保留原操作。"+err.message));await queryAdmission();}
      }finally{writing=false;lock();}
    }
    async function reviewAdmission(){
      if(writing||reading||readFailed||pending)return;
      if(!staged.size){errorAt("poolError",new Error("请先选择入池素材。"));return;}
      writing=true;lock();errorAt("poolError","");
      try{
        const request={...identity(),material_ids:[...staged.keys()]},preview=await post(endpoint+"/items/preview",request);
        if(!modal.isConnected)return;
        const confirmDialog=dialog("poolAdmission","确认入池素材",`<p>向 ${esc(page.page_name)} 加入 ${staged.size} 条素材。本次核对不创建发布任务；计划在每日时点按顺序取材。</p><div id="poolAdmissionError" class="error" hidden></div>${preview.repeat_required?'<div class="notice warning"><strong>包含历史成功发布的素材</strong><label class="check"><input id="poolRepeatAck" type="checkbox">我已核对，确认允许这些素材再次发布</label></div>':""}<div class="pool-admission-list">${[...staged.values()].map(m=>`<p>${esc(m.material_name)} · ${esc(lang(m.language))}</p>`).join("")}</div>`,`<button class="button" data-close>返回选择</button><button id="poolConfirmAdmission" class="button primary"${preview.repeat_required?" disabled":""}>确认入池</button>`);
        if($("poolRepeatAck"))$("poolRepeatAck").onchange=e=>$("poolConfirmAdmission").disabled=!e.target.checked;
        $("poolConfirmAdmission").onclick=()=>{const allow=!!$("poolRepeatAck")?.checked;confirmDialog.close();enqueue({...request,operation_id:uid(),preview_token:preview.preview_token,allow_repeat:allow});};
      }catch(err){errorAt("poolError",err);}
      finally{writing=false;lock();}
    }
    $("poolTimeInput").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();$("poolAddTime").click();}};
    $("poolAddTime").onclick=()=>{const value=$("poolTimeInput").value;if(!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value)){errorAt("poolError",new Error("请选择有效的 HH:mm 时点。"));return;}if(times.includes(value)){errorAt("poolError",new Error("这个时点已存在，无需重复添加。"));return;}times.push(value);times.sort();$("poolTimeInput").value="";paintTimes();markSettings();errorAt("poolError","");};
    $("poolTimes").onclick=e=>{const b=e.target.closest("[data-remove-time]");if(b){times=times.filter(t=>t!==b.dataset.removeTime);paintTimes();markSettings();}};
    $("poolProcessing").onchange=markSettings;
    $("poolSaveSettings").onclick=()=>{if(!times.length){errorAt("poolError",new Error("每天至少设置一个发布时间。"));return;}change("settings",{times:[...times],processing_mode:$("poolProcessing").value});};
    $("poolEnable").onclick=()=>{
      if(settingsDirty){errorAt("poolError",new Error("请先保存设置。"));return;}
      const d=dialog("poolEnableConfirm","启用每日发布计划",`<p>启用后，发布人归属为当前账号 <strong>${esc(auth.user?.name || auth.user?.user_id || "")}</strong>，使用该账号的 Page 访问权限和归因。</p><p>每日 ${times.map(esc).join("、")}（北京时间）各取一条。普通编辑不会改变启用人，暂停不会取消已生成任务。</p>`,`<button class="button" data-close>取消</button><button id="poolConfirmEnable" class="button primary">确认以当前账号启用</button>`);
      $("poolConfirmEnable").onclick=()=>{d.close();change("enable");};
    };
    $("poolPause").onclick=()=>change("pause");
    $("poolRefresh").onclick=()=>{if(writing||reading)return;cursor="";stack=[];load();};
    $("poolTabs").onclick=e=>{const b=e.target.closest("[data-pool-tab]");if(b&&!writing&&!reading&&!readFailed&&b.dataset.poolTab!==tab){tab=b.dataset.poolTab;cursor="";stack=[];selected.clear();load();}};
    $("poolPrev").onclick=()=>{if(writing||reading||readFailed||!stack.length)return;cursor=stack.pop() || "";load();};$("poolNext").onclick=()=>{if(writing||reading||readFailed||!next)return;stack.push(cursor);cursor=next;load();};
    $("poolPickMaterials").onclick=pickMaterials;
    $("poolList").onchange=e=>{const id=e.target.dataset.poolItem;if(id){e.target.checked?selected.add(id):selected.delete(id);$("poolSelectionCount").textContent=`已选 ${selected.size} 条待发布素材`;lock();}};
    $("poolList").onclick=e=>{const b=e.target.closest("[data-pool-move]");if(b)change("move",{item_id:b.dataset.poolMove,direction:b.dataset.direction});};
    $("poolRemoveSelected").onclick=()=>{if(selected.size&&confirm(`移除选中的 ${selected.size} 条待发布素材？已生成任务不会被取消。`))change("remove",{item_ids:[...selected]});};
    $("poolQueryOperation").onclick=async()=>{if(writing||reading)return;writing=true;lock();try{await queryAdmission();}finally{writing=false;lock();}};
    $("poolRetryOperation").onclick=async()=>{
      if(writing||reading||!pending)return;writing=true;lock();const old=pending;
      try{await api(endpoint+"/operations/"+encodeURIComponent(old.request.operation_id));await admitted();}
      catch(err){if(err.status===404){writing=false;await enqueue(old.request,true);}else errorAt("poolError",err);}
      finally{writing=false;lock();}
    };
    lock();await load();if(!modal.isConnected)return;if(pending){writing=true;lock();try{await queryAdmission();}finally{writing=false;lock();}}
  }

  async function pageManagement() {
    let configs=[],page=0,query="",loading=false,loaded=false,languageReady=false;
    $("content").innerHTML=`<section class="panel"><div class="section-head"><div><h2>已配置 Page</h2><p>创建时可选择多个 Page，保存后每个 Page 独立管理。</p></div><button id="createPage" class="button primary" disabled>创建</button></div><div id="pageLanguageNotice" class="helper" role="status">正在加载剧语言选项…</div><div class="filters"><input id="pageSearch" placeholder="搜索 Page 名称或 ID" aria-label="搜索已配置 Page" disabled><button id="reloadPages" class="button">刷新列表</button></div><div class="table-wrap"><table><thead><tr><th>Page 名称 / ID</th><th>剧语言</th><th>授权状态</th><th>发布文案</th><th>创建人</th><th>更新时间（北京）</th><th>操作</th></tr></thead><tbody id="pageRows"></tbody></table></div><div class="pager"><span class="helper" id="pageTotal"></span><div class="actions"><button class="button small" id="pagePrev" disabled>上一页</button><button class="button small" id="pageNext" disabled>下一页</button></div></div></section>`;
    function render() {
      $("createPage").disabled=!languageReady||!loaded;
      $("pageSearch").disabled=!loaded;$("reloadPages").disabled=loading;
      $("pageRows").setAttribute("aria-busy",String(loading));
      if(!loaded){
        $("pageRows").innerHTML=`<tr><td colspan="7" class="empty">${loading?"正在加载 Page 列表…":"Page 列表加载失败，请点击“刷新列表”重试。"}</td></tr>`;
        $("pageTotal").textContent="";$("pagePrev").disabled=true;$("pageNext").disabled=true;return;
      }
      const rows=configs.filter(p=>`${p.page_name} ${p.page_id}`.toLowerCase().includes(query.toLowerCase()));
      page=Math.min(page,Math.max(0,Math.ceil(rows.length/20)-1));
      $("pageRows").innerHTML=rows.slice(page*20,(page+1)*20).map(p=>`<tr><td><strong>${esc(p.page_name)}</strong><br><span class="mono">${esc(p.page_id)}</span></td><td>${esc(lang(p.language))}</td><td><span class="badge ${p.blocked_reason || !p.eligible_token_count?"bad":"good"}">${p.blocked_reason || !p.eligible_token_count?"不可发布":"授权可用"}</span>${p.blocked_reason?`<p class="helper">${esc(p.blocked_reason)}</p>`:""}</td><td><div class="caption-summary">${esc(p.caption_template)}</div><span class="helper">v${p.version}</span></td><td>${esc(p.created_by_name || p.created_by || "—")}</td><td class="no-break">${esc(atBJ(p.updated_at_utc))}</td><td><div class="actions"><button class="button small" data-pool-page="${esc(p.page_id)}">素材池${poolPending(p)?" · 待核对":""}</button><button class="button small" data-edit-page="${esc(p.page_id)}"${languageReady?"":" disabled"}>编辑</button><button class="button small danger" data-delete-page="${esc(p.page_id)}">删除</button></div></td></tr>`).join("") || `<tr><td colspan="7" class="empty">${query?"没有匹配的 Page":"尚未配置 Page，点击“创建”设置文案、剧语言和 Page。"}</td></tr>`;
      $("pageTotal").textContent=`共 ${rows.length} 个 Page · 第 ${page+1} 页`;$("pagePrev").disabled=page===0;$("pageNext").disabled=(page+1)*20>=rows.length;
    }
    async function load() {
      if(loading)return;loading=true;render();errorAt("pageError","");
      try{const data=await api("/pages");configs=data.items || [];loaded=true;}
      finally{loading=false;render();}
    }
    async function loadLanguageOptions() {
      $("pageLanguageNotice").hidden=false;$("pageLanguageNotice").textContent="正在加载剧语言选项…";
      try{await loadLanguages();languageReady=true;$("pageLanguageNotice").hidden=true;}
      catch(error){
        $("pageLanguageNotice").innerHTML=`<span>${esc("剧语言选项加载失败，创建和编辑暂不可用："+error.message)}</span> <button class="button small" id="retryPageLanguages">重试语言选项</button>`;
        $("retryPageLanguages").onclick=()=>loadLanguageOptions();
      }
      render();
    }
    $("pageSearch").oninput=e=>{query=e.target.value;page=0;render();};$("pagePrev").onclick=()=>{page--;render();};$("pageNext").onclick=()=>{page++;render();};
    $("reloadPages").onclick=e=>busy(e.target,()=>load().catch(err=>errorAt("pageError",err)));
    async function editor(existing) {
      let chosen=new Set(),available=[],generation=0,searchTimer;
      const modal=dialog("pageEditor",existing?"编辑 Page":"创建 Page 配置",`<div class="notice">配置在同租户内共用。其他有权访问此 Page 的用户也会使用保存的语言和文案；已提交的任务保持冻结内容。</div><div id="editorError" class="error" role="alert" hidden></div><div class="form-grid"><label class="field"><span>剧语言</span><select id="editLanguage">${langOptions("请选择剧语言",existing?.language)}</select>${existing?'<span class="helper">更改剧语言会暂停素材池；请处理语言不匹配的待发布项后再启用。</span>':""}</label><div class="field"><span>落地页</span><div class="notice mono">${LANDING}</div></div><div class="field"><label for="editCaption">发布文案</label><textarea id="editCaption" rows="8">${esc(existing?.caption_template || "")}</textarea>${macroButtons("editCaption")}<p class="helper">仅支持三个宏参数。{url} 表示每次发布独立生成的短链。</p></div><div class="field"><span>示例文案预览</span><div id="editPreview" class="copy-preview"></div><span class="helper">使用示例剧目演示宏替换，不绑定发布素材、不生成真实链接。</span></div>${existing?`<div class="full notice"><strong>${esc(existing.page_name)}</strong> · ${esc(existing.page_id)} · 当前版本 v${existing.version}</div>`:`<div class="full"><div class="filters"><input id="availableSearch" placeholder="搜索可访问的 Page 名称或 ID" aria-label="搜索可访问的 Page"><button class="button" id="searchAvailable">搜索</button></div><div class="helper" id="availableCount"></div><div class="pick-list" id="availablePages"></div></div>`}</div>`,`<button class="button" data-close>取消</button><button class="button primary" id="savePage">${existing?"保存修改":"创建所选 Page"}</button>`);
      function preview(){const example={drama_name:$("editLanguage").value==="en"?"A New Beginning":"她的第二人生",drama_description:$("editLanguage").value==="en"?"An unexpected meeting changes her life.":"她决定重新开始，却意外遇见改变命运的人。"};$("editPreview").textContent=expand($("editCaption").value,example);}
      $("editCaption").oninput=preview;$("editLanguage").onchange=preview;preview();
      function showAvailable(){const configured=new Set(configs.map(p=>p.page_id));$("availablePages").innerHTML=available.map(p=>`<label class="pick"><input type="checkbox" value="${esc(p.page_id)}"${chosen.has(p.page_id)?" checked":""}${configured.has(p.page_id)?" disabled":""}><span><strong>${esc(p.page_name)}</strong><small>${esc(p.page_id)}</small><small>${configured.has(p.page_id)?"已配置，请在列表单独编辑":esc(p.blocked_reason || "可创建配置")}</small></span></label>`).join("") || '<div class="empty">没有可选 Page</div>';$("availableCount").textContent=`已选择 ${chosen.size} 个 Page`;}
      async function searchAvailable(){const gen=++generation;const data=await api("/pages/available?q="+encodeURIComponent($("availableSearch").value));if(!modal.isConnected || gen!==generation)return;available=data.items || [];showAvailable();}
      if(!existing){$("availablePages").onchange=e=>{if(e.target.type==="checkbox"){e.target.checked?chosen.add(e.target.value):chosen.delete(e.target.value);$("availableCount").textContent=`已选择 ${chosen.size} 个 Page`;}};$("availableSearch").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>searchAvailable().catch(err=>errorAt("editorError",err)),350);};$("searchAvailable").onclick=()=>searchAvailable().catch(err=>errorAt("editorError",err));await searchAvailable().catch(err=>errorAt("editorError",err));}
      if(!modal.isConnected)return;
      $("savePage").onclick=e=>busy(e.target,async()=>{
        try {
          const language=$("editLanguage").value,caption_template=$("editCaption").value;validateTemplate(caption_template);if(!language)throw new Error("请选择剧语言。");if(!existing&&!chosen.size)throw new Error("请至少选择一个 Page。");
          await post(existing?`/pages/${encodeURIComponent(existing.page_id)}`:"/pages",existing?{config_instance_id:existing.config_instance_id,expected_version:existing.version,language,caption_template}:{page_ids:[...chosen],language,caption_template});
          modal.close();toast("Page 配置已保存");await load();
        } catch(err){errorAt(modal.isConnected?"editorError":"pageError",err);}
      });
    }
    $("createPage").onclick=()=>editor(null);
    $("pageRows").onclick=e=>{
      const edit=e.target.closest("[data-edit-page]"),del=e.target.closest("[data-delete-page]"),pool=e.target.closest("[data-pool-page]");
      if(pool)openPool({...configs.find(p=>p.page_id===pool.dataset.poolPage)},render);
      if(edit)editor({...configs.find(p=>p.page_id===edit.dataset.editPage)});
      if(del)busy(del,async()=>{const item=configs.find(p=>p.page_id===del.dataset.deletePage);if(!confirm(`删除 ${item.page_name} 的共享配置？\n同租户其他用户也将失去此配置，素材池会停用并归档。已生成任务和历史保持不变；不会删除 Facebook Page 或授权。`))return;try{await post(`/pages/${encodeURIComponent(item.page_id)}/delete`,{config_instance_id:item.config_instance_id,expected_version:item.version});await load();toast("已删除共享配置");}catch(err){errorAt("pageError",err);}});
    };
    await Promise.all([load().catch(err=>errorAt("pageError",err)),loadLanguageOptions()]);
  }

  async function publishing() {
    let configs=[],selected=new Map(),pageIds=[],pairs=new Map(),active="",operation=uid(),pending=null,dirty=false,preview=null;
    let processing="original",schedules={},legacyDraft=null;
    const storageKey=`fb-video-pending:${auth.user?.tenant_key || ""}:${auth.user?.user_id || ""}`;
    $("content").innerHTML=`<div id="pendingNotice" class="notice warning" hidden><strong>上次提交的受理结果正在核对</strong><p id="pendingText">请查询原操作结果，避免重复发布。</p><div class="actions"><button class="button" id="queryOperation">查询原操作</button><button class="button" id="retryOperation">按原操作重试</button></div></div><div id="legacyDraftNotice" class="notice warning" hidden><strong>发现旧版草稿</strong><p>旧版整批预约和间隔不会自动套用。转换后保留素材、Page 和文案，清空预约时间，请逐条重新设置。</p><button id="convertDraft" class="button">转换草稿并重新设置时间</button></div><fieldset id="publishFields"><section class="panel"><div class="section-head"><div><h2>01 选择素材</h2><p>素材来自现有 Facebook 素材库，支持跨页多选。</p></div><button id="chooseMaterials" class="button primary">选择素材（可多选）</button></div><div id="selectedMaterials"></div></section><section class="panel"><div class="section-head"><div><h2>02 选择 Page</h2><p>仅已配置且剧语言匹配的 Page 可选；异常授权显示原因。</p></div><a class="button" href="/fb-pages.html">管理 Page</a></div><div class="row"><button class="button small" id="selectPages">全选可用 Page</button><button class="button small" id="clearPages">取消全选</button><button class="button small" id="refreshConfigs">刷新配置</button></div><div id="targetPages" class="pick-list"></div></section><section class="panel" id="pairSection"><div class="section-head"><div><h2>03 核对素材与 Page 对应关系</h2><p>一个勾选代表一条任务；同一素材可发到多个同语言 Page。</p></div><button class="button primary" id="matchPairs">按剧语言匹配</button></div><div class="summary" id="batchSummary"></div><div class="filters"><select id="pairLanguage" aria-label="筛选配对语言">${langOptions("全部剧语言")}</select><label class="check"><input type="checkbox" id="unmatchedOnly">只看未分配素材</label></div><div id="pairRows"></div></section><section class="panel"><div class="section-head"><div><h2>04 逐条编辑发布文案</h2><p>本次修改只影响当前“素材 × Page”，不会改写 Page 默认文案。</p></div></div><div class="field"><label for="captionPair">当前编辑配对</label><select id="captionPair"></select></div><div class="form-grid" style="margin-top:16px"><div class="field"><label for="pairCaption">本次发布文案</label><textarea id="pairCaption" rows="7"></textarea>${macroButtons("pairCaption")}<div class="row spread"><span class="helper" id="captionVersion"></span><button id="resetCaption" class="button small">重新应用当前 Page 文案</button></div></div><div class="field"><span>文案预览</span><div class="copy-preview" id="captionPreview"></div><span class="helper">短链为占位，最终提交时生成；简介和字节数由服务端确认。</span></div></div></section><section class="panel"><div class="section-head"><div><h2>05 视频加工</h2><p>视频加工与历史重复发布校验分别处理。</p></div></div><div class="processing"><label class="pick"><input type="radio" name="processing" value="original" checked><span><strong>不去重（原片发布）</strong><small>直接使用通过校验的源视频</small></span></label><label class="pick"><input type="radio" name="processing" value="random_overlay"><span><strong>随机加工</strong><small>使用现有 Facebook 随机加工流程，每条任务冻结加工设置</small></span></label></div><p class="helper">发布时间在每条素材上设置。未填写则立即发布；同一 Page 按预约时间、素材原顺序依次执行，加工或平台处理可能使实际时间顺延。</p></section></fieldset><div class="sticky-actions actions"><span class="helper" id="draftStatus">草稿保存到当前账号</span><button class="button" id="saveDraft">保存草稿</button><button class="button primary" id="preflight">核对并确认发布</button></div>`;
    const material = id => selected.get(String(id));
    const config = id => configs.find(p=>p.page_id===String(id));
    const reason = p => p.blocked_reason || (!p.eligible_token_count?"暂无有效发布授权":!p.language?"尚未设置剧语言":selected.size&&![...selected.values()].some(m=>m.language===p.language)?"与所选素材剧语言不匹配":"");
    function touch(){dirty=true;preview=null;operation=uid();$("draftStatus").textContent="有未保存的修改";}
    function orderedPairs(){return [...selected.keys()].flatMap(m=>pageIds.map(p=>pairs.get(pairKey(m,p))).filter(Boolean));}
    function makePair(m,p){return {material_id:m.material_id,page_id:p.page_id,config_instance_id:p.config_instance_id,config_version:p.version,caption_template:p.caption_template};}
    function restore(raw){
      if(!raw || ![1,2].includes(raw.schema))return;
      legacyDraft=raw.schema===1?structuredClone(raw):null;
      selected=new Map((raw.materials || []).map(m=>[String(m.material_id),m]));pageIds=[...new Set(raw.page_ids || [])];
      pairs=new Map((raw.pairs || []).filter(p=>selected.has(p.material_id)&&pageIds.includes(p.page_id)).map(p=>[pairKey(p.material_id,p.page_id),p]));
      active=raw.active || "";processing=raw.processing_mode || "original";schedules=raw.schema===2?{...(raw.material_schedules_local || {})}:{};operation=raw.operation_id || uid();pending=raw.pending || null;
    }
    function draft(){if(legacyDraft)return {...legacyDraft,pending};return {schema:2,materials:[...selected.values()],page_ids:pageIds,pairs:orderedPairs(),active,processing_mode:processing,material_schedules_local:{...schedules},operation_id:operation,pending};}
    async function saveDraft(){await post("/draft",{draft:draft()});dirty=false;$("draftStatus").textContent="草稿已保存到当前账号";}
    function rememberPending(){try{pending?sessionStorage.setItem(storageKey,JSON.stringify(pending)):sessionStorage.removeItem(storageKey);}catch(_){}}
    function renderCaption(){
      const list=orderedPairs();if(!pairs.has(active) || !list.some(p=>pairKey(p.material_id,p.page_id)===active))active=list[0]?pairKey(list[0].material_id,list[0].page_id):"";
      $("captionPair").innerHTML=list.map(p=>`<option value="${esc(pairKey(p.material_id,p.page_id))}">${esc(material(p.material_id)?.material_name || p.material_id)} → ${esc(config(p.page_id)?.page_name || p.page_id)}</option>`).join("") || '<option value="">请先建立对应关系</option>';
      $("captionPair").value=active;const pair=pairs.get(active);$("pairCaption").value=pair?.caption_template || "";$("pairCaption").disabled=!pair;$("resetCaption").disabled=!pair || !config(pair.page_id);$("captionVersion").textContent=pair?`冻结引用 v${pair.config_version}${config(pair.page_id)?.version!==pair.config_version?" · Page 模板已更新，请重新核对":""}`:"";
      $("captionPreview").textContent=pair?expand(pair.caption_template,material(pair.material_id) || {}):"选择配对后查看本次发布文案。";
    }
    function renderPairs(){
      const filter=$("pairLanguage").value,unmatchedOnly=$("unmatchedOnly").checked,rows=[];
      let unmatched=0;for(const m of selected.values()){const own=pageIds.filter(p=>pairs.has(pairKey(m.material_id,p)));if(!own.length)unmatched++;if((filter&&m.language!==filter)||(unmatchedOnly&&own.length))continue;
        rows.push(`<div class="pair-card ${own.length?"":"unmatched"}"><div class="row spread"><strong>${esc(m.material_name)}</strong><span class="badge">${esc(lang(m.language))} · ${own.length?`${own.length} 个 Page`:"尚未分配"}</span></div><div class="pair-pages">${pageIds.map(pid=>{const p=config(pid);const blocked=!p?"Page 配置已移除":reason(p) || (p.language!==m.language?"剧语言不同":"");return `<label class="pick"><input type="checkbox" data-pair-material="${esc(m.material_id)}" data-pair-page="${esc(pid)}"${pairs.has(pairKey(m.material_id,pid))?" checked":""}${blocked?" disabled":""}><span>${esc(p?.page_name || pid)}${blocked?`<small>${esc(blocked)}</small>`:""}</span></label>`;}).join("") || '<p class="helper">请先选择目标 Page</p>'}</div></div>`);
      }
      $("pairRows").innerHTML=rows.join("") || '<div class="empty">暂无符合条件的素材</div>';$("batchSummary").innerHTML=`<span><strong>${selected.size}</strong>条素材</span><span><strong>${pageIds.length}</strong>个 Page</span><span><strong>${orderedPairs().length}</strong>条任务</span><span><strong>${unmatched}</strong>条素材未分配</span>`;
    }
    function render(){
      $("selectedMaterials").innerHTML=[...selected.values()].map((m,i)=>`<div class="material-row"><div><strong><span class="number">${i+1}</span>${esc(m.material_name)}</strong><p class="helper">${esc(m.material_id)} · ${esc(lang(m.language))} · ${Number(m.duration_seconds || 0)} 秒</p><p class="helper">${esc(m.drama_name || "剧名待核对")}</p><label class="field material-schedule"><span>预约时间（北京时间，选填）</span><div class="row"><input type="datetime-local" data-material-time="${esc(m.material_id)}" value="${esc(schedules[m.material_id] || "")}" aria-label="${esc(m.material_name)} 预约时间"><button class="button small" data-clear-time="${esc(m.material_id)}">清空</button></div><span class="helper">留空立即发布；此素材的所有 Page 共用该时间。</span></label></div><div class="actions"><button class="button small" data-move="${esc(m.material_id)}" data-direction="-1"${i===0?" disabled":""} aria-label="上移 ${esc(m.material_name)}">↑</button><button class="button small" data-move="${esc(m.material_id)}" data-direction="1"${i===selected.size-1?" disabled":""} aria-label="下移 ${esc(m.material_name)}">↓</button><button class="button small" data-remove-material="${esc(m.material_id)}">移除</button></div></div>`).join("") || '<div class="empty">尚未选择素材，点击右上角按钮可多选。</div>';
      $("targetPages").innerHTML=configs.map(p=>`<label class="pick"><input type="checkbox" data-page="${esc(p.page_id)}"${pageIds.includes(p.page_id)?" checked":""}${reason(p)?" disabled":""}><span><strong>${esc(p.page_name)}</strong><small>${esc(p.page_id)} · ${esc(lang(p.language))}</small>${reason(p)?`<small>${esc(reason(p))}</small>`:""}</span></label>`).join("") || '<div class="empty">尚未配置 Page，先到 <a href="/fb-pages.html">Page 管理</a> 创建配置。</div>';
      const missing=pageIds.filter(pid=>!config(pid));if(missing.length)$("targetPages").innerHTML+=`<div class="notice warning">以下已选 Page 配置已移除或无权访问：${missing.map(esc).join("、")}。请取消选择或重新配置。</div>`;
      renderPairs();renderCaption();
      document.querySelectorAll('[name="processing"]').forEach(el=>el.checked=el.value===processing);
      $("publishFields").disabled=!!pending||!!legacyDraft;$("preflight").disabled=!!pending||!!legacyDraft;$("saveDraft").disabled=!!pending||!!legacyDraft;$("pendingNotice").hidden=!pending;$("legacyDraftNotice").hidden=!legacyDraft;$("convertDraft").disabled=!!pending;
    }
    function removeMaterial(id){selected.delete(id);delete schedules[id];for(const [key,p] of pairs)if(p.material_id===id)pairs.delete(key);touch();render();}
    function setPages(ids){pageIds=ids;for(const [key,p] of pairs)if(!ids.includes(p.page_id))pairs.delete(key);touch();render();}
    const picker=()=>materialPickerDialog(selected,staged=>{selected=staged;for(const id of Object.keys(schedules))if(!selected.has(id))delete schedules[id];for(const [key,p] of pairs)if(!selected.has(p.material_id))pairs.delete(key);touch();render();});
    $("chooseMaterials").onclick=picker;
    $("selectedMaterials").onclick=e=>{const remove=e.target.closest("[data-remove-material]"),move=e.target.closest("[data-move]");if(remove)removeMaterial(remove.dataset.removeMaterial);if(move){const entries=[...selected],i=entries.findIndex(([id])=>id===move.dataset.move),j=i+Number(move.dataset.direction);if(j>=0&&j<entries.length){[entries[i],entries[j]]=[entries[j],entries[i]];selected=new Map(entries);touch();render();}}};
    $("targetPages").onchange=e=>{const id=e.target.dataset.page;if(id)setPages(e.target.checked?[...new Set([...pageIds,id])]:pageIds.filter(p=>p!==id));};
    $("selectPages").onclick=()=>setPages(configs.filter(p=>!reason(p)).map(p=>p.page_id));$("clearPages").onclick=()=>setPages([]);
    $("refreshConfigs").onclick=e=>busy(e.target,async()=>{try{configs=(await api("/pages")).items || [];render();toast("已刷新 Page 配置；本次文案保留，提交时核对版本");}catch(err){errorAt("pageError",err);}});
    $("matchPairs").onclick=()=>{if(!selected.size||!pageIds.length){errorAt("pageError",new Error("请先选择素材和 Page。"));return;}if(pairs.size&&!confirm("重新匹配会恢复您手动取消的同语言配对，已有文案修改将保留。继续？"))return;for(const m of selected.values())for(const pid of pageIds){const p=config(pid),key=pairKey(m.material_id,pid);if(p&&!reason(p)&&m.language===p.language&&!pairs.has(key))pairs.set(key,makePair(m,p));}touch();render();};
    $("pairRows").onchange=e=>{const mid=e.target.dataset.pairMaterial,pid=e.target.dataset.pairPage;if(!mid||!pid)return;const key=pairKey(mid,pid);if(e.target.checked)pairs.set(key,pairs.get(key)||makePair(material(mid),config(pid)));else pairs.delete(key);touch();renderPairs();renderCaption();};
    $("pairLanguage").onchange=renderPairs;$("unmatchedOnly").onchange=renderPairs;$("captionPair").onchange=e=>{active=e.target.value;renderCaption();};
    $("pairCaption").oninput=e=>{const p=pairs.get(active);if(p){p.caption_template=e.target.value;touch();$("captionPreview").textContent=expand(p.caption_template,material(p.material_id) || {});}};
    $("content").addEventListener("click",e=>{const macro=e.target.closest("[data-macro]");if(macro&&!pending)insertMacro(macro);});
    $("resetCaption").onclick=()=>{const pair=pairs.get(active),p=pair&&config(pair.page_id);if(p&&confirm("用 Page 当前默认文案替换这一配对的本次文案？")){pairs.set(active,makePair(material(pair.material_id),p));touch();renderCaption();}};
    $("convertDraft").onclick=()=>{if(pending)return;legacyDraft=null;schedules={};touch();render();toast("已转换草稿，预约时间已清空，请逐条核对后保存。");};
    $("selectedMaterials").oninput=e=>{const id=e.target.dataset.materialTime;if(id){schedules[id]=e.target.value;touch();}};
    $("selectedMaterials").addEventListener("click",e=>{const btn=e.target.closest("[data-clear-time]");if(btn){delete schedules[btn.dataset.clearTime];touch();render();}});document.querySelectorAll('[name="processing"]').forEach(el=>el.onchange=()=>{processing=el.value;touch();});
    $("saveDraft").onclick=e=>busy(e.target,()=>saveDraft().then(()=>toast("草稿已保存")).catch(err=>errorAt("pageError",err)));
    function payload(){
      if(!selected.size)throw new Error("请至少选择一条素材。");if(!pageIds.length)throw new Error("请至少选择一个 Page。");
      if(legacyDraft)throw new Error("请先明确转换旧版草稿并重新设置预约时间。");
      const rows=orderedPairs();if(rows.length>200)throw new Error("单批最多 200 条任务，请减少素材或 Page。");
      for(const m of selected.values())if(!rows.some(p=>p.material_id===m.material_id))throw new Error(`素材“${m.material_name}”尚未分配 Page，请建立对应关系或移除此素材。`);
      for(const p of rows){const cfg=config(p.page_id);if(!cfg||reason(cfg)||cfg.language!==material(p.material_id)?.language)throw new Error(`Page ${cfg?.page_name || p.page_id} 当前不可发布，请核对授权、配置与语言。`);try{validateTemplate(p.caption_template);}catch(err){active=pairKey(p.material_id,p.page_id);renderCaption();throw err;}}
      const material_schedules={};for(const [id,m] of selected){const local=schedules[id] || "";const date=local?new Date(local+"+08:00"):null;if(local&&(!Number.isFinite(date.getTime())||date.getTime()<=Date.now()))throw new Error(`素材“${m.material_name}”请选择未来的北京时间，或清空后立即发布。`);material_schedules[id]=date?date.toISOString():null;}
      return {schema_version:2,operation_id:operation,material_ids:[...selected.keys()],page_ids:pageIds,pairs:rows,processing_mode:processing,material_schedules};
    }
    async function accepted(result){
      const id=result.batch_id || result.batch?.batch_id;if(!id)throw new Error("服务已响应但未返回批次编号，请查询原操作结果。");
      pending=null;rememberPending();dirty=false;
      try{await post("/draft",{draft:null});}catch(_){}location.href="/fb-video-publish-records.html?batch="+encodeURIComponent(id);
    }
    async function queryOperation(){
      if(!pending)return;try{const result=await api("/operations/"+encodeURIComponent(pending.request.operation_id));await accepted(result);}
      catch(err){$("pendingText").textContent=err.status===404?"尚未查询到原操作的受理记录。请保留本页，按原操作重试；不要新建相同批次。":`暂时无法核对：${err.message}。表单与操作编号已保留。`;}
    }
    async function submitFrozen(request,token,allowRepeat){
      const recovering=!!pending;
      pending={request:structuredClone(request),preview_token:token,allow_repeat:allowRepeat};rememberPending();render();
      try{await saveDraft();}catch(err){if(!recovering){pending=null;rememberPending();render();}errorAt("pageError",new Error((recovering?"原操作仍待核对，草稿保存失败；已保留原操作编号和输入。":"尚未提交：草稿保存失败，请保留输入后重试。")+err.message));return;}
      try{const result=await post("/batches",{...pending.request,preview_token:pending.preview_token,allow_repeat:pending.allow_repeat});await accepted(result);}
      catch(err){
        if(err.data?.accepted===false&&!recovering){pending=null;rememberPending();render();errorAt("pageError",err);await saveDraft().catch(()=>{});return;}
        errorAt("pageError",new Error("提交结果需要核对，已保留原操作编号。"+err.message));await queryOperation();
      }
    }
    $("queryOperation").onclick=e=>busy(e.target,queryOperation);
    $("retryOperation").onclick=e=>busy(e.target,async()=>{
      if(!pending)return;const old=pending;
      try{const result=await api("/operations/"+encodeURIComponent(old.request.operation_id));await accepted(result);return;}catch(err){if(err.status!==404){errorAt("pageError",err);return;}}
      await submitFrozen(old.request,old.preview_token,old.allow_repeat);
    });
    async function confirmation(request,result,retrying=false){
      preview=result;
      const modal=dialog("publishConfirmation","确认本次发布",`<div class="notice">这里只做预检，尚未创建发布任务、真实短链或加工任务。点击“确认提交”后才生成每条任务独立的真实短链。</div><div class="summary"><span><strong>${result.items?.length || 0}</strong>条任务</span><span>${request.processing_mode==="random_overlay"?"随机加工":"原片发布"}</span><span>按每条素材的预约时间执行</span></div><div id="confirmError" class="error" role="alert" hidden></div>${result.repeat_required?'<div class="notice warning"><strong>所选配对包含历史成功发布记录</strong><p>请逐条核对后勾选确认再次发布。活动任务和未知结果不能通过此确认绕过。</p><label class="check"><input type="checkbox" id="allowRepeat">我已核对，确认再次发布这些历史成功配对</label></div>':""}${(result.items || []).map(item=>`<article class="task-card"><h3>${esc(item.material_name || item.material_id)} → ${esc(item.page_name || item.page_id)}</h3><div class="detail-grid"><span>素材 ${esc(item.material_id)}</span><span>Page ${esc(item.page_id)}</span><span>配置 v${esc(item.config_version)}</span><span>${item.schedule_kind==="immediate" || (request.schema_version===2&&!request.material_schedules[item.material_id]) || request.schedule_mode==="now"?"立即发布（提交后按顺序开始）":"预约 "+esc(atBJ(item.planned_at_utc))+" 北京时间"}</span></div><div class="task-caption">${esc(item.caption)}</div><div class="task-link">短链：<span class="mono">${esc(item.short_url || LINK_PLACEHOLDER)}</span>（占位）</div><details><summary>查看长链参数规则</summary><p class="mono">${esc(result.landing_url || LANDING)}</p><p class="helper">任务编号、记录编号和创建时间在最终提交时冻结；以下由服务端预检返回。</p><div class="detail-grid">${Object.entries(item.long_url_parameters || {}).map(([key,value])=>`<span class="mono"><strong>${esc(key)}</strong><br>${esc(value)}</span>`).join("")}</div></details>${item.repeat_required||item.repeated?'<span class="badge bad">历史已成功发布</span>':""}</article>`).join("")}`,`<button class="button" data-close>返回核对</button><button class="button primary" id="confirmSubmit"${result.repeat_required?" disabled":""}>${retrying?"按原操作确认提交":"确认提交"}</button>`);
      if($("allowRepeat"))$("allowRepeat").onchange=e=>$("confirmSubmit").disabled=!e.target.checked;
      $("confirmSubmit").onclick=e=>busy(e.target,async()=>{if(Date.parse(result.expires_at_utc)<=Date.now()){errorAt("confirmError",new Error("预检已过期，请返回后重新核对。"));return;}const allowed=!!$("allowRepeat")?.checked;modal.close();await submitFrozen(request,result.preview_token,allowed);});
    }
    $("preflight").onclick=e=>busy(e.target,async()=>{try{errorAt("pageError","");const request=payload();const result=await post("/preflight",request);await confirmation(request,result);}catch(err){errorAt("pageError",err);}});
    window.addEventListener("beforeunload",e=>{if(dirty&&!pending){e.preventDefault();e.returnValue="";}});
    const [pagesResult,draftResult]=await Promise.all([api("/pages"),api("/draft")]);configs=pagesResult.items || [];restore(draftResult.draft);
    try{const saved=JSON.parse(sessionStorage.getItem(storageKey) || "null");if(saved?.request?.operation_id)pending=saved;}catch(_){}
    render();if(pending)await queryOperation();
  }

  async function records() {
    let cursor="",next="",stack=[],items=[],detailId="",generation=0;
    $("content").innerHTML=`<section class="panel"><div class="section-head"><div><h2>发布批次</h2><p>每条任务保留提交时的素材、Page、文案、加工方式和链接快照。</p></div><button class="button" id="reloadRecords">刷新记录</button></div><div class="table-wrap"><table><thead><tr><th>批次 / 创建时间（北京）</th><th>状态</th><th>任务数</th><th>发布人</th><th>操作</th></tr></thead><tbody id="recordRows"></tbody></table></div><div class="pager"><span class="helper">每页最多 50 个批次</span><div class="actions"><button class="button small" id="recordsPrev">上一页</button><button class="button small" id="recordsNext">下一页</button></div></div></section><section class="panel" id="batchDetail" hidden></section>`;
    async function load(){const gen=++generation;const result=await api("/batches?"+new URLSearchParams({limit:"50",cursor}));if(gen!==generation)return;items=result.items || [];next=result.next_cursor || "";$("recordRows").innerHTML=items.map(b=>`<tr><td><button class="record-open mono" data-batch="${esc(b.batch_id)}">${esc(b.batch_id)}</button><p class="helper">${esc(atBJ(b.created_at_utc))}</p></td><td><span class="badge">${esc(statuses[b.status] || b.status)}</span></td><td>${Number(b.total_tasks ?? b.task_count ?? b.items?.length ?? 0)}</td><td>${esc(b.owner_name || b.owner_user_id || "—")}</td><td><button class="button small" data-batch="${esc(b.batch_id)}">查看详情</button></td></tr>`).join("") || '<tr><td colspan="5" class="empty">尚无发布批次</td></tr>';$("recordsPrev").disabled=!stack.length;$("recordsNext").disabled=!next;}
    function safeLink(url){try{const parsed=new URL(url);return parsed.protocol==="https:"?`<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`:esc(url);}catch(_){return esc(url || "生成中");}}
    async function detail(id){const result=await api("/batches/"+encodeURIComponent(id)),b=result.batch;detailId=id;$("batchDetail").hidden=false;$("batchDetail").innerHTML=`<div class="section-head"><div><h2>批次详情</h2><p class="mono">${esc(b.batch_id)} · ${esc(statuses[b.status] || b.status)}</p></div><button class="button small" id="refreshDetail">刷新详情</button></div><div id="detailError" class="error" role="alert" hidden></div><p class="helper">创建时间 ${esc(atBJ(b.created_at_utc))}（北京） · ${b.processing_mode==="random_overlay"?"随机加工":"原片发布"}</p>${(b.items || []).map(t=>`<article class="task-card"><div class="row spread"><h3>${esc(t.material_name || t.material?.material_name || t.material_id)} → ${esc(t.page_name || t.page?.page_name || t.page_id)}</h3><span class="badge ${t.status==="published"?"good":["unknown","failed","blocked"].includes(t.status)?"bad":""}">${esc(statuses[t.status] || t.status)}</span></div><div class="detail-grid"><span class="mono">任务 ${esc(t.task_id)}</span><span>发布记录 ${esc(t.ledger_id)}</span><span>Page 内第 ${esc(t.page_sequence)} 条 · 模板 v${esc(t.template_version)}</span><span>计划时间 ${esc(atBJ(t.planned_at_utc))}（北京）</span></div><div class="task-caption">${esc(t.caption)}</div><div class="task-link">真实短链：${safeLink(t.short_url)}</div><details><summary>查看长链参数与冻结文案</summary><pre>${esc(t.long_url || "生成中")}</pre><pre>${esc(t.caption_template)}</pre></details>${t.error_code||t.error_message?`<div class="notice warning"><strong>${esc(t.error_code)}</strong><p>${esc(t.error_message)}</p></div>`:""}${t.graph_post_id?`<p class="helper mono">平台发布编号 ${esc(t.graph_post_id)}</p>`:""}${t.can_cancel?`<div class="actions"><button class="button small danger" data-cancel-task="${esc(t.task_id)}">取消此任务</button></div>`:""}</article>`).join("")}`;
      $("refreshDetail").onclick=e=>busy(e.target,()=>detail(id).catch(err=>errorAt("detailError",err)));
      $("batchDetail").onclick=e=>{const btn=e.target.closest("[data-cancel-task]");if(btn)busy(btn,async()=>{if(!confirm("取消这条尚未发送的任务？同批其他任务继续保留。"))return;try{await post("/tasks/"+encodeURIComponent(btn.dataset.cancelTask)+"/cancel",{});await detail(id);await load();toast("任务已取消");}catch(err){errorAt("detailError",err);}});};
    }
    $("recordRows").onclick=e=>{const btn=e.target.closest("[data-batch]");if(btn)detail(btn.dataset.batch).then(()=>$("batchDetail").scrollIntoView({behavior:"smooth"})).catch(err=>errorAt("pageError",err));};
    $("reloadRecords").onclick=e=>busy(e.target,async()=>{try{await load();if(detailId)await detail(detailId);errorAt("pageError","");}catch(err){errorAt("pageError",err);}});
    $("recordsNext").onclick=()=>{stack.push(cursor);cursor=next;load().catch(err=>errorAt("pageError",err));};$("recordsPrev").onclick=()=>{cursor=stack.pop() || "";load().catch(err=>errorAt("pageError",err));};
    await load();const initial=new URLSearchParams(location.search).get("batch");if(initial)await detail(initial);
  }
  boot();
})();
