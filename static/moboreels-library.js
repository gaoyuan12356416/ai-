(function (root) {
  "use strict";
  const API = "/api/moboreels";
  const LANGUAGES = {2:"繁体中文",3:"英语",4:"西班牙语",5:"葡萄牙语",6:"法语",7:"俄语",8:"意大利语",9:"日语",10:"阿拉伯语",11:"印尼语",12:"泰语",13:"越南语",14:"韩语",15:"菲律宾语",16:"德语",17:"印地语",18:"孟加拉语",21:"马来西亚语",22:"土耳其语"};
  const ACTIVE_RUN = new Set(["queued", "running"]);
  const ACTIVE_PROMOTION = new Set(["creating", "pending", "sending"]);
  const GOOD_PROMOTION = new Set(["succeeded", "success"]);
  function escapeHtml(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
  function safeUrl(value) {
    try { const url = new URL(String(value || "")); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : ""; } catch (_) { return ""; }
  }
  function text(value) { return value == null || value === "" || value === "-" ? "—" : String(value); }
  function number(value) { const n = Number(value); return Number.isFinite(n) && n >= 0 ? n : 0; }
  function count(value) { return number(value).toLocaleString("zh-CN"); }
  function language(id) { return LANGUAGES[Number(id)] || (Number(id) === -1 ? "未提供语言" : `未知语言（${text(id)}）`); }
  function fee(value) { return Number(value) > 0 ? `第 ${Number(value)} 集` : "未提供"; }
  function formatUTC(value) {
    if (value == null || value === "") return "—";
    let dateValue = value;
    if (typeof value === "number") dateValue = value < 1000000000000 ? value * 1000 : value;
    else if (/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d/.test(value) && !/(Z|[+-]\d\d:\d\d)$/i.test(value)) dateValue = value.replace(" ", "T") + "Z";
    const date = new Date(dateValue);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", { timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false }).format(date);
  }
  function promotionButton(row) {
    const status = row.promotion_status || (row.promotion && row.promotion.status) || "";
    if (GOOD_PROMOTION.has(status)) return "查看推广链接";
    if (ACTIVE_PROMOTION.has(status)) return "查看生成进度";
    if (status === "unknown") return "结果待核对";
    if (status === "rejected") return "重新生成";
    return Number(row.is_show) === 1 ? "生成推广链接" : "查看历史链接";
  }
  function runModel(run) {
    if (!run) return {label:"暂无同步记录", kind:"", active:false, progress:0, detail:""};
    const active = ACTIVE_RUN.has(run.status);
    const stage = {fetching:"正在获取剧目",writing:"正在更新剧库",verifying:"正在核对数据"}[run.stage] || "正在准备同步";
    const pages = number(run.total_pages), fetched = number(run.fetched_pages);
    const rows = number(run.total_rows), processed = number(run.processed_rows);
    let progress = 0, detail = "";
    if (run.stage === "fetching") { progress = pages ? Math.min(50, fetched / pages * 50) : 0; detail = `已读取 ${count(fetched)}${pages ? ` / ${count(pages)}` : ""} 页`; }
    else if (run.stage === "writing") { progress = 50 + (rows ? Math.min(45, processed / rows * 45) : 0); detail = `已处理 ${count(processed)} / ${count(rows)} 条剧目`; }
    else if (run.stage === "verifying") { progress = 95; detail = `正在核对 ${count(rows)} 条剧目`; }
    if (run.status === "succeeded") return {label:"同步完成",kind:"success",active:false,progress:100,detail:`本次核对 ${count(rows)} 条剧目`};
    if (run.status === "failed") return {label:"同步未完成",kind:"error",active:false,progress,detail};
    return {label:run.status === "queued" ? "等待开始" : stage,kind:"running",active,progress,detail};
  }
  function renderRow(row) {
    const id = escapeHtml(row.story_id), image = safeUrl(row.img_src), shown = Number(row.is_show) === 1;
    return `<tr><td><div class="drama-cell"><div class="cover-wrap">${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(text(row.story_name))}封面" loading="lazy" referrerpolicy="no-referrer">` : "暂无封面"}</div><div><a class="drama-name" href="#" data-action="detail" data-story-id="${id}">${escapeHtml(text(row.story_name))}</a><span class="story-id">ID ${id}</span></div></div></td><td class="nowrap">${escapeHtml(language(row.lang_id))}</td><td>${escapeHtml(text(row.local_type_text || {1:"本土剧",2:"译制剧",4:"动态漫"}[row.local_type]))}<span class="cell-sub">${escapeHtml(text(row.audio_type_text || {1:"原声剧",2:"配音剧"}[row.audio_type]))}</span></td><td class="nowrap">${escapeHtml(fee(row.payepisfrom))}</td><td><span class="badge ${shown ? "success" : ""}">${shown ? "上架" : "下架 / 隐藏"}</span></td><td class="nowrap">${escapeHtml(text(row.source_publish_time || row.publish_time))}</td><td><div class="row-actions"><button class="btn mini" type="button" data-action="detail" data-story-id="${id}">详情</button><button class="btn mini ${shown ? "btn-primary" : ""}" type="button" data-action="promotion" data-story-id="${id}">${promotionButton(row)}</button></div></td></tr>`;
  }
  function field(label, value) { return `<div class="detail-field"><span>${escapeHtml(label)}</span>${escapeHtml(text(value))}</div>`; }
  function detailHtml(row) {
    const netdisk = row.baidu_link || row.baiduLink || "", diskCode = row.baidu_link_code || row.baiduLinkCode || "";
    return `<div class="detail-grid">${field("剧目 ID",row.story_id)}${field("语言",language(row.lang_id))}${field("状态",Number(row.is_show) === 1 ? "上架" : "下架 / 隐藏")}${field("分类",row.c_name)}${field("频道",row.channel_text)}${field("短剧类型",row.local_type_text)}${field("音轨类型",row.audio_type_text)}${field("版权类型",row.holder_text)}${field("收费起始集",fee(row.payepisfrom))}${field("完结状态",row.is_full_text)}${field("来源上架时间",row.source_publish_time || row.publish_time)}${field("最近同步（北京时间）",formatUTC(row.last_seen_at))}</div><section class="detail-section"><h3>剧情简介</h3><p class="description">${escapeHtml(text(row.introduce))}</p></section><section class="detail-section"><h3>网盘资料</h3><div class="share-info">${escapeHtml(text(netdisk))}</div>${diskCode && diskCode !== "-" ? `<p class="muted">提取码：${escapeHtml(diskCode)}</p>` : ""}${netdisk && netdisk !== "-" ? `<button class="btn mini" data-copy="netdisk" type="button">复制网盘资料</button>` : ""}</section><div class="detail-footer"><button class="btn btn-primary" type="button" data-action="promotion" data-story-id="${escapeHtml(row.story_id)}">${promotionButton(row)}</button><span class="muted">推广链接归属于当前登录用户。</span></div>`;
  }
  function linkCard(label, value, key, isCode) {
    const safe = isCode ? String(value || "") : safeUrl(value);
    return `<div class="link-card"><div class="link-card-head"><span>${label}</span>${safe ? `<button class="btn mini" data-copy="${key}" type="button">复制</button>` : ""}</div>${isCode ? `<span class="link-value code-value">${escapeHtml(text(safe))}</span>` : safe ? `<a class="link-value" href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${escapeHtml(safe)}</a>` : `<span class="link-value">未提供有效链接</span>`}</div>`;
  }
  function promotionHtml(promotion, row, currentName) {
    const attribution = `<p class="promotion-intro">推广归属：${escapeHtml(text((promotion && promotion.star_name) || currentName))}<br>同一用户对同一部短剧复用已有推广链接。</p>`;
    if (!promotion) return attribution + `<div class="notice">${Number(row.is_show) === 1 ? "尚未生成推广链接。" : "该剧已下架，暂不能生成新链接；当前用户没有已保存的历史链接。"}</div>`;
    if (GOOD_PROMOTION.has(promotion.status)) return attribution + linkCard("推广口令",promotion.koc_code,"koc_code",true) + linkCard("剧目推广链接",promotion.deep_link,"deep_link",false) + linkCard("主页链接",promotion.home_link,"home_link",false) + `<p class="promotion-result-note">生成时间（北京时间）：${escapeHtml(formatUTC(promotion.created_at))}${Number(row.is_show) !== 1 ? "<br>该剧目前已下架，以上为历史生成结果。" : ""}</p>`;
    if (ACTIVE_PROMOTION.has(promotion.status)) return attribution + '<div class="notice">推广链接正在生成，页面会自动更新结果，请勿重复提交。</div>';
    if (promotion.status === "unknown") return attribution + '<div class="notice">该请求的创建结果待核对。请联系管理员核对原口令和链接，核对完成前不会再次创建。</div><p class="muted">其他剧目仍可正常生成推广链接。</p>';
    if (promotion.status === "rejected") return attribution + `<div class="notice error">${escapeHtml(promotion.message || promotion.error_message || "本次请求未成功，可稍后重试。")}</div>${Number(row.is_show) === 1 ? '<button class="btn btn-primary" type="button" data-action="retry-promotion">重试生成</button>' : ""}`;
    return attribution + '<div class="notice">推广结果暂不可用，请刷新查看或联系管理员核对。</div>';
  }
  function createPromotionLoader(api) {
    const inFlight = new Map();
    return function load(row, retry) {
      const key = String(row.story_id);
      if (inFlight.has(key)) return inFlight.get(key);
      const path = `${API}/dramas/${encodeURIComponent(key)}/promotion`;
      const promise = (async () => {
        const existing = await api(path);
        if (!retry && existing.promotion) return existing;
        if (retry && existing.promotion && existing.promotion.status !== "rejected") return existing;
        if (Number(row.is_show) !== 1) return existing;
        return api(path,{method:"POST",body:"{}"});
      })().finally(() => inFlight.delete(key));
      inFlight.set(key,promise);
      return promise;
    };
  }

  async function boot() {
    const doc = root.document, byId = id => doc.getElementById(id);
    const ids = ["quickNav","userCard","loginBtn","syncBtn","syncStatus","lastSuccess","syncUpdated","syncProgressWrap","syncProgress","syncProgressText","syncCounts","syncError","accessNotice","resultCount","refreshBtn","filterForm","filterFields","searchInput","languageFilter","statusFilter","typeFilter","audioFilter","resetFiltersBtn","listError","dramaRows","pageSize","pageSummary","pageNumber","prevPage","nextPage","detailDialog","detailTitle","detailBody","promotionDialog","promotionTitle","promotionBody","toast"];
    const el = Object.fromEntries(ids.map(id => [id,byId(id)]));
    const state = {auth:{},allowed:false,rows:[],total:0,page:1,size:50,listSequence:0,listBusy:false,sync:null,lastSuccess:null,syncTimer:null,promotionTimer:null,promotionSequence:0,promotionRow:null,promotion:null,detail:null,detailSequence:0};
    let toastTimer;
    function toast(message, isError) { el.toast.textContent=message;el.toast.classList.toggle("error",!!isError);el.toast.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.toast.classList.remove("show"),isError?5000:2500); }
    function stopPolling() { clearTimeout(state.syncTimer);clearTimeout(state.promotionTimer); }
    function permissionAllowed() { const user=state.auth.user;return !!(state.auth.authenticated && user && (user.is_admin || user.role==="admin" || (user.permissions && user.permissions.moboreels_library))); }
    function renderAuth() {
      if(root.UiTopbar)root.UiTopbar.render({auth:state.auth,userCard:el.userCard,authButton:el.loginBtn,loginText:"登录",logoutText:"退出登录"});
      else {el.userCard.textContent=state.auth.user?state.auth.user.name||"已登录":"未登录";el.loginBtn.textContent=state.auth.authenticated?"退出登录":"登录";}
      state.allowed=permissionAllowed();el.filterFields.disabled=!state.allowed;el.refreshBtn.disabled=!state.allowed||state.listBusy;el.syncBtn.disabled=!state.allowed||ACTIVE_RUN.has((state.sync||{}).status);el.pageSize.disabled=!state.allowed;
      el.accessNotice.classList.toggle("hidden",state.allowed);
      if(!state.allowed){el.accessNotice.textContent=state.auth.authenticated?"当前账号没有 Moboreels 短剧库权限，请联系管理员在用户管理中分配。":"请先登录 AI 后台后使用 Moboreels 短剧库。";stopPolling();state.listSequence++;state.promotionSequence++;state.rows=[];el.dramaRows.innerHTML='<tr><td colspan="7" class="empty">登录并获得剧库权限后查看短剧。</td></tr>';el.resultCount.textContent="";el.prevPage.disabled=true;el.nextPage.disabled=true;}
    }
    async function api(url,options) {
      let response;
      try {response=await root.fetch(url,Object.assign({credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json"}},options||{}));}
      catch(_){const error=new Error("网络连接暂时中断，请稍后刷新查看结果。");error.network=true;throw error;}
      const data=await response.json().catch(()=>({}));
      if(response.status===401){state.auth=data.auth||{authenticated:false};renderAuth();}
      if(response.status===403 && ["permission_denied","navigation_item_disabled","navigation_item_unavailable","navigation_item_permission_denied"].includes(data.error)){state.auth.user=Object.assign({},state.auth.user,{is_admin:false,role:"user",permissions:{}});renderAuth();}
      if(!response.ok){const messages={permission_denied:"当前账号没有此操作权限。",cookie_auth_required:"请重新登录后操作。",drama_not_found:"未找到该剧目，请刷新列表。",drama_unavailable:"该剧已下架，暂不能创建新的推广链接。",promotion_unknown:"请求结果待管理员核对，请勿重复创建。",moboreels_unavailable:"剧库服务暂不可用，请稍后再试。"};const error=new Error(response.status===401?"请先登录。":messages[data.error]||data.message||`请求未完成（${response.status}），请稍后查看结果。`);error.status=response.status;error.data=data;throw error;}
      return data;
    }
    const loadPromotion=createPromotionLoader(api);
    function bindImages(container){container.querySelectorAll("img").forEach(image=>image.addEventListener("error",()=>{image.parentElement.textContent="暂无封面";},{once:true}));}
    function renderRows(){el.dramaRows.innerHTML=state.rows.length?state.rows.map(renderRow).join(""):'<tr><td colspan="7" class="empty">没有符合条件的短剧，试试调整筛选条件。</td></tr>';bindImages(el.dramaRows);const pages=Math.max(1,Math.ceil(state.total/state.size));el.resultCount.textContent=`共 ${count(state.total)} 条短剧`;el.pageNumber.textContent=`${state.page} / ${pages}`;el.pageSummary.textContent=state.total?`显示 ${(state.page-1)*state.size+1}–${Math.min(state.page*state.size,state.total)} 条`:"";el.prevPage.disabled=!state.allowed||state.listBusy||state.page<=1;el.nextPage.disabled=!state.allowed||state.listBusy||state.page>=pages;}
    function languageOptions(items){const selected=el.languageFilter.value;const values=new Map(Object.keys(LANGUAGES).map(id=>[Number(id),language(id)]));values.set(23,language(23));values.set(-1,language(-1));(items||[]).forEach(item=>{const id=typeof item==="object"?item.id:item;if(Number.isFinite(Number(id)))values.set(Number(id),language(id));});el.languageFilter.innerHTML='<option value="">全部语言</option>'+Array.from(values.entries()).sort((a,b)=>a[0]-b[0]).map(([id,label])=>`<option value="${id}">${escapeHtml(label)}</option>`).join("");el.languageFilter.value=selected;}
    async function fetchRows(){if(!state.allowed)return;const sequence=++state.listSequence;state.listBusy=true;el.refreshBtn.disabled=true;el.prevPage.disabled=true;el.nextPage.disabled=true;el.listError.classList.add("hidden");el.resultCount.textContent="正在加载…";const params=new URLSearchParams({page_index:String(state.page),page_size:String(state.size),is_show:el.statusFilter.value});[["search",el.searchInput.value.trim()],["lang_id",el.languageFilter.value],["local_type",el.typeFilter.value],["audio_type",el.audioFilter.value]].forEach(([key,value])=>{if(value!=="")params.set(key,value);});try{const data=await api(`${API}/dramas?${params}`);if(sequence!==state.listSequence)return;state.total=number(data.total);state.rows=Array.isArray(data.list)?data.list:[];const max=Math.max(1,Math.ceil(state.total/state.size));if(state.page>max){state.page=max;state.listBusy=false;return fetchRows();}languageOptions(data.filters&&data.filters.languages);renderRows();}catch(error){if(sequence===state.listSequence){el.listError.textContent=error.message;el.listError.classList.remove("hidden");el.resultCount.textContent="列表加载失败";el.dramaRows.innerHTML='<tr><td colspan="7" class="empty">未能读取短剧列表，请点击“刷新列表”重试。</td></tr>';}}finally{if(sequence===state.listSequence){state.listBusy=false;el.refreshBtn.disabled=!state.allowed;el.prevPage.disabled=!state.allowed||state.page<=1;el.nextPage.disabled=!state.allowed||state.page>=Math.max(1,Math.ceil(state.total/state.size));}}}
    function renderSync(run,lastSuccess){state.sync=run;if(lastSuccess)state.lastSuccess=lastSuccess;if(run&&run.status==="succeeded"&&run.finished_at)state.lastSuccess=run.finished_at;const model=runModel(run);el.syncStatus.textContent=model.label;el.syncStatus.className=`badge ${model.kind}`;el.lastSuccess.textContent=`最近成功：${formatUTC(state.lastSuccess)}（北京时间）`;el.syncUpdated.textContent=run&&run.created_at?`本次开始：${formatUTC(run.created_at)}`:"";el.syncBtn.disabled=!state.allowed||model.active;el.syncBtn.textContent=model.active?"正在更新…":"立即更新";el.syncBtn.setAttribute("aria-busy",String(model.active));el.syncProgressWrap.classList.toggle("hidden",!run||(!model.active&&run.status!=="failed"));el.syncProgress.value=model.progress;el.syncProgressText.textContent=model.detail;el.syncCounts.innerHTML=run?[['新增',run.inserted],['资料更新',run.updated],['上架变化',run.listed],['下架变化',run.delisted]].map(([label,value])=>`<span>${label}<strong>${count(value)}</strong></span>`).join(""):"";const error=run&&run.status==="failed"?(run.message||run.error||"本次同步未完成，已提交的数据保留。请点击立即更新继续处理。"):"";el.syncError.textContent=error;el.syncError.classList.toggle("hidden",!error);}
    async function pollSync(){clearTimeout(state.syncTimer);if(!state.allowed)return;const previous=state.sync;try{const data=await api(`${API}/sync-runs/latest`);renderSync(data.run||null,data.last_success_at||(data.run&&data.run.last_success_at));if(previous&&ACTIVE_RUN.has(previous.status)&&data.run&&data.run.status==="succeeded")fetchRows();}catch(error){if(state.allowed){el.syncError.textContent=`同步状态暂时无法读取：${error.message}`;el.syncError.classList.remove("hidden");}}finally{if(state.allowed)state.syncTimer=setTimeout(pollSync,ACTIVE_RUN.has((state.sync||{}).status)?3000:30000);}}
    async function startSync(){if(!state.allowed||el.syncBtn.disabled)return;el.syncBtn.disabled=true;el.syncBtn.textContent="正在启动…";try{const data=await api(`${API}/sync-runs`,{method:"POST",body:"{}"});renderSync(data.run,data.last_success_at);toast(data.reused?"已有同步任务正在运行，已显示当前进度。":"已启动目录更新，可继续浏览剧库。");clearTimeout(state.syncTimer);state.syncTimer=setTimeout(pollSync,500);}catch(error){toast(error.message,true);await pollSync();if(state.allowed&&!ACTIVE_RUN.has((state.sync||{}).status)){el.syncBtn.disabled=false;el.syncBtn.textContent="立即更新";}}}
    function updatePromotionRow(row,promotion){row.promotion_status=promotion?promotion.status:"";const found=state.rows.find(item=>String(item.story_id)===String(row.story_id));if(found)found.promotion_status=row.promotion_status;renderRows();}
    function showPromotion(promotion,row,sequence){if(sequence!==state.promotionSequence||!el.promotionDialog.open)return;state.promotion=promotion;el.promotionBody.innerHTML=promotionHtml(promotion,row,state.auth.user&&state.auth.user.name);updatePromotionRow(row,promotion);clearTimeout(state.promotionTimer);if(promotion&&ACTIVE_PROMOTION.has(promotion.status))state.promotionTimer=setTimeout(()=>pollPromotion(row,sequence),2000);}
    async function pollPromotion(row,sequence){if(sequence!==state.promotionSequence||!el.promotionDialog.open||!state.allowed)return;try{const data=await api(`${API}/dramas/${encodeURIComponent(String(row.story_id))}/promotion`);showPromotion(data.promotion||null,row,sequence);}catch(error){if(sequence===state.promotionSequence&&el.promotionDialog.open){el.promotionBody.innerHTML='<div class="notice">暂时无法读取生成结果，请关闭后重新查看；不会自动重复创建。</div>';}}}
    async function openPromotion(row,retry){if(!state.allowed)return;clearTimeout(state.promotionTimer);const sequence=++state.promotionSequence;state.promotionRow=row;state.promotion=null;el.promotionTitle.textContent=text(row.story_name);el.promotionBody.innerHTML='<p class="muted">正在读取当前用户的推广信息…</p>';if(!el.promotionDialog.open)el.promotionDialog.showModal();try{const data=await loadPromotion(row,!!retry);showPromotion(data.promotion||null,row,sequence);}catch(error){if(sequence!==state.promotionSequence||!el.promotionDialog.open)return;if(error.data&&error.data.promotion){showPromotion(error.data.promotion,row,sequence);return;}el.promotionBody.innerHTML=`<div class="notice error">${escapeHtml(error.message)}</div><p class="muted">可关闭后重新查看结果。页面不会自动重复提交创建请求。</p>`;if(error.network||error.status>=500)state.promotionTimer=setTimeout(()=>pollPromotion(row,sequence),2000);}}
    async function openDetail(row){const sequence=++state.detailSequence;el.detailTitle.textContent=text(row.story_name);el.detailBody.innerHTML='<p class="muted">正在读取详情…</p>';el.detailDialog.showModal();try{const data=await api(`${API}/dramas/${encodeURIComponent(String(row.story_id))}`);if(sequence!==state.detailSequence||!el.detailDialog.open)return;state.detail=data.drama||row;state.detail.promotion_status=data.promotion?data.promotion.status:row.promotion_status;el.detailTitle.textContent=text(state.detail.story_name);el.detailBody.innerHTML=detailHtml(state.detail);}catch(error){if(sequence===state.detailSequence)el.detailBody.innerHTML=`<div class="notice error">${escapeHtml(error.message)}</div>`;}}
    async function copy(key){let value;if(key==="netdisk")value=state.detail&&(state.detail.baidu_link||state.detail.baiduLink);else value=state.promotion&&state.promotion[key];if(key==="deep_link"||key==="home_link")value=safeUrl(value);if(!value){toast("暂无可复制的内容。",true);return;}try{if(root.navigator.clipboard&&root.isSecureContext)await root.navigator.clipboard.writeText(String(value));else{const box=doc.createElement("textarea");box.value=String(value);box.style.position="fixed";box.style.opacity="0";(doc.querySelector("dialog[open]")||doc.body).appendChild(box);box.select();const done=doc.execCommand("copy");box.remove();if(!done)throw new Error("copy");}toast("已复制。");}catch(_){toast("复制失败，请选中内容后手动复制。",true);}}
    doc.addEventListener("click",event=>{const close=event.target.closest("[data-close]");if(close){byId(close.dataset.close).close();return;}const copyButton=event.target.closest("[data-copy]");if(copyButton){copy(copyButton.dataset.copy);return;}const action=event.target.closest("[data-action]");if(!action)return;event.preventDefault();if(action.dataset.action==="retry-promotion"){if(state.promotionRow)openPromotion(state.promotionRow,true);return;}const id=action.dataset.storyId;const row=state.rows.find(item=>String(item.story_id)===id)||(state.detail&&String(state.detail.story_id)===id?state.detail:null);if(!row)return;if(action.dataset.action==="detail")openDetail(row);else if(action.dataset.action==="promotion"){if(el.detailDialog.open)el.detailDialog.close();openPromotion(row);}});
    el.promotionDialog.addEventListener("close",()=>{state.promotionSequence++;clearTimeout(state.promotionTimer);});el.detailDialog.addEventListener("close",()=>state.detailSequence++);
    [el.detailDialog,el.promotionDialog].forEach(dialog=>dialog.addEventListener("click",event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}}));
    el.filterForm.addEventListener("submit",event=>{event.preventDefault();state.page=1;fetchRows();});el.resetFiltersBtn.addEventListener("click",()=>{el.filterForm.reset();state.page=1;fetchRows();});[el.languageFilter,el.statusFilter,el.typeFilter,el.audioFilter].forEach(select=>select.addEventListener("change",()=>{state.page=1;fetchRows();}));el.pageSize.addEventListener("change",()=>{state.size=Number(el.pageSize.value);state.page=1;fetchRows();});el.prevPage.addEventListener("click",()=>{if(state.page>1){state.page--;fetchRows();}});el.nextPage.addEventListener("click",()=>{if(state.page<Math.ceil(state.total/state.size)){state.page++;fetchRows();}});el.refreshBtn.addEventListener("click",()=>{fetchRows();pollSync();});el.syncBtn.addEventListener("click",startSync);
    el.loginBtn.addEventListener("click",async()=>{if(root.UiTopbar){await root.UiTopbar.handleAuthAction({auth:state.auth,api,afterLogout:()=>root.location.reload()});}else if(state.auth.authenticated){await api("/api/auth/logout",{method:"POST",body:"{}"}).catch(()=>{});root.location.reload();}else root.location.assign(`/api/auth/feishu/login?next=${encodeURIComponent(root.location.pathname)}`);});
    root.addEventListener("pagehide",stopPolling);doc.addEventListener("visibilitychange",()=>{if(doc.visibilityState==="visible"&&state.allowed)pollSync();});languageOptions();
    try{state.auth=await api("/api/ui/topbar");}catch(_){state.auth={authenticated:false};}renderAuth();if(root.QuickNav)root.QuickNav.render({container:el.quickNav,activeKey:"moboreelsLibrary",auth:state.auth,onNavigate:item=>{if(item&&item.href)root.location.assign(item.href);}}).catch(()=>{});if(state.allowed){fetchRows();pollSync();}else{el.syncStatus.textContent="登录后查看";}
  }
  const exported={escapeHtml,safeUrl,language,fee,formatUTC,promotionButton,runModel,renderRow,detailHtml,promotionHtml,createPromotionLoader};
  if(typeof module!=="undefined"&&module.exports)module.exports=exported;
  if(root.document){if(root.document.readyState==="loading")root.document.addEventListener("DOMContentLoaded",boot,{once:true});else boot();}
})(typeof window!=="undefined"?window:globalThis);
