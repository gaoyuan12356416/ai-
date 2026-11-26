(function(){
  "use strict";
  const {id,h,money,time,badge,empty,notify,get:read,post:write,modal,confirmAction,action,boot,byId,name,status,statusBadge}=window.TTPromotion;
  const {channelMetricRanges,channelMetricSort,channelRangeInputs,channelSortOptions,followersText}=window.TTPromotion;
  const modeNames={template:"按模板选材",drama:"指定爆款剧",material:"指定素材",pool:"素材池分配",serial:"短剧连载"};
  let canSetChannels=false,editorDirty=false;
  let templates=[],channels=[],actorId="",currentView="templates",requestToken=0;
  let stats=new Map(),historyPage=1,detailEpoch=0,importedAccounts=[],allowClose=null;
  let accessDenied=false,importHandled=false;
  const sourceReady={channels:false,templates:false},sourceVersion={channels:0,templates:0};
  const canConfigure=()=>!accessDenied&&sourceReady.channels&&sourceReady.templates;
  function checkAccess(error){
    if(![401,403].includes(error.status))return;
    accessDenied=true;
    id("workspace").hidden=true;
    allowClose=null;
    if(id("confirm").open)id("confirm").close();
    if(id("dialog").open)id("dialog").close();
    notify(error.status===401?"登录已过期，请重新登录。":"当前账号没有访问此功能的权限。",false,"error");
    if(error.status===401){
      id("authButton").textContent="登录";
      id("authButton").onclick=()=>location.assign(window.UiTopbar.loginUrl({}));
    }
  }
  async function get(route){
    try{return await read(route);}catch(error){checkAccess(error);throw error;}
  }
  async function post(route,payload){
    if(!canConfigure())throw new Error("请先重试频道与模板数据，恢复配置权限后再操作。");
    try{return await write(route,payload);}catch(error){checkAccess(error);throw error;}
  }
  const templatesPath="/templates";
  const path=(t,suffix="")=>`/templates/${encodeURIComponent(t.id)}${suffix}`;
  const cfg=t=>t.config||{};
  function tName(t){return t.name || `模板 ${t.id}`;}
  function scheduleText(s){
    if(s?.mode==="random")return `每日随机 ${s.daily_count} 条 / 频道`;
    return (s?.times||[]).join("、")||"—";
  }


  const bucketNames={expected:"应发",confirmed:"已确认",failed:"失败",skipped:"跳过",review:"待核对",pending:"进行中"};
  const sourceNames={manual:"人工定稿",ai:"AI 生成",template:"模板稿",fallback:"模板回退",ai_reviewed:"AI 生成，人工确认",ai_edited:"AI 生成，人工修改"};
  function apiUrl(route,params){const q=new URLSearchParams();for(const [k,v] of Object.entries(params))if(v!==""&&v!=null)q.set(k,String(v));return route+"?"+q;}
  function errorBox(node,error,retry){
    if(!node)return;
    node.innerHTML=`<div class="inline-error">${h(error.message||"暂未获取数据")} <button class="row-link" type="button">重试</button></div>`;
    node.querySelector("button").onclick=()=>Promise.resolve(retry()).catch(e=>errorBox(node,e,retry));
  }
  function pageBar(page,size,total,attr){
    const last=Math.max(1,Math.ceil(total/size));
    return `<span class="muted">共 ${h(total)} 条 · ${h(page)} / ${last} 页</span><button class="button" type="button" ${attr}="-1" ${page<=1?"disabled":""}>上一页</button><button class="button" type="button" ${attr}="1" ${page>=last?"disabled":""}>下一页</button>`;
  }
  function countsHtml(obj,template){
    if(!obj)return '<span class="muted">暂无批次</span>';
    return `<div class="counts-line">${Object.entries(bucketNames).filter(([k])=>obj[k]!=null).map(([k,label])=>template && ["failed","skipped","review"].includes(k) && obj[k]>0?`<button class="row-link" data-to-history="${h(k)}" data-template="${h(template)}">${label} ${h(obj[k])}</button>`:`<span>${label} ${h(obj[k])}</span>`).join("")}</div>`;
  }
  function anomaly(s,k){if(!k)return true;if(!s?.latest)return false;return k==="anomaly"?["failed","skipped","review"].some(i=>s.latest[i]>0):s.latest[k]>0;}
  function templateRows(){
    id("new-template").disabled=!canConfigure();
    if(!sourceReady.templates){
      id("templates-meta").textContent="模板清单暂未取得";
      id("templates").innerHTML=empty(6,"模板清单暂未取得，可重试或查看运行记录。");return;
    }
    const term=id("template-search").value.trim().toLowerCase(),state=id("template-state").value;
    const rows=templates.filter(t=>(!term||[t.id,t.name].some(v=>String(v).toLowerCase().includes(term)))&&anomaly(stats.get(String(t.id)),state));
    id("templates-meta").textContent=`${rows.length} / ${templates.length} 个模板 · ${templates.filter(t=>t.enabled).length} 个已启用`;
    id("templates").innerHTML=rows.map(t=>{
      const c=cfg(t),stat=stats.get(String(t.id)),tid=h(t.id);
      const btn=(act,label)=>`<button class="row-link" data-act="${act}" data-id="${tid}" ${!canConfigure()&&["edit","run","toggle"].includes(act)?'disabled title="请重试频道与模板数据"':""}>${label}</button>`;
      return `<tr><td><strong>${h(tName(t))}</strong><small class="secondary">#${tid} · v${h(t.version)}</small></td><td>${statusBadge(t.enabled?"enabled":"draft")}<small class="secondary">${c.model==="rules"?"模板规则":"收入画像"}</small></td><td>${h(c.account_ids?.length??0)} 个频道<small class="secondary">${c.ai_copy?"AI 文案":"模板文案"}</small></td><td>${h(scheduleText(c.schedule))}<small class="secondary">下次 ${h(time(stat?.next_at))}</small></td><td>${stat?countsHtml(stat.latest,t.id):"—"}${stat?.latest?.id?`<button class="row-link" data-open-run="${h(stat.latest.id)}">#${h(stat.latest.id)}</button>`:""}</td><td><div class="inline">${btn("edit","编辑")}${btn("run","预演与执行")}${btn("inventory","素材池库存")}${btn("records","记录")}${btn("toggle",t.enabled?"停用":"启用")}</div></td></tr>`;
    }).join("")||empty(6,templates.length?"没有符合条件的模板":"暂无新版模板，可创建草稿后预演。");
  }
  function options(select,items,label){const old=select.value;select.innerHTML=`<option value="">${label}</option>`+items.map(([key,n])=>`<option value="${h(key)}">${h(n)}</option>`).join("");select.value=old;}
  async function getStats(){
    const node=id("stats-error");
    try{const data=await get("/stats/templates");stats=new Map(data.items.map(s=>[String(s.template_id),s]));node.replaceChildren();templateRows();}
    catch(e){errorBox(node,e,getStats);}
  }
  async function updateTemplates(){
    await loadSource("templates");void getStats();
  }
  function pickerRows(selected){
    const all=new Map(channels.map(c=>[byId(c),c]));
    for(const a of selected)if(!all.has(a))all.set(a,{account_id:a,nickname:"当前不可用",eligible:false});
    return [...all.values()];
  }
  function picker(editor=false){
    return `<fieldset class="group" data-picker>${editor?"":"<legend>目标频道</legend>"}<div class="picker-filters"><label>搜索频道<input type="search" data-search placeholder="昵称或频道 ID"></label>${editor?'<details class="picker-more"><summary>更多筛选</summary><div class="extra-filters">':""}<label>剧语言<select data-pick="language"></select></label><label>主要受众<select data-pick="audience"></select></label><label>发布资格<select data-pick="eligible"><option value="">全部资格</option><option value="yes">可发布</option><option value="no">受限</option></select></label><label>旧排期<select data-pick="conflict"><option value="">全部排期</option><option value="yes">有旧排期或冲突</option><option value="no">未见旧排期或冲突</option><option value="unknown">待核验</option></select></label>${channelRangeInputs("data-pick")}<label>排序<select data-pick="order"><option value="default">频道顺序</option>${channelSortOptions()}</select></label>${editor?"</div></details>":""}</div>
      <div class="inline"><button type="button" class="row-link" data-add>选当前结果</button><button type="button" class="row-link" data-clear>清空已选</button>${editor?'<label class="only-selected"><input type="checkbox" data-selected-only> 只看已选</label>':""}<span class="muted" data-count></span><button type="button" class="row-link" data-first-use hidden>确认所选新频道观看范围</button></div><div class="check-list" data-accounts></div></fieldset>`;
  }
  function setupPicker(form,rows,initial,onChange=()=>{}){
    const wrapper=form.querySelector("[data-picker]"),list=wrapper.querySelector("[data-accounts]"),selected=new Set(initial.map(String));
    const getControl=k=>wrapper.querySelector(`[data-pick="${k}"]`),search=wrapper.querySelector("[data-search]");
    const audience=c=>typeof c.audience==="object"?c.audience?.name:c.audience;
    const facets=(k,values,label)=>options(getControl(k),[...new Set(values.filter(Boolean))].sort().map(v=>[v,v]),label);
    facets("language",rows.map(c=>c.drama_language),"全部语言");facets("audience",rows.map(audience),"全部受众");
    const onlySelected=wrapper.querySelector("[data-selected-only]");
    const conflicts=c=>c.legacy_scheduled===true||c.legacy_conflict===true?"yes":c.legacy_scheduled===false&&c.legacy_conflict!==null?"no":"unknown";
    let visible=[];
    function draw(){
      const term=search.value.trim().toLowerCase(),val=k=>getControl(k).value;
      visible=rows.filter(c=>(!onlySelected?.checked||selected.has(byId(c)))&&(!term||[name(c),byId(c),c.drama_language].some(v=>String(v||"").toLowerCase().includes(term)))&&(!val("language")||c.drama_language===val("language"))&&(!val("audience")||audience(c)===val("audience"))&&(!val("eligible")||(c.eligible?"yes":"no")===val("eligible"))&&(!val("conflict")||conflicts(c)===val("conflict"))&&channelMetricRanges(val,c));
      if(val("order")!=="default")visible.sort((a,b)=>channelMetricSort(val("order"),a,b));
      list.innerHTML=visible.map(c=>{
        const aid=byId(c);
        const qualification=c.eligible?"可发布":window.TTFirstUse.needed(c)?"待选观看范围":"受限";
        const details=[c.drama_language,audience(c),"28 天 "+money(c.revenue_28d),"累计 "+money(c.revenue_total),"粉丝 "+followersText(c.followers),
          ({yes:"有旧排期/冲突",unknown:"旧排期待核验"})[conflicts(c)]].filter(Boolean);
        if(!onlySelected)return `<label><input type="checkbox" value="${h(aid)}" ${selected.has(aid)?"checked":""}><span>${h(name(c))} · #${h(aid)}<small class="secondary">${h([...details,qualification].join(" · "))}</small></span></label>`;
        return `<label><input type="checkbox" value="${h(aid)}" ${selected.has(aid)?"checked":""}><span><strong>${h(name(c))}</strong> <small>#${h(aid)}</small><small class="secondary">${h(details.join(" · "))}</small></span>${badge(qualification,c.eligible?"positive":"warning")}</label>`;
      }).join("")||'<span class="muted">无匹配频道</span>';
      wrapper.querySelector("[data-first-use]").hidden=!canSetChannels || !rows.some(c=>selected.has(byId(c))&&window.TTFirstUse.needed(c));
      wrapper.querySelector("[data-count]").textContent=`已选 ${selected.size} 个 · 当前结果 ${visible.length} 个`;
    }
    wrapper.querySelector(".picker-filters").addEventListener("input",draw);
    if(onlySelected)onlySelected.onchange=draw;
    list.onchange=e=>{if(e.target.checked)selected.add(e.target.value);else selected.delete(e.target.value);const aid=e.target.value;draw();(Array.from(list.querySelectorAll("input")).find(input=>input.value===aid)||onlySelected)?.focus({preventScroll:true});onChange();};
    wrapper.querySelector("[data-add]").onclick=()=>{visible.forEach(c=>selected.add(byId(c)));draw();onChange();};
    wrapper.querySelector("[data-clear]").onclick=()=>{selected.clear();draw();onChange();};
    action(wrapper.querySelector("[data-first-use]"),"click",async()=>{
      const changed=await window.TTFirstUse.open(rows.filter(c=>selected.has(byId(c))));
      if(!changed)return;
      const current=await get("/channels");
      channels=current.items;
      const byAccount=new Map(channels.map(c=>[byId(c),c]));
      rows=rows.map(c=>byAccount.get(byId(c))||{...c,eligible:false,auth_eligible:false,reason:"账号当前不可用"});
      draw();onChange();
    });
    draw();return ()=>rows.map(byId).filter(x=>selected.has(x));
  }
  function guardClose(changed){
    allowClose=changed;
  }
  async function closeEditor(){
    if(allowClose?.() && !await confirmAction("舍弃未保存的更改？","关闭后，本次编辑的内容将丢失。","舍弃更改"))return;
    allowClose=null;id("dialog").close();
  }
  id("dialog").addEventListener("cancel",ev=>{if(allowClose?.()){ev.preventDefault();void closeEditor();}});
  id("dialog").querySelector("form[method=dialog]").addEventListener("submit",ev=>{if(allowClose?.()){ev.preventDefault();void closeEditor();}});
  id("dialog").addEventListener("close",()=>{if(!id("dialog").open){allowClose=null;detailEpoch++;}});
  window.addEventListener("beforeunload",ev=>{if(editorDirty||(id("dialog").open&&allowClose?.())){ev.preventDefault();ev.returnValue="";}});



  const RESOURCE_OPTIONS = [
    { value: "0", label: "其他" },
    { value: "1", label: "翻译剧非首发" },
    { value: "2", label: "本土首发" },
    { value: "3", label: "本土对投" },
    { value: "4", label: "本土二轮采买" },
    { value: "5", label: "本土自制" },
    { value: "6", label: "翻译剧首发" },
    { value: "7", label: "首发本土动态漫" },
    { value: "8", label: "二轮本土动态漫" },
    { value: "9", label: "首发翻译动态漫" },
    { value: "10", label: "二轮翻译动态漫" },
    { value: "11", label: "翻译剧自制" },
    { value: "12", label: "漫剧自制" },
    { value: "13", label: "AI本土真人剧自制" },
    { value: "14", label: "AI本土真人剧首发" },
    { value: "15", label: "二轮本土AI真人剧" },
    { value: "16", label: "翻译AI真人剧首发" },
    { value: "17", label: "二轮翻译AI真人剧" },
    { value: "18", label: "AI本土解说剧自制" },
    { value: "19", label: "AI本土解说剧首发" },
    { value: "20", label: "AI本土解说剧二轮" },
    { value: "21", label: "AI翻译解说剧首发" },
    { value: "22", label: "AI翻译解说剧首发" },
    { value: "100", label: "小说" },
  ];
  const typeNames = new Map(RESOURCE_OPTIONS.map(item => [item.value, item.label]));

  function resourceTypes(rule) {
    const chosen = new Set((rule.resource_type_v2 || []).map(String));
    const values = new Set([...typeNames.keys(), ...chosen]);
    return `<details class="resource-options"><summary>短剧类型 <span data-type-count></span></summary>
      <div class="resource-grid">${[...values].map(value => `<label><input type="checkbox" name="resource_type" value="${h(value)}" ${chosen.has(value)?"checked":""}>${h(typeNames.get(value)||"其他类型")} · ${h(value)}</label>`).join("")}</div>
      <button class="row-link" type="button" data-clear-types>清空类型（不限）</button></details>`;
  }

  const effectSources={x:"X",facebook:"Facebook",all:"X + Facebook 合计",tiktok:"TikTok"};
  const effectMetrics={x_views:"播放",x_likes:"点赞",x_comments:"评论",x_shares:"分享",x_revenue:"归属收入"};
  const sortNames={roas:"D0 ROAS",spend:"消耗",...effectMetrics};

  function ruleCard(key, c) {
    const drama = key === "drama", label = drama ? "剧目" : "素材";
    const rule = {...(drama?{}:{duration_min_seconds:0,duration_max_seconds:3600}),...c[key+"_rule"]};
    function range(title, low, high, unit, limits = {}) {
      const inputs = [[low,"下限"],[high,"上限"]].map(([name, bound]) =>
        `<input type="number" name="${key}_${name}" aria-label="${label}${title}${bound}" min="0" ${limits.max ? `max="${limits.max}"` : ""} step="${limits.step||"any"}" value="${h(rule[name] ?? "")}" placeholder="${bound}不限">`
      );
      return `<fieldset class="range-field"><legend>${title}（${unit}）</legend><div>${inputs.join('<span aria-hidden="true">至</span>')}</div></fieldset>`;
    }
    return `<section class="rule-card"><h3>${drama?"先筛选剧目":"再筛选素材"}</h3>
      ${drama ? resourceTypes(rule) : range("素材时长","duration_min_seconds","duration_max_seconds","秒",{max:3600})}
      ${range("消耗","spend_min","spend_max","USD")}${range("D0 ROAS","roas_min","roas_max","%")}
      <details class="effect-options" ${Object.keys(effectMetrics).some(k=>rule[k+"_min"]!=null||rule[k+"_max"]!=null||rule.sort_by===k)||rule.effect_channel&&rule.effect_channel!=="x"?"open":""}><summary>社媒表现筛选</summary>
        <label class="field">社媒指标来源<select name="${key}_effect_channel">${Object.entries(effectSources).map(([k,v])=>`<option value="${k}">${v}</option>`).join("")}</select></label>
        <p class="muted" data-effect-info="${key}"></p>
        ${Object.entries(effectMetrics).map(([metric,title])=>`<div data-effect-range="${key}_${metric}">${range(title,metric+"_min",metric+"_max",metric==="x_revenue"?"USD":"次",{step:metric==="x_revenue"?"0.01":"1",max:1e15})}</div>`).join("")}</details>
      <div class="form-grid"><label>排序指标<select name="${key}_sort_by">${Object.entries(sortNames).map(([v,label])=>`<option value="${v}">${label}</option>`).join("")}</select></label>
      <label>排序方向<select name="${key}_sort_direction"><option value="desc">从高到低</option><option value="asc">从低到高</option></select></label></div></section>`;
  }

  function metricValues(form, key, previous) {
    const controls = form.elements, rule = {...previous};
    const ranges = [["spend_min","spend_max","消耗"],["roas_min","roas_max","D0 ROAS"]];
    rule.effect_channel=controls[key+"_effect_channel"].value;
    for(const [metric,label] of Object.entries(effectMetrics))ranges.push([metric+"_min",metric+"_max",label]);
    if(key==="material") ranges.push(["duration_min_seconds","duration_max_seconds","时长"]);
    for(const [low,high,label] of ranges) {
      for(const name of [low,high]) {
        const input=controls[key+"_"+name],value=input.disabled?"":input.value;
        rule[name] = value==="" ? null : Number(value);
      }
      if(rule[low]!=null && rule[high]!=null && rule[low]>rule[high]) {
        controls[key+"_"+low].focus();
        throw new Error((key==="drama"?"剧目":"素材")+label+"下限不能超过上限。");
      }
    }
    if(key==="drama") rule.resource_type_v2=Array.from(form.querySelectorAll("[name=resource_type]:checked"),input=>input.value);
    rule.sort_by=controls[key+"_sort_by"].value;
    rule.sort_direction=controls[key+"_sort_direction"].value;
    return rule;
  }

  function syncEffectSource(form,key){
    const e=form.elements,source=e[key+"_effect_channel"].value,sort=e[key+"_sort_by"];
    for(const metric of Object.keys(effectMetrics)){
      const disabled=source==="tiktok"&&metric!=="x_revenue";
      const range=form.querySelector(`[data-effect-range="${key}_${metric}"]`);
      range.hidden=disabled;
      for(const control of range.querySelectorAll("input"))control.disabled=disabled;
      sort.querySelector(`option[value="${metric}"]`).disabled=disabled;
      if(disabled&&sort.value===metric)sort.value="x_revenue";
    }
    form.querySelector(`[data-effect-info="${key}"]`).textContent=source==="tiktok"?"TikTok 按已回传的累计归属收入筛选和排序。":source==="all"?"合计 X 和 Facebook 已回传的累计社媒表现。":"按当前来源已回传的累计社媒表现筛选和排序。";
  }

  function launchWindow(input){
    let value=input.value.trim().replaceAll("～","~");
    if((value.startsWith("[")&&value.endsWith("]"))||(value.startsWith("【")&&value.endsWith("】")))value=value.slice(1,-1).trim();
    if(/^\d{1,10}$/.test(value)&&Number(value)<=3650)return Number(value);
    const span=value.match(/^(-?\d+)\s*~\s*(-?\d+)$/);
    if(span&&Number(span[1])>=-3650&&Number(span[2])<=3650&&Number(span[1])<=Number(span[2]))return Number(span[1])+"~"+Number(span[2]);
    input.focus();throw new Error("剧目上线窗口请填 0、天数或从小到大的相对天数区间，例如 -1~0。");
  }

  function setupSchedule(form, schedule, changed) {
    const e=form.elements, list=form.querySelector("[data-time-list]"), add=form.querySelector("[data-add-time]");
    function addTime(value) {
      const row=document.createElement("div");
      row.className="edit-time-row";
      row.innerHTML=`<input type="time" aria-label="北京时间" required><button type="button" class="row-link">移除</button>`;
      const input=row.querySelector("input");
      input.value=value;
      row.querySelector("button").onclick=()=>{
        row.remove();
        sync();
        add.focus();
        changed();
      };
      list.append(row);
      return input;
    }
    function sync() {
      const fixed=e.schedule_mode.value==="fixed";
      form.querySelector("#fixed-times").disabled=!fixed;
      form.querySelector("#fixed-times").hidden=!fixed;
      const count=form.querySelector("#random-count");
      count.hidden=fixed;
      e.count.disabled=fixed;
      add.disabled=list.children.length>=24;
    }
    const times=()=>Array.from(list.querySelectorAll("input"),input=>input.value);
    e.schedule_mode.value=schedule?.mode||"fixed";
    for(const value of schedule?.times || ["12:30"]) addTime(value);
    add.onclick=()=>{addTime("").focus();sync();changed();};
    for(const radio of form.querySelectorAll("[name=schedule_mode]")) radio.onchange=sync;
    sync();
    return times;
  }

  const numberField=(name,label,value,min,max)=>`<label>${h(label)}<input type="number" min="${min}" max="${max}" step="1" name="${name}" value="${h(value)}" required></label>`;

  function edit(t, preselected=[]) {
    const c=t?cfg(t):{}, fresh=!t, area=id("editor-view");
    const heading=document.querySelector(".topbar h1");
    const title=fresh?"创建自动发布模板":`编辑模板 · ${tName(t)}`;
    heading.textContent=title;
    document.title=title+" · TT 平台推广";
    id("list-view").hidden=true;
    area.hidden=false;
    editorDirty=false;
    const panel=(title,content)=>`<section class="panel editor-section"><h2>${title}</h2>${content}</section>`;
    area.innerHTML=`<div class="editor-nav"><button type="button" class="row-link" data-editor-back>← 返回模板列表</button><button type="button" class="row-link" data-jump-summary>查看保存摘要 ↓</button></div><form id="edit-template"><div class="editor-main">
      ${panel("基础信息",`<div class="form-grid basic-fields">
        <label class="stretch">模板名称<input name="name" value="${h(t?.name||"")}" required maxlength="120" placeholder="例如：英语频道晚间推广"></label>
        <label>选材方式<select name="model"><option value="income">频道收入画像</option><option value="rules">模板规则</option></select></label>
        <label>视频制作<select name="video_template"><option value="random_overlay">随机模板</option><option value="direct_outro">固定教程片尾</option></select></label></div>`)}
      ${panel("选择 TT 频道",picker(true))}
      ${panel("两层筛选与排序",`<div class="window-fields form-grid">
        ${numberField("metric_window_days","投放指标统计（天）",c.metric_window_days??7,1,30)}
        <label>剧目上线窗口（相对天数）<input name="drama_launch_window_days" value="${h(c.drama_launch_window_days??30)}" placeholder="0、30 或 -1~0" required></label>
        ${numberField("material_window_days","素材入库窗口（天）",c.material_window_days??30,0,3650)}
        ${numberField("cooldown_days","同频道同剧冷却（天）",c.cooldown_days??7,0,3650)}</div>
        <p class="window-guide">剧目窗口 0 包含所有已上线剧；填 7 表示近 7 天上线，填 -1~0 包含基准时刻至未来 24 小时上线的剧。素材窗口按入库时间计算，0 表示不限。预演与执行会核对同剧冷却、已用素材和在途占用。</p>
        <div class="rule-flow">${ruleCard("drama",c)}${ruleCard("material",c)}</div>`)}
      ${panel("发布时间",`<div class="schedule-options" role="radiogroup" aria-label="排期方式">
        <label><input name="schedule_mode" type="radio" value="fixed">固定时间</label>
        <label><input name="schedule_mode" type="radio" value="random">每天随机</label></div>
        <fieldset id="fixed-times"><legend>每日时间点（北京时间）</legend><div data-time-list></div>
        <button type="button" class="button" data-add-time>添加时间点</button></fieldset>
        <label id="random-count" class="field">每频道每日条数<input type="number" min="1" max="24" step="1" required name="count" value="${h(c.schedule?.daily_count??1)}"></label>`)}
      ${panel("发布文案",`<div class="form-grid"><label>文案来源<select name="ai_copy"><option value="no">模板文案</option><option value="yes">AI 优先，失败用模板稿</option></select></label>
        <label class="stretch">模板文案<textarea name="caption_template" rows="3" maxlength="2200" required>${h(c.caption_template??"{desc} {code}")}</textarea></label></div>
        <div class="macro-tools"><span>插入变量</span>${[["{desc}","素材描述"],["{{drama_name}}","剧名"],["{code}","四位码"],["{url}","归因链接"]].map(([v,label])=>`<button class="row-link" type="button" data-macro="${h(v)}">${label}</button>`).join("")}</div>`)}
      </div><aside class="panel editor-summary" tabindex="-1" aria-label="保存摘要"><h2>保存摘要</h2><div id="edit-volume"><strong id="edit-summary"></strong><small id="edit-times"></small></div>
      <dl>${[["version","模板版本"],["accounts","已选频道"],["languages","剧语言"],["model","选材方式"],["window","投放指标统计"],["drama","剧目排序"],["material","素材排序"],["video","视频制作"],["copy","文案来源"]].map(([key,title])=>`<div><dt>${title}</dt><dd data-summary="${key}"></dd></div>`).join("")}</dl>
      <p class="muted">${fresh?"草稿保存后可预演，启用后按排期执行。":t.enabled?"当前模板已启用。新版本用于后续排期。":"保存后可继续预演此版本。"}</p>
      <button class="button primary" type="submit">${fresh?"保存草稿":"保存新版本"}</button>
      <button class="button" type="button" data-return>返回模板列表</button></aside></form>`;
    const form=id("edit-template"), e=form.elements;
    e.model.value=c.model||"income";
    e.video_template.value=c.video_template||"random_overlay";
    e.ai_copy.value=c.ai_copy?"yes":"no";
    for(const key of ["drama","material"]){
      e[key+"_sort_by"].value=c[key+"_rule"]?.sort_by||"roas";
      e[key+"_sort_direction"].value=c[key+"_rule"]?.sort_direction||"desc";
      e[key+"_effect_channel"].value=c[key+"_rule"]?.effect_channel||"x";
      syncEffectSource(form,key);
      e[key+"_effect_channel"].oninput=()=>syncEffectSource(form,key);
    }
    const initial=(fresh?preselected:c.account_ids||[]).map(String);
    const getIds=setupPicker(form,pickerRows(initial),initial,changed);
    const getTimes=setupSchedule(form,c.schedule,changed);
    const text=control=>control.selectedOptions[0]?.textContent||"—";
    function summary() {
      const ids=getIds(), fixed=e.schedule_mode.value==="fixed", times=getTimes().filter(Boolean);
      const count=fixed?new Set(times).size:Number(e.count.value)||0;
      id("edit-summary").textContent=`${ids.length} 个频道 × 每日 ${count} 条 = ${ids.length*count} 条计划`;
      id("edit-times").textContent=fixed?`北京时间 · ${times.join("、")||"待设置时间点"}`:"北京时间 · 各频道随机排期";
      const typeCount=form.querySelectorAll("[name=resource_type]:checked").length;
      form.querySelector("[data-type-count]").textContent=typeCount?`已选 ${typeCount} 类`:"不限";
      const sorts=key=>(e[key+"_sort_by"].value.startsWith("x_")?text(e[key+"_effect_channel"])+" · ":"")+text(e[key+"_sort_by"])+" · "+text(e[key+"_sort_direction"]);
      const selectedIds=new Set(ids);
      const languages=[...new Set(channels.filter(ch=>selectedIds.has(byId(ch))).map(ch=>ch.drama_language||"待确认"))];
      const fields={languages:languages.join("、")||"—",version:fresh?"新草稿":`当前 v${t.version}`,accounts:ids.length+" 个",model:text(e.model),window:(e.metric_window_days.value||"—")+" 天",drama:sorts("drama"),material:sorts("material"),video:text(e.video_template),copy:text(e.ai_copy)};
      for(const node of form.querySelectorAll("[data-summary]"))node.textContent=fields[node.dataset.summary];
    }
    function changed(){editorDirty=true;summary();}
    form.addEventListener("input",ev=>{if(!ev.target.closest("[data-picker]"))changed();});
    form.querySelector("[data-clear-types]").onclick=()=>{
      for(const input of form.querySelectorAll("[name=resource_type]"))input.checked=false;
      changed();
    };
    form.querySelector(".macro-tools").onclick=ev=>{
      const macro=ev.target.closest("[data-macro]")?.dataset.macro;
      if(!macro)return;
      const field=e.caption_template;
      field.setRangeText(macro,field.selectionStart,field.selectionEnd,"end");
      field.focus();
      changed();
    };
    function leave() {
      editorDirty=false;
      history.replaceState(null,"",location.pathname);
      area.hidden=true;
      area.replaceChildren();
      id("list-view").hidden=false;
      heading.textContent="自动发布（新版）";
      document.title="自动发布（新版） · TT 平台推广";
      id("new-template").focus({preventScroll:true});
      window.scrollTo(0,0);
    }
    async function cancelEdit(){
      if(editorDirty&&!await confirmAction("舍弃未保存的更改？","本次更改尚未保存。","舍弃更改"))return;
      leave();
      notify("");
    }
    area.querySelector("[data-editor-back]").onclick=cancelEdit;
    form.querySelector("[data-return]").onclick=cancelEdit;
    area.querySelector("[data-jump-summary]").onclick=()=>form.querySelector(".editor-summary").focus();
    summary();
    action(form,"submit",async ev=>{
      ev.preventDefault();
      const button=form.querySelector("button[type=submit]");
      if(button.disabled)return;
      button.disabled=true;
      try {
        const account_ids=getIds();
        if(!account_ids.length)throw new Error("至少选择一个目标频道。");
        const fixed=e.schedule_mode.value==="fixed", times=getTimes();
        if(fixed&&(!times.length||times.some(v=>!v)||new Set(times).size!==times.length))throw new Error("请至少填写一个北京时间，各时间点不能重复。");
        const body={name:e.name.value.trim(),account_ids,model:e.model.value,video_template:e.video_template.value,
          schedule:fixed?{mode:"fixed",times}:{mode:"random",daily_count:Number(e.count.value)},ai_copy:e.ai_copy.value==="yes",caption_template:e.caption_template.value.trim()};
        for(const k of ["metric_window_days","material_window_days","cooldown_days"])body[k]=Number(e[k].value);
        body.drama_launch_window_days=launchWindow(e.drama_launch_window_days);
        for(const prefix of ["drama","material"])body[prefix+"_rule"]=metricValues(form,prefix,c[prefix+"_rule"]||{});
        if(t)body.expected_version=t.version;
        await post(t?path(t):templatesPath,body);
        leave();
        await updateTemplates();
        notify(fresh?"草稿已保存，可预演频道分配。":"模板新版本已保存。");
      }finally{button.disabled=false;}
    });
  }
  async function toggle(t){
    const verb=t.enabled?"停用":"启用";
    if(!await confirmAction(`${verb}模板`,`${tName(t)} · 版本 ${t.version}\n${scheduleText(cfg(t).schedule)}，共 ${cfg(t).account_ids.length} 个频道。${t.enabled?"已进入发布执行的任务将继续处理。":"启用后将依照该排期创建真实发布任务。"}`,verb))return;
    await post(path(t,t.enabled?"/disable":"/enable"),{expected_version:t.version});
    await updateTemplates();notify(`模板已${verb}。`);
  }
  const labels={female:"女性",male:"男性",lgbtq:"LGBTQ+",general:"通用",__unknown__:"未标注",ceo_wealth:"豪门总裁",sweet_romance:"甜宠恋爱",marital:"婚恋纠葛",family:"萌宝家庭",revenge:"逆袭复仇",period_intrigue:"古装权谋",fantasy:"奇幻穿越",suspense:"悬疑冒险",growth:"生活成长"};
  const reasons={preferred_match:"命中首选受众和题材",preferred_theme_unavailable:"首选题材缺少可用素材，已在同一受众内放宽",preferred_audience_unavailable:"首选受众缺少可用素材，已改选其他受众",no_valid_audience_profile:"暂无可用受众画像，按模板排序",no_valid_theme_profile:"暂无可用题材画像，按受众偏好排序",no_eligible_material:"没有符合规则的素材",no_positive_revenue:"尚无正收入画像，按模板排序",unavailable:"画像暂不可用，按模板排序"};
  function audienceTheme(decision,kind){
    if(!decision)return "";
    return [decision[kind+"_audience"],decision[kind+"_theme"]].filter(Boolean).map(key=>labels[key]||key).join(" / ");
  }
  function why(value){
    if(value==null)return "";
    if(typeof value==="string")return reasons[value]||value;
    return [value.source,value.reason||value.message||value.code,value.scheduled_at_utc?time(value.scheduled_at_utc):""].filter(Boolean).join(" · ");
  }

  function beijingIso(value){
    if(!value)return "";
    if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(value))throw new Error("请填写有效的北京时间。");
    return new Date(value+":00+08:00").toISOString();
  }
  function splitIds(value){return value.trim().split(/[\s,，、]+/).filter(Boolean);}


  function metricSummary(layer,value,rule){
    const n=v=>v==null?"—":h(v);
    const limit=(lo,hi)=>lo==null&&hi==null?"不限":`${lo==null?"不限":lo}–${hi==null?"不限":hi}`;
    const sorting=sortNames[rule?.sort_by||"roas"]||"D0 ROAS";
    const thresholds=rule?`阈值：消耗 ${h(limit(rule.spend_min,rule.spend_max))}，ROAS ${h(limit(rule.roas_min,rule.roas_max))}% · ${h(sorting)}${rule.sort_direction==="asc"?"升序":"降序"}`:"此项使用历史冻结版本";
    return `<div class="tool-pair"><strong>${layer}</strong> 消耗 ${n(value?.spend)} USD · D0 ROAS ${value?.d0_roas==null?"—":n(value.d0_roas)+"%"}<small class="secondary">${thresholds}</small></div>`;
  }
  function effectiveSchedule(row){
    const effective=row.scheduled_at_utc,original=row.planned_at_utc,release=row.release_at_utc;
    const changed=original&&effective&&Date.parse(original)!==Date.parse(effective);
    return `<small class="secondary">有效排期（北京） ${h(time(effective))}</small>${changed?`<small class="secondary">原排期 ${h(time(original))}</small>`:""}${release?`<small class="secondary">剧目上线 ${h(time(release))}</small>`:""}`;
  }
  function effectProof(effect,rule){
    if(!effect){
      const expected=Object.keys(effectMetrics).some(k=>rule?.sort_by===k||rule?.[k+"_min"]!=null||rule?.[k+"_max"]!=null);
      return expected?'<small class="secondary">社媒证据 —</small>':"";
    }
    const channel=effectSources[effect.channel]||effect.channel||"—";
    const available=Object.keys(effectMetrics).filter(key=>effect.channel!=="tiktok"||key==="x_revenue");
    const facts=available.map(key=>{
      const field=key.slice(2),value=effect.metrics?.[field],coverage=effect.coverage?.[field];
      const format=n=>field==="revenue"?money(n):Number(n).toLocaleString("zh-CN");
      const shown=value==null||coverage===false?"—":format(value);
      const subtotal=effect.known?.[field];
      const measured=value==null&&subtotal!=null?`已回传小计 ${format(subtotal)}`:"";
      const covered=Number.isInteger(coverage)&&effect.posts!=null?`已回传 ${coverage}/${effect.posts} 条`:"";
      const min=rule?.[key+"_min"],max=rule?.[key+"_max"];
      const threshold=[min!=null?`≥ ${min}`:"",max!=null?`≤ ${max}`:""].filter(Boolean).join("，");
      const note=[measured,covered,threshold?"规则 "+threshold:""].filter(Boolean).join(" · ");
      return `<li>${h(effectMetrics[key])} ${h(shown)}${note?`<small class="muted">（${h(note)}）</small>`:""}</li>`;
    });
    const versions=Object.entries(effect.versions||{}).filter(([,v])=>v!=null&&v!=="").map(([k,v])=>(effectSources[k]||k)+" "+v);
    return `<details class="social-evidence"><summary>${h(channel)} 社媒证据${effect.posts!=null?` · ${h(effect.posts)} 条发布`:""}</summary><ul>${facts.join("")}</ul>${versions.length?`<small class="secondary">数据版本 ${h(versions.join(" · "))}</small>`:""}${effect.as_of?`<small class="secondary">数据截至 ${h(effect.as_of)}</small>`:""}</details>`;
  }
  function execution(t){
    const c=cfg(t),scope=(c.account_ids||[]).map(String),today=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    let preview=null,seq=0,page=1,filter="",allRows=null,previewKey="",dirty=false,busy=false,draftSeq=0,submitting=false;
    const adopted=new Map(),drafts=new Map(),draftKey=(a,m)=>`${a}/${m}`,size=8;
    modal(`${tName(t)} · 预演与执行`,`<form id="execution"><fieldset class="group"><legend>本次任务</legend><div class="form-grid">
      <label>推广方式<select name="mode">${Object.entries(modeNames).map(([k,v])=>`<option value="${k}">${v}</option>`).join("")}</select></label>
      <label>时间方式<select name="when"><option value="plan">模板日排期（仅预演）</option><option value="now">立即执行</option><option value="at">一次性预约</option></select></label>
      <label data-day>计划日期（北京）<input name="plan_date" type="date" value="${today}"></label><label data-when hidden>预约时间（北京）<input name="scheduled_at" type="datetime-local"></label>
      <label data-mode="drama" hidden>单语言剧目 ID（可多条）<textarea name="content_ids" rows="2" placeholder="每行一个 Drama ID"></textarea></label>
      <label data-mode="drama" hidden>跨语言资源 ID（业务系列码，可多条）<textarea name="resource_ids" rows="2" placeholder="每行一个资源 ID"></textarea></label>
      <label data-mode="serial" hidden>连载剧目 ID<input name="content_id"></label><label data-mode="material" hidden>素材 ID<input name="material_id"></label>
      <label data-mode="pool serial" class="stretch" hidden><span id="ids-label"></span><textarea name="inventory" rows="2" placeholder="可逐行粘贴；留空沿用已保存的库存或集序"></textarea></label></div></fieldset>
      ${picker()}<label class="field">整批人工稿（可选）<textarea name="manual_caption" rows="2"></textarea></label>
      <p class="muted">留空时按模板生成。单条 AI 稿可在预演结果中采用或编辑。</p>
      <div id="adopted-copy" class="adopted-copy"></div><button class="button primary" type="submit">只读预演</button></form>
      <div id="preview-feedback" role="status" class="preview-signal"></div><section id="preview-output"></section>
      <footer class="review-footer" id="review-footer" hidden><div><strong id="review-title"></strong><span id="review-note" class="muted"></span></div><button class="button primary" id="execute" type="button" disabled>确认执行</button></footer>`);
    const form=id("execution"),e=form.elements,area=id("preview-output"),feedback=id("preview-feedback"),foot=id("review-footer");
    const getIds=setupPicker(form,pickerRows(scope).filter(ch=>scope.includes(byId(ch))),scope,reset);
    const sourceLabel=d=>d.source==="ai"?"AI 草稿":d.source==="template"?"模板回退":"草稿";
    function live(epoch){return epoch===seq && form.isConnected && id("dialog").open;}
    function reset(){
      seq++;draftSeq++;busy=false;preview=null;previewKey="";allRows=null;filter="";page=1;drafts.clear();area.replaceChildren();foot.hidden=true;feedback.textContent="";
      form.querySelector("[type=submit]").disabled=false;
      for(const [k,v] of adopted)if(!getIds().includes(String(v.account_id)))adopted.delete(k);
      adoptedList();
    }
    function displayFields(){
      for(const field of form.querySelectorAll("[data-mode]"))field.hidden=!field.dataset.mode.split(" ").includes(e.mode.value);
      form.querySelector("[data-day]").hidden=e.when.value!=="plan";form.querySelector("[data-when]").hidden=e.when.value!=="at";id("ids-label").textContent=e.mode.value==="serial"?"按集序排列的素材 ID":"候选素材 ID";
    }
    const onInput=ev=>{if(ev.target.closest("[data-picker],#adopted-copy"))return;if(ev.target===e.manual_caption)dirty=true;reset();displayFields();};
    form.addEventListener("input",onInput);form.addEventListener("change",onInput);displayFields();guardClose(()=>dirty);
    function values(){
      const mode=e.mode.value,account_ids=getIds();if(!account_ids.length)throw new Error("请选择参与预演的频道。");
      const p={expected_version:t.version,mode,account_ids};
      if(e.when.value==="plan"){p.plan_date=e.plan_date.value;if(!p.plan_date)throw new Error("请选择计划日期。");}
      if(e.when.value==="at"){p.scheduled_at=beijingIso(e.scheduled_at.value);if(!p.scheduled_at)throw new Error("请选择预约时间。");}
      if(mode==="drama"){
        p.content_ids=[...new Set(splitIds(e.content_ids.value))];p.resource_ids=[...new Set(splitIds(e.resource_ids.value))];
        if(p.resource_ids.some(v=>!/^(?:[1-9][0-9]{0,30}|[A-Za-z][A-Za-z0-9_-]{0,63})$/.test(v)))throw new Error("请输入有效的业务系列码，例如 12345 或 DW-A01。");
        if(!p.content_ids.length&&!p.resource_ids.length)throw new Error("请输入至少一个剧目或资源 ID。");
      }
      if(mode==="serial"){p.content_id=e.content_id.value.trim();if(!p.content_id)throw new Error("请输入连载剧目 ID。");}
      if(mode==="material"){p.material_id=e.material_id.value.trim();if(!p.material_id)throw new Error("请输入指定素材 ID。");}
      if(mode==="serial"||mode==="pool"){
        const ids=splitIds(e.inventory.value);if(new Set(ids).size!==ids.length)throw new Error("素材 ID 有重复，请检查。");
        if(ids.length)p[mode==="serial"?"episodes":"material_ids"]=ids;
      }
      const caption=e.manual_caption.value.trim();if(caption)p.manual_caption=caption;
      return p;
    }
    function adoptedList(){
      const container=id("adopted-copy");if(!container)return;
      container.innerHTML=[...adopted.entries()].map(([key,d])=>`<div class="batch-copy"><strong>频道 #${h(d.account_id)} · 素材 ${h(d.material_id)} · ${h(d.text===d.caption?sourceLabel(d)+"已采用":"已编辑草稿")}</strong><label class="field">本条用稿<textarea data-copy-text="${h(key)}" rows="2">${h(d.text)}</textarea></label><button type="button" class="row-link" data-remove-copy="${h(key)}">移除此条用稿</button></div>`).join("");
    }
    id("adopted-copy").addEventListener("input",ev=>{const key=ev.target.dataset.copyText;if(!key)return;const d=adopted.get(key);d.text=ev.target.value;dirty=true;ev.target.closest(".batch-copy").querySelector("strong").textContent=`频道 #${d.account_id} · 素材 ${d.material_id} · ${d.text===d.caption?"已采用":"已编辑草稿"}`;review();});
    id("adopted-copy").addEventListener("click",ev=>{const b=ev.target.closest("[data-remove-copy]");if(b){adopted.delete(b.dataset.removeCopy);dirty=true;adoptedList();paint();}});
    function eligible(row){return ["eligible","ready","selected"].includes(row.status)&&!(row.conflicts||[]).length;}
    function inspect(row){
      const reason=[...new Set([why(row.reason),...(row.conflicts||[]).map(why)].filter(Boolean))].join("；")||"—";
      const first=audienceTheme(row.decision,"preferred"),hit=audienceTheme(row.decision,"selected");
      return `${h(reason)}${first?`<small class="secondary">首选 · ${h(first)}</small>`:""}${hit?`<small class="secondary">命中 · ${h(hit)}</small>`:""}`;
    }
    function draftCell(row){
      if(busy||!c.ai_copy||rawPayload.manual_caption||!eligible(row)||!row.material_id||preview.template_version!=null&&preview.template_version!==t.version)return "";
      const k=draftKey(row.account_id,row.material_id),d=drafts.get(k);
      if(adopted.has(k))return '<small class="secondary">已采用本条稿件</small>';
      if(!d)return `<button class="row-link" data-draft-one="${h(k)}">生成单条 AI 稿</button>`;
      if(d.state==="final"&&d.caption)return `<div class="batch-copy"><small>${h(sourceLabel(d))}</small><p class="caption">${h(d.caption)}</p><button class="row-link" data-adopt="${h(k)}">采用此条稿件</button></div>`;
      if(d.state==="error")return `<small>${h(d.reason)}</small><button class="row-link" data-draft-one="${h(k)}">重试生成</button>`;
      return '<small class="secondary">AI 稿生成中…</small>';
    }
    function displayRow(row){
      const account=channels.find(ch=>byId(ch)===String(row.account_id)),effects=row.social_effects||row.choice?.social_effects;
      const historical=preview.template_version!=null&&preview.template_version!==t.version;
      const dramaRule=historical?null:c.drama_rule||{},materialRule=historical?null:c.material_rule||{};
      return `<tr><td>${h(account?name(account):row.account_id)}<small class="secondary">#${h(row.account_id)}</small>${effectiveSchedule(row)}</td><td>${statusBadge(row.status)}</td><td>${h(row.drama_name||row.content_id||"—")}<small class="secondary">${h(row.content_id||"")} · 素材 ${h(row.material_id||"—")}</small></td><td>${metricSummary("剧目",row.drama_metrics,dramaRule)}${effectProof(effects?.drama,dramaRule)}<hr>${metricSummary("素材",row.material_metrics,materialRule)}${effectProof(effects?.material,materialRule)}</td><td>${inspect(row)}</td><td class="caption">${h(row.caption||"—")}${draftCell(row)}</td></tr>`;
    }
    let rawPayload={},shown=[];
    function summary(){
      if(!preview)return {};
      const data=preview.summary||{},rows=allRows||preview.items||[];
      return {total:preview.total??rows.length,eligible:data.eligible??rows.filter(eligible).length,skipped:data.skipped??rows.filter(r=>!eligible(r)).length,pending:data.pending??0};
    }
    function review(){
      if(!preview||!form.isConnected)return;
      const sum=summary();foot.hidden=false;
      id("review-title").textContent=`${sum.eligible} 个可执行时段 · ${sum.skipped} 个跳过 · ${adopted.size} 条采用稿`;
      id("review-note").textContent=rawPayload.plan_date?"日排期仅供预演；立即或一次性预约可提交本批。":"提交前按平台能力和素材占用复核，差异记录在批次详情。";
      id("execute").hidden=!!rawPayload.plan_date;id("execute").disabled=busy||submitting||!!rawPayload.plan_date||!previewKey||!sum.eligible;
      id("execute").textContent=rawPayload.scheduled_at?"确认预约":"确认执行";
    }
    function paint(){
      if(!preview || !form.isConnected)return;
      const sum=summary(),rows=allRows||preview.items||[],source=filter?rows.filter(row=>filter==="eligible"?eligible(row):filter==="conflict"?(row.conflicts||[]).length:!eligible(row)):rows;
      const count=filter?source.length:sum.total;
      shown=allRows?source.slice((page-1)*size,page*size):source;
      const covered=allRows||sum.total<=rows.length?new Set(rows.filter(eligible).map(row=>String(row.account_id))).size:null;
      area.innerHTML=`<div class="preview-header"><h3>预演结果 · ${h(sum.total)} 个频道任务</h3><span class="muted">${h(rawPayload.plan_date||"本次执行")} · v${h(preview.template_version??t.version)}${covered==null?"":` · 当前候选覆盖 ${covered} 个频道`}</span></div>
      <div class="batch-summary">${[["","全部 "+sum.total],["eligible","可执行 "+sum.eligible],["skipped","跳过 "+sum.skipped],["conflict","仅排期冲突"]].map(([k,l])=>`<button type="button" data-pfilter="${k}" class="${filter===k?"selected":""}" aria-pressed="${filter===k}">${h(l)}</button>`).join("")}</div>
      <div class="preview-tools">${c.ai_copy&&!rawPayload.manual_caption&&shown.some(eligible)?'<button type="button" class="row-link" data-draft-all>为本页可用候选生成 AI 稿</button>':""}${shown.some(r=>r.conflicts?.length&&!r.material_id)?'<button type="button" class="row-link" data-conflict-info>查看冲突频道原候选</button>':""}${rawPayload.mode==="pool"&&covered==null?'<button type="button" class="row-link" data-coverage>统计可覆盖频道</button>':""}</div>
      <div class="table-wrap"><table><thead><tr><th>频道 / 排期</th><th>状态</th><th>剧目 / 素材</th><th>剧目指标 / 素材指标</th><th>原因与画像匹配</th><th>文案</th></tr></thead><tbody>${shown.map(displayRow).join("")||empty(6,"当前筛选无候选。")}</tbody></table></div><div class="tool-pages">${pageBar(page,size,count,"data-ppage")}</div>`;
      if(busy){for(const btn of area.querySelectorAll("button"))btn.disabled=true;}
      review();
    }
    const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    async function readPage(snapshot,offset,limit,extra={}){
      return post(path(t,"/preview"),{expected_version:t.version,preview_id:snapshot,offset,limit,...extra});
    }
    function progress(data){
      const count=data.computed??0,total=data.total??0;
      feedback.textContent=`正在试算 · 已完成 ${count} / ${total||"…"} 个时段`;
    }
    async function previewRun(extra={}){
      const payload=values(),token=++seq;previewKey="";allRows=null;filter="";page=1;drafts.clear();draftSeq++;busy=true;
      area.replaceChildren();foot.hidden=true;feedback.textContent="正在读取频道排期、收入画像和候选素材…";form.querySelector("[type=submit]").disabled=true;
      try{
        rawPayload=payload;
        let data=await post(path(t,"/preview"),{...payload,async_preview:true,offset:0,limit:size, ...extra});
        while(live(token)&&["working","queued","pending"].includes(data.state)){
          progress(data);
          if(data.items?.some(row=>row.status!=="pending")){preview=data;paint();}
          await sleep(1500);if(!live(token))return;
          data=await readPage(data.preview_id,0,size);
        }
        if(!live(token))return;
        if(data.state==="failed")throw new Error(why(data.error)||"预演失败");
        if(!data.preview_id)throw new Error("预演结果缺少快照编号，请重新预演。");
        preview=data;
        const carried=[...adopted].filter(([,d])=>d.preview_id!==data.preview_id);
        let removed=0;
        if(carried.length){
          const rows=await getAll(token);if(!live(token)||!rows)return;
          for(const [k,d] of carried){
            const same=rows.some(row=>eligible(row)&&draftKey(row.account_id,row.material_id)===k&&d.facts_key&&d.facts_key===row.copy_facts_key);
            if(!same){adopted.delete(k);removed++;}
          }
        }
        adoptedList();previewKey=JSON.stringify(payload);feedback.textContent=`已完成 ${summary().total} 个时段的试算。`+(removed?`${removed} 条前次用稿的候选或文案事实已变化，请重新生成和采用。`:"")+(carried.length>removed?`已沿用 ${carried.length-removed} 条文案事实一致的用稿。`:"");
      }catch(error){
        if(live(token)){preview=null;area.replaceChildren();foot.hidden=true;errorBox(feedback,error,()=>previewRun(extra));}
      }finally{if(live(token)){busy=false;form.querySelector("[type=submit]").disabled=false;paint();}}
    }
    action(form,"submit",async ev=>{ev.preventDefault();if(!busy)await previewRun();});
    async function getAll(token){
      if(allRows)return allRows;
      const snapshot=preview.preview_id,total=preview.total??preview.items.length,items=[];
      if(total<=preview.items.length && (preview.offset??0)===0){allRows=preview.items;return allRows;}
      for(let off=0;off<total;off+=24){
        const data=await readPage(snapshot,off,24);if(!live(token))return null;
        if(data.state==="failed")throw new Error(why(data.error)||"快照读取失败");
        if(!data.items?.length)throw new Error("快照分页不完整，请重试。");
        items.push(...data.items);
      }
      if(items.length!==total)throw new Error("快照条数发生变化，请重新预演。");
      allRows=items;return items;
    }
    function copyInput(){
      return [...adopted.values()].map(d=>{
        if(!d.text.trim())throw new Error(`频道 ${d.account_id} 的采用稿不能为空。`);
        return {draft_id:d.id,account_id:String(d.account_id),material_id:String(d.material_id),...(d.text!==d.caption?{edited_caption:d.text}:{})};
      });
    }
    async function generateDrafts(rows){
      const epoch=seq,owner=draftSeq,target=rows.filter(r=>eligible(r)&&r.material_id).map(r=>({account_id:String(r.account_id),material_id:String(r.material_id)}));
      const items=[...new Map(target.map(r=>[draftKey(r.account_id,r.material_id),r])).values()].filter(r=>{const key=draftKey(r.account_id,r.material_id);return !adopted.has(key)&&(!drafts.has(key)||drafts.get(key).state==="error");});
      if(!items.length)return;
      for(const r of items)drafts.set(draftKey(r.account_id,r.material_id),{...r,state:"waiting"});
      paint();
      const current=()=>live(epoch)&&owner===draftSeq;
      try{
        const operation=await operationId(t,JSON.stringify(["drafts",preview.preview_id,items]));
        const result=await post(path(t,"/drafts"),{expected_version:t.version,preview_id:preview.preview_id,items,operation_id:operation.id});
        if(!current())return;
        const expected=new Set(items.map(r=>draftKey(r.account_id,r.material_id)));
        function update(d){const k=draftKey(d.account_id,d.material_id);if(!expected.has(k)||!d.id)throw new Error("草稿和本次频道或素材不一致。");drafts.set(k,d);}
        if(result.items?.length!==items.length)throw new Error("没有取得全部草稿。");
        result.items.forEach(update);paint();
        while(current()){
          const pending=[...expected].map(k=>drafts.get(k)).filter(d=>["waiting","generating"].includes(d.state));
          if(!pending.length)break;
          await sleep(2400);if(!current())return;
          const batch=await Promise.all(pending.map(d=>get(path(t,"/draft/"+encodeURIComponent(d.id)))));
          if(!current())return;
          batch.forEach(update);paint();
        }
        if(current())sessionStorage.removeItem(operation.storage);
      }catch(error){if(current()){for(const r of items){const k=draftKey(r.account_id,r.material_id);if(drafts.get(k)?.state!=="final")drafts.set(k,{state:"error",reason:error.message});}paint();}}
    }
    action(area,"click",async ev=>{
      const btn=ev.target.closest("button");if(!btn||btn.disabled||!preview)return;
      const token=seq;
      if(btn.hasAttribute("data-pfilter")||btn.hasAttribute("data-coverage")){
        feedback.textContent="正在读取快照明细…";await getAll(token);if(!live(token))return;
        filter=btn.dataset.pfilter||"";page=1;feedback.textContent="";paint();return;
      }
      if(btn.dataset.ppage){
        const next=Math.max(1,page+Number(btn.dataset.ppage));
        if(!allRows){const response=await readPage(preview.preview_id,(next-1)*size,size);if(!live(token))return;preview=response;}
        page=next;paint();return;
      }
      if(btn.hasAttribute("data-conflict-info")){await previewRun({preview_id:preview.preview_id,include_conflict_candidates:true});return;}
      if(btn.hasAttribute("data-draft-all")){await generateDrafts(shown);return;}
      if(btn.dataset.draftOne){await generateDrafts(shown.filter(r=>draftKey(r.account_id,r.material_id)===btn.dataset.draftOne));return;}
      if(btn.dataset.adopt){const d=drafts.get(btn.dataset.adopt);if(!d?.caption)return;
        const candidate=shown.find(row=>draftKey(row.account_id,row.material_id)===btn.dataset.adopt);
        adopted.set(btn.dataset.adopt,{...d,text:d.caption,preview_id:preview.preview_id,facts_key:candidate?.copy_facts_key});dirty=true;adoptedList();paint();
        feedback.textContent=`已采用频道 #${d.account_id} 的草稿，其余所选频道仍在本批。`;
      }
    });
    action(id("execute"),"click",async()=>{
      if(submitting)return;
      const payload=values();if(payload.plan_date||previewKey!==JSON.stringify(payload)||!preview?.preview_id)throw new Error("请在对应时间方式下重新预演。");
      const selectedDrafts=copyInput();
      if(selectedDrafts.length){
        const rows=await getAll(seq);if(!rows)return;
        const matches=new Set(rows.filter(eligible).map(r=>draftKey(r.account_id,r.material_id)));
        if(selectedDrafts.some(d=>!matches.has(draftKey(d.account_id,d.material_id))))throw new Error("采用稿对应的频道或素材已变化，请移除失配稿件，再确认本批。");
      }
      const sum=summary(),verb=payload.scheduled_at?"预约":"执行";
      if(!await confirmAction(`确认${verb}`,`${tName(t)} · ${payload.account_ids.length} 个频道\n可执行 ${sum.eligible} 条，跳过 ${sum.skipped} 条，采用稿 ${selectedDrafts.length} 条。\n${payload.scheduled_at?time(payload.scheduled_at)+"（北京时间）":"立即执行"}。确认后将创建真实发布任务。`,verb))return;
      submitting=true;review();
      try{
        const body={...payload,preview_id:preview.preview_id,drafts:selectedDrafts};
        const operation=await operationId(t,JSON.stringify(body));
        const result=await post(path(t,"/run"),{...body,operation_id:operation.id});
        sessionStorage.removeItem(operation.storage);dirty=false;allowClose=null;id("dialog").close();await view("runs");await showRun(result.id);notify(`已提交批次 #${result.id}`);
      }finally{submitting=false;review();}
    });
  }

  async function operationId(t,parameters){
    const bytes=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(`${actorId}/${t.id}/${parameters}`));
    const storage="tt.promotion.operation."+Array.from(new Uint8Array(bytes),v=>v.toString(16).padStart(2,"0")).join("");
    const existing=sessionStorage.getItem(storage);if(existing)return {storage,id:existing};
    const operation=crypto.randomUUID();sessionStorage.setItem(storage,operation);
    return {storage,id:operation};
  }

  function publicUrl(raw){
    try{const url=new URL(raw);return url.protocol==="https:" && (url.hostname==="tiktok.com"||url.hostname.endsWith(".tiktok.com")) ? url.href:"";}catch{return "";}
  }


  let historyQuery={},historyTotal=0;
  function selectedHistory(){
    const params=Object.fromEntries(new FormData(id("history-filters")));
    if(params.start&&params.end&&params.start>params.end)throw new Error("起始日期不能晚于截止日期。");
    return params;
  }
  async function loadHistory(){
    const epoch=++requestToken,error=id("history-error"),pager=id("history-pages");
    error.textContent="正在读取运行记录…";pager.replaceChildren();id("runs").innerHTML=empty(6,"读取中…");
    try{
      const result=await get(apiUrl("/history",{...historyQuery,page:historyPage,page_size:25}));
      if(epoch!==requestToken)return;
      const rows=result.items||[];historyTotal=result.total??rows.length;historyPage=result.page??historyPage;
      id("history-total").textContent=`${historyTotal} 个批次`;error.replaceChildren();
      id("runs").innerHTML=rows.map(r=>`<tr><td><strong>#${h(r.id)}</strong><small class="secondary">${h(r.template_name||`模板 #${r.template_id}`)}</small></td><td>${h(status(r.trigger_type))}</td><td>${h(time(r.scheduled_at_utc||r.created_at))}</td><td>${statusBadge(r.status)}</td><td>${countsHtml(r.counts)}</td><td><button class="row-link" data-run="${h(r.id)}">查看批次</button></td></tr>`).join("")||empty(6,"当前条件下没有运行记录。");
      pager.innerHTML=pageBar(historyPage,result.page_size||25,historyTotal,"data-history-page");
    }catch(e){if(epoch!==requestToken)return;id("runs").innerHTML=empty(6,"尚未取得查询结果。");errorBox(error,e,loadHistory);}
  }
  async function view(v){
    currentView=v;id("templates-view").hidden=v!=="templates";id("runs-view").hidden=v!=="runs";
    for(const btn of document.querySelectorAll("[data-view]")){btn.classList.toggle("selected",btn.dataset.view===v);if(btn.dataset.view===v)btn.setAttribute("aria-current","page");else btn.removeAttribute("aria-current");}
    if(v==="runs"){historyQuery=selectedHistory();historyPage=1;await loadHistory();}
  }
  async function toHistory(template,phase=""){
    const e=id("history-filters").elements;e.template_id.value=String(template);e.account_id.value="";e.trigger_type.value="";e.status.value=phase;e.start.value="";e.end.value="";await view("runs");
  }
  function rowBucket(row){
    if(bucketNames[row.result_bucket])return row.result_bucket;
    if(row.unknown_outcome||row.status==="published"&&!row.publish_id)return "review";
    if(["published","confirmed"].includes(row.status))return "confirmed";
    if(["unknown","reconciling","needs_review"].includes(row.status))return "review";
    if(["skipped","missed","no_candidate","canceled","cancelled","blocked"].includes(row.status))return "skipped";
    return row.status==="failed"?"failed":"pending";
  }
  function proofLine(row){
    const d=row.preview_comparison;if(!d)return "—";
    const desc={matched:"与预演一致",changed:"正式结果有变化",skipped:"正式复核后跳过",pending:"等待执行时选材",not_previewed:"该任务无对应预演"}[d.status]||d.status;
    return [desc,d.expected_content_id?`预演剧目 ${d.expected_content_id}`:"",d.expected_material_id?`预演素材 ${d.expected_material_id}`:"",d.reason?why(d.reason):""].filter(Boolean).join(" · ");
  }
  function copyLabel(row){
    const d=row.copy_provenance;
    if(d?.draft_id){if(d.overridden_by_manual)return "整批人工定稿";const src=d.generated_source||d.source,origin=src==="ai"?"AI 生成":sourceNames[src]||src||"草稿";return `${origin}，人工${d.edited?"修改":"确认"}`;}
    return row.copy_reason&&row.copy_source==="template"?"模板回退":sourceNames[row.copy_source]||row.copy_source;
  }
  async function showRun(runId,preselect=""){
    const seq=++detailEpoch;allowClose=null;modal(`批次 #${runId}`,'<div id="detail-loading" class="empty">正在读取执行记录…</div>');
    let r;try{r=await get("/runs/"+encodeURIComponent(runId));}catch(err){if(detailEpoch===seq)errorBox(id("detail-loading"),err,()=>showRun(runId,preselect));return;}
    if(seq!==detailEpoch||!id("dialog").open)return;
    const rows=r.items||[],batchStatus=rows.some(row=>rowBucket(row)==="review")?"needs_review":r.status;
    modal(`批次 #${runId}`,`<div class="heading"><h3>${h(r.template_name||`模板 #${r.template_id}`)}</h3>${statusBadge(batchStatus)}</div><p class="muted">${h(time(r.scheduled_at_utc||r.created_at))} · ${h(status(r.trigger_type))} · ${rows.length} 个任务</p>
      <div id="run-buckets" class="batch-summary"></div><div id="task-rows"></div>`);
    function render(bucket){
      const counts=Object.fromEntries(Object.keys(bucketNames).map(k=>[k,rows.filter(row=>rowBucket(row)===k).length]));
      id("run-buckets").innerHTML=[["","全部",rows.length],...["failed","review","skipped","pending","confirmed"].map(k=>[k,bucketNames[k],counts[k]])].map(([k,l,n])=>`<button type="button" data-bucket="${k}" class="${k===bucket?"selected":""}">${l} ${n}</button>`).join("");
      const filtered=rows.filter(row=>!bucket||rowBucket(row)===bucket);
      id("task-rows").innerHTML=filtered.map((row,i)=>{
        const account=channels.find(ch=>byId(ch)===String(row.account_id)),url=publicUrl(row.publish_url);
        const fields=[["目标时间",time(row.scheduled_at_utc)],["预演与执行复核",proofLine(row)],["剧目",row.drama_name||row.content_id],["素材",row.material_id],["错误码",row.error_code],["选材 / 跳过原因",why(row.reason)],["首选受众 / 题材",audienceTheme(row.decision,"preferred")],["命中受众 / 题材",audienceTheme(row.decision,"selected")],["画像版本",row.profile_version],["文案来源",copyLabel(row)],["采用草稿 ID",row.copy_provenance?.draft_id],["文案回退原因",row.copy_reason],["推广四位码",row.code],["平台回执",row.publish_id],["平台作品 ID",(row.public_video_ids||[]).join("、")]];
        const displayStatus=rowBucket(row)==="review"?"needs_review":row.status;
        return `<details class="branch" ${bucket&&i===0?"open":""}><summary>${h(account?name(account):row.account_id)} · ${h(status(displayStatus))}<span>${row.id?"任务 #"+h(row.id):"等待任务入队"}</span></summary><div class="task-detail"><dl>${fields.filter(([,val])=>val!=null&&val!=="").map(([k,val])=>`<dt class="muted">${h(k)}</dt><dd>${h(val)}</dd>`).join("")}</dl>${row.caption?`<h4>最终文案</h4><p class="caption">${h(row.caption)}</p>`:""}${url?`<a class="row-link" href="${h(url)}" target="_blank" rel="noopener noreferrer">打开 TikTok 作品</a>`:row.publish_id&&rowBucket(row)==="confirmed"?'<p class="muted">公开作品链接待取得</p>':""}</div></details>`;
      }).join("")||'<p class="empty">此分类暂无任务。</p>';
    }
    id("run-buckets").onclick=ev=>{const btn=ev.target.closest("[data-bucket]");if(btn)render(btn.dataset.bucket);};render(preselect);
  }
  function inventory(t){
    allowClose=null;modal(`${tName(t)} · 素材池库存`,`<form id="inventory-import" class="group"><label class="field">批量补充素材 ID<textarea name="material_ids" rows="2" placeholder="每行或逗号分隔多个 ID"></textarea></label><div class="inline"><button type="submit" class="button">加入素材池</button><button type="button" id="coverage-preview" class="row-link">预演可覆盖频道</button></div></form><div id="inventory-note" role="status"></div><div id="inventory-table"></div>`);
    let page=1,epoch=0;const form=id("inventory-import"),node=id("inventory-table");
    for(const button of form.querySelectorAll("button"))button.disabled=!canConfigure();
    const names={available:"未占用 / 待预检",occupied:"已占用",published:"已发布",review:"待核对"};
    async function refresh(){
      const token=++epoch;node.innerHTML='<p class="muted">正在读取库存…</p>';
      try{
        const result=await get(apiUrl(path(t,"/inventory"),{page,page_size:25}));if(!node.isConnected||token!==epoch)return;
        node.innerHTML=`<div class="inventory-summary">${Object.entries(names).map(([k,l])=>`<span>${h(l)} ${h(result.counts?.[k]??0)}</span>`).join("")}</div><div class="table-wrap"><table><thead><tr><th>素材 ID</th><th>状态</th><th>频道</th><th>占用批次</th></tr></thead><tbody>${result.items.map(row=>`<tr><td>${h(row.material_id)}</td><td>${h(names[row.status]||row.status)}</td><td>${h(row.account_id??"—")}</td><td>${row.job_id?`<button class="row-link" data-inventory-job="${h(row.job_id)}">#${h(row.job_id)}</button>`:"—"}</td></tr>`).join("")||empty(4,"素材池暂无库存。")}</tbody></table></div><div class="tool-pages">${pageBar(page,result.page_size||25,result.total||0,"data-istep")}</div>`;
      }catch(error){if(node.isConnected&&epoch===token)errorBox(node,error,refresh);}
    }
    action(form,"submit",async ev=>{ev.preventDefault();const material_ids=[...new Set(splitIds(form.elements.material_ids.value))];if(!material_ids.length||material_ids.some(n=>!/^[1-9]\d*$/.test(n)))throw new Error("请输入有效的数字素材 ID。");
      const btn=form.querySelector("[type=submit]");if(btn.disabled)return;btn.disabled=true;
      try{const result=await post(path(t,"/inventory"),{expected_version:t.version,material_ids});form.elements.material_ids.value="";id("inventory-note").textContent=`已加入 ${result.added} 条，库存共 ${result.total} 条。`;page=1;await refresh();}finally{btn.disabled=false;}});
    id("coverage-preview").onclick=()=>{execution(t);const form=id("execution");form.elements.mode.value="pool";form.elements.when.value="now";form.elements.mode.dispatchEvent(new Event("change",{bubbles:true}));form.requestSubmit();};
    action(node,"click",async ev=>{const step=ev.target.closest("[data-istep]"),job=ev.target.closest("[data-inventory-job]");if(step){page+=Number(step.dataset.istep);await refresh();}if(job)await showRun(job.dataset.inventoryJob);});
    void refresh();
  }
  id("template-search").addEventListener("input",templateRows);id("template-state").onchange=templateRows;
  id("new-template").onclick=()=>location.assign(location.pathname+"?editor=new");
  action(id("templates"),"click",async ev=>{
    const link=ev.target.closest("[data-to-history]");if(link){await toHistory(link.dataset.template,link.dataset.toHistory);return;}
    const run=ev.target.closest("[data-open-run]");if(run){await showRun(run.dataset.openRun);return;}
    const b=ev.target.closest("[data-act]");if(!b)return;const t=templates.find(row=>String(row.id)===b.dataset.id);if(!t)return;
    if(b.dataset.act==="records")await toHistory(t.id);
    if(b.dataset.act==="edit")location.assign(location.pathname+"?editor="+encodeURIComponent(t.id));
    if(b.dataset.act==="run")execution(t);
    if(b.dataset.act==="toggle")await toggle(t);
    if(b.dataset.act==="inventory")inventory(t);
  });
  action(document.querySelector(".tabs"),"click",async ev=>{const button=ev.target.closest("[data-view]");if(button&&button.dataset.view!==currentView)await view(button.dataset.view);});
  action(id("runs"),"click",async ev=>{const b=ev.target.closest("[data-run]");if(b)await showRun(b.dataset.run,historyQuery.status);});
  action(id("history-pages"),"click",async ev=>{const b=ev.target.closest("[data-history-page]");if(b&&!b.disabled){historyPage+=Number(b.dataset.historyPage);await loadHistory();}});
  action(id("history-filters"),"submit",async ev=>{ev.preventDefault();historyQuery=selectedHistory();historyPage=1;await loadHistory();});
  id("history-filters").onreset=()=>setTimeout(()=>{historyQuery=selectedHistory();historyPage=1;void loadHistory();},0);
  function importSelected(){
    if(!canConfigure()||importHandled)return;
    const wanted=(new URLSearchParams(location.search).get("accounts")||"").split(",").map(s=>s.trim()).filter(Boolean),visible=new Set(channels.map(byId));
    importedAccounts=[...new Set(wanted)].filter(a=>visible.has(a));
    importHandled=true;
    const target=new URLSearchParams(location.search).get("editor");
    if(target==="new"||(!target&&wanted.length)){
      edit(null,importedAccounts);
      if(wanted.length)notify(importedAccounts.length?`已带入 ${importedAccounts.length} 个频道。`:"所选频道当前不可见，请重新选择。");
    }else if(target){
      const template=templates.find(item=>String(item.id)===target);
      if(template)edit(template);
      else notify("模板不存在或当前账号无权查看。");
    }
  }
  async function loadSource(key){
    const accountSource=key==="channels",node=id(accountSource?"channel-source-error":"template-source-error");
    const version=++sourceVersion[key];
    sourceReady[key]=false;templateRows();
    try{
      const {items}=await get(accountSource?"/channels":templatesPath);
      if(version!==sourceVersion[key]||accessDenied)return;
      if(!Array.isArray(items))throw new Error("服务响应缺少清单");
      if(accountSource){
        channels=items;
        options(id("history-channel"),channels.map(c=>[byId(c),`${name(c)} · #${byId(c)}`]),"全部频道");
      }else{
        templates=items;
        options(id("run-filter"),templates.map(t=>[t.id,tName(t)]),"全部模板");
      }
      sourceReady[key]=true;node.replaceChildren();
    }catch(e){
      if([401,403].includes(e.status))throw e;
      if(version!==sourceVersion[key]||accessDenied)return;
      if(accountSource){channels=[];options(id("history-channel"),[],"频道清单未取得");}
      else{templates=[];options(id("run-filter"),[],"模板清单未取得");}
      const source=accountSource?"频道清单":"模板清单";
      errorBox(node,new Error(`${source}暂未取得 · ${e.message}`),()=>loadSource(key));
    }finally{
      if(version===sourceVersion[key]&&!accessDenied){templateRows();importSelected();}
    }
  }
  boot("ttPromotionAuto",async user=>{
    canSetChannels=user.role==="admin" || user.permissions?.tt_promotion_channels===true;
    actorId=user.id||user.user_id||user.email||user.name;accessDenied=false;
    sourceReady.channels=false;sourceReady.templates=false;
    const sources=await Promise.allSettled([loadSource("channels"),loadSource("templates")]);
    const blocked=sources.find(s=>s.status==="rejected");if(blocked)throw blocked.reason;
    if(!accessDenied)void getStats();
  });

})();
