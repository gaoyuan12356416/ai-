(function(root) {
  "use strict";
  const API="/api/moboreels", ACTIVE=new Set(["queued","running"]);
  const GROUPS={promotion:"推广口令 / 短剧",day:"来源日期",story:"短剧"};
  const METRICS=[
    ["order_recharge_usd","订单充值","order_recharge_count","笔充值"],
    ["order_refund_usd","订单退款","order_refund_count","笔退款"],
    ["order_net_usd","订单净额",null,"充值减退款，不代表机构佣金"],
    ["advertising_revenue_usd","广告收益",null,"供应方 T+1 数据"],
    ["unclassified_order_usd","未分类订单金额","unclassified_order_count","笔待分类订单"],
    ["unmatched_attribution_usd","未匹配归因金额",null,"已包含在对应金额中，不重复相加"]
  ];
  const escapeHtml=value=>String(value==null?"":value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const text=value=>value==null||value===""?"—":String(value);
  const n=value=>Number.isFinite(Number(value))?Math.max(0,Number(value)):0;
  function formatCount(value) {
    const raw=String(value==null?"":value);
    return /^\d+$/.test(raw)?raw.replace(/^0+(?=\d)/,"").replace(/\B(?=(\d{3})+(?!\d))/g,","):"—";
  }
  // Keep provider decimal strings exact, including fractional cents; never sum page rows.
  function money(value) {
    const raw=String(value==null?"":value), match=/^([+-]?)(\d+)(?:\.(\d+))?$/.exec(raw);
    if(!match)return "—";
    const whole=match[2].replace(/^0+(?=\d)/,""), fraction=(match[3]||"").replace(/0+$/,"").padEnd(2,"0");
    const sign=match[1]==="-"&&/[1-9]/.test(whole+fraction)?"-":"";
    return `${sign}$${whole.replace(/\B(?=(\d{3})+(?!\d))/g,",")}.${fraction}`;
  }
  function defaultRange(now=new Date()) {
    const end=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate())-86400000);
    return {date_from:new Date(end.getTime()-30*86400000).toISOString().slice(0,10),date_to:end.toISOString().slice(0,10)};
  }
  function validateRange(from,to,now=new Date()) {
    function parse(value) {
      if(!/^\d{4}-\d{2}-\d{2}$/.test(value))throw new Error("请选择完整的来源起止日期。");
      const parsed=new Date(value+"T00:00:00Z");
      if(!Number.isFinite(parsed.getTime())||parsed.toISOString().slice(0,10)!==value)throw new Error("来源日期无效。");
      return parsed.getTime();
    }
    const start=parse(from),end=parse(to);
    if(start>end)throw new Error("起始日期不能晚于结束日期。");
    if((end-start)/86400000>=31)throw new Error("单次最多查询 31 个日历日期，请缩小日期范围。");
    if(to>now.toISOString().slice(0,10))throw new Error("结束日期不能晚于今天。广告收益为 T+1，建议查询到昨日。");
    return {date_from:from,date_to:to};
  }
  function reportUrl(query) {
    const params=new URLSearchParams({date_from:query.date_from,date_to:query.date_to,group_by:query.group_by||"promotion",page_index:String(query.page_index||1),page_size:"50"});
    if(query.q)params.set("q",query.q);
    return `${API}/reports?${params}`;
  }
  function operationalTime(value) {
    if(!value)return "—";
    const source=String(value), normalized=/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d/.test(source)&&!/(Z|[+-]\d\d:\d\d)$/i.test(source)?source.replace(" ","T")+"Z":source;
    const date=new Date(normalized);
    return Number.isFinite(date.getTime())?date.toISOString().replace("T"," ").replace(/\.\d{3}Z$/," UTC"):"—";
  }
  function summaryHtml(summary) {
    return METRICS.map(([key,label,countKey,note])=>`<article class="metric ${key==="order_net_usd"?"primary":""}"><h3 class="metric-label">${label} · USD</h3><p class="metric-value">${escapeHtml(money(summary&&summary[key]))}</p><p class="metric-note">${countKey?`${formatCount(summary&&summary[countKey])} ${note}`:note}</p></article>`).join("");
  }
  function attribution(row,group) {
    const status=row.attribution_status;
    const labels={matched:"已匹配",unmatched:"未匹配",ambiguous:"共享口令，归属待核对",mixed:"含不同归因状态",partial:"部分未匹配",contains_unmatched:"含未匹配记录"};
    return `<span class="badge ${status==="matched"?"success":["unmatched","ambiguous","contains_unmatched"].includes(status)?"warning":""}">${escapeHtml(labels[status]||(group!=="promotion"?"汇总":"未说明"))}</span>${row.owner_name?`<span class="cell-sub">${escapeHtml(row.owner_name)}</span>`:""}`;
  }
  function unknownDetails(row) {
    if(!Array.isArray(row.unknown_states)||!row.unknown_states.length)return "";
    const lines=row.unknown_states.filter(item=>item&&typeof item==="object").slice(0,10).map(item=>[item.source_status_text,item.coo_order_status_raw,item.classification_reason].filter(value=>value!=null&&value!=="").map(String).join(" / "));
    return `<details class="unknown-details"><summary>查看未分类原因</summary><ul>${lines.map(line=>`<li>${escapeHtml(line||"供应方状态待核对")}</li>`).join("")}</ul></details>`;
  }
  function rowHtml(row,group="promotion") {
    const label=group==="day"?(row.date||"未提供来源日期"):group==="story"?(row.story_name||row.story_id||"未匹配短剧"):(row.koc_code||"未提供推广口令");
    const sub=group==="day"?"来源日期，时区未注明":[row.story_name, row.story_id?`ID ${row.story_id}`:""].filter(Boolean).join(" · ");
    const cells=METRICS.map(([key])=>`<td class="number ${key==="order_net_usd"&&String(row[key]||"").startsWith("-")?"negative":""}">${escapeHtml(money(row[key]))}${key==="unclassified_order_usd"?`<span class="cell-sub">${formatCount(row.unclassified_order_count)} 笔</span>${unknownDetails(row)}`:""}</td>`).join("");
    return `<tr><td><strong class="dimension">${escapeHtml(label)}</strong><span class="cell-sub">${escapeHtml(sub)}</span></td><td>${attribution(row,group)}</td>${cells}</tr>`;
  }
  function progressModel(run) {
    if(!run)return {active:false,label:"暂无同步记录",kind:"",percent:0,detail:""};
    const active=ACTIVE.has(run.status),parts=Array.isArray(run.parts)?run.parts:[];
    const current=parts.find(part=>part.status==="running")||{};
    const totalParts=n(run.total_parts)||parts.length,completed=n(run.completed_parts);
    const pages=n(current.total_pages||run.total_pages),fetched=n(current.fetched_pages||run.fetched_pages);
    const total=n(current.total==null?(run.total_rows==null?run.total:run.total_rows):current.total);
    const processed=n(current.processed==null?(run.processed_rows==null?run.processed:run.processed_rows):current.processed);
    const verified=n(current.verified==null?run.verified:current.verified);
    const partName=current.kind==="orders"?"订单":current.kind==="ad"?`广告 ${text(current.date)}`:"";
    const stages={fetching:"正在分页取数",writing:"正在写入数据",verifying:"正在读回核对"};
    let fraction=0,detail="等待任务开始";
    if(run.stage==="fetching"){fraction=pages?Math.min(.5,fetched/pages*.5):0;detail=`已读取 ${formatCount(fetched)}${pages?` / ${formatCount(pages)}`:""} 页`;}
    if(run.stage==="writing"){fraction=.5+(total?Math.min(.45,processed/total*.45):0);detail=`已写入 ${formatCount(processed)} / ${formatCount(total)} 条`;}
    if(run.stage==="verifying"){fraction=.95+(total?Math.min(.05,verified/total*.05):0);detail=`已核对 ${formatCount(verified)} / ${formatCount(total)} 条`;}
    if(totalParts)detail=`完成 ${formatCount(completed)} / ${formatCount(totalParts)} 个分项${partName?` · ${partName}`:""} · ${detail}`;
    let percent=totalParts?(completed+fraction)/totalParts*100:fraction*100;
    if(run.status==="succeeded")return {active:false,label:"同步完成",kind:"success",percent:100,detail:`已完成 ${formatCount(totalParts)} 个分项，核对 ${formatCount(run.verified==null?0:run.verified)} 条记录`};
    if(run.status==="failed")return {active:false,label:"同步未完成",kind:"error",percent:Math.min(99,percent),detail:totalParts?`已完成 ${formatCount(completed)} / ${formatCount(totalParts)} 个分项；已成功的数据保留，请核对下方失败原因。`:"已成功的数据保留，请核对失败原因。"};
    return {active,label:run.status==="queued"?"等待开始":stages[run.stage]||"正在同步",kind:"running",percent:Math.min(99,percent),detail};
  }
  function partsHtml(parts) {
    const labels={queued:"等待中",running:"进行中",succeeded:"已完成",failed:"未完成"};
    return (parts||[]).map(part=>`<div class="sync-part"><strong>${escapeHtml(part.kind==="orders"?"订单":`广告 ${text(part.date)}`)}</strong> · ${escapeHtml(labels[part.status]||"状态未说明")}<p class="muted">分页 ${formatCount(part.fetched_pages)} / ${formatCount(part.total_pages)} · 写入 ${formatCount(part.processed)} / ${formatCount(part.total)} · 校验 ${formatCount(part.verified)}</p>${part.error?`<p class="error-text">${escapeHtml(part.error)}</p>`:""}</div>`).join("");
  }
  function readLoader(api) {
    let active;
    return {load(url){
      if(active&&active.url===url)return active.promise;
      if(active)active.controller.abort();
      const request={url,controller:new root.AbortController()};
      request.promise=api(url,{signal:request.controller.signal}).finally(()=>{if(active===request)active=null;});
      active=request;return request.promise;
    },cancel(){if(active){active.controller.abort();active=null;}}};
  }
  const settled=promise=>promise.then(data=>({data}),error=>({error}));
  function outcome(result){if(result.error)throw result.error;return result.data;}

  async function boot() {
    const doc=root.document,el={};
    ["quickNav","userCard","loginBtn","accessNotice","filterForm","filterFields","dateFrom","dateTo","groupBy","searchInput","resetBtn","scopeLabel","queryPeriod","filterError","adCoverage","undatedNote","summaryCards","resultCount","refreshBtn","reportError","dimensionHeading","reportRows","pageSummary","pageNumber","prevPage","nextPage","syncBtn","syncStatus","lastSuccess","syncRange","progressWrap","syncProgress","progressText","syncError","syncDetails","syncParts","toast"].forEach(id=>{el[id]=doc.getElementById(id);});
    const state={auth:{},allowed:false,blocked:null,pageActive:true,page:1,total:0,sequence:0,busy:false,run:null,lastSuccess:null,poll:null,timer:null,revision:0,starting:false};
    let toastTimer;
    function toast(message,error){el.toast.textContent=message;el.toast.classList.toggle("error",!!error);el.toast.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.toast.classList.remove("show"),error?5000:3000);}
    function clearReport(message) {
      state.total=0;el.summaryCards.innerHTML=summaryHtml(null);el.reportRows.innerHTML=`<tr><td colspan="8" class="empty">${escapeHtml(message)}</td></tr>`;
      el.resultCount.textContent="";el.scopeLabel.textContent="范围：待确认";el.adCoverage.textContent="广告数据更新日期：未读取";el.undatedNote.textContent="";el.undatedNote.classList.add("hidden");el.pageSummary.textContent="每页 50 条";el.pageNumber.textContent="1 / 1";el.prevPage.disabled=true;el.nextPage.disabled=true;
    }
    function renderAuth() {
      const user=state.auth.user;
      state.allowed=!state.blocked&&!!(state.auth.authenticated&&user&&(user.is_admin||user.role==="admin"||(user.permissions&&user.permissions.moboreels_report)));
      if(root.UiTopbar)root.UiTopbar.render({auth:state.auth,userCard:el.userCard,authButton:el.loginBtn});
      else{el.userCard.textContent=user?user.name||"已登录":"未登录";el.loginBtn.textContent=state.auth.authenticated?"退出登录":"登录";}
      el.filterFields.disabled=!state.allowed;el.refreshBtn.disabled=!state.allowed||state.busy;el.syncBtn.disabled=!state.allowed||state.starting||ACTIVE.has((state.run||{}).status);
      el.accessNotice.classList.toggle("hidden",state.allowed);
      if(!state.allowed){
        el.accessNotice.textContent=state.auth.authenticated?"当前账号没有 Moboreels 收益报表权限，请联系管理员分配。":"请先登录 AI 后台后查看收益报表。";
        state.sequence++;reports.cancel();syncReads.cancel();clearTimeout(state.timer);
        clearReport("登录并获得收益报表权限后查看数据。");el.queryPeriod.textContent="";el.syncStatus.textContent="登录并获得权限后查看";el.syncParts.textContent="";el.syncDetails.classList.add("hidden");el.progressWrap.classList.add("hidden");el.lastSuccess.textContent="最近成功：—";el.syncRange.textContent="";
      }
    }
    async function api(url,options={}) {
      let response;
      try{response=await root.fetch(url,Object.assign({credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json"}},options));}
      catch(error){if(error.name==="AbortError")throw error;throw new Error("网络连接暂时中断，请稍后刷新查看。");}
      const data=await response.json().catch(()=>({}));
      if(response.status===401){state.blocked=401;state.auth={authenticated:false};renderAuth();}
      if(response.status===403&&["permission_denied","navigation_item_disabled","navigation_item_unavailable","navigation_item_permission_denied"].includes(data.error)){state.blocked=403;renderAuth();}
      if(!response.ok){const error=new Error(data.message||({permission_denied:"当前账号没有收益报表权限。",moboreels_unavailable:"报表服务暂不可用，请稍后重试。"}[data.error])||`请求未完成（${response.status}），请稍后重试。`);error.status=response.status;throw error;}
      return data;
    }
    const reports=readLoader(api),syncReads=readLoader(api);
    function filters() {
      const range=validateRange(el.dateFrom.value,el.dateTo.value);
      const q=el.searchInput.value.trim();if(q.length>200)throw new Error("搜索内容不能超过 200 个字符。");
      return Object.assign(range,{group_by:GROUPS[el.groupBy.value]?el.groupBy.value:"promotion",q,page_index:state.page});
    }
    function resetDates(){const range=defaultRange();el.dateFrom.value=range.date_from;el.dateTo.value=range.date_to;const max=new Date().toISOString().slice(0,10);el.dateFrom.max=max;el.dateTo.max=max;}
    function pagination(){const pages=Math.max(1,Math.ceil(state.total/50));el.prevPage.disabled=!state.allowed||state.busy||state.page<=1;el.nextPage.disabled=!state.allowed||state.busy||state.page>=pages;el.pageNumber.textContent=`${state.page} / ${pages}`;el.pageSummary.textContent=state.total?`每页 50 条 · 显示 ${(state.page-1)*50+1}–${Math.min(state.page*50,state.total)} 条`:"每页 50 条";}
    async function fetchReport(initial) {
      if(!state.allowed)return;
      let query;
      try{query=filters();el.filterError.classList.add("hidden");}
      catch(error){state.sequence++;reports.cancel();state.busy=false;el.refreshBtn.disabled=false;pagination();el.filterError.textContent=error.message;el.filterError.classList.remove("hidden");el.resultCount.textContent="本次查询未执行，请检查日期范围。";return;}
      const sequence=++state.sequence;state.busy=true;el.refreshBtn.disabled=true;pagination();el.reportError.classList.add("hidden");el.resultCount.textContent="正在查询…";
      try {
        const data=initial?outcome(await initial):await reports.load(reportUrl(query));
        if(!state.allowed||sequence!==state.sequence)return;
        if(!Array.isArray(data.list)||!data.summary||data.currency!=="USD")throw new Error("报表返回格式异常，请刷新重试。");
        state.total=n(data.total);const max=Math.max(1,Math.ceil(state.total/50));
        if(state.page>max){state.page=max;return fetchReport();}
        const group=GROUPS[data.group_by]?data.group_by:query.group_by;
        el.summaryCards.innerHTML=summaryHtml(data.summary);el.dimensionHeading.textContent=GROUPS[group];
        el.reportRows.innerHTML=data.list.length?data.list.map(row=>rowHtml(row,group)).join(""):'<tr><td colspan="8" class="empty">查询已完成，当前日期与筛选范围内暂无记录。请结合数据更新日期和同步状态核对。</td></tr>';
        el.resultCount.textContent=`查询已完成 · 共 ${formatCount(state.total)} 条汇总记录`;
        el.scopeLabel.textContent=`范围：${data.scope&&data.scope.label?data.scope.label:"后端授权范围"}`;
        el.queryPeriod.textContent=`来源日期 ${text(data.date_from||query.date_from)} 至 ${text(data.date_to||query.date_to)} · 汇总金额涵盖全部查询结果`;
        el.adCoverage.textContent=`广告数据更新日期：${data.ad_data_as_of||"尚无已同步日期"}${data.complete_ad_days!=null?`；本范围已同步 ${formatCount(data.complete_ad_days)} 个广告日期`:""}`;
        el.undatedNote.textContent=n(data.summary.undated_order_count)>0?`另有 ${formatCount(data.summary.undated_order_count)} 笔来源日期未明确的订单，作为未分类展示，不参与订单净额。`:"";el.undatedNote.classList.toggle("hidden",!el.undatedNote.textContent);
        if(data.last_success_at){state.lastSuccess=data.last_success_at;el.lastSuccess.textContent=`最近成功：${operationalTime(state.lastSuccess)}`;}
      }catch(error){if(error.name!=="AbortError"&&state.allowed&&sequence===state.sequence){clearReport("报表读取失败，请点击“刷新报表”重试。");el.reportError.textContent=error.message;el.reportError.classList.remove("hidden");el.queryPeriod.textContent="本次请求未完成，暂不展示金额。";}}
      finally{if(sequence===state.sequence){state.busy=false;el.refreshBtn.disabled=!state.allowed;pagination();}}
    }
    function renderRun(run,lastSuccess) {
      state.run=run;if(lastSuccess)state.lastSuccess=lastSuccess;if(run&&run.status==="succeeded"&&run.finished_at)state.lastSuccess=run.finished_at;
      const model=progressModel(run);el.syncStatus.textContent=model.label;el.syncStatus.className=`badge ${model.kind}`;el.syncBtn.disabled=!state.allowed||state.starting||model.active;el.syncBtn.textContent=model.active?"正在同步…":"更新当前日期范围";
      el.lastSuccess.textContent=`最近成功：${operationalTime(state.lastSuccess)}`;el.syncRange.textContent=run?`任务来源日期 ${text(run.date_from)} 至 ${text(run.date_to)} · 任务 ${text(run.run_id||run.id)}`:"";
      el.progressWrap.classList.toggle("hidden",!run);el.syncProgress.value=model.percent;el.progressText.textContent=model.detail;
      el.syncError.textContent=run&&run.status==="failed"?(run.error||"部分分项未完成，已成功数据保留。可重新更新该日期范围继续同步。"):"";el.syncError.classList.toggle("hidden",!el.syncError.textContent);
      const parts=run&&Array.isArray(run.parts)?run.parts:[];el.syncDetails.classList.toggle("hidden",!parts.length);el.syncParts.innerHTML=partsHtml(parts);
    }
    function schedule(delay){clearTimeout(state.timer);if(state.allowed&&state.pageActive&&!state.starting&&doc.visibilityState!=="hidden")state.timer=setTimeout(pollSync,delay);}
    function pollSync(initial) {
      clearTimeout(state.timer);if(!state.allowed||!state.pageActive||state.starting||doc.visibilityState==="hidden")return Promise.resolve();if(state.poll)return state.poll;
      const previous=state.run,revision=state.revision;
      const id=previous&&(previous.run_id||previous.id),url=id&&ACTIVE.has(previous.status)?`${API}/report-sync-runs/${encodeURIComponent(id)}`:`${API}/report-sync-runs/latest`;
      const pending=(async()=>{
        try{const data=initial?outcome(await initial):await syncReads.load(url);if(!state.allowed||!state.pageActive||revision!==state.revision)return;renderRun(data.run||null,data.last_success_at);if(previous&&ACTIVE.has(previous.status)&&data.run&&data.run.status==="succeeded")fetchReport();}
        catch(error){if(error.name!=="AbortError"&&state.allowed&&revision===state.revision){el.syncError.textContent=`同步状态读取失败：${error.message}`;el.syncError.classList.remove("hidden");}}
        finally{if(state.poll===pending)state.poll=null;if(revision===state.revision)schedule(ACTIVE.has((state.run||{}).status)?3000:30000);}
      })();state.poll=pending;return pending;
    }
    async function startSync() {
      if(!state.allowed||state.starting||ACTIVE.has((state.run||{}).status))return;
      let range;try{range=validateRange(el.dateFrom.value,el.dateTo.value);el.filterError.classList.add("hidden");}catch(error){el.filterError.textContent=error.message;el.filterError.classList.remove("hidden");return;}
      state.starting=true;state.revision++;clearTimeout(state.timer);syncReads.cancel();el.syncBtn.disabled=true;el.syncBtn.textContent="正在启动…";
      try{const data=await api(`${API}/report-sync-runs`,{method:"POST",body:JSON.stringify(range)});if(!state.allowed)return;renderRun(data.run,data.last_success_at);toast(data.reused?"已有报表同步任务正在执行，已显示其日期范围与进度。":"报表同步已启动，可继续查询已入库的数据。");}
      catch(error){if(state.allowed)toast(error.message,true);}
      finally{state.starting=false;el.syncBtn.disabled=!state.allowed||ACTIVE.has((state.run||{}).status);if(!el.syncBtn.disabled)el.syncBtn.textContent="更新当前日期范围";schedule(500);}
    }
    el.filterForm.addEventListener("submit",event=>{event.preventDefault();state.page=1;fetchReport();});
    el.groupBy.addEventListener("change",()=>{state.page=1;fetchReport();});
    el.resetBtn.addEventListener("click",()=>{resetDates();state.page=1;fetchReport();});
    el.prevPage.addEventListener("click",()=>{if(!state.busy&&state.page>1){state.page--;fetchReport();}});
    el.nextPage.addEventListener("click",()=>{if(!state.busy&&state.page<Math.ceil(state.total/50)){state.page++;fetchReport();}});
    el.refreshBtn.addEventListener("click",()=>{fetchReport();pollSync();});el.syncBtn.addEventListener("click",startSync);
    el.loginBtn.addEventListener("click",async()=>{if(root.UiTopbar)await root.UiTopbar.handleAuthAction({auth:state.auth,api,afterLogout:()=>root.location.reload()});else if(state.auth.authenticated){await api("/api/auth/logout",{method:"POST",body:"{}"}).catch(()=>{});root.location.reload();}else root.location.assign(`/api/auth/feishu/login?next=${encodeURIComponent(root.location.pathname)}`);});
    root.addEventListener("pagehide",()=>{state.pageActive=false;clearTimeout(state.timer);reports.cancel();syncReads.cancel();});
    root.addEventListener("pageshow",()=>{state.pageActive=true;if(state.allowed)pollSync();});
    doc.addEventListener("visibilitychange",()=>{if(doc.visibilityState==="hidden"){clearTimeout(state.timer);syncReads.cancel();}else if(state.allowed)pollSync();});
    resetDates();el.summaryCards.innerHTML=summaryHtml(null);
    const authRead=settled(api("/api/ui/topbar")),firstReport=settled(reports.load(reportUrl(filters()))),firstSync=settled(syncReads.load(`${API}/report-sync-runs/latest`));
    const auth=await authRead;if(state.blocked!==401)state.auth=auth.error?{authenticated:false}:auth.data;renderAuth();
    if(root.QuickNav)root.QuickNav.render({container:el.quickNav,activeKey:"moboreelsReport",auth:state.auth,onNavigate:item=>{if(item&&item.href)root.location.assign(item.href);}}).catch(()=>{});
    if(state.allowed){fetchReport(firstReport);pollSync(firstSync);}
  }
  const exported={escapeHtml,money,formatCount,defaultRange,validateRange,reportUrl,operationalTime,summaryHtml,rowHtml,progressModel,partsHtml,readLoader};
  if(typeof module!=="undefined"&&module.exports)module.exports=exported;
  if(root.document){if(root.document.readyState==="loading")root.document.addEventListener("DOMContentLoaded",boot,{once:true});else boot();}
})(typeof window!=="undefined"?window:globalThis);
