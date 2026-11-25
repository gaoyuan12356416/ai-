(function(){
  "use strict";
  const {id,h,money,time,badge,empty,notify,get,post,modal,confirmAction,action,boot,byId,name,status,statusBadge}=window.TTPromotion;
  const modeNames={template:"按模板选材",drama:"指定爆款剧",material:"指定素材",pool:"素材池分配",serial:"短剧连载"};
  let templates=[],channels=[],runs=[],actorId="",currentView="templates",requestToken=0;
  const templatesPath="/templates";
  const path=(t,suffix="")=>`/templates/${encodeURIComponent(t.id)}${suffix}`;
  const cfg=t=>t.config||{};
  function tName(t){return t.name || `模板 ${t.id}`;}
  function scheduleText(s){
    if(s?.mode==="random")return `每日随机 ${s.daily_count} 条 / 频道`;
    return (s?.times||[]).join("、")||"—";
  }
  function tableChannels(rows){
    return rows.map(([key,label])=>`<label><input type="checkbox" value="${h(key)}"> <span>${h(label)}</span></label>`).join("");
  }
  function renderTemplates(){
    id("templates-meta").textContent=`${templates.length} 个模板 · ${templates.filter(t=>t.enabled).length} 个已启用`;
    id("templates").innerHTML=templates.map(t=>{
      const c=cfg(t),tid=h(t.id);
      const actionBtn=(act,label)=>`<button class="row-link" data-act="${act}" data-id="${tid}">${label}</button>`;
      return `<tr><td><strong>${h(tName(t))}</strong><small class="secondary">#${tid} · v${h(t.version)}</small></td><td>${statusBadge(t.enabled?"enabled":"draft")}</td><td>${h(c.account_ids?.length??0)}</td><td>${c.model==="rules"?"模板规则":"频道收入画像"}</td><td>${h(scheduleText(c.schedule))}</td><td>${c.ai_copy?"开启":"关闭"}</td><td><div class="inline">${actionBtn("edit","编辑")}${actionBtn("run","预演与执行")}${actionBtn("records","记录")}${actionBtn("toggle",t.enabled?"停用":"启用")}</div></td></tr>`;
    }).join("") || empty(7,"暂无新版模板，可创建草稿后预演。");
    const filter=id("run-filter"),old=filter.value;
    filter.innerHTML='<option value="">全部模板</option>'+templates.map(t=>`<option value="${h(t.id)}">${h(tName(t))}</option>`).join("");
    filter.value=old;
  }
  async function updateTemplates(){
    templates=(await get(templatesPath)).items;
    renderTemplates();
  }
  function pickerRows(selected){
    const rows=channels.map(c=>[byId(c),[name(c),`#${byId(c)}`,c.drama_language,c.eligible?"可发布":"受限",c.revenue_28d==null?"":money(c.revenue_28d)].filter(Boolean).join(" · ")]);
    const available=new Set(rows.map(r=>r[0]));
    for(const old of selected)if(!available.has(old))rows.push([old,`#${old} · 当前不可见或不可用`]);
    return rows;
  }
  function setupPicker(form,rows,initial){
    const checks=form.querySelector("[data-accounts]"),search=form.querySelector("[data-search]");
    checks.innerHTML=tableChannels(rows);
    const selected=new Set(initial.map(String));
    const summary=form.querySelector("[data-count]");
    const boxes=[...checks.querySelectorAll("input")];
    function count(){summary.textContent=`已选 ${boxes.filter(b=>b.checked).length} 个`;}
    for(const b of boxes)b.checked=selected.has(b.value);
    search.oninput=()=>{const term=search.value.trim().toLowerCase();for(const b of boxes)b.parentElement.hidden=!b.parentElement.textContent.toLowerCase().includes(term);};
    checks.onchange=count;
    form.querySelector("[data-add]").onclick=()=>{for(const b of boxes)if(!b.parentElement.hidden)b.checked=true;count();};
    form.querySelector("[data-clear]").onclick=()=>{for(const b of boxes)b.checked=false;count();};
    count();
    return ()=>boxes.filter(b=>b.checked).map(b=>b.value);
  }
  function picker(){
    return `<fieldset class="group"><legend>目标频道</legend><div class="form-grid"><label>筛选频道<input type="search" data-search placeholder="昵称、ID 或剧语言"></label><div class="inline"><button type="button" class="row-link" data-add>选当前结果</button><button type="button" class="row-link" data-clear>清空</button><span class="muted" data-count></span></div></div><div class="check-list" data-accounts></div></fieldset>`;
  }
  const numberField=(name,label,value,min=0)=>`<label>${h(label)}<input type="number" min="${min}" step="1" name="${name}" value="${h(value)}" required></label>`;
  async function edit(t){
    const c=t?cfg(t):{},fresh=!t;
    modal(fresh?"创建自动发布模板":`编辑 ${tName(t)} · v${t.version}`,`<form id="edit-template">
      <fieldset class="group"><legend>基础设置</legend><div class="form-grid">
        <label>模板名称<input name="name" value="${h(t?.name||"")}" required></label>
        <label>选材方式<select name="model"><option value="income">频道收入画像</option><option value="rules">模板规则</option></select></label>
        <label>视频制作<select name="video_template"><option value="random_overlay">随机模板</option><option value="direct_outro">固定教程片尾</option></select></label>
      </div></fieldset>
      ${picker()}
      <fieldset class="group"><legend>选材窗口</legend><div class="form-grid">
        ${numberField("metric_window_days","投放指标天数",c.metric_window_days??7,1)}
        ${numberField("drama_launch_window_days","剧上线窗口（天）",c.drama_launch_window_days??30)}
        ${numberField("material_window_days","素材窗口（天）",c.material_window_days??30)}
        ${numberField("cooldown_days","同剧冷却（天）",c.cooldown_days??7)}
      </div></fieldset>
      <fieldset class="group"><legend>逐频道排期 · 北京时间</legend><div class="form-grid">
        <label>排期方式<select name="schedule_mode"><option value="fixed">固定时间</option><option value="random">每天随机</option></select></label>
        <label id="fixed-times">每日时间（逗号分隔）<input name="times" placeholder="10:00, 15:30" value="${h((c.schedule?.times||["12:30"]).join(", "))}"></label>
        <label id="random-count">每频道每日条数<input type="number" min="1" max="24" step="1" name="count" value="${h(c.schedule?.daily_count??1)}"></label>
      </div></fieldset>
      <fieldset class="group"><legend>发布文案</legend><div class="form-grid"><label>文案来源<select name="ai_copy"><option value="no">模板文案</option><option value="yes">AI 优先，失败用模板稿</option></select></label>
      <label class="stretch">文案模板<textarea name="caption_template" rows="3" required>${h(c.caption_template??"{desc} {code}")}</textarea></label></div></fieldset>
      <div class="modal-foot"><button class="button primary" type="submit">${fresh?"保存草稿":"保存新版本"}</button></div></form>`);
    const form=id("edit-template"),controls=form.elements;
    controls.model.value=c.model||"income";
    controls.video_template.value=c.video_template||"random_overlay";
    controls.ai_copy.value=c.ai_copy?"yes":"no";
    controls.schedule_mode.value=c.schedule?.mode||"fixed";
    const initial=(c.account_ids||[]).map(String),getIds=setupPicker(form,pickerRows(initial),initial);
    function timing(){id("fixed-times").hidden=controls.schedule_mode.value!=="fixed";id("random-count").hidden=!id("fixed-times").hidden;}
    controls.schedule_mode.onchange=timing;timing();
    action(form,"submit",async ev=>{
      ev.preventDefault();
      const button=form.querySelector("button[type=submit]");if(button.disabled)return;button.disabled=true;
      try{
        const account_ids=getIds();
        if(!account_ids.length)throw new Error("至少选择一个目标频道。");
        const fixed=controls.schedule_mode.value==="fixed";
        const times=controls.times.value.trim().split(/[\s,，、]+/).filter(Boolean);
        if(fixed && (!times.length||times.some(v=>!/^([01]\d|2[0-3]):[0-5]\d$/.test(v))||new Set(times).size!==times.length))throw new Error("请填写不重复的北京时间，例如 10:00, 15:30。");
        const body={name:controls.name.value.trim(),account_ids,model:controls.model.value,video_template:controls.video_template.value,
          schedule:fixed?{mode:"fixed",times}:{mode:"random",daily_count:Number(controls.count.value)},ai_copy:controls.ai_copy.value==="yes",caption_template:controls.caption_template.value.trim()};
        for(const k of ["metric_window_days","drama_launch_window_days","material_window_days","cooldown_days"])body[k]=Number(controls[k].value);
        if(t)body.expected_version=t.version;
        await post(t?path(t):templatesPath,body);id("dialog").close();await updateTemplates();notify(fresh?"草稿已保存，可预演频道分配。":"模板新版本已保存。");
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
  function resultRows(items,aiEnabled){
    return items.map(r=>{
      const channel=channels.find(c=>byId(c)===String(r.account_id));
      const reasons=[...new Set([why(r.reason),...(r.conflicts||[]).map(why)].filter(Boolean))].join("；");
      const preferred=audienceTheme(r.decision,"preferred"),matched=audienceTheme(r.decision,"selected");
      return `<tr><td>${h(channel?name(channel):r.account_id)}<small class="secondary">#${h(r.account_id)}</small></td><td>${statusBadge(r.status)}<small class="secondary">${h(time(r.scheduled_at_utc))}</small></td><td>${h(r.drama_name||r.content_id||"—")}<small class="secondary">${h(r.material_id?`素材 ${r.material_id}`:"")}</small></td><td>${h(reasons||"—")}${preferred?`<small class="secondary">首选 · ${h(preferred)}</small>`:""}${matched?`<small class="secondary">命中 · ${h(matched)}</small>`:""}</td><td class="caption">${h(r.caption||"—")}${aiEnabled && r.material_id && ["eligible","ready"].includes(r.status)?`<div><button class="row-link" data-draft-account="${h(r.account_id)}" data-draft-material="${h(r.material_id)}">生成单条 AI 稿</button><div data-draft-result aria-live="polite"></div></div>`:""}</td></tr>`;
    }).join("")||empty(5,"没有可预演的任务。");
  }
  function beijingIso(value){
    if(!value)return "";
    if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(value))throw new Error("请填写有效的北京时间。");
    return new Date(value+":00+08:00").toISOString();
  }
  function splitIds(value){return value.trim().split(/[\s,，、]+/).filter(Boolean);}
  function execution(t){
    const c=cfg(t),scope=(c.account_ids||[]).map(String);
    modal(`${tName(t)} · 预演与执行`,`<form id="execution">
      <fieldset class="group"><legend>本次任务</legend><div class="form-grid">
      <label>推广方式<select name="mode">${Object.entries(modeNames).map(([v,l])=>`<option value="${v}">${l}</option>`).join("")}</select></label>
      <label>执行时间<select name="when"><option value="now">立即执行</option><option value="at">一次性预约</option></select></label>
      <label data-when hidden>预约时间（北京）<input name="scheduled_at" type="datetime-local"></label>
      <label data-mode="drama serial" hidden>剧目 ID<input name="content_id" placeholder="Drama ID"></label>
      <label data-mode="material" hidden>素材 ID<input name="material_id" inputmode="numeric"></label>
      <label data-mode="pool serial" class="stretch" hidden><span id="ids-label">素材 ID 列表</span><textarea name="inventory" rows="3" placeholder="首次使用时每行一个素材 ID；留空沿用已保存素材池或集序"></textarea></label></div></fieldset>
      ${picker()}
      <label class="field">本次人工定稿（可选）<textarea name="manual_caption" rows="2" placeholder="留空按模板的文案设置执行"></textarea></label>
      <p class="muted" id="caption-help">填写后会用于本次所选频道。要单独改一条文案，请仅勾选对应频道。</p>
      <div class="inline"><button class="button" type="submit">只读预演</button><button class="button primary" id="execute" type="button" disabled>确认执行</button></div></form>
      <div id="preview-output" aria-live="polite"></div>`);
    const form=id("execution"),e=form.elements,getIds=setupPicker(form,pickerRows(scope).filter(([v])=>scope.includes(v)),scope);
    let previewKey="",previewSeq=0;
    function reset(){previewSeq++;previewKey="";id("execute").disabled=true;id("preview-output").replaceChildren();}
    function displayFields(){
      for(const node of form.querySelectorAll("[data-mode]"))node.hidden=!node.dataset.mode.split(" ").includes(e.mode.value);
      form.querySelector("[data-when]").hidden=e.when.value!=="at";
      id("ids-label").textContent=e.mode.value==="serial"?"按集序排列的素材 ID":"候选素材 ID 列表";
      id("execute").textContent=e.when.value==="at"?"确认预约":"确认执行";
    }
    form.addEventListener("change",()=>{reset();displayFields();});
    form.addEventListener("input",ev=>{if(!ev.target.matches("[data-search]"))reset();});
    form.addEventListener("click",ev=>{if(ev.target.matches("[data-add],[data-clear]"))reset();});
    function values(){
      const mode=e.mode.value,account_ids=getIds();
      if(!account_ids.length)throw new Error("请至少勾选一个目标频道。");
      const payload={expected_version:t.version,mode,account_ids};
      if(e.when.value==="at"){
        if(!e.scheduled_at.value)throw new Error("请选择本次预约的北京时间。");
        payload.scheduled_at=beijingIso(e.scheduled_at.value);
      }
      if(["drama","serial"].includes(mode)){
        payload.content_id=e.content_id.value.trim();if(!payload.content_id)throw new Error("请填写剧目 ID。");
      }
      if(mode==="material"){payload.material_id=e.material_id.value.trim();if(!payload.material_id)throw new Error("请填写素材 ID。");}
      if(["pool","serial"].includes(mode)){
        const ids=splitIds(e.inventory.value);
        if(new Set(ids).size!==ids.length)throw new Error("请填写不重复的素材 ID，每行一条。");
        if(ids.length)payload[mode==="serial"?"episodes":"material_ids"]=ids;
      }
      const caption=e.manual_caption.value.trim();if(caption)payload.manual_caption=caption;
      return payload;
    }
    displayFields();
    action(form,"submit",async ev=>{
      ev.preventDefault();
      const button=form.querySelector("button[type=submit]");
      if(button.disabled)return;
      const payload=values(),key=JSON.stringify(payload),seq=++previewSeq;
      button.disabled=true;id("execute").disabled=true;
      try{
        const result=await post(path(t,"/preview"),payload);
        if(seq!==previewSeq || !form.isConnected)return;
        const items=result.items||[];
        id("preview-output").innerHTML=`<h3>预演结果 · ${items.length} 个频道任务</h3>${t.config.ai_copy&&!payload.manual_caption?'<p class="muted">此处为模板样例。可为单条候选生成并审阅 AI 稿，或按模板在执行时生成。</p>':""}<div class="table-wrap"><table><thead><tr><th>频道</th><th>状态 / 排期</th><th>剧目 / 素材</th><th>选材理由 / 冲突</th><th>文案样例</th></tr></thead><tbody>${resultRows(items,t.config.ai_copy&&!payload.manual_caption)}</tbody></table></div>`;
        previewKey=key;
        id("execute").disabled=!items.some(row=>!["failed","blocked","conflict","skipped","no_candidate"].includes(row.status) && !(row.conflicts||[]).length);
        notify("",true);
      }finally{button.disabled=false;}
    });
    action(id("preview-output"),"click",async ev=>{
      const button=ev.target.closest("[data-draft-account]");
      if(!button || button.disabled)return;
      const account=button.dataset.draftAccount, material=button.dataset.draftMaterial;
      const box=button.parentElement.querySelector("[data-draft-result]");
      button.disabled=true;
      box.textContent="正在准备 AI 草稿…";
      try{
        const operation=await operationId(t,JSON.stringify(["draft",t.version,account,material]));
        let draft=await post(path(t,"/draft"),{expected_version:t.version,account_id:account,material_id:material,operation_id:operation.id});
        while(draft.state==="waiting" || draft.state==="generating"){
          if(!box.isConnected || !id("dialog").open)return;
          box.textContent=draft.state==="waiting"?"AI 文案已排队…":"正在生成单条文案…";
          await new Promise(resolve=>setTimeout(resolve,2500));
          if(!box.isConnected || !id("dialog").open)return;
          draft=await get(path(t,"/draft/"+encodeURIComponent(draft.id)));
        }
        if(!box.isConnected || !id("dialog").open)return;
        if(draft.state!=="final" || !draft.caption)throw new Error(draft.reason||"本次草稿生成失败，请重试。");
        if(String(draft.account_id)!==account || String(draft.material_id)!==material)throw new Error("草稿的频道或素材已变化，请重新预演。");
        sessionStorage.removeItem(operation.storage);
        box.innerHTML=`<p class="caption">${h(draft.caption)}</p>${draft.reason?`<small class="secondary">${h(draft.reason)}</small>`:""}<button type="button" class="row-link" data-adopt>采用${draft.source==="ai"?"AI 稿":"此稿"}并锁定频道和素材</button>`;
        button.hidden=true;
        box.querySelector("[data-adopt]").onclick=()=>{
          for(const check of form.querySelectorAll("[data-accounts] input"))check.checked=check.value===account;
          form.querySelector("[data-count]").textContent="已选 1 个";
          e.mode.value="material";e.material_id.value=material;e.manual_caption.value=draft.caption;
          reset();displayFields();
          e.manual_caption.focus();
          notify("已采用单条草稿，请检查文案并重新预演。",true);
        };
      }catch(err){
        if(box.isConnected){box.textContent=err.message||"未取得草稿";button.textContent="重新获取草稿";}
      }finally{button.disabled=false;}
    });
    action(id("execute"),"click",async ev=>{
      const btn=ev.currentTarget,payload=values(),key=JSON.stringify(payload);
      if(previewKey!==key)throw new Error("配置已变更，请重新预演。");
      const verb=payload.scheduled_at?"预约":"执行";
      if(!await confirmAction(`确认${verb}`,`${tName(t)} · ${modeNames[payload.mode]} · ${payload.account_ids.length} 个所选频道\n${payload.scheduled_at?time(payload.scheduled_at)+"（北京时间）":"立即执行"}。确认后系统将为符合条件的频道创建真实发布任务。`,verb))return;
      btn.disabled=true;
      try{
        const operation=await operationId(t,key);
        const result=await post(path(t,"/run"),{...payload,operation_id:operation.id});
        sessionStorage.removeItem(operation.storage);
        await view("runs");await showRun(result.id);notify(`已提交批次 #${result.id}`);
      }finally{if(btn.isConnected)btn.disabled=false;}
    });
  }
  async function operationId(t,parameters){
    const bytes=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(`${actorId}/${t.id}/${parameters}`));
    const storage="tt.promotion.operation."+Array.from(new Uint8Array(bytes),v=>v.toString(16).padStart(2,"0")).join("");
    const existing=sessionStorage.getItem(storage);if(existing)return {storage,id:existing};
    const operation=crypto.randomUUID();sessionStorage.setItem(storage,operation);
    return {storage,id:operation};
  }
  function renderRuns(){
    const filter=id("run-filter").value;
    const rows=runs.filter(r=>!filter || String(r.template_id)===filter);
    id("runs").innerHTML=rows.map(r=>`<tr><td>#${h(r.id)}</td><td>${h(r.template_name||`模板 #${r.template_id}`)}</td><td>${h(status(r.trigger_type))}</td><td>${h(time(r.scheduled_at_utc||r.created_at))}</td><td>${statusBadge(r.status)}</td><td><button class="row-link" data-run="${h(r.id)}">查看批次</button></td></tr>`).join("")||empty(6,"暂无运行记录。");
  }
  async function refreshRuns(){
    const seq=++requestToken;const result=await get("/runs");
    if(seq!==requestToken)return;runs=result.items||[];renderRuns();
  }
  async function view(v){
    currentView=v;
    id("templates-view").hidden=v!=="templates";id("runs-view").hidden=v!=="runs";
    for(const btn of document.querySelectorAll("[data-view]")){btn.classList.toggle("selected",btn.dataset.view===v);if(btn.dataset.view===v)btn.setAttribute("aria-current","page");else btn.removeAttribute("aria-current");}
    if(v==="runs")await refreshRuns();
  }
  function publicUrl(raw){
    try{const url=new URL(raw);return url.protocol==="https:" && (url.hostname==="tiktok.com"||url.hostname.endsWith(".tiktok.com")) ? url.href:"";}catch{return "";}
  }
  async function showRun(runId){
    if(!runId)throw new Error("运行记录未返回批次编号，请刷新记录列表核对。");
    const seq=++requestToken;modal(`批次 #${runId}`,'<div class="empty">正在读取执行记录…</div>');
    const r=await get("/runs/"+encodeURIComponent(runId));
    if(seq!==requestToken || !id("dialog").open)return;
    const rows=r.items||[],sources={manual:"人工定稿",ai:"AI 文案",template:"模板稿",fallback:"模板回退"};
    const counts=new Map();for(const row of rows)counts.set(row.status,(counts.get(row.status)||0)+1);
    modal(`批次 #${runId}`,`<div class="heading"><h3>${h(r.template_name||`模板 #${r.template_id}`)}</h3>${statusBadge(r.status)}</div>
      <p class="muted">${h(time(r.scheduled_at_utc||r.created_at))} · ${h(status(r.trigger_type))} · ${rows.length} 个任务</p>
      <div class="inline">${[...counts].map(([key,n])=>badge(`${status(key)} ${n}`)).join("")}</div>
      ${rows.map(row=>{
        const url=publicUrl(row.publish_url),account=channels.find(c=>byId(c)===String(row.account_id));
        const fields=[["目标时间",time(row.scheduled_at_utc)],["剧目",row.drama_name||row.content_id],["素材",row.material_id],["选材 / 跳过原因",why(row.reason)],["首选受众 / 题材",audienceTheme(row.decision,"preferred")],["命中受众 / 题材",audienceTheme(row.decision,"selected")],["画像版本",row.profile_version],["文案来源",row.copy_reason&&row.copy_source==="template"?"模板回退":sources[row.copy_source]||row.copy_source],["文案回退原因",row.copy_reason],["推广四位码",row.code],["平台回执",row.publish_id]];
        return `<details class="branch"><summary>${h(account?name(account):row.account_id)} · ${h(status(row.status))}<span>${row.id?"任务 #"+h(row.id):"等待任务入队"}</span></summary><div class="group" style="padding:0 12px"><dl>${fields.filter(([,value])=>value!=null && value!=="").map(([label,value])=>`<dt class="muted">${h(label)}</dt><dd>${h(value)}</dd>`).join("")}</dl>${row.caption?`<h4>最终文案</h4><p class="caption">${h(row.caption)}</p>`:""}${url?`<a href="${h(url)}" target="_blank" rel="noopener noreferrer" class="row-link">打开 TikTok 作品</a>`:row.publish_id&&["published","confirmed"].includes(row.status)?'<p class="muted">公开作品链接待取得</p>':""}</div></details>`;
      }).join("")||'<p class="empty">暂无已建任务。</p>'}`);
  }
  id("new-template").onclick=()=>edit(null);
  action(id("templates"),"click",async ev=>{
    const b=ev.target.closest("[data-act]");if(!b)return;
    const t=templates.find(row=>String(row.id)===b.dataset.id);if(!t)return;
    if(b.dataset.act==="records"){id("run-filter").value=String(t.id);await view("runs");}
    if(b.dataset.act==="edit")await edit(t);
    if(b.dataset.act==="run")execution(t);
    if(b.dataset.act==="toggle")await toggle(t);
  });
  action(document.querySelector(".tabs"),"click",async ev=>{const b=ev.target.closest("[data-view]");if(b&&b.dataset.view!==currentView)await view(b.dataset.view);});
  action(id("runs"),"click",async ev=>{const b=ev.target.closest("[data-run]");if(b)await showRun(b.dataset.run);});
  action(id("refresh-runs"),"click",refreshRuns);id("run-filter").onchange=renderRuns;
  boot("ttPromotionAuto",async user=>{
    actorId=user.id||user.user_id||user.email||user.name;
    const [accounts,initial]=await Promise.all([get("/channels"),get(templatesPath)]);
    channels=accounts.items;templates=initial.items;renderTemplates();
  });
})();
