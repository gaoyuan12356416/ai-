(function(root) {
  'use strict';
  const API='/api/moboreels', STATUS={queued:'等待提交',submitting:'提交中',succeeded:'报备成功',failed:'提交失败',unknown:'结果待核实',held:'已有报备记录'};
  const escape=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const time=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
  function parseIDs(value){return [...new Set(String(value).split(/[\s,，;；]+/).filter(Boolean))];}
  function https(value){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:'';}catch(_){return '';}}
  function csvCell(value){const s=String(value==null?'':value);return '"'+(/^[=+@\-\t\r]/.test(s)?"'"+s:s).replace(/"/g,'""')+'"';}
  async function request(path,options={}){
    const response=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
    const data=await response.json();
    if(!response.ok){const error=new Error(data.message||'请求失败，请稍后重试');error.status=response.status;throw error;}
    return data;
  }
  function recommendationRow(r,business){return `<tr><td><strong>${escape(r.story_name)}</strong><span class="cell-sub">${escape(r.story_id)}</span></td><td>${escape(r.language_name)}</td><td><span class="badge">${escape(r.recommend_grade||'未分级')}</span></td><td>${escape(r.recommend_reason||'—')}<span class="cell-sub">${escape(r.recommend_remark||'')}</span></td><td>${escape(Array.isArray(r.tags)?r.tags.join('、'):r.tags||'—')}</td><td>${escape(r.recommend_time)}</td><td>${Number(business)===2?`<a class="btn mini" href="/moboreels-library.html?story_id=${encodeURIComponent(r.story_id)}">查看短剧</a>`:'—'}</td></tr>`;}
  function accountRow(r,admin){return `<tr><td>${escape(r.platform_name)}</td><td>${escape(r.account_id)}</td><td>${escape(r.nickname)}<span class="cell-sub">归属：${escape(r.star_name)}</span></td><td>${https(r.platform_link)?`<a href="${escape(https(r.platform_link))}" target="_blank" rel="noopener noreferrer">查看主页</a>`:'—'}</td><td><span class="badge ${r.status==='succeeded'?'success':r.status==='unknown'?'warning':r.status==='failed'?'error':'running'}">${escape(STATUS[r.status]||r.status)}</span><span class="cell-sub">${escape(r.message)}</span></td><td>${escape(r.provider_id||'—')}</td><td>${escape(time(r.updated_at))}${admin&&['unknown','failed'].includes(r.status)?`<br><button class="btn mini" data-resolve="${escape(r.id)}">核实结果</button>`:''}</td></tr>`;}
  const exported={parseIDs,https,csvCell,recommendationRow,accountRow};
  if(typeof module==='object'&&module.exports)module.exports=exported;
  if(!root.document)return;
  const doc=root.document, $=id=>doc.getElementById(id);
  let episodeDialog, episodeData, episodeId, episodeSeq=0;
  async function copy(value,feedback){try{await navigator.clipboard.writeText(value);feedback.textContent='已复制';}catch(_){feedback.textContent='复制失败，请手动复制地址';}}
  async function showEpisodes(id,refresh=false){
    if(!episodeDialog){episodeDialog=doc.createElement('dialog');episodeDialog.className='dialog';episodeDialog.setAttribute('aria-label','剧集地址');doc.body.appendChild(episodeDialog);episodeDialog.addEventListener('close',()=>episodeSeq++);}
    const seq=++episodeSeq;episodeId=id;episodeData=null;
    episodeDialog.innerHTML='<div class="dialog-head"><h2>获取剧集地址</h2><button class="icon-btn" data-episodes-close aria-label="关闭">×</button></div><div class="dialog-body"><p>正在获取剧集地址…</p></div>';
    if(!episodeDialog.open)episodeDialog.showModal();
    try{
      const data=await request(`${API}/dramas/${encodeURIComponent(id)}/episodes${refresh?'?refresh=1':''}`);
      if(seq!==episodeSeq||!episodeDialog.open)return;
      episodeData=data;
      episodeDialog.querySelector('.dialog-body').innerHTML=`<h3>${escape(data.story_name||id)}</h3><div class="notice">地址有效期 1 小时，预计到期时间：${escape(time(data.expires_at))}（北京时间）。过期后请重新获取。</div><div class="row-actions"><button class="btn" data-episodes-refresh>重新获取</button><button class="btn" data-episodes-all>复制全部地址</button></div><p data-episode-feedback role="status"></p><div class="episode-list">${data.list.map((r,i)=>`<div class="episode-item"><strong>第 ${escape(r.epis_no)} 集</strong><a href="${escape(https(r.epis_url))}" target="_blank" rel="noopener noreferrer">打开视频</a><button class="btn mini" data-episode-copy="${i}">复制地址</button></div>`).join('')||'<p>供应方暂无可用剧集地址。</p>'}</div>`;
    }catch(error){if(seq===episodeSeq)episodeDialog.querySelector('.dialog-body').innerHTML=`<div class="notice error">${escape(error.message)}</div><button class="btn" data-episodes-refresh>重新获取</button>`;}
  }
  doc.addEventListener('click',event=>{
    const target=event.target.closest('[data-action="episodes"],[data-episodes-close],[data-episodes-refresh],[data-episodes-all],[data-episode-copy]');if(!target)return;
    event.preventDefault();
    if(target.hasAttribute('data-episodes-close')){episodeDialog.close();return;}
    if(target.dataset.action==='episodes'){showEpisodes(target.dataset.storyId);return;}
    if(target.hasAttribute('data-episodes-refresh')){showEpisodes(episodeId,true);return;}
    if(!episodeData)return;
    const feedback=episodeDialog.querySelector('[data-episode-feedback]');
    if(Date.now()>=Date.parse(episodeData.expires_at)){feedback.textContent='地址已过期，请重新获取。';return;}
    const value=target.hasAttribute('data-episodes-all')?episodeData.list.map(r=>`第${r.epis_no}集\t${r.epis_url}`).join('\n'):episodeData.list[Number(target.dataset.episodeCopy)].epis_url;
    copy(value,feedback);
  });
  async function boot(){
    const mode=doc.body.dataset.moboreelsPage;if(!mode)return;
    const recommendations=mode==='recommendations', activeKey=recommendations?'moboreelsRecommendations':'moboreelsMediaAccounts';
    let auth={},allowed=false,page=1,total=0,rows=[],preview=[],timer=null,sequence=0,controller=null,busy=false,exportDate='';
    const notice=$('notice'),table=$('rows'),filter=$('filters');
    function showError(e){notice.textContent=e.message||String(e);notice.classList.remove('hidden');if(e.status===401||e.status===403){allowed=false;table.innerHTML='';rows=[];clearTimeout(timer);$('controls').disabled=true;doc.querySelectorAll('dialog[open]').forEach(d=>d.close());}}
    function query(){const q=new URLSearchParams(new FormData(filter));q.set('page_index',page);return q;}
    function nav(){if(root.UiTopbar)root.UiTopbar.render({auth,userCard:$('userCard'),authButton:$('loginBtn')});if(root.QuickNav)root.QuickNav.render({container:$('quickNav'),activeKey,auth,onNavigate:item=>{if(item.href)location.assign(item.href);}}).catch(()=>{});}
    function choices(select,values){const previous=select.value;select.innerHTML='<option value="">全部</option>'+values.map(([value,label])=>`<option value="${escape(value)}">${escape(label)}</option>`).join('');select.value=previous;}
    async function load(refresh=false){
      if(!allowed)return;clearTimeout(timer);if(controller)controller.abort();controller=new AbortController();const seq=++sequence;
      const params=query();if(refresh&&recommendations)params.set('refresh','1');$('refreshBtn').disabled=true;table.setAttribute('aria-busy','true');if(recommendations)$('exportBtn').disabled=true;
      try{
        const data=await request(`${API}/${recommendations?'recommendations':'media-accounts'}?${params}`,{signal:controller.signal});
        if(seq!==sequence||!allowed)return;rows=data.list;total=data.total;notice.classList.add('hidden');
        table.innerHTML=rows.map(r=>recommendations?recommendationRow(r,data.project_type):accountRow(r,auth.user.role==='admin')).join('')||'<tr><td colspan="7" class="empty">暂无符合条件的记录</td></tr>';
        $('resultCount').textContent=`共 ${total.toLocaleString('zh-CN')} 条`; $('pageNumber').textContent=`${page} / ${Math.max(1,Math.ceil(total/50))}`;
        $('prevPage').disabled=page<=1;$('nextPage').disabled=page*50>=total;
        if(recommendations){
          exportDate=data.query_date||$('queryDate').value;$('exportBtn').disabled=false;
          $('snapshotTime').textContent=`数据获取：${time(data.fetched_at)}（北京时间）；统计基于所选日期完整推荐列表，共 ${data.source_total} 条。`;
          $('summary').innerHTML=Object.entries(data.summary.grades).map(([k,v])=>`<span class="stat-chip">${escape(k)} <strong>${v}</strong></span>`).join('')+'<span class="muted">'+Object.entries(data.summary.languages).map(([k,v])=>`${escape(k)} ${v}`).join(' · ')+'</span>';
          choices($('languageFilter'),data.languages);choices($('gradeFilter'),Object.keys(data.summary.grades).filter(v=>v!=='未分级').map(v=>[v,v]));
        }else if(rows.some(r=>['queued','submitting'].includes(r.status))&&!doc.hidden){timer=setTimeout(()=>load(),2500);}
      }catch(e){if(e.name!=='AbortError'&&seq===sequence)showError(e);}finally{if(seq===sequence){$('refreshBtn').disabled=!allowed;table.removeAttribute('aria-busy');}}
    }
    $('loginBtn').addEventListener('click',()=>root.UiTopbar.handleAuthAction({auth,api:request}));
    filter.addEventListener('submit',e=>{e.preventDefault();page=1;load();});$('refreshBtn').onclick=()=>load(true);
    $('prevPage').onclick=()=>{if(page>1){page--;load();}};$('nextPage').onclick=()=>{if(page*50<total){page++;load();}};
    doc.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!doc.hidden&&allowed&&!recommendations)load();});root.addEventListener('pagehide',()=>{clearTimeout(timer);if(controller)controller.abort();});
    doc.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
    if(recommendations){
      $('queryDate').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());$('queryDate').max=$('queryDate').value;
      $('exportBtn').onclick=()=>{const data=[['剧目ID','名称','语言','推荐等级','推荐理由','推荐备注','标签','推荐日期'],...rows.map(r=>[r.story_id,r.story_name,r.language_name,r.recommend_grade,r.recommend_reason,r.recommend_remark,Array.isArray(r.tags)?r.tags.join(','):r.tags,r.recommend_time])];const blob=new Blob(['\ufeff'+data.map(r=>r.map(csvCell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=doc.createElement('a');a.href=url;a.download=`Moboreels推荐-${exportDate}-第${page}页.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
    }else{
      $('createBtn').onclick=()=>{preview=[];$('previewRows').innerHTML='';$('submitBtn').disabled=true;$('createError').textContent='';$('accountDialog').showModal();};
      $('platformType').onchange=()=>{preview=[];$('previewRows').innerHTML='';$('submitBtn').disabled=true;};
      $('accountIDs').oninput=()=>{$('submitBtn').disabled=true;};
      $('matchBtn').onclick=async()=>{
        if(busy)return;busy=true;$('matchBtn').disabled=true;$('submitBtn').disabled=true;$('createError').textContent='正在匹配账号…';
        try{const ids=parseIDs($('accountIDs').value);if(!ids.length||ids.length>100)throw new Error('每批请输入 1 至 100 个平台账号 ID');
          const kind=$('platformType').value,original=$('accountIDs').value;
          const data=await request(`${API}/media-accounts/preview`,{method:'POST',body:JSON.stringify({platform_type:Number(kind),account_ids:ids})});
          if(!allowed||kind!==$('platformType').value||original!==$('accountIDs').value)return;
          preview=data.list;$('previewRows').innerHTML=preview.map((r,i)=>`<tr data-preview-index="${i}"><td><input type="checkbox" aria-label="选择账号 ${escape(r.account_id)}" ${r.match_status==='conflict'?'disabled':'checked'}></td><td>${escape(r.account_id)}<span class="cell-sub">${r.match_status==='matched'?'已匹配':r.match_status==='conflict'?'匹配冲突，请核对账号库':'未匹配，请补全'}</span></td><td><input data-field="nickname" aria-label="账号名称" maxlength="255" value="${escape(r.nickname)}" ${r.match_status==='matched'?'readonly':''}></td><td><input data-field="platform_link" aria-label="主页链接" placeholder="https://…" value="${escape(r.platform_link)}" ${r.match_status==='matched'?'readonly':''}></td></tr>`).join('');
          $('createError').textContent=`已匹配 ${preview.filter(r=>r.match_status==='matched').length} / ${preview.length} 个账号。勾选需要报备的账号；未匹配项可补全名称和主页，或取消勾选。`;$('submitBtn').disabled=false;
        }catch(e){$('createError').textContent=e.message;if(e.status===401||e.status===403)showError(e);}finally{busy=false;$('matchBtn').disabled=false;}
      };
      $('submitBtn').onclick=async()=>{
        if(busy||!allowed)return;busy=true;$('submitBtn').disabled=true;
        try{const accounts=Array.from($('previewRows').querySelectorAll('tr')).filter(r=>r.querySelector('input[type=checkbox]').checked).map(r=>({account_id:preview[Number(r.dataset.previewIndex)].account_id,nickname:r.querySelector('[data-field=nickname]').value.trim(),platform_link:r.querySelector('[data-field=platform_link]').value.trim()}));
          if(!accounts.length||accounts.some(r=>!r.nickname||!https(r.platform_link)))throw new Error('请勾选账号，并补全名称和有效的 HTTPS 主页链接');
          const data=await request(`${API}/media-accounts`,{method:'POST',body:JSON.stringify({platform_type:Number($('platformType').value),accounts})});
          $('accountDialog').close();filter.reset();page=1;await load();const held=data.list.filter(r=>r.status==='held');if(held.length){notice.textContent=`${held.length} 个账号已有其他达人报备记录，未重复提交：${held.map(r=>r.account_id).join('、')}`;notice.classList.remove('hidden');}
        }catch(e){$('createError').textContent=e.message+'。网络异常时可关闭窗口，在列表核对已提交记录。';if(e.status===401||e.status===403)showError(e);}finally{busy=false;$('submitBtn').disabled=!allowed;}
      };
      let resolveID='';table.addEventListener('click',event=>{const b=event.target.closest('[data-resolve]');if(!b)return;resolveID=b.dataset.resolve;$('resolveForm').reset();$('resolveError').textContent='';$('resolveDialog').showModal();});
      $('resolveForm').onsubmit=async event=>{event.preventDefault();const button=$('resolveSubmit');button.disabled=true;try{await request(`${API}/media-accounts/${encodeURIComponent(resolveID)}/resolve`,{method:'POST',body:JSON.stringify({outcome:$('resolveOutcome').value,provider_id:$('providerID').value.trim(),evidence:$('resolveEvidence').value.trim()})});$('resolveDialog').close();load();}catch(e){$('resolveError').textContent=e.message;if(e.status===401||e.status===403)showError(e);}finally{button.disabled=false;}};
    }
    try{auth=await request('/api/ui/topbar');nav();const user=auth.user||{};allowed=!!(auth.authenticated&&(user.role==='admin'||user.is_admin||(user.permissions&&user.permissions.moboreels_library)));if(!allowed)throw new Error('请登录并联系管理员开通 Moboreels 短剧库权限');$('controls').disabled=false;if($('createBtn'))$('createBtn').disabled=false;await load();}catch(e){showError(e);}
  }
  if(doc.readyState==='loading')doc.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:globalThis);
