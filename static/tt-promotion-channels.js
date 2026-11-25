(function (){
  "use strict";
  const {id,h,money,cents,time,badge,empty,notify,get,post,modal,action,boot,byId,name,status}=window.TTPromotion;
  const selected=new Set();
  let items=[],meta={},displayed=[],detailRequest=0;
  const filters=id("filters");
  const periodNames={all:"累计","28d":"近 28 天","7d":"近 7 天"};
  function topLabel(value){return typeof value==="object" ? value?.name : value;}
  function filtered(){
    const controls=filters.elements;
    const query=controls.search.value.trim().toLowerCase();
    const min=controls.min.value,max=controls.max.value;
    return items.filter(row=>{
      const amount=row.revenue_28d;
      return (!query || [name(row),row.account_id].some(piece=>String(piece??"").toLowerCase().includes(query)))
        && (!controls.language.value || row.drama_language===controls.language.value)
        && (!controls.eligible.value || !!row.eligible===(controls.eligible.value==="yes"))
        && (!controls.profile.value || !!row.has_profile===(controls.profile.value==="yes"))
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
    const all=id("select-all");
    all.checked=displayed.length>0 && displayed.every(row=>selected.has(byId(row)));
    all.indeterminate=!all.checked && displayed.some(row=>selected.has(byId(row)));
    all.disabled=!displayed.length;
    id("bulk").disabled=selected.size===0;
    id("bulk").textContent=selected.size ? `批量设置（${selected.size}）` : "批量设置";
  }
  function render(){
    displayed=filtered();
    const labels=[`${displayed.length} / ${items.length} 个频道`];
    if(meta.end) labels.push(`截至 ${meta.end} UTC`);
    if(meta.stale) labels.push("画像更新延迟");
    id("list-meta").textContent=labels.join(" · ");
    id("channels").innerHTML=displayed.map(row=>{
      const account=h(byId(row));
      const themes=(row.themes||[]).map(topLabel).filter(Boolean).slice(0,3).join("、");
      return `<tr><td><input type="checkbox" data-select="${account}" aria-label="${h("选择 "+name(row))}" ${selected.has(byId(row))?"checked":""}></td>
      <td><button class="row-link" data-detail="${account}">${h(name(row))}</button><small class="secondary">${account}</small></td>
      <td>${h(row.drama_language||"未配置")}</td><td>${badge(row.eligible?"可发布":"受限",row.eligible?"positive":"warning")}<small class="secondary">${h(row.reason || (row.enabled===false?"新版未参与":""))}</small></td>
      <td>${h(time(row.last_published_at))}</td><td>${h(row.today_published??"—")} / ${h(row.today_expected??"—")}</td><td>${h(money(row.revenue_28d))}</td>
      <td>${h(topLabel(row.audience)||"—")}<small class="secondary">${h(themes)}</small></td></tr>`;
    }).join("") || empty(8,items.length?"当前条件下没有频道":"暂无可见频道");
    selectionState();
  }
  async function refresh(){
    const result=await get("/channels");
    items=result.items;meta=result.meta||{};
    for(const selectedId of selected) if(!items.some(r=>byId(r)===selectedId))selected.delete(selectedId);
    const lang=filters.elements.language,previous=lang.value;
    lang.innerHTML='<option value="">全部语言</option>'+[...new Set(items.map(row=>row.drama_language).filter(Boolean))].sort().map(x=>`<option>${h(x)}</option>`).join("");
    lang.value=previous;
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
      return rows?.length? `<ul class="compact">${rows.map(row=>`<li>${h(row.name||row.template_name||({promotion:"新版自动发布",auto_template:"旧自动模板",material_pool:"旧素材池"})[row.source]||"任务")} ${h(row.id!=null?`#${row.id}`:"")} ${h(row.status?status(row.status):"")}</li>`).join("")}</ul>`:"—";
    }
    return `<div class="form-grid handoff"><div>新版模板${list(data.new_templates)}</div><div>旧排期模板${list(data.old_templates)}</div><div>在途任务${list(data.inflight)}</div></div>`;
  }
  async function openDetail(accountId){
    const seq=++detailRequest;
    modal("频道详情",'<div class="empty">正在读取频道、收入画像和交接记录…</div>');
    const result=await get("/channels/"+encodeURIComponent(accountId));
    if(!id("dialog").open || seq!==detailRequest)return;
    const channel=result.channel,profile=result.profile||{},snapshot=result.meta||{};
    modal(name(channel),`<div class="group"><div class="inline">${badge(channel.eligible?"可发布":"受限",channel.eligible?"positive":"warning")} ${h(channel.drama_language||"未配置剧语言")} · ID ${h(accountId)} ${channel.enabled===false?"· 新版未参与":""}</div><p class="muted">${h(channel.reason||"")} 最近成功发布 ${h(time(channel.last_published_at))} · 今日已发 / 应发 ${h(channel.today_published??"—")} / ${h(channel.today_expected??"—")}</p></div>
    <div class="group"><div class="heading"><h3>收入画像</h3><div id="periods" class="periods">${Object.entries(periodNames).map(([v,label])=>`<button class="tab" data-period="${v}">${label}</button>`).join("")}</div></div><div id="window"></div></div>
    <div class="group"><h3>模板与交接</h3>${handoff(result.handoff||{})}</div>`);
    function change(period){
      const w=profile.windows?.[period];
      for(const b of id("periods").querySelectorAll("button")){const active=b.dataset.period===period;b.classList.toggle("selected",active);b.setAttribute("aria-pressed",String(active));}
      const date=w?.start&&w?.end ? `${w.start} – ${w.end} UTC` : snapshot.end ? `截至 ${snapshot.end} UTC` : "暂无完整统计日期";
      if(!w){id("window").innerHTML=`<p class="empty">${h(date)} · 暂无可用画像</p>`;return;}
      const metrics=[["总收入",cents(w.revenue_cents)],["充值",cents(w.iap_cents)],["IAA",cents(w.iaa_cents)],["已确认发布",w.published_count??"—"]];
      id("window").innerHTML=`<p class="muted">${h(date)}${snapshot.stale?" · 画像更新延迟":""}${snapshot.generated_at?" · 刷新于 "+h(time(snapshot.generated_at)):""}</p><div class="kpis">${metrics.map(([l,v])=>`<div><span>${l}</span><strong>${h(v)}</strong></div>`).join("")}</div>${tree(w.audiences||[])||'<p class="muted">暂无可展开的受众与题材收入。</p>'}`;
    }
    id("periods").onclick=e=>{const b=e.target.closest("[data-period]");if(b)change(b.dataset.period);};
    change("all");
  }
  function bulk(){
    detailRequest++;
    const ids=[...selected];
    modal(`批量设置 · ${ids.length} 个频道`,`<form id="settings"><div class="form-grid"><label>新版参与状态<select name="participation"><option value="">保留当前值</option><option value="yes">参与新版发布</option><option value="no">暂停新版发布</option></select></label><label>剧语言<input name="language" placeholder="留空保留当前值，如 en"></label></div><p class="muted">仅更新本次勾选频道的新版运营配置。</p><footer class="modal-foot"><button class="button primary" type="submit">保存频道设置</button></footer></form>`);
    action(id("settings"),"submit",async ev=>{
      ev.preventDefault();
      const form=ev.currentTarget,settings={};
      if(form.elements.participation.value)settings.enabled=form.elements.participation.value==="yes";
      if(form.elements.language.value.trim())settings.drama_language=form.elements.language.value.trim();
      if(!Object.keys(settings).length)throw new Error("请选择参与状态或填写剧语言。");
      const button=form.querySelector("[type=submit]");if(button.disabled)return;button.disabled=true;
      try {await post("/channels/settings",{account_ids:ids,settings});id("dialog").close();await refresh();notify(`已保存 ${ids.length} 个频道的新版配置。`);} finally {button.disabled=false;}
    });
  }
  filters.addEventListener("input",render);
  filters.addEventListener("reset",()=>setTimeout(render,0));
  id("select-all").onchange=e=>{for(const row of displayed){if(e.target.checked)selected.add(byId(row));else selected.delete(byId(row));}render();};
  id("channels").onchange=e=>{if(e.target.dataset.select){if(e.target.checked)selected.add(e.target.dataset.select);else selected.delete(e.target.dataset.select);selectionState();}};
  action(id("channels"),"click",async ev=>{const button=ev.target.closest("[data-detail]");if(button)await openDetail(button.dataset.detail);});
  id("bulk").onclick=bulk;
  boot("ttPromotionChannels",refresh);
})();
