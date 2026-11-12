(() => {
  const reasons = {portrait_mapping_outdated:'画像标签版本待更新，暂按原筛选排序分配',portrait_unavailable:'画像暂不可用，按原筛选排序分配',portrait_incomplete:'画像历史日期不完整，按原筛选排序分配',no_revenue_history:'暂无收入画像，按原筛选排序分配',no_positive_revenue:'暂无正收入，按原筛选排序分配',invalid_revenue_distribution:'收入比例暂不可用，按原筛选排序分配',no_known_revenue_tags:'暂无可识别的收入标签，按原筛选排序分配',candidate_labels_unavailable:'候选标签暂不可用，按原筛选排序分配',no_matching_eligible_tag:'当前候选没有匹配标签，按原筛选排序分配',preferred_tag_no_eligible_material:'首选标签无合格素材，已匹配其他偏好标签'};
  function explain(a) {
    if (!a || !a.policy_version) return '尚未形成分配记录';
    if (a.hierarchy_version) {
      const hierarchyReasons={portrait_hierarchy_outdated:'三级画像待更新，按原顺序',candidate_metadata_unavailable:'候选分类暂不可用，按原顺序',no_valid_type_profile:'无有效类型画像，按原顺序',no_valid_audience_profile:'保留类型偏好，受众层按原顺序',no_valid_theme_profile:'保留类型和受众偏好，题材层按原顺序',preferred_type_no_eligible_material:'首选类型无合格素材，已切换类型',preferred_audience_no_eligible_material:'同类型首选受众无合格素材，已切换受众',preferred_theme_no_eligible_material:'同类型同受众下改选其他画像题材',no_matching_eligible_theme:'同类型同受众下按原顺序选择其他题材'};
      if(a.profile_levels?.[0]==='audience')Object.assign(hierarchyReasons,{portrait_hierarchy_outdated:'受众题材画像待更新，按原顺序',no_valid_audience_profile:'无有效受众画像，按原顺序',no_valid_theme_profile:'保留受众偏好，题材按原顺序',preferred_audience_no_eligible_material:'首选受众无合格素材，已改选其他受众',preferred_theme_no_eligible_material:'同受众下改选其他画像题材',no_matching_eligible_theme:'同受众下按原顺序选择其他题材'});
      return [a.selected_path_names?.length?`实际选择：${a.selected_path_names.join(' → ')}`:'',hierarchyReasons[a.fallback_reason]||reasons[a.fallback_reason]||'',a.classification?.audience_basis==='inferred'?'受众为规则推导':'',a.profile_stale?'使用上次成功画像':'',a.profile_version?`画像 ${a.profile_version}`:''].filter(Boolean).join(' · ');
    }
    return [a.selected_tag ? `匹配标签：${a.selected_tag}` : '', reasons[a.fallback_reason] || '', a.profile_stale ? '使用上次成功画像' : '', a.profile_version ? `画像 ${a.profile_version}` : ''].filter(Boolean).join(' · ');
  }
  async function previewTemplate(item, ui) {
    if (document.getElementById('tagPreviewDialog')) return;
    const ids=[...new Set((item.config?.account_ids || item.account_ids || []).map(String))];
    if(!ids.length){ui.showToast('模板尚未配置频道',true);return;}
    const dialog=node('dialog');dialog.id='tagPreviewDialog';dialog.setAttribute('aria-labelledby','tagPreviewTitle');
    const head=node('div','','dialog-head'),title=node('h2','按频道预览');title.id='tagPreviewTitle';head.append(title);
    const body=node('div','','dialog-body'),label=node('label','选择频道');label.htmlFor='tagPreviewAccount';
    const select=node('select');select.id='tagPreviewAccount';select.style.width='100%';
    for(const id of ids){const option=node('option',`频道 ${id}`);option.value=id;select.append(option);}
    const result=node('p','预览仅检查本频道的选材结果，不保留素材或创建发布任务。');result.setAttribute('role','status');result.style.whiteSpace='pre-wrap';
    body.append(label,select,result);
    const foot=node('div','','dialog-foot'),close=node('button','关闭','button'),run=node('button','预览选中频道','button primary');close.type=run.type='button';foot.append(close,run);
    dialog.append(head,body,foot);document.body.append(dialog);
    const finished=new Promise(resolve=>dialog.addEventListener('close',()=>{dialog.remove();resolve();},{once:true}));
    close.addEventListener('click',()=>dialog.close());
    run.addEventListener('click',async()=>{
      run.disabled=select.disabled=true;result.textContent='正在按该频道的受众与题材画像检查合格剧目和素材…';
      try{
        const payload=await ui.api(`${ui.API_BASE}/templates/${item.id || item.template_id}/preview`,{method:'POST',body:JSON.stringify({expected_version:Number(item.version || item.current_version),account_id:select.value})});
        const row=ui.readItems(payload,['preview','items'])[0];
        if(!row || !ui.boolValue(row.ok))throw Error(row?.error_message || row?.error_code || '暂无合格素材');
        const selection=ui.objectValue(row.selection),drama=ui.objectValue(selection.drama),material=ui.objectValue(selection.material);
        if(dialog.open)result.textContent=`${select.selectedOptions[0].textContent}\n${drama.name || drama.drama_name || '已选剧'} / 素材 ${material.material_id || material.id || '已选'}\n${explain(ui.objectValue(selection.reservation).tag_profile)}`;
      }catch(error){if(dialog.open)result.textContent=error.message || '频道预览失败，请稍后重试';}
      finally{run.disabled=select.disabled=false;}
    });
    dialog.showModal();
    // Names improve the picker, but a failed auxiliary lookup must not block it.
    ui.api(`${ui.API_BASE}/accounts`).then(payload=>{
      const accounts=new Map(ui.readItems(payload,['accounts','items']).map(a=>[String(a.id || a.account_id),a]));
      for(const option of select.options){const a=accounts.get(option.value);if(a)option.textContent=a.username?`@${a.username}`:a.display_name || `频道 ${option.value}`;}
    }).catch(()=>{});
    await finished;
  }
  window.TagPublishUI = {explain,previewTemplate};
  function node(tag, text, cls) { const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n; }
  function start() {
    if (document.querySelector('.tag-lane-banner')) return;
    const host=document.querySelector('main,.main') || document.body;
    const banner=node('section', '', 'tag-lane-banner');
    banner.append(node('strong','X 标签画像发布'));
    banner.append(node('p','模板、素材池、排期、防重和频道发布量独立计算。按受众方向 → 题材逐层按收入比例选择，受众优先。先在同受众内放宽题材，再尝试其他受众；无有效受众画像时沿用原排序，题材画像缺失时保留受众偏好。'));
    const nav=node('nav');
    for(const [href,label] of [['/x-tag-auto-publish-templates.html','自动发布模板'],['/x-tag-material-pool.html','画像素材池'],['/x-tag-auto-publish-runs.html','模板运行记录'],['/x-tag-post-logs.html','画像发布日志'],['/x-account-list.html','频道收入画像']]){const a=node('a',label);a.href=href;nav.append(a);}
    banner.append(nav);
    const details=node('details');details.append(node('summary','查看频道画像和本流程发布量'));
    const list=node('div','','tag-profile-list');details.append(list);banner.append(details);host.prepend(banner);
    details.addEventListener('toggle', async()=>{if(!details.open || details.dataset.loaded)return;list.textContent='正在读取…';
      try{const r=await fetch('/api/admin/x-tag-posts/profiles',{credentials:'same-origin',cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.message||'读取失败');list.replaceChildren();
        const table=node('table');const head=node('tr');['频道','受众收入偏好','本流程已发布'].forEach(x=>head.append(node('th',x)));table.append(head);
        for(const item of d.items || []){const a=item.profile || {}, tags=(a.weights||[]).slice().sort((x,y)=>y.revenue_cents-x.revenue_cents), total=tags.reduce((s,x)=>s+x.revenue_cents,0);const row=node('tr');row.append(node('td',item.username?`@${item.username}`:String(item.account_id)),node('td',tags.length?tags.slice(0,3).map(t=>`${t.name} ${(100*t.revenue_cents/total).toFixed(1)}%`).join(' / '):(explain(a)||'暂无画像')),node('td',String(item.published_count||0)));table.append(row);}list.append(table);details.dataset.loaded='1';
      }catch(e){list.textContent=e.message;}
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
