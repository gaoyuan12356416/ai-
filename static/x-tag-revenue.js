(() => {
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=c=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(c/100);
  const share=b=>b==null?'暂无占比':`${(b/100).toFixed(2)}%`;
  const clock=v=>new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  const basis={explicit:'明确标签',inferred:'规则推导',conflict:'标注冲突',unlabeled:'证据不足'};
  const rules={explicit_audience_label:'明确受众标签',conflicting_explicit_audiences:'明确受众标签冲突',conflicting_audience_signals:'男女向信号同时出现',relationship_and_female_story:'恋爱关系／女性成长信号',male_power_and_son_in_law_story:'战神／赘婿／强者信号',general_subject_without_directional_signal:'通用题材且无明确方向信号',insufficient_audience_evidence:'缺少足够受众信号'};
  let items={},meta=null,error='',loading=false,sequence=0,currentId='',currentPath=[],dialog;
  async function api(path){const r=await fetch(path,{credentials:'same-origin',cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.message||'画像暂不可用');return d;}
  function cell(id){
    const item=items[String(id)];
    if(!item)return error?`<span class="xtr-note">${esc(error)}</span><button type="button" class="button small" data-xtr-retry>重试</button>`:'<span class="xtr-note">正在读取画像…</span>';
    if(item.status==='no_history')return '<span class="xtr-note">该快照暂无已确认发布</span>';
    return `<button type="button" class="xtr-open" data-xtr-open="${esc(id)}" aria-label="查看频道三级收入画像详情"><strong>${esc(money(item.revenue_cents))}</strong><span class="xtr-note">类型：${(item.types||[]).map(t=>`${esc(t.name)} ${share(t.share_bp)}`).join('｜')||'等待三级画像更新'}</span><span class="xtr-note">主要组合（占频道）：</span>${(item.combinations||[]).map(c=>`<span class="xtr-tag"><span>${esc(c.name)}</span><b>${share(c.channel_share_bp)}</b></span>`).join('')}<span class="xtr-note">${item.status==='partial'?'部分归因待核实 · ':''}展开 类型 → 受众 → 题材 →</span></button>`;
  }
  function warning(info){const notes=[];if(info.stale)notes.push('收入快照更新已延迟，展示上次成功结果');if(info.refresh_failed)notes.push('最近一次画像更新失败，保留上次成功结果');if(info.missing_dates?.length)notes.push(`${info.missing_dates.length} 天源数据未覆盖，不按零统计`);return notes.length?`<div class="xtr-warning" role="status">${esc(notes.join('；'))}</div>`:'';}
  function paint(){
    document.querySelectorAll('[data-x-tag-id]').forEach(n=>{n.innerHTML=cell(n.dataset.xTagId);});
    let note=document.getElementById('xtrSummary');if(!note){const table=document.getElementById('accountRows')?.closest('.table-wrap');if(!table)return;note=document.createElement('div');note.id='xtrSummary';note.className='xtr-summary';table.before(note);}
    note.innerHTML=meta?`<span>三级收入画像：${esc(meta.start)} 至 ${esc(meta.end)}（UTC 完整日），USD。类型 → 受众 → 题材；同一题材大类去重后均分。</span><span class="xtr-note">收入快照 ${esc(clock(meta.source_generated_at))}（北京时间）；每小时更新。受众由明确标签或版本化规则提炼。</span>${warning(meta)}`:esc(error||'正在读取历史收入画像…');
  }
  async function refresh(){if(loading)return;loading=true;error='';try{const d=await api('/api/x-channel-tag-revenue');items=d.items;meta=d.meta;}catch(e){items={};meta=null;error=e.message;}finally{loading=false;paint();}}
  function ensureDialog(){if(dialog)return;dialog=document.createElement('dialog');dialog.className='xtr-dialog';dialog.setAttribute('aria-labelledby','xtrTitle');dialog.innerHTML='<div class="xtr-head"><h2 id="xtrTitle">三级收入画像</h2><button type="button" class="button small" data-xtr-close aria-label="关闭画像详情">关闭</button></div><div id="xtrBody" aria-live="polite"></div>';document.body.append(dialog);dialog.addEventListener('click',e=>{if(e.target===dialog||e.target.closest('[data-xtr-close]'))dialog.close();});dialog.addEventListener('close',()=>sequence++);}
  function intro(d){return `<p class="xtr-note">${esc(d.meta.start)} 至 ${esc(d.meta.end)} · UTC · USD · 收入快照 ${esc(clock(d.meta.source_generated_at))}（北京时间）<br>类型取实际剧目版本的 drama_info.resource_type；受众优先明确标签，再按现有标签规则推导。缺失或冲突列为“未标注”，不默认本土或通用。</p>${warning(d.meta)}`;}
  async function open(id,path=[]){
    ensureDialog();currentId=id;currentPath=path;if(!dialog.open)dialog.showModal();const request=++sequence,body=document.getElementById('xtrBody');body.innerHTML='<p>正在读取详情…</p>';
    try{
      const d=await api(`/api/x-channel-tag-revenue/${encodeURIComponent(id)}${path.length?'?path='+encodeURIComponent(path.join('/')):''}`);if(request!==sequence)return;
      document.getElementById('xtrTitle').textContent=`${d.account_name}${d.username?' (@'+d.username+')':''} · 三级收入画像`;
      const p=d.portrait,parent=['频道','剧类型','类型＋受众'][path.length]||'类型＋受众',level=['剧类型','受众方向','题材'][path.length];
      let html=intro(d)+`<div class="xtr-breadcrumb"><button type="button" class="button small" data-xtr-path="">全部类型</button>${path.length?'<button type="button" class="button small" data-xtr-back>← 返回上一级</button>':''}<span>${['类型','受众','题材'].slice(0,path.length).join(' → ')}</span></div>`;
      if(path.length){html+=`<p><strong>${esc(p.name)}</strong> · 收入 ${esc(money(p.revenue_cents))} · 占上一级 ${share(p.share_bp)}${path.length===3?' · 占频道 '+share(p.channel_share_bp):''}</p>`;}
      else{html+=`<div class="xtr-stats"><span>累计收入 <strong>${esc(money(p.revenue_cents))}</strong></span><span>${p.drama_count} 部剧 · ${p.published_count} 次成功发布</span></div><p class="xtr-note">受众分类：${Object.entries(p.audience_basis_counts||{}).map(([k,v])=>`${esc(basis[k]||k)} ${v} 部`).join('｜')}。进入题材后可查看每部剧的原标签和规则依据。</p>`;}
      if(p.status==='partial')html+='<p class="xtr-warning">部分归因待核对，仅展示已确认归属的收入。</p>';
      if(path.length<3){
        const rows=path.length?p.children:p.types;
        html+=rows?.length?`<p class="xtr-note">当前层：${level}。下列占比均以当前${parent}的收入为分母。</p><div class="xtr-table-scroll"><table class="xtr-table"><thead><tr><th>${level}（点击展开）</th><th>收入</th><th>占上一级比例</th><th>剧目数</th><th>成功发布数</th></tr></thead><tbody>${rows.map(r=>`<tr><td><button type="button" class="xtr-tag-link" data-xtr-path="${esc([...path,r.key].join('/'))}">${esc(r.name)}</button></td><td>${esc(money(r.revenue_cents))}</td><td>${share(r.share_bp)}</td><td>${r.drama_count}</td><td>${r.published_count}</td></tr>`).join('')}</tbody><tfoot><tr><th>合计</th><td>${esc(money(p.revenue_cents))}</td><td>${rows.every(r=>r.share_bp!=null)?'100.00%':'—'}</td><td colspan="2">${rows.length} 个分类</td></tr></tfoot></table></div>`:'<p>该快照暂无可展开的三级画像。</p>';
      }else{
        html+=`<div class="xtr-table-scroll"><table class="xtr-table"><thead><tr><th>实际剧目版本／分类依据</th><th>剧目总收入</th><th>去重题材数</th><th>本组合分摊收入</th><th>成功发布数</th></tr></thead><tbody>${(p.dramas||[]).map(r=>{const c=r.classification||{};return `<tr><td><strong>${esc(r.name)}</strong><div class="xtr-note">${esc(r.content_id)} · ${esc(r.language)} · ${esc(c.drama_type_name)} · ${esc(c.audience_name)}</div><div class="xtr-note">受众：${esc(basis[c.audience_basis]||'未标注')}；${(c.audience_rules||[]).map(k=>esc(rules[k]||k)).join('、')}</div><details><summary>原标签与推导依据</summary><div class="xtr-note">原标签：${esc((c.raw_labels||[]).join('、')||'未记录')}<br>命中依据：${esc((c.audience_evidence||[]).join('、')||'无')}<br>题材：${esc((c.theme_names||[]).join('、'))}<br>分类版本：${esc(c.taxonomy_version)}</div></details></td><td>${esc(money(r.revenue_cents))}</td><td>${(c.themes||[]).length}</td><td>${esc(money(r.allocated_cents))}</td><td>${r.published_count}</td></tr>`;}).join('')}</tbody></table></div>`;
      }
      body.innerHTML=html+'<p class="xtr-note">历史收入按当前标签与分类规则回算。题材映射到同一大类后先去重，再平均分摊；各层金额保留分币尾差并保持总额一致。没有可识别题材时列为未标注；不明确的单个标签不单独增加题材份额。剧目数和发布数不可跨题材相加；总额非正或子项含负值时，该层不展示比例。</p>';
    }catch(e){if(request===sequence)body.innerHTML=`<p>${esc(e.message)}</p><button type="button" class="button small" data-xtr-path="">重新读取频道详情</button>`;}
  }
  document.addEventListener('click',e=>{const t=e.target.closest('[data-xtr-open],[data-xtr-retry],[data-xtr-path],[data-xtr-back]');if(!t)return;if(t.hasAttribute('data-xtr-open'))open(t.dataset.xtrOpen);else if(t.hasAttribute('data-xtr-retry'))refresh();else if(t.hasAttribute('data-xtr-back'))open(currentId,currentPath.slice(0,-1));else open(currentId,(t.dataset.xtrPath||'').split('/').filter(Boolean));});
  window.XTagRevenue={cell,refresh};if(document.querySelector('[data-x-tag-id]'))refresh();
})();
