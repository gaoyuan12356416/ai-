(() => {
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=c=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(c/100);
  const share=b=>b==null?'暂无占比':`${(b/100).toFixed(2)}%`;
  const clock=v=>new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  const basis={api:'接口受众',missing:'待补录',language_mismatch:'语言不匹配',unsupported:'受众值待确认',explicit:'明确标签',inferred:'规则推导',conflict:'标注冲突',unlabeled:'未标注'};
  const rules={authoritative_audience_labels:'剧目接口受众字段',explicit_audience_label:'明确受众标签',conflicting_explicit_audiences:'明确受众标签冲突',conflicting_audience_signals:'男女向信号同时出现',relationship_and_female_story:'恋爱关系／女性成长信号',male_power_and_son_in_law_story:'战神／赘婿／强者信号',general_subject_without_directional_signal:'通用题材且无明确方向信号',insufficient_audience_evidence:'缺少足够受众信号'};
  let items={},meta=null,error='',loading=false,sequence=0,currentId='',currentPath=[],currentVersion='',dialog;
  // Only the currently open channel, for this dialog session. The server keeps
  // its durable portraits on disk; navigating a loaded tree needs no request.
  let detailRoot=null;const detailPages=new Map();
  async function api(path){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
    try{
      const r=await fetch(path,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      let d;try{d=await r.json();}catch(e){if(e.name==='AbortError')throw e;throw Error(r.status===504?'画像读取超时，请稍后重试':'画像服务响应异常，请稍后重试');}
      if(!r.ok)throw Error(d.message||(r.status===504?'画像读取超时，请稍后重试':'画像暂不可用'));
      return d;
    }catch(e){if(e.name==='AbortError')throw Error('画像读取超时，请稍后重试');throw e;}
    finally{clearTimeout(timer);}
  }
  function cell(id,section='audience'){
    const item=items[String(id)];
    if(!item)return error?(section==='audience'?'<button type="button" class="xtr-retry" data-xtr-retry>读取失败 · 重试</button>':'<span class="xtr-empty">—</span>'):'<span class="xtr-note">读取中…</span>';
    if(item.status==='no_history'||!item.audiences?.length)return `<span class="xtr-empty">${section==='audience'?'暂无发布画像':'—'}</span>`;
    const f=item.focus||{},audience=f.audience||{};
    const rows=section==='audience'?item.audiences:(f.themes||[]);
    const path=section==='audience'?[]:[audience.key];
    const context=section==='audience'?'占频道收入':`${audience.name||'未标注'}内`;
    const content=rows.map(n=>`<span class="xtr-metric"><span class="xtr-metric-line"><span>${esc(n.name)}</span><b>${share(n.share_bp)}</b></span><span class="xtr-track"><i style="width:${Math.max(0,Math.min(100,(n.share_bp||0)/100))}%"></i></span></span>`).join('');
    return `<button type="button" class="xtr-open xtr-${esc(section)}" data-xtr-open="${esc(id)}" data-xtr-start="${esc(path.filter(Boolean).join('/'))}" aria-label="查看${section==='audience'?'受众方向':'题材'}收入画像详情"><span class="xtr-context">${esc(context)}</span>${content||'<span class="xtr-empty">暂无分类</span>'}<span class="xtr-more">${item.status==='partial'?'部分归因待核实 · ':''}${section==='audience'?'完整画像':'查看全部'} <span aria-hidden="true">↗</span></span></button>`;
  }
  function warning(info){const notes=[];if(info.stale)notes.push('收入快照更新已延迟，展示上次成功结果');if(info.refresh_failed)notes.push('最近一次画像更新失败，保留上次成功结果');if(info.missing_dates?.length)notes.push(`${info.missing_dates.length} 天源数据未覆盖，不按零统计`);return notes.length?`<div class="xtr-warning" role="status">${esc(notes.join('；'))}</div>`:'';}
  function paint(){
    document.querySelectorAll('[data-x-tag-id]').forEach(n=>{n.innerHTML=cell(n.dataset.xTagId,n.dataset.xTagLevel||'audience');});
    let note=document.getElementById('xtrSummary');if(!note){const table=document.getElementById('accountRows')?.closest('.table-wrap');if(!table)return;note=document.createElement('div');note.id='xtrSummary';note.className='xtr-summary';table.before(note);}
    note.innerHTML=meta?`<div class="xtr-summary-line"><strong>历史收入画像</strong><span>${esc(meta.start)} — ${esc(meta.end)} · UTC · USD</span><span class="xtr-updated">更新于 ${esc(clock(meta.source_generated_at))}（北京时间）</span></div><span class="xtr-note">受众占比以频道总收入为分母；题材展示主要受众内部的收入偏好。点击可展开全部受众、题材及贡献剧目。</span>${warning(meta)}`:`<span class="xtr-note">${esc(error||'正在读取历史收入画像…')}</span>`;
  }
  async function refresh(){if(loading)return;loading=true;error='';try{const d=await api('/api/x-channel-tag-revenue');items=d.items;meta=d.meta;}catch(e){items={};meta=null;error=e.message;}finally{loading=false;paint();}}
  function ensureDialog(){if(dialog)return;dialog=document.createElement('dialog');dialog.className='xtr-dialog';dialog.setAttribute('aria-labelledby','xtrTitle');dialog.innerHTML='<div class="xtr-head"><h2 id="xtrTitle">受众与题材收入画像</h2><button type="button" class="button small" data-xtr-close aria-label="关闭画像详情">关闭</button></div><div id="xtrBody" aria-live="polite"></div>';document.body.append(dialog);dialog.addEventListener('click',e=>{if(e.target===dialog||e.target.closest('[data-xtr-close]'))dialog.close();});dialog.addEventListener('close',()=>sequence++);}
  function intro(d){return `<p class="xtr-note">${esc(d.meta.start)} 至 ${esc(d.meta.end)} · UTC · USD · 收入快照 ${esc(clock(d.meta.source_generated_at))}（北京时间）<br>受众优先明确标签，再按现有标签规则推导；缺失或冲突列为“未标注”。收入按受众 → 题材汇总，不区分剧类型。</p>${warning(d.meta)}`;}
  async function open(id,path=[],page=1,reset=false){
    if(reset||String(id)!==String(currentId)){currentVersion='';detailRoot=null;detailPages.clear();}
    ensureDialog();currentId=id;currentPath=path;if(!dialog.open)dialog.showModal();const request=++sequence,body=document.getElementById('xtrBody');body.innerHTML='<p>正在读取详情…</p>';
    try{
      if(!detailRoot){const loaded=await api(`/api/x-channel-tag-revenue/${encodeURIComponent(id)}`);if(request!==sequence)return;detailRoot=loaded;currentVersion=loaded.meta.version;}
      let d=detailRoot;
      if(path.length<2){
        let portrait=d.portrait;
        for(let index=0;index<path.length;index++){portrait=(index?portrait.children:portrait.audiences)?.find(n=>n.key===path[index]);if(!portrait)throw Error('该分类已更新，请重新打开详情');}
        d={...d,portrait};
      }else{
        const key=`${path.join('/')}?page=${page}`;
        if(detailPages.has(key))d=detailPages.get(key);
        else{const query=new URLSearchParams({path:path.join('/'),version:currentVersion,page:String(page)});d=await api(`/api/x-channel-tag-revenue/${encodeURIComponent(id)}?${query}`);if(request!==sequence)return;if(detailPages.size>=20)detailPages.clear();detailPages.set(key,d);}
      }
      if(request!==sequence)return;
      document.getElementById('xtrTitle').textContent=`${d.account_name}${d.username?' (@'+d.username+')':''} · 受众与题材收入画像`;
      const p=d.portrait,parent=['频道','受众'][path.length]||'受众',level=['受众方向','题材'][path.length];
      let html=intro(d)+`<div class="xtr-breadcrumb"><button type="button" class="button small" data-xtr-path="">全部受众</button>${path.length?'<button type="button" class="button small" data-xtr-back>← 返回上一级</button>':''}<span>${['受众','题材'].slice(0,path.length).join(' → ')}</span></div>`;
      if(path.length){html+=`<p><strong>${esc(p.name)}</strong> · 收入 ${esc(money(p.revenue_cents))} · 占上一级 ${share(p.share_bp)}${path.length===2?' · 占频道 '+share(p.channel_share_bp):''}</p>`;}
      else{html+=`<div class="xtr-stats"><span>累计收入 <strong>${esc(money(p.revenue_cents))}</strong></span><span>${p.drama_count} 部剧 · ${p.published_count} 次成功发布</span></div><p class="xtr-note">受众分类：${Object.entries(p.audience_basis_counts||{}).map(([k,v])=>`${esc(basis[k]||k)} ${v} 部`).join('｜')}。进入题材后可查看每部剧的题材标签和受众来源。</p>`;}
      if(p.status==='partial')html+='<p class="xtr-warning">部分归因待核对，仅展示已确认归属的收入。</p>';
      if(path.length<2){
        const rows=path.length?p.children:p.audiences;
        html+=rows?.length?`<p class="xtr-note">当前层：${level}。下列占比均以当前${parent}的收入为分母。</p><div class="xtr-table-scroll"><table class="xtr-table"><thead><tr><th>${level}（点击展开）</th><th>收入</th><th>占上一级比例</th><th>剧目数</th><th>成功发布数</th></tr></thead><tbody>${rows.map(r=>`<tr><td><button type="button" class="xtr-tag-link" data-xtr-path="${esc([...path,r.key].join('/'))}">${esc(r.name)}</button></td><td>${esc(money(r.revenue_cents))}</td><td>${share(r.share_bp)}</td><td>${r.drama_count}</td><td>${r.published_count}</td></tr>`).join('')}</tbody><tfoot><tr><th>合计</th><td>${esc(money(p.revenue_cents))}</td><td>${rows.every(r=>r.share_bp!=null)?'100.00%':'—'}</td><td colspan="2">${rows.length} 个分类</td></tr></tfoot></table></div>`:'<p>该快照暂无可展开的受众与题材画像。</p>';
      }else{
        html+=`<div class="xtr-table-scroll"><table class="xtr-table"><thead><tr><th>实际剧目版本／分类依据</th><th>剧目总收入</th><th>去重题材数</th><th>本组合分摊收入</th><th>成功发布数</th></tr></thead><tbody>${(p.dramas||[]).map(r=>{const c=r.classification||{};return `<tr><td><strong>${esc(r.name)}</strong><div class="xtr-note">${esc(r.content_id)} · ${esc(r.language)} · ${esc(c.audience_name)}</div><div class="xtr-note">受众：${esc(basis[c.audience_basis]||'未标注')}；${(c.audience_rules||[]).map(k=>esc(rules[k]||k)).join('、')}</div><details><summary>题材标签与受众来源</summary><div class="xtr-note">原标签：${esc((c.raw_labels||[]).join('、')||'未记录')}<br>接口受众：${esc((c.audience_evidence||[]).join('、')||'无')}<br>题材：${esc((c.theme_names||[]).join('、'))}<br>分类版本：${esc(c.taxonomy_version)}</div></details></td><td>${esc(money(r.revenue_cents))}</td><td>${(c.themes||[]).length}</td><td>${esc(money(r.allocated_cents))}</td><td>${r.published_count}</td></tr>`;}).join('')}</tbody></table></div>`;
        const pg=p.pagination;if(pg&&pg.total>pg.page_size)html+=`<div class="xtr-pagination"><span>共 ${pg.total} 部剧 · 第 ${pg.page} / ${Math.ceil(pg.total/pg.page_size)} 页</span><button type="button" class="button small" data-xtr-page="${pg.page-1}" ${pg.page<=1?'disabled':''}>上一页</button><button type="button" class="button small" data-xtr-page="${pg.page+1}" ${pg.page*pg.page_size>=pg.total?'disabled':''}>下一页</button></div>`;
      }
      body.innerHTML=html+'<p class="xtr-note">历史收入按当前标签与分类规则回算。题材映射到同一大类后先去重，再平均分摊；各层金额保留分币尾差并保持总额一致。没有可识别题材时列为未标注；不明确的单个标签不单独增加题材份额。剧目数和发布数不可跨题材相加；总额非正或子项含负值时，该层不展示比例。</p>';
    }catch(e){if(request===sequence)body.innerHTML=`<p>${esc(e.message)}</p><button type="button" class="button small" data-xtr-open="${esc(id)}">重新读取频道详情</button>`;}
  }
  document.addEventListener('click',e=>{const t=e.target.closest('[data-xtr-open],[data-xtr-retry],[data-xtr-path],[data-xtr-back],[data-xtr-page]');if(!t)return;if(t.hasAttribute('data-xtr-open'))open(t.dataset.xtrOpen,(t.dataset.xtrStart||'').split('/').filter(Boolean),1,true);else if(t.hasAttribute('data-xtr-retry'))refresh();else if(t.hasAttribute('data-xtr-page'))open(currentId,currentPath,Number(t.dataset.xtrPage));else if(t.hasAttribute('data-xtr-back'))open(currentId,currentPath.slice(0,-1));else open(currentId,(t.dataset.xtrPath||'').split('/').filter(Boolean));});
  window.XTagRevenue={cell,refresh,paint};if(document.querySelector('[data-x-tag-id]'))refresh();
})();
