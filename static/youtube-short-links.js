/* Independent mounted dialog: publishing-list polling cannot replace its controls. */
(() => {
  'use strict';
  const trigger=document.querySelector('#short-link-button');
  if(!trigger)return;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dialog=document.createElement('dialog');
  dialog.className='modal manual-link-dialog';dialog.setAttribute('aria-labelledby','manual-link-title');
  dialog.innerHTML=`<header class="modal-header"><div><h2 class="modal-title" id="manual-link-title">生成短链</h2><p class="modal-subtitle">选择频道与短剧，生成链接后复制使用。</p></div><button type="button" class="icon-btn close-modal" data-link-action="close" aria-label="关闭弹窗">×</button></header>
    <div class="modal-body"><div id="manual-link-error" class="note-box note-error" role="alert" hidden></div>
    <div class="field"><div class="manual-link-label"><label for="manual-link-channel">频道 <span class="required">*</span></label><button type="button" class="text-button" data-link-action="channels">刷新频道</button></div><select id="manual-link-channel" class="select"><option value="">正在加载频道…</option></select><span id="manual-link-channel-hint" class="field-hint" role="status"></span></div>
    <div class="field manual-link-search-field"><label for="manual-link-search">短剧 <span class="required">*</span></label><input id="manual-link-search" class="input" placeholder="输入剧名开头或完整剧 ID（至少 2 个字）" autocomplete="off" disabled/><span class="field-hint">从完整 DramaWave 剧库搜索，请核对剧名与语言。</span></div>
    <div id="manual-link-results" class="manual-link-results" role="group" aria-label="短剧搜索结果"></div>
    <div class="manual-link-pages"><button type="button" class="text-button" data-link-action="previous" disabled>上一页</button><span id="manual-link-page"></span><button type="button" class="text-button" data-link-action="next" disabled>下一页</button><button type="button" class="text-button" data-link-action="search" hidden>重新查询</button></div>
    <div id="manual-link-selection" class="note-box note-blue" hidden></div>
    <div id="manual-link-result" class="manual-link-result" hidden><label for="manual-link-url">短链已生成</label><div class="manual-link-copy"><input id="manual-link-url" class="input" readonly/><button type="button" class="btn btn-primary" data-link-action="copy">复制</button></div><span id="manual-link-copy-hint" class="field-hint" role="status">可复制到视频描述、评论等位置使用。</span></div>
    </div><footer class="modal-footer"><span class="footer-hint" id="manual-link-status" role="status">每次生成一条新的短链</span><div class="inline-actions"><button type="button" class="btn btn-secondary" data-link-action="close">关闭</button><button type="button" class="btn btn-secondary" data-link-action="check" hidden>核对结果</button><button type="button" class="btn btn-primary" data-link-action="generate" disabled>生成短链</button></div></footer>`;
  const $=selector=>dialog.querySelector(selector);
  const state={channels:[],items:[],channel:'',selected:null,result:null,pending:null,busy:false,uncertain:false,channelLoading:false,searchLoading:false,page:1,hasMore:false,searchError:false};
  let channelSerial=0,searchSerial=0,searchTimer=null,channelTimer=null,channelUntil=0,searchAbort=null;
  const locked=()=>state.busy||state.uncertain;
  const eligible=()=>state.channels.some(c=>String(c.id)===state.channel&&c.eligible===true);
  function error(message=''){$('#manual-link-error').hidden=!message;$('#manual-link-error').textContent=message;}
  async function request(path,body,signal){
    const controller=new AbortController(),abort=()=>controller.abort();
    signal?.addEventListener('abort',abort,{once:true});
    const timer=setTimeout(abort,body?45000:30000);
    try{
      const response=await fetch('/api/youtube-auto-publish/short-links'+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json',...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,signal:controller.signal});
      const data=await response.json();
      if(!response.ok){const e=new Error(data.message||'请求失败，请重试');e.status=response.status;e.code=data.error;throw e;}
      return data;
    }catch(e){if(e.status)throw e;const failure=new Error(body?'生成结果待确认，请核对本次结果。':'请求暂未完成，请稍后重试。');failure.cancelled=!!signal?.aborted;throw failure;}
    finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
  }
  function clearResult(){state.result=null;state.pending=null;state.uncertain=false;$('#manual-link-copy-hint').textContent='可复制到视频描述、评论等位置使用。';error();}
  function controls(){
    const lock=locked();$('#manual-link-channel').disabled=lock||state.channelLoading;$('#manual-link-search').disabled=lock||!state.channel;
    $('[data-link-action="channels"]').disabled=lock||state.channelLoading;
    for(const radio of dialog.querySelectorAll('[name="manual-link-drama"]'))radio.disabled=lock||radio.dataset.selectable!=='true';
    $('[data-link-action="previous"]').disabled=lock||state.searchLoading||state.page<=1;
    $('[data-link-action="next"]').disabled=lock||state.searchLoading||!state.hasMore;
    $('[data-link-action="search"]').hidden=!state.searchError;$('[data-link-action="search"]').disabled=lock||state.searchLoading;
    $('[data-link-action="generate"]').disabled=state.busy||!state.selected||(!state.uncertain&&(!eligible()||state.channelLoading));
    $('[data-link-action="generate"]').textContent=state.busy?'处理中…':state.uncertain?'按原选择重试':state.result?'再生成一条':'生成短链';
    $('[data-link-action="check"]').hidden=!state.uncertain;$('[data-link-action="check"]').disabled=state.busy;
    $('#manual-link-status').textContent=state.busy?'正在处理本次生成…':state.uncertain?'本次结果待确认，重试不会重复建链':'每次生成一条新的短链';
    $('#manual-link-result').hidden=!state.result;$('#manual-link-url').value=state.result?.short_url||'';
    $('#manual-link-selection').hidden=!state.selected;$('#manual-link-selection').textContent=state.selected?'已选：'+state.selected.name+' · '+state.selected.language+' · '+state.selected.content_id:'';
    $('#manual-link-page').textContent=state.items.length?'第 '+state.page+' 页':'';
  }
  function results(message){
    $('#manual-link-results').innerHTML=message?`<p class="manual-link-empty" role="status">${esc(message)}</p>`:state.items.map((d,index)=>`<label class="manual-link-drama"><input type="radio" name="manual-link-drama" value="${index}" data-selectable="${d.selectable===true}" ${state.selected?.content_id===d.content_id&&state.selected?.language===d.language?'checked':''}/><span><strong>${esc(d.name)}</strong><small>${esc(d.language)} · ${esc(d.content_id)}${d.selectable?'':' · 剧名存在冲突，暂不可选'}</small></span></label>`).join('');controls();
  }
  async function loadChannels(refresh=false){
    const serial=++channelSerial;clearTimeout(channelTimer);state.channelLoading=true;controls();$('#manual-link-channel-hint').textContent='正在核验频道发布资格…';
    try{
      const data=await request('/channels'+(refresh?'?refresh=1':''));if(serial!==channelSerial)return;
      state.channels=data.channels||[];
      $('#manual-link-channel').innerHTML='<option value="">请选择频道</option>'+state.channels.map(c=>`<option value="${esc(c.id)}" ${c.eligible?'':'disabled'}>${esc(c.name)}${c.eligible?'':'（'+esc(c.reason||'暂不可用')+'）'}</option>`).join('');
      $('#manual-link-channel').value=state.channel;const selected=state.channels.find(c=>String(c.id)===state.channel);
      $('#manual-link-channel-hint').textContent=data.error||(data.checking?'正在核验频道，请稍候…':selected&&!selected.eligible?selected.reason:'仅可选择能够正常发布的频道；生成前会再次核验。');
      if(data.checking&&dialog.open&&Date.now()<channelUntil)channelTimer=setTimeout(()=>loadChannels(),1800);
    }catch(e){if(serial===channelSerial)$('#manual-link-channel-hint').textContent=e.message+' 可点击刷新频道重试。';}
    finally{if(serial===channelSerial){state.channelLoading=false;controls();}}
  }
  async function search(page=1){
    const serial=++searchSerial;searchAbort?.abort();searchAbort=new AbortController();clearTimeout(searchTimer);
    const query=$('#manual-link-search').value.trim();state.page=page;state.hasMore=false;state.searchError=false;state.items=[];
    if(query.length<2){state.searchLoading=false;results('输入剧名开头或完整剧 ID，搜索后选择对应语言。');return;}
    state.searchLoading=true;results('正在查询剧库…');
    try{const data=await request('/dramas?'+new URLSearchParams({search:query,page:String(page)}),undefined,searchAbort.signal);if(serial!==searchSerial)return;state.items=data.items||[];state.hasMore=data.has_more===true;results(state.items.length?'':'没有找到短剧，请尝试剧名开头或完整剧 ID。');}
    catch(e){if(serial===searchSerial&&!e.cancelled){state.searchError=true;results(e.message);}}
    finally{if(serial===searchSerial){state.searchLoading=false;controls();}}
  }
  function accept(link){
    if(link?.status==='published'&&/^https:\/\/gy\.g2flow\.com\/s2l\/youtube\/[1-9][0-9]*\.html$/.test(link.short_url)){state.result=link;state.pending=null;state.uncertain=false;$('#manual-link-copy-hint').textContent='可复制到视频描述、评论等位置使用。';error();return true;}return false;
  }
  async function check(){
    if(state.busy||!state.pending)return;state.busy=true;controls();
    try{const data=await request('/'+state.pending.operation_id);if(!accept(data.link))error(data.link?.message||'本次生成尚未完成，可按原选择重试。');}
    catch(e){error(e.status===404?'尚未找到本次记录，可按原选择重试。':e.message);}
    finally{state.busy=false;controls();}
  }
  async function generate(){
    if(state.busy||!state.selected||(!state.uncertain&&!eligible()))return;
    if(!state.pending)state.pending={operation_id:crypto.randomUUID(),channel_local_id:state.channel,content_id:state.selected.content_id,language:state.selected.language};
    state.result=null;state.busy=true;error();controls();
    try{const data=await request('',state.pending);if(!accept(data.link)){state.uncertain=true;error('生成结果待确认，请核对本次结果。');}}
    catch(e){state.uncertain=!e.status||e.status>=500||e.code==='operation_conflict';if(!state.uncertain)state.pending=null;error(e.message);}
    finally{state.busy=false;controls();}
  }
  async function copy(){const value=state.result?.short_url;if(!value)return;try{await navigator.clipboard.writeText(value);$('#manual-link-copy-hint').textContent='已复制';}catch(_){$('#manual-link-url').focus();$('#manual-link-url').select();$('#manual-link-copy-hint').textContent='自动复制不可用，链接已选中，请手动复制。';}}
  trigger.addEventListener('click',()=>{if(document.querySelector('#new-publish')?.disabled||dialog.open)return;document.body.append(dialog);dialog.showModal();controls();channelUntil=Date.now()+120000;void loadChannels();if(state.uncertain&&!state.busy)void check();if(!state.items.length)results('输入剧名开头或完整剧 ID，搜索后选择对应语言。');});
  dialog.addEventListener('close',()=>{clearTimeout(channelTimer);clearTimeout(searchTimer);searchAbort?.abort();++searchSerial;state.searchLoading=false;dialog.remove();trigger.focus();});
  dialog.addEventListener('click',event=>{const action=event.target.closest('[data-link-action]')?.dataset.linkAction;if(action==='close')dialog.close();else if(action==='channels'&&!locked()){channelUntil=Date.now()+120000;void loadChannels(true);}else if(action==='search')void search(state.page);else if(action==='previous')void search(state.page-1);else if(action==='next')void search(state.page+1);else if(action==='generate')void generate();else if(action==='check')void check();else if(action==='copy')void copy();});
  $('#manual-link-channel').addEventListener('change',event=>{if(locked())return;state.channel=event.target.value;clearResult();controls();});
  $('#manual-link-search').addEventListener('input',()=>{if(locked())return;clearTimeout(searchTimer);searchAbort?.abort();++searchSerial;state.selected=null;clearResult();state.hasMore=false;state.items=[];state.searchLoading=true;results('正在查询剧库…');searchTimer=setTimeout(()=>search(),300);});
  $('#manual-link-results').addEventListener('change',event=>{if(locked()||event.target.name!=='manual-link-drama')return;const d=state.items[Number(event.target.value)];if(!d?.selectable)return;state.selected=d;clearResult();controls();});
})();
