(() => {
  'use strict';
  const names = {ceo_wealth:'豪门总裁',sweet_romance:'甜宠恋爱',marital:'婚恋纠葛',family:'萌宝家庭',revenge:'逆袭复仇',period_intrigue:'古装权谋',fantasy:'奇幻穿越',suspense:'悬疑冒险',growth:'生活成长'};
  function node(tag,text) { const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n; }
  function value(v) { return v===null || v===undefined ? '待积累' : Number(v).toLocaleString('zh-CN'); }
  function showProfile(account) {
    const p=account.selection_profile?.growth_profile || {}, f=p.followers || {};
    const dialog=node('dialog');dialog.style.cssText='max-width:760px;width:90%;border:1px solid #dbe3ef;border-radius:14px;padding:24px';
    dialog.append(node('h2',`${account.username ? '@'+account.username : '频道 '+(account.id || account.account_id)} · 成长画像`));
    dialog.append(node('p',`有效 72 小时样本：${value(p.mature_posts)} 条；近 7 天净增粉：${value(f.net_7d)}；前 7 天净增粉：${value(f.previous_net_7d)}`));
    dialog.append(node('p','媒体播放与观看时长作为辅助观察；涨粉是账号整体净变化，不归因到某条帖子。样本不足时均衡探索，不自动切换画像类型。'));
    const table=node('table');table.style.cssText='width:100%;border-collapse:collapse;text-align:left';
    const head=node('tr');['题材','帖子 / 剧数','表现分','状态'].forEach(t=>head.append(node('th',t)));table.append(head);
    for(const t of p.themes || []) { const row=node('tr');[names[t.theme]||t.theme,`${t.posts} / ${t.dramas}`,String(t.score),t.mature?'可用于优先选材':'继续积累'].forEach(s=>{const c=node('td',s);c.style.padding='10px 4px';row.append(c);});table.append(row); }
    dialog.append(table);
    if(!p.themes?.length)dialog.append(node('p','尚无足够的成熟内容样本。采集从功能启用后开始，不能补造历史 72 小时指标。'));
    const policy=p.policy || {}, parts=Object.entries(policy.weights || {}).map(([k,v])=>`${names[k]||k} ${(v*100).toFixed(0)}%`);
    dialog.append(node('p',`当前分配：${parts.length?parts.join('，')+'，':''}探索 ${((policy.exploration ?? 1)*100).toFixed(0)}%`));
    const last=account.selection_profile?.collector || {};
    dialog.append(node('p',`画像更新时间：${p.computed_at ? new Date(p.computed_at).toLocaleString('zh-CN') : '等待首次采集'}；采集最近完成：${last.finished_at ? new Date(last.finished_at).toLocaleString('zh-CN') : '等待首次采集'}`));
    if(last.failures?.some(r=>String(r.account_id)===String(account.id || account.account_id)))dialog.append(node('p','本频道最近采集未完成，系统保留已有数据并稍后重试。'));
    const close=node('button','关闭');close.type='button';close.className='button';close.addEventListener('click',()=>dialog.close());dialog.append(close);
    dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();
  }
  function controls(account) {
    const box=node('div');box.className='growth-profile-controls';box.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px';
    box.addEventListener('click',e=>e.stopPropagation());
    const data=account.selection_profile;
    if(!data) { box.append(node('span','画像设置暂不可用'));return box; }
    const label=node('span','选材画像');const select=node('select');select.setAttribute('aria-label','选材画像');select.style.cssText='width:auto;min-width:110px';
    for(const [key,title] of [['revenue','收入画像'],['growth','成长画像']]){const o=node('option',title);o.value=key;select.append(o);}
    select.value=data.mode || 'revenue';select.disabled=!data.can_edit;
    const save=node('button','保存画像');save.type='button';save.className='button small';save.disabled=true;
    const inspect=node('button','成长数据');inspect.type='button';inspect.className='button small';
    const status=node('span');status.setAttribute('role','status');status.style.fontSize='12px';
    select.addEventListener('change',()=>{save.disabled=select.value===account.selection_profile.mode;status.textContent='';});
    save.addEventListener('click',async e=>{
      e.preventDefault();save.disabled=select.disabled=true;status.textContent='保存中…';
      try {
        const response=await fetch(`/api/admin/x-tag-posts/profiles/${encodeURIComponent(account.id || account.account_id)}`,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:select.value,expected_version:account.selection_profile.mode_version})});
        const payload=await response.json();if(!response.ok)throw Error(payload.message || '保存失败');
        account.selection_profile=payload.selection_profile;status.textContent='已保存，新任务生效';
      } catch(error) {status.textContent=error.message;save.disabled=false;}
      finally {select.disabled=!account.selection_profile.can_edit;}
    });
    inspect.addEventListener('click',e=>{e.preventDefault();showProfile(account);});
    box.append(label,select,save,inspect,status);return box;
  }
  window.XGrowthProfile={controls,showProfile};
})();
