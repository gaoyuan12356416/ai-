(() => {
  const reasons = {portrait_mapping_outdated:'画像标签版本待更新，暂按原筛选排序分配',portrait_unavailable:'画像暂不可用，按原筛选排序分配',portrait_incomplete:'画像历史日期不完整，按原筛选排序分配',no_revenue_history:'暂无收入画像，按原筛选排序分配',no_positive_revenue:'暂无正收入，按原筛选排序分配',invalid_revenue_distribution:'收入比例暂不可用，按原筛选排序分配',no_known_revenue_tags:'暂无可识别的收入标签，按原筛选排序分配',candidate_labels_unavailable:'候选标签暂不可用，按原筛选排序分配',no_matching_eligible_tag:'当前候选没有匹配标签，按原筛选排序分配',preferred_tag_no_eligible_material:'首选标签无合格素材，已匹配其他偏好标签'};
  function explain(a) {
    if (!a || !a.policy_version) return '尚未形成分配记录';
    return [a.selected_tag ? `匹配标签：${a.selected_tag}` : '', reasons[a.fallback_reason] || '', a.profile_stale ? '使用上次成功画像' : '', a.profile_version ? `画像 ${a.profile_version}` : ''].filter(Boolean).join(' · ');
  }
  window.TagPublishUI = {explain};
  function node(tag, text, cls) { const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n; }
  function start() {
    if (document.querySelector('.tag-lane-banner')) return;
    const host=document.querySelector('main,.main') || document.body;
    const banner=node('section', '', 'tag-lane-banner');
    banner.append(node('strong','X 标签画像发布'));
    banner.append(node('p','模板、素材池、排期、防重和频道发布量独立计算。按标签收入比例加权选择，同义标签先合并再平均分摊收入；同标签内继续使用模板排序或素材入池顺序。没有可用画像或匹配候选时会显示降级原因。'));
    const nav=node('nav');
    for(const [href,label] of [['/x-tag-auto-publish-templates.html','自动发布模板'],['/x-tag-material-pool.html','画像素材池'],['/x-tag-auto-publish-runs.html','模板运行记录'],['/x-tag-post-logs.html','画像发布日志'],['/x-account-list.html','频道收入画像'],['/x-auto-publish-templates.html','原自动发布']]){const a=node('a',label);a.href=href;nav.append(a);}
    banner.append(nav);
    const details=node('details');details.append(node('summary','查看频道画像和本流程发布量'));
    const list=node('div','','tag-profile-list');details.append(list);banner.append(details);host.prepend(banner);
    details.addEventListener('toggle', async()=>{if(!details.open || details.dataset.loaded)return;list.textContent='正在读取…';
      try{const r=await fetch('/api/admin/x-tag-posts/profiles',{credentials:'same-origin',cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.message||'读取失败');list.replaceChildren();
        const table=node('table');const head=node('tr');['频道','标签收入偏好','本流程已发布'].forEach(x=>head.append(node('th',x)));table.append(head);
        for(const item of d.items || []){const a=item.profile || {}, tags=(a.weights||[]).slice().sort((x,y)=>y.revenue_cents-x.revenue_cents), total=tags.reduce((s,x)=>s+x.revenue_cents,0);const row=node('tr');row.append(node('td',item.username?`@${item.username}`:String(item.account_id)),node('td',tags.length?tags.slice(0,3).map(t=>`${t.name} ${(100*t.revenue_cents/total).toFixed(1)}%`).join(' / '):(reasons[a.fallback_reason]||'暂无画像')),node('td',String(item.published_count||0)));table.append(row);}list.append(table);details.dataset.loaded='1';
      }catch(e){list.textContent=e.message;}
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
