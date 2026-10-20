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
      await QuickNav.render({container:"#quickNav",auth,activeKey:navKey});
      UiTopbar.render({auth,userCard:"#userCard",authButton:"#authButton",refreshButton:"#refreshPage"});
      $("authButton").onclick=()=>UiTopbar.handleAuthAction({auth,api});
      if (!auth.authenticated) { $("gate").innerHTML="<strong>请先登录 AI 自动后台</strong><p>登录后可查看 Page 配置和发布记录。</p>"; return; }
      if (!(auth.user?.is_admin || auth.user?.permissions?.fb_video_publish)) { $("gate").innerHTML="<strong>当前账号没有 Facebook 视频发布权限</strong><p>请联系管理员在用户管理中分配“Facebook 视频发布”。</p>"; return; }
      const result=await api("/languages"); for(const item of result.items || []) languages.set(String(item.value),item.label);
      $("gate").hidden=true;$("content").hidden=false;
      if(view==="pages")await pageManagement();else if(view==="publish")await publishing();else await records();
    } catch(error) { $("gate").hidden=true; errorAt("pageError",error); }
  }

  async function pageManagement() {
    let configs=[],page=0,query="";
    $("content").innerHTML=`<section class="panel"><div class="section-head"><div><h2>已配置 Page</h2><p>创建时可选择多个 Page，保存后每个 Page 独立管理。</p></div><button id="createPage" class="button primary">创建</button></div><div class="filters"><input id="pageSearch" placeholder="搜索 Page 名称或 ID" aria-label="搜索已配置 Page"><button id="reloadPages" class="button">刷新列表</button></div><div class="table-wrap"><table><thead><tr><th>Page 名称 / ID</th><th>剧语言</th><th>授权状态</th><th>发布文案</th><th>更新时间（北京）</th><th>操作</th></tr></thead><tbody id="pageRows"></tbody></table></div><div class="pager"><span class="helper" id="pageTotal"></span><div class="actions"><button class="button small" id="pagePrev">上一页</button><button class="button small" id="pageNext">下一页</button></div></div></section>`;
    function render() {
      const rows=configs.filter(p=>`${p.page_name} ${p.page_id}`.toLowerCase().includes(query.toLowerCase()));
      page=Math.min(page,Math.max(0,Math.ceil(rows.length/20)-1));
      $("pageRows").innerHTML=rows.slice(page*20,(page+1)*20).map(p=>`<tr><td><strong>${esc(p.page_name)}</strong><br><span class="mono">${esc(p.page_id)}</span></td><td>${esc(lang(p.language))}</td><td><span class="badge ${p.blocked_reason || !p.eligible_token_count?"bad":"good"}">${p.blocked_reason || !p.eligible_token_count?"不可发布":"授权可用"}</span>${p.blocked_reason?`<p class="helper">${esc(p.blocked_reason)}</p>`:""}</td><td><div class="caption-summary">${esc(p.caption_template)}</div><span class="helper">v${p.version}</span></td><td class="no-break">${esc(atBJ(p.updated_at_utc))}</td><td><div class="actions"><button class="button small" data-edit-page="${esc(p.page_id)}">编辑</button><button class="button small danger" data-delete-page="${esc(p.page_id)}">删除</button></div></td></tr>`).join("") || `<tr><td colspan="6" class="empty">${query?"没有匹配的 Page":"尚未配置 Page，点击“创建”设置文案、剧语言和 Page。"}</td></tr>`;
      $("pageTotal").textContent=`共 ${rows.length} 个 Page · 第 ${page+1} 页`;$("pagePrev").disabled=page===0;$("pageNext").disabled=(page+1)*20>=rows.length;
    }
    async function load() { const data=await api("/pages");configs=data.items || [];render();errorAt("pageError",""); }
    $("pageSearch").oninput=e=>{query=e.target.value;page=0;render();};$("pagePrev").onclick=()=>{page--;render();};$("pageNext").onclick=()=>{page++;render();};
    $("reloadPages").onclick=e=>busy(e.target,()=>load().catch(err=>errorAt("pageError",err)));
    async function editor(existing) {
      let chosen=new Set(),available=[],generation=0,searchTimer;
      const modal=dialog("pageEditor",existing?"编辑 Page":"创建 Page 配置",`<div class="notice">配置在同租户内共用。其他有权访问此 Page 的用户也会使用保存的语言和文案；已提交的任务保持冻结内容。</div><div id="editorError" class="error" role="alert" hidden></div><div class="form-grid"><label class="field"><span>剧语言</span><select id="editLanguage">${langOptions("请选择剧语言",existing?.language)}</select></label><div class="field"><span>落地页</span><div class="notice mono">${LANDING}</div></div><div class="field"><label for="editCaption">发布文案</label><textarea id="editCaption" rows="8">${esc(existing?.caption_template || "")}</textarea>${macroButtons("editCaption")}<p class="helper">仅支持三个宏参数。{url} 表示每次发布独立生成的短链。</p></div><div class="field"><span>示例文案预览</span><div id="editPreview" class="copy-preview"></div><span class="helper">使用示例剧目演示宏替换，不绑定发布素材、不生成真实链接。</span></div>${existing?`<div class="full notice"><strong>${esc(existing.page_name)}</strong> · ${esc(existing.page_id)} · 当前版本 v${existing.version}</div>`:`<div class="full"><div class="filters"><input id="availableSearch" placeholder="搜索可访问的 Page 名称或 ID" aria-label="搜索可访问的 Page"><button class="button" id="searchAvailable">搜索</button></div><div class="helper" id="availableCount"></div><div class="pick-list" id="availablePages"></div></div>`}</div>`,`<button class="button" data-close>取消</button><button class="button primary" id="savePage">${existing?"保存修改":"创建所选 Page"}</button>`);
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
      const edit=e.target.closest("[data-edit-page]"),del=e.target.closest("[data-delete-page]");
      if(edit)editor({...configs.find(p=>p.page_id===edit.dataset.editPage)});
      if(del)busy(del,async()=>{const item=configs.find(p=>p.page_id===del.dataset.deletePage);if(!confirm(`删除 ${item.page_name} 的共享配置？\n同租户其他用户也将失去此配置。不会删除 Facebook Page、授权或历史任务。`))return;try{await post(`/pages/${encodeURIComponent(item.page_id)}/delete`,{config_instance_id:item.config_instance_id,expected_version:item.version});await load();toast("已删除共享配置");}catch(err){errorAt("pageError",err);}});
    };
    await load();
  }

  async function publishing() {
    let configs=[],selected=new Map(),pageIds=[],pairs=new Map(),active="",operation=uid(),pending=null,dirty=false,preview=null;
    let processing="original",schedule="now",scheduled="",interval=0;
    const storageKey=`fb-video-pending:${auth.user?.tenant_key || ""}:${auth.user?.user_id || ""}`;
    $("content").innerHTML=`<div id="pendingNotice" class="notice warning" hidden><strong>上次提交的受理结果正在核对</strong><p id="pendingText">请查询原操作结果，避免重复发布。</p><div class="actions"><button class="button" id="queryOperation">查询原操作</button><button class="button" id="retryOperation">按原操作重试</button></div></div><fieldset id="publishFields"><section class="panel"><div class="section-head"><div><h2>01 选择素材</h2><p>素材来自现有 Facebook 素材库，支持跨页多选。</p></div><button id="chooseMaterials" class="button primary">选择素材（可多选）</button></div><div id="selectedMaterials"></div></section><section class="panel"><div class="section-head"><div><h2>02 选择 Page</h2><p>仅已配置且剧语言匹配的 Page 可选；异常授权显示原因。</p></div><a class="button" href="/fb-pages.html">管理 Page</a></div><div class="row"><button class="button small" id="selectPages">全选可用 Page</button><button class="button small" id="clearPages">取消全选</button><button class="button small" id="refreshConfigs">刷新配置</button></div><div id="targetPages" class="pick-list"></div></section><section class="panel" id="pairSection"><div class="section-head"><div><h2>03 核对素材与 Page 对应关系</h2><p>一个勾选代表一条任务；同一素材可发到多个同语言 Page。</p></div><button class="button primary" id="matchPairs">按剧语言匹配</button></div><div class="summary" id="batchSummary"></div><div class="filters"><select id="pairLanguage" aria-label="筛选配对语言">${langOptions("全部剧语言")}</select><label class="check"><input type="checkbox" id="unmatchedOnly">只看未分配素材</label></div><div id="pairRows"></div></section><section class="panel"><div class="section-head"><div><h2>04 逐条编辑发布文案</h2><p>本次修改只影响当前“素材 × Page”，不会改写 Page 默认文案。</p></div></div><div class="field"><label for="captionPair">当前编辑配对</label><select id="captionPair"></select></div><div class="form-grid" style="margin-top:16px"><div class="field"><label for="pairCaption">本次发布文案</label><textarea id="pairCaption" rows="7"></textarea>${macroButtons("pairCaption")}<div class="row spread"><span class="helper" id="captionVersion"></span><button id="resetCaption" class="button small">重新应用当前 Page 文案</button></div></div><div class="field"><span>文案预览</span><div class="copy-preview" id="captionPreview"></div><span class="helper">短链为占位，最终提交时生成；简介和字节数由服务端确认。</span></div></div></section><section class="panel"><div class="section-head"><div><h2>05 视频加工与发布时间</h2><p>视频加工与历史重复发布校验分别处理。</p></div></div><div class="processing"><label class="pick"><input type="radio" name="processing" value="original" checked><span><strong>不去重（原片发布）</strong><small>直接使用通过校验的源视频</small></span></label><label class="pick"><input type="radio" name="processing" value="random_overlay"><span><strong>随机加工</strong><small>使用现有 Facebook 随机加工流程，每条任务冻结加工设置</small></span></label></div><div class="form-grid" style="margin-top:18px"><label class="field"><span>发布方式</span><select id="scheduleMode"><option value="now">提交后立即开始</option><option value="scheduled">预约开始时间</option></select></label><label class="field"><span>同一 Page 发布间隔（分钟）</span><input id="intervalMinutes" type="number" min="0" max="1440" step="1" value="0"></label><label class="field" id="scheduleField" hidden><span>预约开始时间（北京时间 UTC+08:00）</span><input id="scheduledAt" type="datetime-local"></label></div><p class="helper">按素材顺序依次发布；同批次同 Page 成功后至少等待设定间隔，加工或平台处理可继续延后。</p></section></fieldset><div class="sticky-actions actions"><span class="helper" id="draftStatus">草稿保存到当前账号</span><button class="button" id="saveDraft">保存草稿</button><button class="button primary" id="preflight">核对并确认发布</button></div>`;
    const material = id => selected.get(String(id));
    const config = id => configs.find(p=>p.page_id===String(id));
    const reason = p => p.blocked_reason || (!p.eligible_token_count?"暂无有效发布授权":!p.language?"尚未设置剧语言":selected.size&&![...selected.values()].some(m=>m.language===p.language)?"与所选素材剧语言不匹配":"");
    function touch(){dirty=true;preview=null;operation=uid();$("draftStatus").textContent="有未保存的修改";}
    function orderedPairs(){return [...selected.keys()].flatMap(m=>pageIds.map(p=>pairs.get(pairKey(m,p))).filter(Boolean));}
    function makePair(m,p){return {material_id:m.material_id,page_id:p.page_id,config_instance_id:p.config_instance_id,config_version:p.version,caption_template:p.caption_template};}
    function restore(raw){
      if(!raw || raw.schema!==1)return;
      selected=new Map((raw.materials || []).map(m=>[String(m.material_id),m]));pageIds=[...new Set(raw.page_ids || [])];
      pairs=new Map((raw.pairs || []).filter(p=>selected.has(p.material_id)&&pageIds.includes(p.page_id)).map(p=>[pairKey(p.material_id,p.page_id),p]));
      active=raw.active || "";processing=raw.processing_mode || "original";schedule=raw.schedule_mode || "now";scheduled=raw.scheduled_local || "";interval=raw.interval_minutes ?? 0;operation=raw.operation_id || uid();pending=raw.pending || null;
    }
    function draft(){return {schema:1,materials:[...selected.values()],page_ids:pageIds,pairs:orderedPairs(),active,processing_mode:processing,schedule_mode:schedule,scheduled_local:scheduled,interval_minutes:interval,operation_id:operation,pending};}
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
      $("selectedMaterials").innerHTML=[...selected.values()].map((m,i)=>`<div class="material-row"><div><strong><span class="number">${i+1}</span>${esc(m.material_name)}</strong><p class="helper">${esc(m.material_id)} · ${esc(lang(m.language))} · ${Number(m.duration_seconds || 0)} 秒</p><p class="helper">${esc(m.drama_name || "剧名待核对")}</p></div><div class="actions"><button class="button small" data-move="${esc(m.material_id)}" data-direction="-1"${i===0?" disabled":""} aria-label="上移 ${esc(m.material_name)}">↑</button><button class="button small" data-move="${esc(m.material_id)}" data-direction="1"${i===selected.size-1?" disabled":""} aria-label="下移 ${esc(m.material_name)}">↓</button><button class="button small" data-remove-material="${esc(m.material_id)}">移除</button></div></div>`).join("") || '<div class="empty">尚未选择素材，点击右上角按钮可多选。</div>';
      $("targetPages").innerHTML=configs.map(p=>`<label class="pick"><input type="checkbox" data-page="${esc(p.page_id)}"${pageIds.includes(p.page_id)?" checked":""}${reason(p)?" disabled":""}><span><strong>${esc(p.page_name)}</strong><small>${esc(p.page_id)} · ${esc(lang(p.language))}</small>${reason(p)?`<small>${esc(reason(p))}</small>`:""}</span></label>`).join("") || '<div class="empty">尚未配置 Page，先到 <a href="/fb-pages.html">Page 管理</a> 创建配置。</div>';
      const missing=pageIds.filter(pid=>!config(pid));if(missing.length)$("targetPages").innerHTML+=`<div class="notice warning">以下已选 Page 配置已移除或无权访问：${missing.map(esc).join("、")}。请取消选择或重新配置。</div>`;
      renderPairs();renderCaption();$("scheduleMode").value=schedule;$("scheduledAt").value=scheduled;$("intervalMinutes").value=interval;$("scheduleField").hidden=schedule!=="scheduled";
      document.querySelectorAll('[name="processing"]').forEach(el=>el.checked=el.value===processing);
      $("publishFields").disabled=!!pending;$("preflight").disabled=!!pending;$("saveDraft").disabled=!!pending;$("pendingNotice").hidden=!pending;
    }
    function removeMaterial(id){selected.delete(id);for(const [key,p] of pairs)if(p.material_id===id)pairs.delete(key);touch();render();}
    function setPages(ids){pageIds=ids;for(const [key,p] of pairs)if(!ids.includes(p.page_id))pairs.delete(key);touch();render();}
    async function picker(){
      let staged=new Map(selected),items=[],cursor="",next="",stack=[],generation=0;
      const modal=dialog("materialPicker","选择素材（可多选）",`<div id="pickerError" class="error" role="alert" hidden></div><div class="filters"><input id="materialSearch" placeholder="搜索素材名称、ID 或剧名" aria-label="搜索素材"><select id="materialLanguage" aria-label="素材语言">${langOptions("全部剧语言")}</select><button id="materialSearchButton" class="button">搜索</button></div><div class="row spread"><span class="helper" id="pickerCount"></span><div class="actions"><button id="pickCurrent" class="button small">全选当前页</button><button id="clearCurrent" class="button small">取消当前页</button></div></div><div id="materialChoices" class="pick-list"></div><div class="pager"><span class="helper">跨页和搜索保留已选项</span><div class="actions"><button id="materialsPrev" class="button small">上一页</button><button id="materialsNext" class="button small">下一页</button></div></div>`,`<button class="button" data-close>取消</button><button id="applyMaterials" class="button primary">使用所选素材</button>`);
      function paint(){$("materialChoices").innerHTML=items.map(m=>`<label class="pick"><input type="checkbox" data-material="${esc(m.material_id)}"${staged.has(m.material_id)?" checked":""}><span><strong>${esc(m.material_name)}</strong><small>${esc(m.material_id)} · ${esc(lang(m.language))} · ${Number(m.duration_seconds || 0)} 秒</small><small>${esc(m.drama_name || "")}</small></span></label>`).join("") || '<div class="empty">没有符合条件的素材</div>';$("pickerCount").textContent=`已选择 ${staged.size} 条素材`;$("materialsPrev").disabled=!stack.length;$("materialsNext").disabled=!next;}
      async function load(){const gen=++generation;errorAt("pickerError","");const query=new URLSearchParams({q:$("materialSearch").value,language:$("materialLanguage").value,cursor,limit:"50"});try{const data=await api("/materials?"+query);if(!modal.isConnected||gen!==generation)return;items=data.items || [];next=data.next_cursor || "";paint();}catch(err){if(modal.isConnected&&gen===generation)errorAt("pickerError",err);}}
      $("materialChoices").onchange=e=>{const id=e.target.dataset.material;if(id){e.target.checked?staged.set(id,items.find(m=>m.material_id===id)):staged.delete(id);$("pickerCount").textContent=`已选择 ${staged.size} 条素材`;}};
      $("materialSearchButton").onclick=()=>{cursor="";stack=[];load();};$("materialLanguage").onchange=()=>{cursor="";stack=[];load();};$("materialSearch").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();$("materialSearchButton").click();}};
      $("materialsNext").onclick=()=>{stack.push(cursor);cursor=next;load();};$("materialsPrev").onclick=()=>{cursor=stack.pop() || "";load();};
      $("pickCurrent").onclick=()=>{items.forEach(m=>staged.set(m.material_id,m));paint();};$("clearCurrent").onclick=()=>{items.forEach(m=>staged.delete(m.material_id));paint();};
      $("applyMaterials").onclick=()=>{selected=staged;for(const [key,p] of pairs)if(!selected.has(p.material_id))pairs.delete(key);touch();render();modal.close();};await load();
    }
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
    $("scheduleMode").onchange=e=>{schedule=e.target.value;touch();$("scheduleField").hidden=schedule!=="scheduled";};$("scheduledAt").oninput=e=>{scheduled=e.target.value;touch();};$("intervalMinutes").oninput=e=>{interval=e.target.value;touch();};document.querySelectorAll('[name="processing"]').forEach(el=>el.onchange=()=>{processing=el.value;touch();});
    $("saveDraft").onclick=e=>busy(e.target,()=>saveDraft().then(()=>toast("草稿已保存")).catch(err=>errorAt("pageError",err)));
    function payload(){
      if(!selected.size)throw new Error("请至少选择一条素材。");if(!pageIds.length)throw new Error("请至少选择一个 Page。");
      if(!Number.isInteger(Number(interval))||Number(interval)<0||Number(interval)>1440||String(interval).trim()==="")throw new Error("发布间隔须为 0–1440 的整数分钟。");
      const rows=orderedPairs();if(rows.length>200)throw new Error("单批最多 200 条任务，请减少素材或 Page。");
      for(const m of selected.values())if(!rows.some(p=>p.material_id===m.material_id))throw new Error(`素材“${m.material_name}”尚未分配 Page，请建立对应关系或移除此素材。`);
      for(const p of rows){const cfg=config(p.page_id);if(!cfg||reason(cfg)||cfg.language!==material(p.material_id)?.language)throw new Error(`Page ${cfg?.page_name || p.page_id} 当前不可发布，请核对授权、配置与语言。`);try{validateTemplate(p.caption_template);}catch(err){active=pairKey(p.material_id,p.page_id);renderCaption();throw err;}}
      let planned="";if(schedule==="scheduled"){const date=new Date(scheduled+"+08:00");if(!scheduled||!Number.isFinite(date.getTime())||date.getTime()<=Date.now())throw new Error("请选择未来的北京时间。");planned=date.toISOString();}
      return {operation_id:operation,material_ids:[...selected.keys()],page_ids:pageIds,pairs:rows,processing_mode:processing,schedule_mode:schedule,scheduled_at_utc:planned,interval_minutes:Number(interval)};
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
      const modal=dialog("publishConfirmation","确认本次发布",`<div class="notice">这里只做预检，尚未创建发布任务、真实短链或加工任务。点击“确认提交”后才生成每条任务独立的真实短链。</div><div class="summary"><span><strong>${result.items?.length || 0}</strong>条任务</span><span>${request.processing_mode==="random_overlay"?"随机加工":"原片发布"}</span><span>同 Page 间隔 ${request.interval_minutes} 分钟</span></div><div id="confirmError" class="error" role="alert" hidden></div>${result.repeat_required?'<div class="notice warning"><strong>所选配对包含历史成功发布记录</strong><p>请逐条核对后勾选确认再次发布。活动任务和未知结果不能通过此确认绕过。</p><label class="check"><input type="checkbox" id="allowRepeat">我已核对，确认再次发布这些历史成功配对</label></div>':""}${(result.items || []).map(item=>`<article class="task-card"><h3>${esc(item.material_name || item.material_id)} → ${esc(item.page_name || item.page_id)}</h3><div class="detail-grid"><span>素材 ${esc(item.material_id)}</span><span>Page ${esc(item.page_id)}</span><span>配置 v${esc(item.config_version)}</span><span>${request.schedule_mode==="now"?"提交后按顺序开始":esc(atBJ(item.planned_at_utc))+" 北京时间"}</span></div><div class="task-caption">${esc(item.caption)}</div><div class="task-link">短链：<span class="mono">${esc(item.short_url || LINK_PLACEHOLDER)}</span>（占位）</div><details><summary>查看长链参数规则</summary><p class="mono">${esc(result.landing_url || LANDING)}</p><p class="helper">任务编号、记录编号和创建时间在最终提交时冻结；以下由服务端预检返回。</p><div class="detail-grid">${Object.entries(item.long_url_parameters || {}).map(([key,value])=>`<span class="mono"><strong>${esc(key)}</strong><br>${esc(value)}</span>`).join("")}</div></details>${item.repeat_required||item.repeated?'<span class="badge bad">历史已成功发布</span>':""}</article>`).join("")}`,`<button class="button" data-close>返回核对</button><button class="button primary" id="confirmSubmit"${result.repeat_required?" disabled":""}>${retrying?"按原操作确认提交":"确认提交"}</button>`);
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
