/* Bounded current-page summaries; details and 50-drama pages load on demand. */
(() => {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = v => '$'+((v||0)/100).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const pct = v => v == null ? '—' : (v/100).toFixed(1)+'%';
  const colors = {female:'#b156a6',male:'#4a78c2',general:'#208d81',lgbtq:'#9361be',__unknown__:'#8490a2'};
  let request=0,abort=null,cache=new Map(),meta=null;
  const pending = '<span class="fpp-muted">画像加载中…</span>';
  async function get(path,signal) {
    const r=await fetch('/api/fb-page-portraits'+path,{credentials:'same-origin',cache:'no-store',signal});
    const data=await r.json();if(!r.ok)throw new Error(data.message||'画像暂不可用');return data;
  }
  function cells(id) {
    const p=cache.get(id);
    if(!p)return `<td data-fpp-income="${esc(id)}">${pending}</td><td data-fpp-profile="${esc(id)}">${pending}</td>`;
    return `<td data-fpp-income="${esc(id)}">${income(p)}</td><td data-fpp-profile="${esc(id)}">${portrait(p)}</td>`;
  }
  function income(p){return `<div class="fpp-income"><strong>${money(p.revenue_cents)}</strong><small>投放 ${money(p.paid_cents)}<br>社媒 ${money(p.social_cents)}</small></div>`;}
  function portrait(p){
    if(p.status!=='ready')return `<span class="fpp-muted">${p.status==='signed_adjustment'?'含收入调账，暂不计算比例':'近 7 天暂无收入画像'}</span><small class="fpp-sub">${p.drama_count} 部剧 · ${p.active_days} 个有效日</small>`;
    const a=p.focus.audience;
    const bar=p.audiences.filter(a=>a.share_bp>0).map(a=>`<i style="width:${a.share_bp/100}%;background:${colors[a.key]||colors.__unknown__}" title="${esc(a.name)} ${pct(a.share_bp)}"></i>`).join('');
    return `<button class="fpp-open" data-fpp-open="${esc(p.page_id)}"><span class="fpp-title">${esc(a.name)} <b>${pct(a.share_bp)}</b></span><span class="fpp-bar">${bar}</span><span class="fpp-themes">${p.focus.themes.slice(0,2).map(t=>`${esc(t.name)} ${pct(t.share_bp)}`).join(' · ')}</span><small>题材比例为该方向内占比 · 查看详情 →</small></button>`;
  }
  function repaint(id,p){
    document.querySelectorAll(`[data-fpp-income="${id}"]`).forEach(el=>el.innerHTML=p?income(p):'<span class="fpp-muted">等待下次统计</span>');
    document.querySelectorAll(`[data-fpp-profile="${id}"]`).forEach(el=>el.innerHTML=p?portrait(p):'<span class="fpp-muted">尚未生成画像</span>');
  }
  async function load(ids,{fresh=false}={}){
    const gen=++request;abort?.abort();abort=new AbortController();
    if(fresh){cache.clear();meta=null;}
    if(!ids.length)return;
    ids.forEach(id=>{if(cache.has(id))repaint(id,cache.get(id));});
    if(ids.every(id=>cache.has(id)))return;
    try{
      const data=await get('?ids='+encodeURIComponent(ids.join(',')),abort.signal);
      if(gen!==request)return;
      if(meta&&meta.version!==data.meta.version)cache.clear();
      meta=data.meta;
      const notice=document.getElementById('pagePortraitNotice');
      if(notice){notice.textContent=`近 7 个完整报告日 ${meta.start} — ${meta.end} · 投放 D0 + AI 后台已确认发布的社媒收入（USD） · ${meta.stale?'数据刷新延迟':'每小时更新'}。比例代表收入偏好，非真实用户性别比例。`;notice.classList.toggle('fpp-warning',!!meta.stale);}
      ids.forEach(id=>{const p=data.items[id];if(p)cache.set(id,p);repaint(id,p);});
    }catch(error){
      if(gen!==request||error.name==='AbortError')return;
      ids.forEach(id=>document.querySelectorAll(`[data-fpp-profile="${id}"],[data-fpp-income="${id}"]`).forEach(el=>el.innerHTML=`<span class="fpp-muted">${esc(error.message)}</span>`));
    }
  }
  async function open(id,name,dialog){
    const modal=dialog('pagePortraitDetail',name+' · 收入画像','<div class="fpp-detail">正在加载画像…</div>','<button class="button" data-close>关闭</button>');
    const ctl=new AbortController();modal.addEventListener('close',()=>ctl.abort(),{once:true});
    const target=modal.querySelector('.fpp-detail');
    let data,p,version,detailGeneration=0,dramaAbort=null;
    function showTree(){
      ++detailGeneration;dramaAbort?.abort();
      target.innerHTML=`<p class="fpp-period">${esc(data.meta.start)} — ${esc(data.meta.end)} · 近 7 个完整报告日</p><div class="fpp-cards"><div><small>合计收入</small><strong>${money(p.revenue_cents)}</strong></div><div><small>投放 D0 收入</small><strong>${money(p.paid_cents)}</strong></div><div><small>社媒收入</small><strong>${money(p.social_cents)}</strong></div></div><p class="fpp-explain">先按故事受众方向分类，再按题材分配收入。同一部剧命中多个题材时均分，合计不会重复。“未标注”表示标签证据不足，不代表没有收入。</p><div class="fpp-tree">${p.audiences.map(a=>`<section style="--fpp-color:${colors[a.key]||colors.__unknown__}"><header><strong>${esc(a.name)}</strong><b>${pct(a.share_bp)}</b><span>${money(a.revenue_cents)}</span></header>${a.children.map(t=>`<button data-fpp-path="${esc(a.key+'/'+t.key)}"><span>${esc(t.name)}<small>${t.drama_count} 部剧</small></span><span class="fpp-track"><i style="width:${Math.max(0,Math.min(100,(t.share_bp||0)/100))}%"></i></span><b>${pct(t.share_bp)}</b><span>${money(t.revenue_cents)} →</span></button>`).join('')}</section>`).join('')||'<p class="empty">近 7 天暂无可统计收入。</p>'}</div><p class="fpp-muted">方向占比：占 Page 总收入；题材占比：占所属方向收入。退款与调整保留源表口径。</p><div id="fppDramaArea"></div>`;
      target.querySelectorAll('[data-fpp-path]').forEach(b=>b.onclick=()=>showDramas(b.dataset.fppPath,1));
    }
    async function showDramas(path,page){
      const gen=++detailGeneration;dramaAbort?.abort();dramaAbort=new AbortController();
      const area=target.querySelector('#fppDramaArea');area.innerHTML='<p class="fpp-muted">正在加载剧目明细…</p>';area.scrollIntoView({block:'nearest',behavior:'smooth'});
      try{
        const detail=await get('/'+encodeURIComponent(id)+'/dramas?path='+encodeURIComponent(path)+'&version='+encodeURIComponent(version)+'&page='+page,dramaAbort.signal);
        if(!modal.isConnected||gen!==detailGeneration)return;
        const [ak,tk]=path.split('/');const a=p.audiences.find(a=>a.key===ak),t=a.children.find(t=>t.key===tk);
        area.innerHTML=`<h3>${esc(a.name)} / ${esc(t.name)} · ${detail.total} 部剧</h3><div class="table-wrap"><table><thead><tr><th>剧目 / 语言</th><th>投放收入</th><th>社媒收入</th><th>剧目总收入</th><th>分配到本题材</th></tr></thead><tbody>${detail.items.map(d=>`<tr><td><strong>${esc(d.name)}</strong><small class="fpp-sub">${esc(d.content_id)} · ${esc(d.language)}</small><small class="fpp-sub">${esc(d.classification.raw_labels.join(' · ')||'标签缺失')}</small></td><td>${money(d.paid_cents)}</td><td>${money(d.social_cents)}</td><td>${money(d.revenue_cents)}</td><td>${money(d.allocated_cents)}</td></tr>`).join('')}</tbody></table></div><div class="pager"><span>第 ${page} 页 · 每页 50 部剧</span><div class="actions"><button class="button small" id="fppDramaPrev" ${page===1?'disabled':''}>上一页</button><button class="button small" id="fppDramaNext" ${page*50>=detail.total?'disabled':''}>下一页</button></div></div>`;
        area.querySelector('#fppDramaPrev').onclick=()=>showDramas(path,page-1);area.querySelector('#fppDramaNext').onclick=()=>showDramas(path,page+1);
      }catch(e){if(modal.isConnected&&gen===detailGeneration&&e.name!=='AbortError')area.innerHTML=`<p class="fpp-warning">${esc(e.message)}。可重新点击题材重试。</p>`;}
    }
    modal.addEventListener('close',()=>{++detailGeneration;dramaAbort?.abort();},{once:true});
    try{
      data=await get('/'+encodeURIComponent(id),ctl.signal);if(!modal.isConnected)return;
      p=data.items[id];version=data.meta.version;
      if(!p){target.textContent='此 Page 尚未生成画像，请稍后刷新。';return;}
      showTree();
    }catch(e){if(modal.isConnected&&e.name!=='AbortError')target.textContent=e.message;}
  }
  window.FbPagePortraits={cells,load,open,clear:()=>{++request;abort?.abort();cache.clear();meta=null;}};
})();
