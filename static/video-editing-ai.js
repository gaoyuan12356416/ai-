(() => {
  'use strict';
  const API = '/api/ai-video-editing';
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
  const uuid = () => crypto.randomUUID();
  const controls = ['grouping', 'method', 'duration', 'scriptCount', 'variantCount'];
  const cfg = () => Object.fromEntries(controls.map(id => [id, $(id).value]));
  const labels = {queued:'排队中', running:'分析中', succeeded:'分析完成', failed:'失败', queued_gpu:'等待 GPU', processing:'处理中', submitted:'已提交', completed:'已完成', cancelled:'已取消', unknown:'结果待核实', recovery_required:'待恢复', unprobed:'待校验', probing:'正在校验', ready:'已就绪'};
  const terminal = status => ['completed','failed','cancelled','unknown','recovery_required'].includes(status);
  const nameOf = src => src.name || src.title || src.source_id;
  const sourceId = src => src.source_id || src.id;
  const durationOf = src => Number(src?.metadata?.duration_ms || src?.duration_ms || 0);
  const secs = ms => String(Math.round(ms) / 1000);
  const time = ms => {
    const total = Math.max(0, Math.round(ms || 0));
    return Math.floor(total / 60000) + ':' + String(Math.floor(total / 1000) % 60).padStart(2,'0') + '.' + String(total % 1000).padStart(3,'0');
  };
  const model = {selected:[], settings:null, editingProject:null, analysisId:'', plan:null, active:0, edited:false, pendingDrafts:[], projects:[], jobs:[], projectName:'AI 智能剪辑'};
  let auth, storageKey, rows = [], total = 0, seq = 0, uploading = false, latestUploads = [], uploadFilesPending = [];
  let analysis = null, timer, jobTimer, mediaSeq = 0, stopAt = null, busy = false, pollFailures = 0;
  const liveJobs = new Map();
  function persist() {
    if (!storageKey) return;
    try { sessionStorage.setItem(storageKey, JSON.stringify(model)); }
    catch { notice('浏览器无法保存本页进度。已保存的草稿与任务仍可从本页历史中打开。', true); }
  }
  function notice(text, error = false) {
    $('notice').hidden = !text; $('notice').textContent = text;
    $('notice').classList.toggle('error', error);
  }
  async function api(path, options = {}) {
    const init = {credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}, ...options};
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
    }
    let res;
    try { res = await fetch(path.startsWith('/api/') ? path : API + path, init); }
    catch { throw new Error('网络连接中断，请核对状态后重试'); }
    const data = await res.json().catch(() => null);
    if (!res.ok || !data) {
      const error = new Error(data?.error || ('请求未取得有效回执（' + res.status + '）'));
      error.status = res.status; throw error;
    }
    return data;
  }
  function lock(on) {
    busy = on;
    $('sources').inert = on; $('review').inert = on; $('works').inert = on;
    $('workspace').setAttribute('aria-busy', String(on));
  }
  async function action(fn) {
    if (busy) return;
    lock(true); notice('');
    try { await fn(); } catch (e) { notice(e.message || '操作未完成，请重试', true); }
    finally { lock(false); buttons(); }
  }
  function btn(label, key, val, disabled = false) {
    return '<button type="button" class="ai-mini" data-' + key + '="' + escape(val) + '"' + (disabled ? ' disabled' : '') + '>' + escape(label) + '</button>';
  }
  const empty = msg => '<div class="ai-empty">' + escape(msg) + '</div>';
  const compactSource = s => ({id:sourceId(s),name:nameOf(s),series_code:s.series_code||'',duration_ms:durationOf(s),episode_number:s.episode_number??null});
  const seriesEpisode = s => [s.series_code,s.episode_number?'第 '+s.episode_number+' 集':''].filter(Boolean).join(' · ');
  const selectedSource = id => model.selected.find(s => s.id === id);
  const allUnits = () => model.plan?.drafts?.flatMap(d => d.units) || [];
  function buttons() {
    const activeAnalysis = ['queued','running'].includes(analysis?.status);
    $('analyze').disabled = !model.selected.length || activeAnalysis;
    $('analyze').textContent = activeAnalysis ? '正在分析…' : analysis?.status === 'failed' ? '重新分析' : model.analysisId ? '重新分析所选素材' : '分析所选素材';
    $('makePlan').disabled = analysis?.status !== 'succeeded';
    $('refreshAnalysis').hidden = !model.analysisId;
    const hook = $('method').value === 'hook_first';
    $('durationField').hidden = hook;
    const groups = $('grouping').value === 'merge' ? Math.min(1,model.selected.length) : model.selected.length;
    $('estimated').textContent = '预计 ' + (groups * Number($('scriptCount').value) * Number($('variantCount').value)) + ' 条成片';
    $('saveDraft').disabled = !allUnits().length || (!model.pendingDrafts.length && !!model.plan?.saved);
    $('saveDraft').textContent = model.pendingDrafts.length ? '核实并继续保存' : model.plan?.saved ? '草稿已保存' : model.editingProject ? '保存草稿修改' : '创建剪辑草稿';
    $('saveCopy').hidden = !model.editingProject;
  }
  function mayChange() {
    if (model.pendingDrafts.some(p => p.pending)) { notice('上一笔草稿提交的回执仍待核实，请先继续创建草稿。',true); return false; }
    return !model.edited || window.confirm('更改素材或配置会替换还未保存的选段，继续吗？');
  }
  function clearPlan() {
    model.plan=null; model.editingProject=null; model.active=0; model.edited=false; model.pendingDrafts=[];
    renderPlan();
  }
  function selectionChanged() {
    clearTimeout(timer); model.analysisId=''; analysis=null; clearPlan(); persist(); renderSelection(); renderAnalysis(); renderList();
  }
  function renderSelection() {
    $('selectedCount').textContent='已选 '+model.selected.length+' 段';
    $('selection').innerHTML=model.selected.map((s,i)=>'<div class="ai-row"><span class="ai-rank">'+(i+1)+'</span><div class="ai-maintext"><strong>'+escape(s.name)+'</strong><small>'+escape([seriesEpisode(s),time(s.duration_ms)].filter(Boolean).join(' · '))+'</small></div><div class="ai-tools">'+btn('上移','move',i+':-1',i===0)+btn('下移','move',i+':1',i===model.selected.length-1)+btn('移除','remove',s.id)+'</div></div>').join('')||empty('从素材库选择 1–20 段视频，可在这里调整分析顺序。');
    buttons();
  }
  function renderList() {
    $('moreSources').hidden = rows.length >= total;
    $('sourceList').innerHTML=rows.map((s,i)=>{
      const ready=s.status==='ready';
      const details=[seriesEpisode(s),durationOf(s)?time(durationOf(s)):'',labels[s.status]||s.status].filter(Boolean).join(' · ');
      return '<div class="ai-row"><input type="checkbox" aria-label="选择 '+escape(nameOf(s))+'" data-pick="'+i+'" '+(selectedSource(sourceId(s))?'checked ':'')+(ready?'':'disabled')+'><div class="ai-maintext"><strong>'+escape(nameOf(s))+'</strong><small>'+escape(details)+'</small></div>'+btn('预览','sourcepreview',i,!ready)+'</div>';
    }).join('')||empty('暂无匹配素材。可以上传视频后直接选用。');
  }
  async function search(append=false) {
    const turn=++seq, q=$('sourceQuery').value.trim();
    if(!append){rows=[];total=0;$('sourceList').replaceChildren();}
    $('searchState').textContent='正在查询…';$('moreSources').hidden=true;
    try {
      const res=await api('/assets?'+new URLSearchParams({q,series:$('seriesFilter').value,limit:40,offset:append?rows.length:0}));
      if(turn!==seq)return;
      rows=append?rows.concat(res.items||[]):res.items||[];
      total=res.total??rows.length;
      $('searchState').textContent=total?'共 '+total+' 项':'';
      renderList();
    } catch(e) {
      if(turn!==seq)return;
      $('searchState').textContent=e.message+'。可再次查询。'; if(append)renderList();
    }
  }
  async function readSeries() {
    const response=await api('/series');
    const series=response.items||[];
    const value=$('seriesFilter').value;
    $('seriesFilter').innerHTML='<option value="">全部剧名</option>'+series.filter(s=>s.series_code).map(s=>'<option value="'+escape(s.series_code)+'">'+escape(s.series_code)+'（'+Number(s.asset_count||0)+'）</option>').join('');
    if(series.some(s=>s.series_code===value))$('seriesFilter').value=value;
    $('seriesNames').innerHTML=series.map(s=>'<option value="'+escape(s.series_code)+'"></option>').join('');
  }
  function drawQueue() {
    const start=Number($('uploadEpisode').value)||0;
    $('uploadQueue').innerHTML=uploadFilesPending.map((f,i)=>'<li>'+escape(f.name)+' · '+(f.size/1048576).toFixed(1)+' MiB'+(start?' · 第 '+(start+i)+' 集':'')+'</li>').join('');
  }
  function uploadFile(file,series,episode,position,count) {
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();
      const query=new URLSearchParams({name:file.name,series});
      if(episode)query.set('episode',String(episode));
      xhr.open('POST',API+'/assets?'+query);
      xhr.setRequestHeader('Content-Type',/\.mov$/i.test(file.name)?'video/quicktime':'video/mp4');
      xhr.setRequestHeader('Accept','application/json');
      xhr.upload.onprogress=e=>{
        if(e.lengthComputable)$('uploadProgress').value=Math.round(e.loaded*100/e.total);
        $('uploadState').textContent='正在上传 '+position+'/'+count+' · '+file.name+' · '+$('uploadProgress').value+'%';
      };
      xhr.upload.onload=()=>{$('uploadState').textContent='上传完成，正在校验 '+file.name+'…';};
      xhr.onerror=()=>reject(new Error('上传回执未到达，请刷新素材库核对后再试。'));
      xhr.onload=()=>{
        let data;try{data=JSON.parse(xhr.responseText);}catch{}
        if(xhr.status>=200&&xhr.status<300&&data?.item?.id)return resolve(data.item);
        reject(new Error(data?.error||'未取得上传结果，请刷新素材库核对。'));
      };
      xhr.send(file);
    });
  }
  async function uploadFiles() {
    if(uploading)return;
    const files=uploadFilesPending.slice();
    if(!files.length)return;
    const series=$('uploadSeries').value.trim(),start=Number($('uploadEpisode').value)||0;
    const invalid=files.find(f=>!/[.](mp4|mov)$/i.test(f.name)||!f.size||f.size>1024**3);
    if(invalid){$('uploadState').textContent=invalid.name+' 的格式或大小不符合要求，请重新选择。';return;}
    if($('uploadEpisode').value&&(!Number.isInteger(start)||start<1||start+files.length-1>100000)){$('uploadState').textContent='起始集号和末集集号需在 1–100000 之间。';return;}
    latestUploads=[];$('pickUploaded').hidden=true;
    uploading=true;$('uploadSeries').disabled=true;$('uploadEpisode').disabled=true;$('submitUpload').disabled=true;$('uploadFile').disabled=true;$('uploadProgress').hidden=false;
    let done=0;
    try{
      for(const file of files){
        $('uploadProgress').value=0;
        const item=await uploadFile(file,series,start?start+done:0,done+1,files.length);
        latestUploads.push(item);
        done++;
      }
      $('uploadFile').value='';$('uploadState').textContent=done+' 个视频已上传，可在素材库勾选。';
    }catch(e){$('uploadState').textContent='已成功 '+done+' 个。'+e.message+' 整文件上传中断后需重新上传剩余文件。';}
    finally{
      uploadFilesPending=files.slice(done);
      if(done&&start)$('uploadEpisode').value=uploadFilesPending.length?String(start+done):'';
      drawQueue();
      uploading=false;$('uploadSeries').disabled=false;$('uploadEpisode').disabled=false;$('submitUpload').disabled=false;$('uploadFile').disabled=false;$('uploadProgress').hidden=true;
      $('pickUploaded').hidden=!latestUploads.some(s=>s.status==='ready');
      if(done){$('sourceQuery').value='';$('seriesFilter').value='';}
      await Promise.allSettled([search(),readSeries()]);
    }
  }
  function renderAnalysis() {
    $('analysisState').textContent=analysis ? labels[analysis.status]||analysis.status : model.analysisId?'等待读取分析':model.editingProject?'正在编辑已保存草稿':'尚未分析';
    const note=analysis?.error||analysis?.error_message||(analysis?.sources||[]).flatMap(s=>s.warnings||[]).filter((w,i,arr)=>arr.indexOf(w)===i).join('；');
    $('analysisNote').textContent=note;
    $('analysisNote').classList.toggle('error',analysis?.status==='failed');
    $('candidates').innerHTML=(analysis?.sources||[]).map((s,si)=>{
      const candidates=s.candidates||[];
      const engine=s.engine==='multimodal'?'画面语义分析':s.engine==='signal'?'画面与声音信号':'';
      return '<div class="ai-note"><strong>'+escape(s.title||selectedSource(s.source_id)?.name||s.source_id)+'</strong> · '+escape(engine||'分析中')+' · '+candidates.length+' 个候选</div>'+candidates.map((c,ci)=>{
        const detail=[c.reason,c.evidence,c.transcript].filter(x=>typeof x==='string'&&x).filter((v,n,a)=>a.indexOf(v)===n).join(' · ');
        const evidenceType=c.engine==='multimodal'?'画面语义':c.engine==='signal'?'视听信号':'';
        const score=Number.isFinite(c.score)?'评分 '+Math.round(c.score*100)+'/100':'';
        return '<div class="ai-row"><div class="ai-maintext"><strong class="ai-time">'+time(c.start_ms)+' – '+time(c.end_ms)+'</strong><small>'+escape([c.category,evidenceType,score,detail].filter(Boolean).join(' · '))+'</small></div><div class="ai-tools">'+btn('预览','candidate',si+':'+ci)+(model.plan?btn('加入当前版本','insert',si+':'+ci):'')+'</div></div>';
      }).join('');
    }).join('');
    buttons();
  }
  function queuePoll() {
    clearTimeout(timer);
    if (model.analysisId&&['queued','running'].includes(analysis?.status)) timer=setTimeout(()=>refreshAnalysis(false),Math.min(20000,3000*(1+pollFailures)));
  }
  async function refreshAnalysis(manual=false) {
    const id=model.analysisId; if(!id)return;
    try {
      const data=await api('/analyses/'+encodeURIComponent(id));
      if(id!==model.analysisId)return;
      pollFailures=0; analysis=data; renderAnalysis();
    } catch(e) {
      if(id!==model.analysisId)return;
      pollFailures++; $('analysisNote').textContent='读取进度失败：'+e.message+'。可刷新分析。';
      if(manual)notice(e.message,true);
    }
    queuePoll();
  }
  async function analyze() {
    if(!mayChange())return;
    const ids=model.selected.map(s=>s.id);
    const data=await api('/analyses',{method:'POST',body:{source_ids:ids}});
    if(!data.analysis_id)throw new Error('未取得分析任务编号，请重试');
    clearPlan(); model.analysisId=data.analysis_id; analysis=data; pollFailures=0; persist(); renderAnalysis(); queuePoll();
    $('review').scrollIntoView({block:'start'});
  }
  function options() {
    const body={analysis_id:model.analysisId,method:$('method').value,merge:$('grouping').value==='merge',script_count:Number($('scriptCount').value),variant_count:Number($('variantCount').value)};

    if(body.method!=='hook_first' && $('duration').value!=='') {
      const v=Number($('duration').value);
      if(!Number.isInteger(v)||v<1)throw new Error('目标时长请填写正整数秒，或留空使用自动时长');
      body.target_duration_ms=v*1000;
    }
    return body;
  }
  async function generate() {
    if(!mayChange())return;
    const data=await api('/plans',{method:'POST',body:options()});
    if(!data.drafts?.length)throw new Error('没有可创建的剪辑版本，请调整素材或配置');
    model.plan=data;model.active=0;model.editingProject=null;model.edited=false;model.pendingDrafts=[];
    persist(); renderPlan(); renderAnalysis();
    $('planBlock').scrollIntoView({block:'nearest'});
  }
  function renderPlan() {
    const units=allUnits(); $('planBlock').hidden=!units.length;
    if(!units.length){buttons();return;}
    if(!units[model.active])model.active=0;
    const unit=units[model.active],detail=model.plan.outputs?.find(x=>x.unit_id===unit.id);
    $('unitSelect').innerHTML=units.map((u,i)=>'<option value="'+i+'">'+escape(u.title||'剪辑版本 '+(i+1))+'</option>').join('');
    $('unitSelect').value=String(model.active);
    const totalTime=unit.clips.reduce((v,c)=>v+c.end_ms-c.start_ms,0);
    $('planCount').textContent='共 '+units.length+' 个版本 · 当前 '+time(totalTime)+(model.plan.warnings?.length?' · '+model.plan.warnings.join('；'):'');
    $('clipRows').innerHTML=unit.clips.map((c,i)=>{
      const seg=detail?.segments?.find(x=>x.clip_id===c.id);
      const title=selectedSource(c.source_id)?.name||c.source_id;
      return '<div class="ai-clip"><span class="ai-rank">'+(i+1)+'</span><div class="ai-maintext"><strong>'+escape(title)+'</strong><small>'+escape([seg?.role,seg?.reason].filter(Boolean).join(' · '))+'</small></div><div class="ai-timing"><label>入点（秒）<input type="number" step=".001" min="0" data-start="'+i+'" value="'+secs(c.start_ms)+'"></label><label>出点（秒）<input type="number" step=".001" min="0" data-end="'+i+'" value="'+secs(c.end_ms)+'"></label></div><div class="ai-tools">'+btn('预览','clipplay',i)+btn('上移','clipmove',i+':-1',i===0)+btn('下移','clipmove',i+':1',i===unit.clips.length-1)+btn('移除','clipremove',i)+'</div></div>';
    }).join('')||empty('此版本尚无片段，请从上方候选加入。');
    buttons();
  }
  function editable() {
    if(model.pendingDrafts.length){notice('尚有草稿待创建，请先核实提交结果。',true); return false;}
    return true;
  }
  function markEdited() { model.plan.saved=false;model.edited=true;persist();renderPlan(); }
  function changeClip(index,field,value) {
    if(!editable()){renderPlan();return;}
    const c=allUnits()[model.active].clips[index];
    const limit=durationOf(selectedSource(c.source_id));
    const ms=Math.round(Number(value)*1000);
    if(!value.trim()||!Number.isFinite(ms)||ms<0||(limit&&ms>limit)||(field==='start_ms'?ms>=c.end_ms:ms<=c.start_ms)){
      renderPlan();notice('入点和出点须在原片时长内，且入点必须早于出点。',true);return;
    }
    c[field]=ms;markEdited();
  }
  async function preview(urlFn,title,start=0,end=null) {
    const token=++mediaSeq; stopAt=end; const video=$('player');
    video.pause(); video.removeAttribute('src');video.load();
    $('previewHeading').textContent=title; $('previewInfo').textContent='正在读取预览…';
    if(!$('preview').open)$('preview').showModal();
    try {
      const url=await urlFn();
      if(token!==mediaSeq)return;
      if(!url)throw new Error('预览链接暂不可用');
      const parsed=new URL(url,location.href);
      if(!['http:','https:'].includes(parsed.protocol))throw new Error('预览地址无效');
      video.onloadedmetadata=()=>{
        if(token!==mediaSeq)return;
        if(start)video.currentTime=start/1000;
        $('previewInfo').textContent=end!==null?'原片片段 '+time(start)+' – '+time(end):'';
        video.play().catch(()=>{});
      };
      video.onerror=()=>{if(token===mediaSeq)$('previewInfo').textContent='视频加载失败，请关闭后重试以刷新预览地址。';};
      video.src=parsed.href;video.load();
    } catch(e) {if(token===mediaSeq)$('previewInfo').textContent=e.message;}
  }
  const previewSource=(id,start=0,end=null,title='')=>preview(async()=>API+'/assets/'+encodeURIComponent(id)+'/media',title||selectedSource(id)?.name||'素材预览',start,end);
  function projectSummary(p,units) {
    return {id:p.id,name:p.name,version:p.version,unitIds:(units||p.units||[]).map(u=>u.id),render:null};
  }
  async function openProject(id) {
    if(!mayChange())return;
    const project=(await api('/projects/'+encodeURIComponent(id))).item;
    if(!Array.isArray(project?.units)||!project.units.length)throw new Error('此草稿没有制作单元');
    const ids=[...new Set(project.units.flatMap(u=>u.clips.map(c=>c.source_id)))],selected=[];
    for(let i=0;i<ids.length;i+=6) {
      const sources=await Promise.all(ids.slice(i,i+6).map(k=>api('/assets/'+encodeURIComponent(k))));
      sources.forEach(({item:s})=>selected.push(compactSource(s)));
    }
    clearTimeout(timer);analysis=null;model.analysisId='';model.selected=selected;
    model.editingProject={id,version:project.version};
    model.plan={drafts:[{name:project.name,units:project.units}],outputs:[],warnings:[],saved:true};
    model.active=0;model.edited=false;model.pendingDrafts=[];model.projectName=project.name;
    $('projectName').value=project.name;
    persist();renderSelection();renderAnalysis();renderPlan();renderList();$('planBlock').scrollIntoView({block:'start'});
  }
  async function createDrafts(asCopy=false) {
    const units=allUnits();
    if(!units.length||units.some(u=>!u.clips.length))throw new Error('每个版本至少要保留一个片段');
    if(!model.pendingDrafts.length){
      const name=$('projectName').value.trim();
      if(!name)throw new Error('请填写项目名称');
      model.pendingDrafts=model.plan.drafts.map((draft,i)=>({
        operation_id:uuid(),index:i,name:name+(model.plan.drafts.length>1?' · '+(i+1):asCopy?'（副本）':''),
        existing:asCopy?null:(model.editingProject||model.plan.savedParts?.[i]||null),pending:false
      }));
      persist();
    }
    while(model.pendingDrafts.length){
      const next=model.pendingDrafts[0],draft=model.plan.drafts[next.index];
      next.pending=true; persist();
      let p;
      const payload={name:next.name,units:draft.units,operation_id:next.operation_id};
      if(next.existing)payload.expected_version=next.existing.version;
      try {p=(await api(next.existing?'/projects/'+encodeURIComponent(next.existing.id):'/projects',{method:next.existing?'PATCH':'POST',body:payload})).item;}
      catch(e){
        if(e.status>=400&&e.status<500){model.pendingDrafts=[];persist();}
        if(e.status===409&&next.existing)throw new Error('草稿版本已变化，请另存为新草稿，或重新打开后修改。');
        throw e;
      }
      if(!p?.id)throw new Error('未取得草稿编号，请用相同操作继续核实');
      model.plan.savedParts ||= {};model.plan.savedParts[next.index]={id:p.id,version:p.version};
      const summary=projectSummary(p,draft.units),old=model.projects.find(row=>row.id===p.id);
      if(old)Object.assign(old,summary,{completedRender:false});else model.projects.unshift(summary);
      if(model.plan.drafts.length===1){model.editingProject={id:p.id,version:p.version};model.projectName=next.name;$('projectName').value=next.name;}
      model.pendingDrafts.shift();persist();renderWorks();
    }
    model.edited=false;model.plan.saved=true;persist();renderPlan();
    notice('草稿已保存，可在下方选择项目制作成片。');
    $('works').scrollIntoView({block:'start'});
  }
  function renderWorks() {
    $('projects').innerHTML=model.projects.map((p,i)=>'<div class="ai-project"><div class="ai-maintext"><strong>'+escape(p.name)+'</strong><small>'+(p.unitCount??p.unitIds.length)+' 个制作单元'+(p.completedRender?' · 已提交制作':'')+'</small></div><div class="ai-tools">'+btn('打开草稿','openproject',i)+btn(p.render?'核实制作回执':'提交制作','render',i,!!p.completedRender)+'</div></div>').join('')||empty('创建草稿后，可在这里精修和制作。');
    $('jobs').innerHTML=model.jobs.map((entry,i)=>{
      const t=liveJobs.get(entry.id)||entry;
      const finished=t.status==='completed';
      const links=finished?btn('预览','jobplay',i)+btn('下载','download',i)+btn('解说与翻译','language',i)+btn('制作字幕','subtitles',i):t.status==='failed'?btn('重试原任务','retry',i):'';
      const status=t.status==='running'?'制作中':labels[t.status]||t.status;
      return '<div class="ai-job"><div class="ai-maintext"><strong>'+escape(t.title||t.drama_title||entry.name||'制作任务')+'</strong><small>'+escape([status,t.error_message||t.error].filter(Boolean).join(' · '))+'</small></div><div class="ai-tools">'+links+'</div></div>';
    }).join('');
  }
  function track(task) {
    if(!task?.id)return;
    liveJobs.set(task.id,task);
    const old=model.jobs.find(j=>j.id===task.id);
    const brief={id:task.id,name:task.title||task.drama_title||task.name,status:task.status};
    if(old)Object.assign(old,brief);else model.jobs.push(brief);
  }
  async function renderProject(i) {
    const p=model.projects[i];
    if (!p || p.completedRender)return;
    if(model.edited&&model.editingProject?.id===p.id)throw new Error('请先保存本页草稿修改，再提交制作。');
    if(!p.render){
      const current=(await api('/projects/'+encodeURIComponent(p.id))).item;
      if(!current?.units?.length)throw new Error('草稿中没有可制作的单元');
      p.version=current.version;p.unitIds=current.units.map(u=>u.id);
      p.render={operation_id:uuid(),expected_version:p.version,unit_ids:p.unitIds};persist();
    }
    let response;
    try { response=await api('/projects/'+encodeURIComponent(p.id)+'/render',{method:'POST',body:p.render}); }
    catch(e){if(e.status>=400&&e.status<500){p.render=null;persist();renderWorks();}throw e;}
    if(!Array.isArray(response.items))throw new Error('未取得任务回执，请用原请求核实');
    response.items.forEach(track);
    const invalid=response.issues||[];
    p.render=null;p.completedRender=response.items.length>0;persist();renderWorks();scheduleJobs();
    if(invalid.length)notice('已受理 '+response.items.length+' 个制作单元。'+invalid.map(x=>x.message).join('；')+'，可打开草稿核对。',true);
    else notice('已提交 '+response.items.length+' 个制作单元。');
  }
  function scheduleJobs() {
    clearTimeout(jobTimer);
    if(!document.hidden&&model.jobs.some(j=>!terminal(j.status)))jobTimer=setTimeout(()=>refreshJobs(false),5000);
  }
  async function readWorks() {
    const [projectList,taskList]=await Promise.allSettled([api('/projects'),api('/tasks')]);
    if(projectList.status==='fulfilled'){
      const server=projectList.value.items||[];
      model.projects=server.map(p=>{
        const previous=model.projects.find(x=>x.id===p.id);
        const summary=projectSummary(p);
        if(!p.units&&previous)summary.unitIds=previous.unitIds;
        if(!p.units&&p.unit_count)summary.unitCount=p.unit_count;
        if(previous?.version===p.version){summary.completedRender=previous.completedRender;summary.render=previous.render;}
        return summary;
      });
    }
    if(taskList.status==='fulfilled'){
      const records=taskList.value.items||[];
      model.jobs=[];records.forEach(track);
    }
    if(projectList.status==='rejected'||taskList.status==='rejected')notice('部分历史记录暂时不可用，请刷新进度。',true);
    persist();renderWorks();scheduleJobs();
  }
  let jobsRefreshing=false;
  async function refreshJobs(manual=false) {
    if(jobsRefreshing)return;
    jobsRefreshing=true;
    try {
      const jobs=model.jobs.filter(j=>manual||!terminal(j.status));
      for(let i=0;i<jobs.length;i+=6) {
        const chunk=await Promise.allSettled(jobs.slice(i,i+6).map(j=>api('/tasks/'+encodeURIComponent(j.id))));
        chunk.forEach(r=>{if(r.status==='fulfilled')track(r.value.item);});
        if(chunk.some(r=>r.status==='rejected')&&manual)notice('部分任务状态暂时无法读取，可再次刷新。',true);
      }
      persist();renderWorks();
    } finally { jobsRefreshing=false;scheduleJobs(); }
  }
  function selectionToggle(i,checked){
    const s=rows[i]; if(!s)return;
    if(!mayChange()){renderList();return;}
    const id=sourceId(s);
    if(checked&&!selectedSource(id)&&model.selected.length>=20){renderList();notice('单次最多分析 20 段素材。',true);return;}
    if(checked&&!selectedSource(id))model.selected.push(compactSource(s));
    if(!checked)model.selected=model.selected.filter(x=>x.id!==id);
    selectionChanged();
  }
  function bind() {
    $('authButton').onclick=()=>window.UiTopbar.handleAuthAction({auth,api});
    $('searchForm').onsubmit=e=>{e.preventDefault();search();};
    $('seriesFilter').onchange=()=>search();
    $('uploadFile').onchange=()=>{uploadFilesPending=Array.from($('uploadFile').files);drawQueue();};
    $('uploadEpisode').oninput=drawQueue;
    $('pickUploaded').onclick=()=>{
      if(!mayChange())return;
      const choices=latestUploads.filter(s=>s.status==='ready'&&!selectedSource(sourceId(s)));
      if(model.selected.length+choices.length>20){notice('单次最多分析 20 段，请在素材库中选择本批需要使用的文件。',true);return;}
      choices.forEach(s=>model.selected.push(compactSource(s)));
      selectionChanged();notice('已选用本次上传的就绪素材。');
    };
    $('showUpload').onclick=()=>{$('uploadForm').hidden=!$('uploadForm').hidden;};
    $('uploadForm').onsubmit=e=>{e.preventDefault();uploadFiles();};
    $('moreSources').onclick=()=>action(()=>search(true));
    $('sourceList').onchange=e=>{if(e.target.matches('[data-pick]'))selectionToggle(Number(e.target.dataset.pick),e.target.checked);};
    $('sourceList').onclick=e=>{
      const el=e.target.closest('[data-sourcepreview]');if(!el)return;
      const s=rows[el.dataset.sourcepreview];previewSource(sourceId(s),0,null,nameOf(s));
    };
    $('selection').onclick=e=>{
      const el=e.target.closest('button');if(!el||!mayChange())return;
      if(el.hasAttribute('data-remove'))model.selected=model.selected.filter(s=>s.id!==el.dataset.remove);
      if(el.hasAttribute('data-move')){const [i,delta]=el.dataset.move.split(':').map(Number);const [s]=model.selected.splice(i,1);model.selected.splice(i+delta,0,s);}
      selectionChanged();
    };
    controls.forEach(key=>$(key).onchange=()=>{
      if(!mayChange()){$(key).value=model.settings[key];return;}
      model.settings=cfg();clearPlan();persist();renderAnalysis();buttons();
    });
    $('analyze').onclick=()=>action(analyze); $('makePlan').onclick=()=>action(generate);
    $('refreshAnalysis').onclick=()=>refreshAnalysis(true);
    $('unitSelect').onchange=()=>{model.active=Number($('unitSelect').value);persist();renderPlan();};
    $('projectName').oninput=()=>{model.projectName=$('projectName').value;if(model.plan){model.edited=true;model.plan.saved=false;buttons();}persist();};
    $('candidates').onclick=e=>{
      const el=e.target.closest('button'); if(!el)return;
      const [si,ci]=(el.dataset.candidate||el.dataset.insert).split(':').map(Number);
      const s=analysis.sources[si],c=s.candidates[ci];
      if(el.hasAttribute('data-candidate'))return previewSource(s.source_id,c.start_ms,c.end_ms);
      if(!editable())return;
      const unit=allUnits()[model.active];
      if(!unit)return;
      const id=uuid(); unit.clips.push({id,source_id:s.source_id,start_ms:c.start_ms,end_ms:c.end_ms,fit:'contain',mute:false,mirror:false,transition:'cut'});
      const details=model.plan.outputs?.find(x=>x.unit_id===unit.id);
      if(details)details.segments.push({clip_id:id,candidate_id:c.id,role:'手动加入',reason:c.reason});
      markEdited();notice('已将候选加入当前版本。');
    };
    $('clipRows').onchange=e=>{
      const el=e.target;if(el.hasAttribute('data-start'))changeClip(Number(el.dataset.start),'start_ms',el.value);
      if(el.hasAttribute('data-end'))changeClip(Number(el.dataset.end),'end_ms',el.value);
    };
    $('clipRows').onclick=e=>{
      const el=e.target.closest('button'); if(!el)return;
      const unit=allUnits()[model.active];
      if(el.hasAttribute('data-clipplay')){const c=unit.clips[el.dataset.clipplay];return previewSource(c.source_id,c.start_ms,c.end_ms);}
      if(!editable())return;
      if(el.hasAttribute('data-clipremove'))unit.clips.splice(Number(el.dataset.clipremove),1);
      if(el.hasAttribute('data-clipmove')){const [i,delta]=el.dataset.clipmove.split(':').map(Number);const [clip]=unit.clips.splice(i,1);unit.clips.splice(i+delta,0,clip);}
      markEdited();
    };
    $('saveDraft').onclick=()=>action(()=>createDrafts(false));
    $('saveCopy').onclick=()=>action(()=>createDrafts(true));
    $('projects').onclick=e=>{
      const el=e.target.closest('button');if(!el)return;
      if(el.hasAttribute('data-render'))action(()=>renderProject(Number(el.dataset.render)));
      if(el.hasAttribute('data-openproject'))action(()=>openProject(model.projects[Number(el.dataset.openproject)].id));
    };
    $('jobs').onclick=e=>{
      const el=e.target.closest('button');if(!el)return;
      const id=model.jobs[Number(Object.values(el.dataset)[0])]?.id;if(!id)return;
      const url=API+'/tasks/'+encodeURIComponent(id)+'/media';
      if(el.hasAttribute('data-retry'))return action(async()=>{
        const result=(await api('/tasks/'+encodeURIComponent(id)+'/retry',{method:'POST',body:{}})).item;
        if(!result||result.id!==id)throw new Error('未取得原任务回执，请刷新核实。');
        track(result);persist();renderWorks();scheduleJobs();notice('已重新受理原任务。');
      });
      if(el.hasAttribute('data-jobplay'))return preview(async()=>url,'成片预览');
      if(el.hasAttribute('data-download')){
        const link=document.createElement('a');link.href=url+'?download=1';link.download='';link.rel='noopener';link.click();return;
      }
      if(el.hasAttribute('data-language'))return action(()=>window.VideoEditingLanguage.open(id));
      if(el.hasAttribute('data-subtitles'))return action(()=>window.VideoEditingLanguage.subtitles(id));
    };
    $('subtitleTool').onclick=()=>action(()=>window.VideoEditingLanguage.subtitles());
    $('refreshWorks').onclick=()=>action(readWorks);
    $('closePreview').onclick=()=>$('preview').close();
    $('preview').addEventListener('close',()=>{mediaSeq++;stopAt=null;const v=$('player');v.onloadedmetadata=null;v.onerror=null;v.pause();v.removeAttribute('src');v.load();});
    $('player').addEventListener('timeupdate',()=>{if(stopAt!==null && $('player').currentTime>=stopAt/1000)$('player').pause();});
    window.addEventListener('beforeunload',e=>{if(uploading){e.preventDefault();e.returnValue='';}});
    document.addEventListener('visibilitychange',()=>{if(!document.hidden){if(model.analysisId)refreshAnalysis();refreshJobs();}});
  }
  async function init() {
    bind();
    auth=await api('/api/ui/topbar');window.UiTopbar.render({auth,userCard:'#userCard',authButton:'#authButton'});
    await window.QuickNav.render({container:'#quickNav',auth,activeKey:'videoEditingAi'});
    if(!auth.authenticated){$('gate').textContent='请登录后进入智能剪辑工作台。';return;}
    $('gate').hidden=true;
    const identity=auth.user?.user_id||auth.user?.email;
    if(identity){
      storageKey='independent-ai-edit:v1:'+JSON.stringify([auth.user.tenant_key||'',identity]);
      try{const saved=JSON.parse(sessionStorage.getItem(storageKey)||'null');if(saved&&Array.isArray(saved.selected)&&Array.isArray(saved.projects))Object.assign(model,saved);}catch{}
    }
    if(model.settings)controls.forEach(k=>$(k).value=model.settings[k]);else model.settings=cfg();
    $('projectName').value=model.projectName;
    $('workspace').hidden=false;renderSelection();renderPlan();renderWorks();buttons();
    await Promise.allSettled([search(),readSeries(),refreshAnalysis(true),readWorks()]);
  }
  init().catch(e=>{$('gate').hidden=false;$('gate').textContent='工作台暂时无法打开：'+e.message+'。请刷新重试。';$('gate').classList.add('error');});
})();
