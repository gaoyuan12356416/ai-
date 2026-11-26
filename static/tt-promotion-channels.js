(function (){
  "use strict";
  const {id,h,money,cents,time,badge,empty,notify,moduleRetry,get,post,modal,action,boot,byId,name,status}=window.TTPromotion;
  const selected=new Set();
  let selectedScope="";
  let items=[],meta={},displayed=[],detailRequest=0,listRequest=0,canCreate=false,catalogReady=false;
  const sectionRequests={profile:0,handoff:0};
  const detailCache=new Map();
  const filters=id("filters");
  const privacyNames={PUBLIC_TO_EVERYONE:"所有人",MUTUAL_FOLLOW_FRIENDS:"互相关注的朋友",FOLLOWER_OF_CREATOR:"关注者",SELF_ONLY:"仅自己"};
  const periodNames={"28d":"近 28 天","7d":"近 7 天",all:"累计 · 选材依据"};
  function topLabel(value){return typeof value==="object" ? value?.name : value;}
  function legacyState(row){
    if(row.legacy_conflict!=null)return row.legacy_conflict?"yes":"no";
    return row.legacy_scheduled===true?"yes":"unknown";
  }
  function legacyLabel(row){
    if(legacyState(row)==="unknown")return "旧排期待核验";
    const facts=[];
    if(row.legacy_scheduled)facts.push("旧排期"+(row.legacy_template_count?" "+row.legacy_template_count+" 项":""));
    if(row.legacy_inflight_count)facts.push("在途 "+row.legacy_inflight_count+" 项");
    return facts.join(" · ") || (legacyState(row)==="yes"?"有旧排期或在途":"无旧排期和在途");
  }
  const needsFirst = window.TTFirstUse.needed;
  function readiness(row){return row.eligible?"ready":needsFirst(row)?"first":"restricted";}
  const pct=v=>Number.isInteger(v)?(v/100).toLocaleString("zh-CN",{maximumFractionDigits:2})+"%":"—";
  function profileCells(row){
    const mix=row.audience_mix||[],focus=mix.find(a=>a.name===topLabel(row.audience));
    const audience=topLabel(row.audience);
    const bars=mix.filter(a=>Number.isInteger(a.share_bp)&&a.share_bp>0).map(a=>{
      const tone=["female","male","lgbtq","general"].includes(a.key)?a.key:"other";
      return `<i class="mix-${tone}" style="flex-basis:${Math.min(100,a.share_bp/100)}%" title="${h(a.name)} ${h(pct(a.share_bp))}"></i>`;
    }).join("");
    const themes=(row.theme_mix?.length?row.theme_mix:(row.themes||[]).map(t=>({name:topLabel(t)})));
    return `<td class="portrait"><span class="mobile-only">累计受众</span>${audience?`<button class="profile-link" data-detail="${h(byId(row))}" data-period-open="all">${h(audience)} <b>${focus?h(pct(focus.share_bp)):""}</b></button>`:h(row.has_profile==null?"画像待回传":mix.length?"受众未识别":"暂无受众画像")}${bars?`<span class="mix-bar">${bars}</span>`:""}</td>
      <td class="themes"><span class="mobile-only">受众内优势题材</span>${themes.map(t=>`<div>${h(t.name)} <b>${t.share_bp==null?"":h(pct(t.share_bp))}</b></div>`).join("")||"—"}</td>`;
  }
  function filtered(){
    const controls=filters.elements;
    const query=controls.search.value.trim().toLowerCase();
    const min=controls.min.value,max=controls.max.value;
    return items.filter(row=>{
      const amount=row.revenue_28d;
      return (!selectedScope || readiness(row)===selectedScope) && (!query || [name(row),row.username,row.account_id].some(piece=>String(piece??"").toLowerCase().includes(query)))
        && (!controls.language.value || row.drama_language===controls.language.value)
        && (!controls.eligible.value || !!row.eligible===(controls.eligible.value==="yes"))
        && (!controls.participating.value || row.enabled===(controls.participating.value==="yes"))
        && (!controls.profile.value || row.has_profile===(controls.profile.value==="yes"))
        && (!controls.audience.value || topLabel(row.audience)===controls.audience.value)
        && (!controls.legacy.value || legacyState(row)===controls.legacy.value)
        && (min==="" || (amount!=null && amount>=Number(min)))
        && (max==="" || (amount!=null && amount<=Number(max)));
    }).sort((a,b)=>{
      if(controls.sort.value==="name")return name(a).localeCompare(name(b),"zh-CN");
      if(a.revenue_28d==null && b.revenue_28d!=null)return 1;
      if(b.revenue_28d==null && a.revenue_28d!=null)return -1;
      const difference=Number(a.revenue_28d)-Number(b.revenue_28d);
      return (controls.sort.value==="asc"?difference:-difference)||byId(a).localeCompare(byId(b));
    });
  }
  function selectionState(){
    const visible=displayed.filter(row=>selected.has(byId(row))).length;
    for(const all of [id("select-all"),id("select-mobile")]){
      all.checked=displayed.length>0 && visible===displayed.length;
      all.indeterminate=!all.checked && visible>0;
      all.disabled=!displayed.length;
    }
    id("bulk").disabled=selected.size===0;
    id("bulk").textContent=selected.size ? `批量设置（${selected.size}）` : "批量设置";
    id("new-for-selected").hidden=!canCreate;
    id("new-for-selected").disabled=selected.size===0;
    const need=items.filter(r=>selected.has(byId(r))&&needsFirst(r)).length;
    id("first-for-selected").hidden=!need;
    id("first-for-selected").textContent=`确认观看范围（${need}）`;
    id("selected-strip").hidden=!selected.size;
    id("selected-summary").textContent=selectionLabel(visible);
  }
  function selectionLabel(visible){
    const hidden=selected.size-visible;
    return `已选 ${selected.size} 个频道${hidden?"，其中 "+hidden+" 个在当前筛选外":""}`;
  }
  function render(){
    if(!catalogReady)return;
    displayed=filtered();
    const labels=[`${displayed.length} / ${items.length} 个频道`];
    if(meta.end) labels.push(`截至 ${meta.end} UTC`);
    if(meta.stale) labels.push("沿用上次完整画像");
    id("list-meta").textContent=labels.join(" · ");
    for(const b of id("directory-overview").querySelectorAll("button")){
      b.querySelector("strong").textContent=b.dataset.scope?items.filter(r=>readiness(r)===b.dataset.scope).length:items.length;
      b.setAttribute("aria-pressed",String(b.dataset.scope===selectedScope));
    }
    id("channel-summary").textContent=`当前结果 · 参与 ${displayed.filter(r=>r.enabled).length} · 旧排期或在途 ${meta.module_errors?.handoff?"待核验":displayed.filter(r=>legacyState(r)==="yes").length}`;
    id("channels").innerHTML=displayed.map(row=>{
      const account=h(byId(row)),state=readiness(row);
      const username=row.username&&row.username!==row.account_id?"@"+String(row.username).replace(/^@/,"")+" · ":"";
      return `<tr><td class="check"><input type="checkbox" data-select="${account}" aria-label="${h("选择 "+name(row))}" ${selected.has(byId(row))?"checked":""}></td>
      <td class="identity"><button class="row-link" data-detail="${account}">${h(name(row))}</button><small class="secondary">${h(username)}#${account} · ${h(row.drama_language||"未配置")}</small></td>
      <td class="progress"><span class="metric-label">今日已发 / 应发</span><strong>${h(row.today_published??"—")} / ${h(row.today_expected??"—")}</strong><small class="secondary">最近 ${h(time(row.last_published_at))}</small></td>
      <td class="income"><span class="mobile-only">28 天收入</span><strong>${h(money(row.revenue_28d))}</strong></td>
      ${profileCells(row)}
      <td class="wide-only">${h(legacyLabel(row))}</td>
      <td class="readiness">${badge(state==="ready"?"可发布":state==="first"?"待选观看范围":"账号受限",state==="ready"?"positive":"warning")}
      ${!row.enabled?badge("已暂停"):""}${state==="first"?`<button class="row-link first-action" data-first="${account}">首次确认</button>`:""}
      ${state==="restricted"?`<small class="secondary">${h(row.reason||"请查看账号授权状态")}</small>`:""}</td></tr>`;
    }).join("") || empty(8,items.length?"当前条件下没有频道":"暂无可见频道");
    selectionState();
  }
  const profileKeys=["revenue_28d","audience","themes","has_profile","audience_mix","theme_mix"];
  const planKeys=["today_expected","today_published","last_published_at","legacy_scheduled","legacy_template_count","legacy_inflight_count","legacy_conflict"];
  function mergeChannel(row,prior,section){
    if(!prior || !section)return row;
    const keys=section==="profile"?planKeys:profileKeys;
    return {...row,...Object.fromEntries(keys.map(key=>[key,prior[key]]))};
  }
  function mergeMeta(prior,latest,section){
    if(!section)return latest;
    const errors={...prior?.module_errors};
    delete errors[section];
    return {...(section==="handoff"?prior:latest),module_errors:{...errors,...latest.module_errors}};
  }
  async function refresh(section){
    const seq=section?listRequest:++listRequest;
    const ticket=section?++sectionRequests[section]:0;
    const result=await get("/channels"+(section?"?section="+section:""));
    if(seq!==listRequest || section && ticket!==sectionRequests[section])return;
    catalogReady=true;
    const old=new Map(items.map(row=>[byId(row),row]));
    meta=mergeMeta(meta,result.meta||{},section);
    const errors=meta.module_errors||{};
    items=result.items.map(row=>{
      const prior=old.get(byId(row));
      row=mergeChannel(row,prior,section);
      if(prior && errors.profile)row={...row,...Object.fromEntries(profileKeys.map(k=>[k,prior[k]]))};
      if(prior && errors.handoff)row={...row,today_expected:prior.today_expected,today_published:prior.today_published,last_published_at:prior.last_published_at};
      return row;
    });
    for(const [module,label] of [["profile","收入画像"],["handoff","排期与执行进度"]]){
      moduleRetry(id("list-"+module+"-error"),errors[module]?`${errors[module]}。已加载的${label}仅供参考。`:"",()=>refresh(module));
    }
    for(const selectedId of selected) if(!items.some(r=>byId(r)===selectedId))selected.delete(selectedId);
    for(const [field,label,values] of [["language","全部语言",items.map(row=>row.drama_language)],["audience","全部受众",items.map(row=>topLabel(row.audience))]]){
      const control=filters.elements[field],previous=control.value;
      control.innerHTML=`<option value="">${label}</option>`+[...new Set(values.filter(Boolean))].sort().map(x=>`<option>${h(x)}</option>`).join("");
      control.value=previous;
    }
    render();
  }
  function contribution(node){
    const percent=node.share_bp==null?"":` · 占比 ${(node.share_bp/100).toFixed(2)}%`;
    return `<span>${h(cents(node.revenue_cents))}${h(percent)} · ${h(node.published_count??"—")} 条发布</span>`;
  }
  function dramaTable(rows){
    if(!rows.length)return '<p class="muted">暂无贡献剧目明细</p>';
    return `<div class="table-wrap"><table><thead><tr><th>剧目</th><th>语言</th><th>充值</th><th>IAA</th><th>分摊收入</th><th>发布数</th></tr></thead><tbody>${rows.map(d=>`<tr><td>${h(d.name||d.content_id)}<small class="secondary">${h(d.content_id)}</small></td><td>${h(d.language||"—")}</td><td>${h(cents(d.iap_cents))}</td><td>${h(cents(d.iaa_cents))}</td><td>${h(cents(d.allocated_cents))}</td><td>${h(d.published_count??"—")}</td></tr>`).join("")}</tbody></table></div>`;
  }
  function tree(nodes){
    return nodes.map(node=>`<details class="branch"><summary>${h(node.name)}${contribution(node)}</summary>${(node.children||[]).map(theme=>`<details class="branch"><summary>${h(theme.name)}${contribution(theme)}</summary>${dramaTable(theme.dramas||[])}</details>`).join("")}</details>`).join("");
  }
  function handoff(data){
    function list(rows) {
      return rows?.length? `<ul class="compact">${rows.map(row=>`<li>${h(row.name||row.template_name||({promotion:"新版自动发布",auto_template:"旧自动模板",material_pool:"旧素材池",direct_test:"旧立即测试"})[row.source]||"任务")} ${h(row.id!=null?`#${row.id}`:"")} ${h(row.status?status(row.status):"")}${row.scheduled_at_utc?" · "+h(time(row.scheduled_at_utc)):""}</li>`).join("")}</ul>`:"—";
    }
    return `<div class="form-grid handoff"><div>新版模板${list(data.new_templates)}</div><div>旧排期模板${list(data.old_templates)}</div><div>在途任务${list(data.inflight)}</div></div>`;
  }
  function policySummary(channel){
    const policy=channel.publish_policy;
    if(!policy?.privacy_level || policy.configured===false || channel.policy_configured===false)return "待设置发布策略";
    const labels=[privacyNames[policy.privacy_level]||policy.privacy_level];
    if(policy.allow_comment)labels.push("允许评论");
    if(policy.allow_duet)labels.push("允许合拍");
    if(policy.allow_stitch)labels.push("允许拼接");
    if(policy.brand_organic_toggle)labels.push("自有品牌");
    if(policy.brand_content_toggle)labels.push("第三方品牌");
    if(policy.is_aigc)labels.push("已声明 AI 内容");
    return labels.join(" · ");
  }
  async function policyEditor(channel){
    const account=byId(channel),seq=++detailRequest;
    modal("编辑频道发布策略",`<p>${h(name(channel))} · #${h(account)}</p><div id="policy-area" class="empty">正在读取当前 TikTok 创作者权限…</div>`);
    let info;
    try{
      const result=await post("/channels/"+encodeURIComponent(account)+"/creator-info",{});
      info=result.creator_info||result;
    }catch(error){
      if(seq!==detailRequest || !id("dialog").open)return;
      id("policy-area").innerHTML='<button type="button" class="button" id="retry-policy">重新读取创作者权限</button>';
      action(id("retry-policy"),"click",()=>policyEditor(channel));
      throw error;
    }
    if(seq!==detailRequest || !id("dialog").open)return;
    const allowed=(info.privacy_level_options||[]).filter(v=>Object.hasOwn(privacyNames,v));
    const saved=channel.policy_configured===false || channel.publish_policy?.configured===false ? {} : channel.publish_policy||{};
    const controls=[
      ["allow_comment","允许评论","comment_disabled"],
      ["allow_duet","允许合拍（Duet）","duet_disabled"],
      ["allow_stitch","允许拼接（Stitch）","stitch_disabled"]
    ];
    modal("编辑频道发布策略",`<form id="channel-policy"><p><strong>${h(info.creator_nickname||name(channel))}</strong> · #${h(account)}${info.creator_username?" · @"+h(info.creator_username):""}</p>
      <fieldset class="group"><legend>观看范围</legend><label class="field">谁可以观看<select name="privacy_level" required><option value="">请手动选择</option>${allowed.map(v=>`<option value="${h(v)}">${h(privacyNames[v])}</option>`).join("")}</select></label></fieldset>
      <fieldset class="group"><legend>互动偏好</legend><div class="policy-checks">${controls.map(([key,label,restriction])=>`<label><input type="checkbox" name="${key}" ${info[restriction]!==false?"disabled":""}> ${label}${info[restriction]!==false?' <small class="muted">平台当前已关闭</small>':""}</label>`).join("")}</div></fieldset>
      <fieldset class="group"><legend>商业推广与 AI 内容披露</legend><div class="policy-checks">
      <label><input name="commercial" type="checkbox"> 包含商业推广</label><div id="brand-fields" class="policy-checks" hidden>
      <label><input name="brand_organic_toggle" type="checkbox"> 推广自己的品牌或业务</label>
      <label><input name="brand_content_toggle" type="checkbox"> 推广第三方品牌或合作方</label></div>
      <label><input name="is_aigc" type="checkbox"> 声明为 AI 生成或经 AI 显著处理的内容</label></div></fieldset>
      <div class="modal-foot"><span id="policy-state" aria-live="polite" class="muted"></span><button id="recheck-policy" type="button" class="button" hidden>重新检测平台能力</button><button type="submit" class="button primary">保存新版发布策略</button></div></form>`);
    const form=id("channel-policy"),e=form.elements;
    if(allowed.includes(saved.privacy_level))e.privacy_level.value=saved.privacy_level;
    for(const [key,,restriction] of controls)e[key].checked=info[restriction]===false && saved[key]===true;
    e.commercial.checked=saved.brand_organic_toggle===true||saved.brand_content_toggle===true;
    for(const key of ["brand_content_toggle","brand_organic_toggle","is_aigc"])e[key].checked=saved[key]===true;
    let saving=false;
    function validate(){
      id("brand-fields").hidden=!e.commercial.checked;
      let error="";
      if(!allowed.length)error="平台未返回可用观看范围，请稍后重新检测。";
      else if(!allowed.includes(e.privacy_level.value))error="请选择当前频道允许的观看范围。";
      else if(e.commercial.checked && !e.brand_organic_toggle.checked && !e.brand_content_toggle.checked)error="请至少选择一种商业推广类型。";
      else if(e.commercial.checked && e.brand_content_toggle.checked && e.privacy_level.value==="SELF_ONLY")error="第三方品牌推广不能使用“仅自己”的观看范围。";
      id("policy-state").textContent=error;
      form.querySelector("[type=submit]").disabled=saving || !!error;
      return !error;
    }
    form.addEventListener("change",validate);validate();
    action(form,"submit",async event=>{
      event.preventDefault();if(saving || !validate())return;
      const publish_policy={privacy_level:e.privacy_level.value,brand_content_toggle:e.commercial.checked&&e.brand_content_toggle.checked,
        brand_organic_toggle:e.commercial.checked&&e.brand_organic_toggle.checked,is_aigc:e.is_aigc.checked};
      for(const [key,,restriction] of controls)publish_policy[key]=info[restriction]===false && e[key].checked;
      saving=true;validate();
      try{
        await post("/channels/settings",{account_ids:[account],settings:{publish_policy}});
        await refresh();
        if(seq!==detailRequest || !id("dialog").open)return;
        await openDetail(account);notify("已保存该频道的新版发布策略。",true);
      }catch(error){
        if(form.isConnected && error.status===409){
          const retry=id("recheck-policy");retry.hidden=false;retry.onclick=()=>policyEditor({...channel,policy_configured:true,publish_policy}).catch(e=>notify(e.message,true,"error"));
        }
        throw error;
      }finally{if(form.isConnected){saving=false;validate();}}
    });
  }
  async function openDetail(accountId,period="28d",section=null){
    const seq=++detailRequest;
    const prior=detailCache.get(accountId);
    let result;
    if(!prior || !id("dialog").open)modal("频道详情",'<div class="empty">正在读取频道、收入画像和交接记录…</div>');
    try{
      result=await get("/channels/"+encodeURIComponent(accountId)+(section?"?section="+section:""));
    }catch(e){
      if(!id("dialog").open || seq!==detailRequest)return;
      if([401,403,404].includes(e.status)){detailCache.delete(accountId);id("dialog").close();throw e;}
      const fallback=prior?.channel||items.find(row=>byId(row)===accountId);
      if(!fallback)throw e;
      const errors={profile:"未能读取最新收入画像",handoff:"未能核验当前排期与在途任务"};
      result={channel:fallback,meta:{module_errors:section?{[section]:errors[section]}:errors}};
    }
    if(!id("dialog").open || seq!==detailRequest)return;
    const currentMeta=mergeMeta(prior?.meta,result.meta||{},section);
    const errors=currentMeta.module_errors||{},channel=mergeChannel(result.channel,prior?.channel,section);
    const profile=(errors.profile||section==="handoff")&&prior?.profile?prior.profile:result.profile||{};
    const snapshot=errors.profile&&prior?.meta?{...prior.meta,module_errors:errors}:currentMeta;
    const plan=errors.handoff||section==="profile"?prior?.handoff:result.handoff;
    detailCache.set(accountId,{channel,profile,meta:snapshot,handoff:plan});
    modal(name(channel),`<div class="group"><div class="inline">${badge(channel.eligible?"可发布":"受限",channel.eligible?"positive":"warning")} ${badge(channel.enabled?"新版已参与":"新版已暂停")} ${h(channel.drama_language||"未配置剧语言")} · ID ${h(accountId)}</div><p class="muted">${h(channel.reason||"")} 最近成功发布 ${h(time(channel.last_published_at))} · 今日已发 / 应发 ${h(channel.today_published??"—")} / ${h(channel.today_expected??"—")}</p></div>
    <div class="group"><div class="heading"><h3>收入画像</h3><div id="periods" class="periods">${Object.entries(periodNames).map(([v,label])=>`<button class="tab" data-period="${v}">${label}</button>`).join("")}</div></div><div id="detail-profile-error" class="module-error" hidden></div><div id="window"></div></div>
    <div class="group"><h3>模板与交接</h3><div id="detail-handoff-error" class="module-error" hidden></div>${plan?handoff(plan):""}</div>
    <div class="group"><div class="heading"><h3>新版发布策略</h3><button id="edit-policy" class="button" type="button">编辑发布策略</button></div><p class="muted">${h(policySummary(channel))}${channel.policy_source==="account"?" · 已沿用该账号的设置":channel.policy_source==="channel"?" · 已保存于新版":""}</p></div>`);
    action(id("edit-policy"),"click",()=>policyEditor(channel));
    function change(period){
      const w=profile.windows?.[period];
      for(const b of id("periods").querySelectorAll("button")){const active=b.dataset.period===period;b.classList.toggle("selected",active);b.setAttribute("aria-pressed",String(active));}
      const date=w?.start&&w?.end ? `${w.start} – ${w.end} UTC` : snapshot.end ? `截至 ${snapshot.end} UTC` : "暂无完整统计日期";
      if(!w){id("window").innerHTML=`<p class="empty">${h(date)} · 暂无可用画像</p>`;return;}
      const metrics=[["总收入",cents(w.revenue_cents)],["充值",cents(w.iap_cents)],["IAA",cents(w.iaa_cents)],["已确认发布",w.published_count??"—"]];
      const gap=[w.iaa_pending_dates?.length?"部分日期 IAA 待回传":"",w.missing_dates?.length?"部分日期待入仓":""].filter(Boolean).join("，");
      id("window").innerHTML=`<p class="muted">${h(date)}${snapshot.stale?" · 画像更新延迟":""}${snapshot.generated_at?" · 刷新于 "+h(time(snapshot.generated_at)):""}${gap?" · "+gap+"，收入仅含已回传金额。":""}</p><div class="kpis">${metrics.map(([l,v])=>`<div><span>${l}</span><strong>${h(v)}</strong></div>`).join("")}</div>${tree(w.audiences||[])||'<p class="muted">暂无可展开的受众与题材收入。</p>'}`;
    }
    id("periods").onclick=e=>{const b=e.target.closest("[data-period]");if(b){period=b.dataset.period;change(period);}};
    change(period);
    for(const module of ["profile","handoff"]){
      const cached=module==="profile"?prior?.profile?.windows:prior?.handoff;
      moduleRetry(id("detail-"+module+"-error"),errors[module]?errors[module]+(cached?"，以下沿用上次结果。":"。"):"",()=>openDetail(accountId,period,module));
    }
  }
  function showSelected(){
    const list=id("chosen-accounts");
    const visible=new Set(displayed.map(byId));
    const chosen=items.filter(row=>selected.has(byId(row)));
    id("chosen-count").textContent=selectionLabel(chosen.filter(row=>visible.has(byId(row))).length);
    list.innerHTML=chosen.map(row=>`<li><span>${h(name(row))} <small>#${h(byId(row))}${visible.has(byId(row))?"":" · 当前筛选外"}</small></span><button type="button" class="row-link" data-remove="${h(byId(row))}" aria-label="移除 ${h(name(row))}">移除</button></li>`).join("");
    id("chosen-continue").disabled=!chosen.length;
    id("dialog-title").textContent=id("settings")?`批量设置 · ${chosen.length} 个频道`:"所选频道";
    list.onclick=e=>{
      const button=e.target.closest("[data-remove]");
      if(!button)return;
      selected.delete(button.dataset.remove);render();showSelected();
    };
  }
  const chosenList='<div class="chosen"><strong id="chosen-count"></strong><ul id="chosen-accounts"></ul></div>';
  function startTemplate(){
    if(canCreate && selected.size){
      location.assign("/tt-promotion-auto.html?accounts="+[...selected].map(encodeURIComponent).join(","));
    }
  }
  function review(){
    detailRequest++;
    modal("所选频道",chosenList+`<footer class="modal-foot"><button id="chosen-continue" type="button" class="button primary">${canCreate?"用所选频道建模板":"批量设置"}</button></footer>`);
    showSelected();id("chosen-continue").onclick=canCreate?startTemplate:bulk;
  }
  function bulk(){
    detailRequest++;
    modal("批量设置",`<form id="settings">${chosenList}<div class="form-grid"><label>新版参与状态<select name="participation"><option value="">保留当前值</option><option value="yes">参与新版发布</option><option value="no">暂停新版发布</option></select></label><label>剧语言<input name="language" placeholder="留空保留当前值，如 en"></label></div><footer class="modal-foot"><button id="chosen-continue" class="button primary" type="submit">保存频道设置</button></footer></form>`);
    showSelected();
    action(id("settings"),"submit",async ev=>{
      ev.preventDefault();
      const ids=[...selected];if(!ids.length)return;
      const form=ev.currentTarget,settings={};
      if(form.elements.participation.value)settings.enabled=form.elements.participation.value==="yes";
      if(form.elements.language.value.trim())settings.drama_language=form.elements.language.value.trim();
      if(!Object.keys(settings).length)throw new Error("请选择参与状态或填写剧语言。");
      const button=form.querySelector("[type=submit]");if(button.disabled)return;button.disabled=true;
      try {
        await post("/channels/settings",{account_ids:ids,settings});
        id("dialog").close();notify(`已保存 ${ids.length} 个频道的新版配置。`);
        try{await refresh();}catch(e){moduleRetry(id("notice"),"设置已保存，列表刷新失败。"+e.message,async()=>{await refresh();notify("");});}
      } finally {button.disabled=false;}
    });
  }
  filters.addEventListener("input",render);
  filters.addEventListener("reset",()=>{selectedScope="";setTimeout(render,0);});
  id("directory-overview").onclick=e=>{const b=e.target.closest("[data-scope]");if(b){selectedScope=b.dataset.scope;render();}};
  async function setupFirst(rows){if(await window.TTFirstUse.open(rows))await refresh();}
  action(id("first-for-selected"),"click",()=>setupFirst(items.filter(r=>selected.has(byId(r)))));
  for(const node of [id("select-all"),id("select-mobile")])node.onchange=e=>{for(const row of displayed){if(e.target.checked)selected.add(byId(row));else selected.delete(byId(row));}render();};
  id("channels").onchange=e=>{if(e.target.dataset.select){if(e.target.checked)selected.add(e.target.dataset.select);else selected.delete(e.target.dataset.select);selectionState();}};
  action(id("channels"),"click",async ev=>{const first=ev.target.closest("[data-first]");if(first){await setupFirst(items.filter(r=>byId(r)===first.dataset.first));return;}const button=ev.target.closest("[data-detail]");if(button)await openDetail(button.dataset.detail,button.dataset.periodOpen||"28d");});
  id("bulk").onclick=bulk;
  id("new-for-selected").onclick=()=>displayed.filter(row=>selected.has(byId(row))).length<selected.size?review():startTemplate();
  id("review-selection").onclick=review;
  id("clear-selection").onclick=()=>{selected.clear();render();};
  id("filter-toggle").onclick=e=>{const expanded=e.currentTarget.getAttribute("aria-expanded")!=="true";e.currentTarget.setAttribute("aria-expanded",String(expanded));id("advanced-filters").hidden=!expanded;};
  boot("ttPromotionChannels",async user=>{
    canCreate=user.role==="admin" || user.permissions?.tt_promotion_auto===true;
    try {
      await refresh();
      moduleRetry(id("list-identity-error"),"");
    } catch(e) {
      if(e.status===401 || e.status===403)throw e;
      moduleRetry(id("list-identity-error"),"频道清单暂时无法读取。"+(items.length?"以下为上次加载结果。 ":" ")+e.message,()=>id("refreshPage").onclick());
      if(!items.length){
        id("list-meta").textContent="等待取得频道清单";
        id("channel-summary").textContent="";
        id("channels").innerHTML=empty(8,"重试后将在这里显示频道。");
      }
    }
  });
})();
