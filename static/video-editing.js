(function (global) {
  'use strict';
  const FADE_MS = 300;
  const toFrame = milliseconds => Math.floor((milliseconds * 30 + 500) / 1000);
  const clone = value => JSON.parse(JSON.stringify(value));
  const uid = () => global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : `ve-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const durationOf = source => Number(source && source.metadata && source.metadata.duration_ms || 0);
  function probeDisplay(source) {
    if(source.status==='ready')return {text:`${(durationOf(source)/1000).toFixed(3).replace(/\.?0+$/,'')} 秒 · 已就绪`,percent:null};
    const p=source.probe_progress||{},status=p.status||source.status,stage=p.stage;
    if(['failed','unknown','recovery_required','cancelled'].includes(status))return {text:({failed:'校验失败',unknown:'连接中断，正在核实',recovery_required:'等待恢复',cancelled:'已取消'})[status],percent:null};
    if(status==='queued'||status==='queued_gpu'||stage==='queued'||stage==='waiting_gpu_capacity')return {text:'排队中 · 等待校验资源',percent:null};
    if(stage==='downloading'){
      const done=Math.max(0,Number(p.download_bytes)||0),total=Math.max(0,Number(p.total_bytes)||0);
      const percent=total?Math.min(100,Math.floor(done/total*100)):null;
      const mb=n=>(n/1048576).toFixed(1);
      return {text:`下载中${percent===null?'':` ${percent}%`} · ${mb(done)}${total?` / ${mb(total)}`:''} MB`,percent};
    }
    return {text:({waiting_media:'排队中 · 等待预览处理',verifying:'校验视频信息',normalizing:'生成预览',uploading:'保存校验结果',starting:'准备下载'})[stage]||(source.status==='unprobed'?'待校验':'正在准备校验'),percent:null};
  }
  function layout(clips) {
    let cursor = 0;
    return (clips || []).map((clip, index) => {
      const length = (toFrame(Number(clip.end_ms)) - toFrame(Number(clip.start_ms))) * 1000 / 30;
      const item = { clip, index, start: cursor, end: cursor + length, length };
      cursor += length - (index < clips.length - 1 && clip.transition === 'fade' ? FADE_MS : 0);
      return item;
    });
  }
  function totalDuration(clips) { const items = layout(clips); return items.length ? items[items.length - 1].end : 0; }
  function validateClips(clips, sources) {
    const errors = [];
    if (!clips || !clips.length) return ['请至少添加一个片段'];
    if(clips.length>300)errors.push('每部短剧最多保留 300 个片段，请减少切割或删除不需要的片段');
    clips.forEach((clip, index) => {
      const name = `第 ${index + 1} 个片段`;
      const source = sources && sources[clip.source_id];
      const length = clip.end_ms - clip.start_ms;
      const frameLength = toFrame(clip.end_ms) - toFrame(clip.start_ms);
      if (!Number.isInteger(clip.start_ms) || !Number.isInteger(clip.end_ms) || clip.start_ms < 0 || length <= 0) errors.push(`${name}的保留范围无效`);
      else if(frameLength < 1) errors.push(`${name}按 30fps 对齐后不足一帧，请扩大保留范围`);
      if (source && source.status !== 'ready') errors.push(`${name}尚未通过媒体校验`);
      if (source && durationOf(source) && clip.end_ms > durationOf(source)) errors.push(`${name}超出原视频时长`);
      if (!['contain', 'cover'].includes(clip.fit)) errors.push(`${name}的画面适配无效`);
      if (!['cut', 'fade'].includes(clip.transition)) errors.push(`${name}的衔接方式无效`);
      if (index === clips.length - 1 && clip.transition !== 'cut') errors.push(`${name}是最后一个片段，应使用直接切换`);
      const overlaps = (index && clips[index - 1].transition === 'fade' ? FADE_MS : 0) + (index < clips.length - 1 && clip.transition === 'fade' ? FADE_MS : 0);
      if (overlaps && frameLength < toFrame(overlaps)) errors.push(`${name}按 30fps 计算不足 ${secondsForError(overlaps)} 秒，无法容纳所选交叉淡化`);
    });
    return errors;
  }
  function secondsForError(milliseconds){return (milliseconds/1000).toFixed(1);}
  function split(clips, id, atMs) {
    const result = clone(clips); const index = result.findIndex(clip => clip.id === id);
    if (index < 0) throw new Error('请先选择片段');
    const clip = result[index]; const point = Math.round(atMs);
    if (point <= clip.start_ms || point >= clip.end_ms) throw new Error('请将播放位置移到片段内部再切割');
    result.splice(index, 1, { ...clip, end_ms: point, transition: 'cut' }, { ...clip, id: uid(), start_ms: point });
    return result;
  }
  function normalizeLast(clips) { if (clips.length) clips[clips.length - 1].transition = 'cut'; return clips; }
  function move(clips, from, to) {
    const result = clone(clips);
    if (from < 0 || from >= result.length || to < 0 || to >= result.length) return result;
    result.splice(to, 0, result.splice(from, 1)[0]); return normalizeLast(result);
  }
  function applyCommon(dramas, assetClip, position, transition, assetSourceId) {
    return dramas.map(drama => {
      // Asset edits are replaced only when an insertion rule is explicitly selected.
      const clips = clone(drama.clips || []).filter(clip => position === 'none' || clip.source_id !== assetSourceId);
      if (position !== 'none') {
        if (!assetClip) throw new Error('请先选择已通过媒体校验的素材');
        if (position === 'start') clips.unshift({ ...clone(assetClip), id: uid() });
        else clips.push({ ...clone(assetClip), id: uid() });
      }
      clips.forEach((clip, index) => { clip.transition = index < clips.length - 1 ? transition : 'cut'; });
      return { ...drama, clips };
    });
  }
  // RFC 1321 MD5 over one bounded upload part. Only the base64 digest is sent to the signer.
  function md5Base64(input) {
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
    const padded = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64); padded.set(bytes); padded[bytes.length] = 128;
    const view = new DataView(padded.buffer); const bits = bytes.length * 8;
    view.setUint32(padded.length - 8, bits >>> 0, true); view.setUint32(padded.length - 4, Math.floor(bits / 4294967296), true);
    let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476;
    const shifts = [[7,12,17,22],[5,9,14,20],[4,11,16,23],[6,10,15,21]];
    const constants = Array.from({ length: 64 }, (_, index) => Math.floor(Math.abs(Math.sin(index + 1)) * 4294967296) >>> 0);
    for (let offset = 0; offset < padded.length; offset += 64) {
      let a = h0, b = h1, c = h2, d = h3;
      for (let i = 0; i < 64; i++) {
        let f, g;
        if (i < 16) { f = (b & c) | (~b & d); g = i; }
        else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
        else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
        else { f = c ^ (b | ~d); g = (7 * i) % 16; }
        const sum = (a + f + constants[i] + view.getUint32(offset + 4 * g, true)) >>> 0;
        const shift = shifts[Math.floor(i / 16)][i % 4]; const rotated = (sum << shift) | (sum >>> (32 - shift));
        a = d; d = c; c = b; b = (b + rotated) >>> 0;
      }
      h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    }
    const digest = new Uint8Array(16); const out = new DataView(digest.buffer);
    [h0,h1,h2,h3].forEach((value, index) => out.setUint32(index * 4, value, true));
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'; let encoded = '';
    for (let i = 0; i < digest.length; i += 3) {
      const value = (digest[i] << 16) | ((digest[i + 1] || 0) << 8) | (digest[i + 2] || 0);
      encoded += alphabet[(value >>> 18) & 63] + alphabet[(value >>> 12) & 63] + (i + 1 < 16 ? alphabet[(value >>> 6) & 63] : '=') + (i + 2 < 16 ? alphabet[value & 63] : '=');
    }
    return encoded;
  }
  const model = { FADE_MS, toFrame, layout, totalDuration, validateClips, split, move, applyCommon, md5Base64, probeDisplay };
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  if (!global.document) return;
  global.VideoEditingModel = model;
  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  const ROOT = '/api/video-editing';
  const state = { auth:null, config:{ max_file_bytes:8*1024**3, part_size:16*1024**2, max_dramas:20 }, assets:[], assetsTotal:0, sources:{}, projects:[], project:{ name:'未命名剪辑项目', asset_id:'', dramas:[] }, activeDrama:0, selectedClip:null, undo:[], dirty:false, saving:null, saveTimer:null, revision:0, projectLoadSeq:0, tasks:[], uploads:new Map(), probeRecoveries:new Map(), playMs:0, playing:false, playTick:0, playOrigin:0, activeView:'editor', catalog:[], episodes:[], selectedCatalog:null, catalogLoadSeq:0, operationId:null, renderOperation:null, ready:false };
  const readRequests = new Map(), actions = new Map(), busyButtons = new WeakMap();
  Object.assign(state, { catalogSessionSeq:0, catalogProducts:null, pendingAdd:null, polling:null, assetsLoadSeq:0, projectsLoading:null, tasksLoading:null, projectOpening:0, sourceLoading:false, sourceLoadFailed:false, renderSubmitting:false, materialUploadSubmitting:new Set() });
  let toastTimer;
  function toast(message, error) { $('toast').textContent = message; $('toast').classList.toggle('error', !!error); $('toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.add('hidden'), error ? 7500 : 4200); }
  async function api(path, options = {}) {
    if (demo && path.startsWith(ROOT)) throw new Error('模拟预览不会访问业务接口');
    const read = !options.method || options.method === 'GET';
    if (read && readRequests.has(path)) return readRequests.get(path);
    const headers = { Accept:'application/json', ...(options.headers || {}) };
    if (options.body && typeof options.body !== 'string') { options = { ...options, body:JSON.stringify(options.body) }; headers['Content-Type'] = 'application/json'; }
    if (options.method && options.method !== 'GET' && !options.body) { options = { ...options, body:'{}' }; headers['Content-Type'] = 'application/json'; }
    const request = (async()=>{
      const controller=read&&!options.signal&&typeof global.AbortController==='function'?new global.AbortController():null;
      let timedOut=false;const timeout=controller?setTimeout(()=>{timedOut=true;controller.abort();},30000):null;
      try{
        const response = await fetch(path, { credentials:'same-origin', cache:'no-store', ...options, ...(controller?{signal:controller.signal}:{}), headers });
        let payload;try{payload=await response.json();}catch(error){throw new Error(read?'查询响应无效，请重试':'提交回执无效，结果需要核实');}
        if(timedOut)throw new Error('查询超时，请重试');
        if (!response.ok) { const error = new Error(payload.error || `请求失败 (${response.status})`); error.status=response.status; error.code=payload.code; throw error; }
        return payload;
      }catch(error){if(timedOut)throw new Error('查询超时，请重试');throw error;}
      finally{if(timeout!==null)clearTimeout(timeout);}
    })();
    if (read) readRequests.set(path,request);
    try { return await request; } finally { if(read && readRequests.get(path)===request)readRequests.delete(path); }
  }
  async function mapLimit(values, limit, fn) {
    const results=new Array(values.length);let cursor=0;
    let failure;
    const outcomes=await Promise.allSettled(Array.from({length:Math.min(limit,values.length)},async()=>{while(cursor<values.length&&!failure){const index=cursor++;try{results[index]=await fn(values[index],index);}catch(error){failure=error;throw error;}}}));
    const rejected=outcomes.find(result=>result.status==='rejected');if(rejected)throw rejected.reason;
    return results;
  }
  function withBusy(key, button, label, fn) {
    if(actions.has(key))return actions.get(key);
    const token={};
    if(button){const saved=busyButtons.get(button)||{html:button.innerHTML,disabled:button.disabled};busyButtons.set(button,{...saved,token});button.disabled=true;button.setAttribute('aria-busy','true');if(label)button.textContent=label;}
    // Register the lock before invoking the operation, including synchronous handlers.
    let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});actions.set(key,promise);
    (async()=>{try{resolve(await fn());}catch(error){reject(error);}finally{actions.delete(key);const saved=button&&busyButtons.get(button);if(saved&&saved.token===token){button.innerHTML=saved.html;button.disabled=saved.disabled;button.removeAttribute('aria-busy');busyButtons.delete(button);}syncEditorLocks();}})();
    return promise;
  }
  function editingLocked(){return !!(state.projectOpening||state.sourceLoading||state.sourceLoadFailed||state.renderSubmitting);}
  function syncEditorLocks(){
    const locked=editingLocked();
    for(const id of ['projectName','chooseAsset','addDrama','addDramaBottom','insertAsset','applyCommon','clipStart','clipEnd','trimStart','trimEnd','clipSound','splitClip','removeClip','removeEpisode','moveLeft','moveRight','confirmInsert']){const el=$(id);if(el&&!busyButtons.has(el))el.disabled=locked;}
    document.querySelectorAll('[data-fit]').forEach(button=>{button.disabled=locked;});
    $('undoEdit').disabled=locked||!state.undo.length;
    const clip=selectedClip(),drama=currentDrama();$('clipTransition').disabled=locked||!clip||clip===(drama&&drama.clips[drama.clips.length-1]);
    $('renderProject').disabled=locked||actions.has('renderProject')||!state.project.dramas.length||!state.project.asset_id||state.config.intake_enabled===false||state.config.worker_configured===false;
    if(!busyButtons.has($('newProject')))$('newProject').disabled=state.renderSubmitting;
    $('projectPicker').disabled=state.renderSubmitting;
    $('retryProjectSources').classList.toggle('hidden',!state.sourceLoadFailed);$('retryProjectSources').disabled=!!state.projectOpening||state.sourceLoading;
  }
  const item = response => response && response.item || response;
  function time(ms) { const number = Math.max(0, Math.round(ms || 0)); return `${String(Math.floor(number / 60000)).padStart(2,'0')}:${String(Math.floor(number / 1000) % 60).padStart(2,'0')}.${String(number % 1000).padStart(3,'0')}`; }
  function seconds(ms) { return (Math.max(0, ms || 0) / 1000).toFixed(3).replace(/\.?0+$/, ''); }
  function bytes(value) { return value >= 1024**3 ? `${(value / 1024**3).toFixed(2)} GiB` : `${(value / 1024**2).toFixed(1)} MiB`; }
  function sourceTitle(source) { return source && (source.name || source.title || (source.episode_number ? `第 ${source.episode_number} 集` : source.id)) || '待载入来源'; }
  function currentDrama() { return state.project.dramas[state.activeDrama]; }
  function selectedClip() { return (currentDrama() && currentDrama().clips || []).find(clip => clip.id === state.selectedClip); }
  function selectedAsset() { return state.assets.find(asset => asset.id === state.project.asset_id) || state.sources[state.project.asset_id]; }
  function newClip(source, overrides = {}) { return { id:uid(), source_id:source.source_id || source.id, start_ms:0, end_ms:durationOf(source), fit:'contain', mute:false, transition:'cut', ...overrides }; }
  function rememberSources(sources) { for (const source of sources || []) { const id=source.source_id||source.id;state.sources[id] = { ...source, _previewFetchedAt:Date.now() };if(source.status==='ready')state.probeRecoveries.delete(id); } }
  function snapshot() { state.undo.push(clone(state.project)); if (state.undo.length > 60) state.undo.shift(); }
  function changed(mutator) { if(editingLocked())return toast('正在载入或提交项目，请稍候');snapshot(); stopPreview(); mutator(); state.revision++; state.dirty=true; state.renderOperation=null; renderEditor(); scheduleSave(); }
  function scheduleSave() { clearTimeout(state.saveTimer); $('saveStatus').textContent = demo ? '模拟草稿 · 仅本页' : '有未保存修改'; if (!demo) state.saveTimer=setTimeout(() => saveProject(false).catch(error => toast(`草稿未保存：${error.message}`, true)), 1000); }
  async function ensureSource(id, refresh = false) {
    const cached = state.sources[id];
    if (demo) return cached;
    const expires=cached&&(cached.preview_expires_at||cached.expires_at);
    if (!refresh && cached && cached.status === 'ready' && cached.preview_url && Date.now()-(cached._previewFetchedAt||0)<240000 && (!expires || expiryTime(expires) > Date.now()+30000)) return cached;
    const source = item(await api(`${ROOT}/sources/${encodeURIComponent(id)}`)); rememberSources([source]); return source;
  }
  function setView(view) {
    stopPreview(); state.activeView=view;
    document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === view));
    ['editor','library','tasks'].forEach(name => $(`${name}View`).classList.toggle('hidden', name !== view));
    if (view === 'library') loadAssets().catch(error => toast(error.message,true));
    if (view === 'tasks') {if(!state.tasks.length)$('taskList').innerHTML='<div class="empty" role="status">正在加载制作任务…</div>';loadTasks().catch(error=>{$('taskList').innerHTML=state.tasks.length?$('taskList').innerHTML:`<div class="empty danger">任务加载失败：${esc(error.message)}，请点击刷新任务。</div>`;toast(error.message,true);});}
  }
  function renderEditor() {
    $('projectName').value=state.project.name || ''; $('dramaCount').textContent=`${state.project.dramas.length} 部`;
    $('selectedAssetName').textContent=selectedAsset() ? sourceTitle(selectedAsset()) : '选择一个素材';
    $('undoEdit').disabled=!state.undo.length;
    $('saveCopy').classList.toggle('hidden',!state.conflicted);
    $('renderProject').disabled=!state.project.dramas.length || !state.project.asset_id || state.config.intake_enabled===false || state.config.worker_configured===false;
    $('dramaList').innerHTML=state.project.dramas.map((drama,index) => `<div class="drama-row ${index===state.activeDrama?'active':''}"><span class="drama-cover">${esc((drama.title||'剧').slice(0,1))}</span><button data-drama-index="${index}"><strong>${esc(drama.title || drama.content_id)}</strong><small>${drama.clips.length} 片段 · ${seconds(totalDuration(drama.clips))} 秒</small></button><button class="remove-drama" data-remove-drama="${index}" title="移除此短剧" aria-label="移除此短剧">×</button></div>`).join('') || '<div class="empty">还没有短剧</div>';
    const drama=currentDrama(); const clips=drama && drama.clips || []; const duration=totalDuration(clips);
    if(!clips.length){$('previewA').style.opacity=0;$('previewB').style.opacity=0;$('previewPlaceholder').classList.remove('hidden');$('previewMessage').textContent=demo?'演示画面，正式素材校验后可播放':'添加素材与剧集后即可预览';$('previewExpiry').textContent='';}
    if (!clips.find(clip => clip.id === state.selectedClip)) state.selectedClip=clips.length ? clips[0].id : null;
    $('previewTitle').textContent=drama ? drama.title : '选择短剧后预览';
    $('timelineDuration').textContent=`预计 ${seconds(duration)} 秒 · ${clips.length} 片段`;
    const maxWidth=Math.max(105, Math.min(250, 800/Math.max(1,clips.length)));
    $('timeline').innerHTML=layout(clips).map(({clip,index,length,start}) => {
      const source=state.sources[clip.source_id]; const isAsset=source && source.kind==='asset';
      return `<button class="timeline-clip ${isAsset?'asset':''} ${clip.id===state.selectedClip?'selected':''}" draggable="true" data-clip-id="${esc(clip.id)}" style="width:${Math.max(105,Math.min(280,maxWidth+Math.log2(Math.max(1,length/1000))*7))}px" title="${esc(sourceTitle(source))}"><span class="clip-index">${time(start)}</span><strong>${isAsset?'▧':'▤'} ${esc(sourceTitle(source))}</strong><small>${seconds(clip.start_ms)}—${seconds(clip.end_ms)} 秒${clip.mute?' · 静音':''}</small>${clip.transition==='fade'&&index<clips.length-1?'<span class="transition-mark" title="0.3 秒交叉淡化">⋈</span>':''}</button>`;
    }).join('') || '<div class="timeline-empty">添加剧集后开始剪辑</div>';
    const errors=clips.length ? validateClips(clips,state.sources) : [];
    $('validationErrors').textContent=errors.join('；'); $('validationErrors').classList.toggle('hidden',!errors.length);
    $('totalTime').textContent=time(duration); $('playhead').max=Math.max(1,duration); state.playMs=Math.min(state.playMs,Math.max(0,duration)); updatePlayhead(); renderInspector();syncEditorLocks();
  }
  function renderInspector() {
    const clip=selectedClip(); $('emptyInspector').classList.toggle('hidden',!!clip); $('clipInspector').classList.toggle('hidden',!clip);
    if (!clip) { $('clipKind').textContent='未选择'; return; }
    const source=state.sources[clip.source_id]; const isAsset=source && source.kind==='asset';
    $('clipKind').textContent=isAsset?'素材':'剧集'; $('clipTitle').textContent=sourceTitle(source); $('sourceDuration').textContent=`原长 ${seconds(durationOf(source))} 秒`;
    $('clipStart').value=(clip.start_ms/1000).toFixed(3); $('clipEnd').value=(clip.end_ms/1000).toFixed(3);
    for(const id of ['trimStart','trimEnd']) $(id).max=durationOf(source)||clip.end_ms;
    $('trimStart').value=clip.start_ms; $('trimEnd').value=clip.end_ms; $('clipSound').checked=!clip.mute;
    $('clipTransition').value=clip.transition; $('clipTransition').disabled=clip===currentDrama().clips[currentDrama().clips.length-1];
    $('removeEpisode').classList.toggle('hidden',isAsset);
    document.querySelectorAll('[data-fit]').forEach(button => button.classList.toggle('active',button.dataset.fit===clip.fit));
  }
  function selectClip(id, seek = true) {
    stopPreview(); state.selectedClip=id; renderEditor();
    const segment=layout(currentDrama().clips).find(entry=>entry.clip.id===id);
    if (seek && segment) { state.playMs=segment.start; updatePlayhead(); previewAt(state.playMs,false).catch(error=>toast(error.message,true)); }
  }
  function updateClip(patch) { const clip=selectedClip(); if (!clip) return; changed(()=>Object.assign(clip,patch)); previewAt(state.playMs,false).catch(()=>{}); }
  function updatePlayhead() { $('playTime').textContent=time(state.playMs); $('playhead').value=state.playMs; }
  function stopPreview() { state.playing=false; previewGeneration++; cancelAnimationFrame(state.playTick); $('previewA').pause(); $('previewB').pause(); $('playPreview').textContent='▶'; $('playPreview').setAttribute('aria-label','播放时间线'); }
  let previewGeneration=0;
  async function previewAt(position, play) {
    const generation=++previewGeneration; const drama=currentDrama(); if (!drama) return;
    const segments=layout(drama.clips).filter(segment=>position>=segment.start && position<segment.end);
    if (!segments.length) return;
    if (demo) { $('previewMessage').textContent=`模拟预览 · ${sourceTitle(state.sources[segments[0].clip.source_id])}`; return; }
    const sources=await Promise.all(segments.map(segment=>ensureSource(segment.clip.source_id)));
    if (generation!==previewGeneration) return;
    const videos=[$('previewA'),$('previewB')]; const both=segments.length===2;
    for (let i=0;i<2;i++) {
      const video=videos[i]; const segment=segments[i];
      if (!segment) { video.pause(); video.style.opacity=0; continue; }
      const source=sources[i]; const url=source.preview_url;
      if(source.expires_at)$('previewExpiry').textContent=` 当前链接有效至 ${new Date(expiryTime(source.expires_at)).toLocaleTimeString('zh-CN')}，届时自动续签。`;
      if (!url) { $('previewPlaceholder').classList.remove('hidden'); $('previewMessage').textContent=source.status==='ready'?'此来源暂未提供浏览器预览':'媒体校验中，完成后可预览'; video.pause(); video.style.opacity=0; continue; }
      if (video.dataset.sourceId!==segment.clip.source_id || video.dataset.previewUrl!==url) { video.src=url; video.dataset.sourceId=segment.clip.source_id; video.dataset.previewUrl=url; }
      const localTime=toFrame(segment.clip.start_ms)/30+(position-segment.start)/1000;
      if (!Number.isFinite(video.currentTime) || Math.abs(video.currentTime-localTime)>.22 || !play) { try { video.currentTime=localTime; } catch (_) {} }
      const fade=both?Math.min(1,Math.max(0,(position-segments[1].start)/FADE_MS)):1;
      video.style.opacity=both?(i===0?1:fade):1; video.style.objectFit=segment.clip.fit;
      video.volume=segment.clip.mute?0:(both?(i===0?1-fade:fade):1); video.muted=segment.clip.mute;
      $('previewPlaceholder').classList.add('hidden');
      if (play) video.play().catch(()=>{}); else video.pause();
    }
  }
  async function playPreview() {
    if(state.playing) { stopPreview(); return; }
    const drama=currentDrama(); if(!drama||!drama.clips.length) return toast('先添加剧集再预览');
    const errors=validateClips(drama.clips,state.sources); if(errors.length) return toast(errors[0],true);
    if(state.playMs>=totalDuration(drama.clips)) state.playMs=0;
    state.playing=true; state.playOrigin=performance.now()-state.playMs; $('playPreview').textContent='Ⅱ'; $('playPreview').setAttribute('aria-label','暂停时间线');
    let lastDraw=-1000;
    const tick=now=> { if(!state.playing)return; state.playMs=Math.min(totalDuration(drama.clips),now-state.playOrigin); updatePlayhead(); if(now-lastDraw>85){lastDraw=now;previewAt(state.playMs,true).catch(error=>{stopPreview();toast(error.message,true);});} if(state.playMs>=totalDuration(drama.clips)){stopPreview();return;} state.playTick=requestAnimationFrame(tick); };
    state.playTick=requestAnimationFrame(tick);
  }
  function serializeProject(project=state.project) { return { name:project.name, asset_id:project.asset_id, dramas:clone(project.dramas).map(drama=>({...drama,app_id:String(drama.app_id),content_id:String(drama.content_id)})) }; }
  function canonical(value){if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';return JSON.stringify(value);}
  async function saveProject(explicit=true) {
    clearTimeout(state.saveTimer);
    if(demo){state.dirty=false;$('saveStatus').textContent='模拟草稿 · 仅本页';if(explicit)toast('模拟草稿已保留在本页');return state.project;}
    if(state.saving){await state.saving;if(state.dirty)return saveProject(explicit);return state.project;}
    if(!state.dirty&&state.project.id){if(explicit)toast('草稿已是最新版本');return state.project;}
    let revision=state.revision;let data=serializeProject();const projectId=state.project.id;const savingProject=state.project;
    if(!projectId){if(!state.pendingCreate)state.pendingCreate={revision,data,operation_id:state.operationId||(state.operationId=uid())};revision=state.pendingCreate.revision;data=state.pendingCreate.data;}
    $('saveStatus').textContent='正在保存…';
    state.saving=(async()=>{
      let saved;
      try{saved=item(await api(projectId?`${ROOT}/projects/${encodeURIComponent(projectId)}`:`${ROOT}/projects`,{method:projectId?'PATCH':'POST',body:projectId?{...data,expected_version:state.project.version}:{...data,operation_id:state.pendingCreate.operation_id}}));}
      catch(error){if(projectId&&(error.status===409||!error.status)){const remote=item(await api(`${ROOT}/projects/${encodeURIComponent(projectId)}`));if(canonical(serializeProject(remote))===canonical(data))saved=remote;else throw error;}else throw error;}
      if(state.project!==savingProject)return saved;
      state.project.id=saved.id;state.project.version=saved.version;state.operationId=null;state.pendingCreate=null;state.conflicted=false;$('saveCopy').classList.add('hidden');
      if(state.revision===revision)state.dirty=false;
      $('saveStatus').textContent=state.dirty?'有未保存修改':`已自动保存 · v${saved.version}`;
       loadProjects().catch(error=>toast(`草稿已保存，但项目列表刷新失败：${error.message}`,true));if(explicit)toast('草稿已保存');return state.project;
    })();
    try{return await state.saving;}catch(error){if(error.status===409){state.conflicted=true;$('saveCopy').classList.remove('hidden');}$('saveStatus').textContent=error.status===409?'版本冲突 · 可另存副本保留修改':'保存失败 · 可重试';throw error;}finally{state.saving=null;}
  }
  async function loadProjects(){
    if(demo)return;if(state.projectsLoading)return state.projectsLoading;
    const request=(async()=>{const response=await api(`${ROOT}/projects`);state.projects=response.items||[];renderProjectPicker();})();state.projectsLoading=request;
    try{return await request;}finally{if(state.projectsLoading===request)state.projectsLoading=null;}
  }
  function renderProjectPicker(){$('projectPicker').innerHTML='<option value="">打开项目…</option>'+state.projects.map(project=>`<option value="${esc(project.id)}">${esc(project.name)}</option>`).join('');}
  async function flushDraft(){
    if(demo)return;
    // Finish a queued save and edits made while that request was in flight before leaving this project.
    if(state.saving)await state.saving;
    while(state.dirty)await saveProject(false);
  }
  async function openProject(id){
    if(!id||state.renderSubmitting)return;cancelPendingAdd();const requestSeq=++state.projectLoadSeq;state.projectOpening=requestSeq;$('projectPicker').setAttribute('aria-busy','true');$('saveStatus').textContent='正在打开项目…';syncEditorLocks();
    try{
      await flushDraft();if(requestSeq!==state.projectLoadSeq)return;stopPreview();
      const project=demo?clone(state.projects.find(entry=>entry.id===id)):item(await api(`${ROOT}/projects/${encodeURIComponent(id)}`));
      if(!project||requestSeq!==state.projectLoadSeq)return;
      state.project=project;state.activeDrama=0;state.selectedClip=null;state.undo=[];state.dirty=false;state.operationId=null;state.pendingCreate=null;state.renderOperation=null;state.conflicted=false;state.sourceLoadFailed=false;
      const ids=[...new Set([project.asset_id,...project.dramas.flatMap(drama=>drama.clips.map(clip=>clip.source_id))].filter(Boolean))];
      const missing=ids.filter(sourceId=>!durationOf(state.sources[sourceId]));state.sourceLoading=missing.length>0;state.projectOpening=0;
      $('saveStatus').textContent=missing.length?`项目已打开，正在加载媒体信息 0/${missing.length}…`:`已保存 · v${project.version}`;renderEditor();setView('editor');
      let loaded=0;const errors=[];
      await mapLimit(missing,3,async sourceId=>{if(state.project!==project||requestSeq!==state.projectLoadSeq)return;try{await ensureSource(sourceId);}catch(error){errors.push(error.message);}loaded++;if(state.project===project&&requestSeq===state.projectLoadSeq){$('saveStatus').textContent=`项目已打开，正在加载媒体信息 ${loaded}/${missing.length}…`;}});
      if(state.project!==project||requestSeq!==state.projectLoadSeq)return;
      state.sourceLoading=false;state.sourceLoadFailed=missing.some(sourceId=>!durationOf(state.sources[sourceId]));
      $('saveStatus').textContent=state.sourceLoadFailed?'媒体信息未载入，请点击重试载入':demo?'模拟草稿 · 仅本页':`已保存 · v${project.version}`;renderEditor();if(errors.length)toast(`项目已打开，媒体信息加载失败：${errors[0]}`,true);
    }catch(error){if(requestSeq===state.projectLoadSeq){
      // The previous project can remain visible if opening its successor fails.
      // Its old source-loader is obsolete, so expose an explicit retry instead of
      // leaving controls locked behind that abandoned loader's generation.
      const retainedIds=[state.project.asset_id,...state.project.dramas.flatMap(drama=>drama.clips.map(clip=>clip.source_id))].filter(Boolean);
      state.sourceLoading=false;state.sourceLoadFailed=retainedIds.some(sourceId=>!durationOf(state.sources[sourceId]));
      $('saveStatus').textContent=`打开项目失败：${error.message}${state.sourceLoadFailed?'；当前项目媒体信息未载入，请点击重试载入':''}`;
    }throw error;}
    finally{if(requestSeq===state.projectLoadSeq){state.projectOpening=0;$('projectPicker').removeAttribute('aria-busy');syncEditorLocks();}}
  }
  async function renderProject(){
    const errors=state.project.dramas.flatMap(drama=>validateClips(drama.clips,state.sources).map(error=>`${drama.title}：${error}`));
    if(errors.length)return toast(errors[0],true);
    if(demo){state.tasks=state.project.dramas.map((drama,index)=>({id:`demo-task-${index}`,drama_title:drama.title,status:'queued',stage:'模拟排队',progress:0,created_at:new Date().toISOString()}));renderTasks();setView('tasks');return toast('模拟制作任务已生成，不会调用 GPU');}
    if(editingLocked())return toast('项目媒体信息尚未就绪，请稍候');state.renderSubmitting=true;syncEditorLocks();
    const project=state.project;
    try{await flushDraft();if(project!==state.project)return;const operation=state.renderOperation||(state.renderOperation=uid());await api(`${ROOT}/projects/${encodeURIComponent(project.id)}/render`,{method:'POST',body:{operation_id:operation,expected_version:project.version}});toast('已加入制作队列，每部剧分别出片');setView('tasks');await loadTasks();}finally{state.renderSubmitting=false;renderEditor();}
  }
  const assetLoads=new Map();
  async function loadAssets(append=false){
    if(demo){renderAssets();return;}const query=$('assetSearch').value,offset=append?state.assets.length:0,key=`${query}:${offset}`;
    if(assetLoads.has(key))return assetLoads.get(key);const sequence=++state.assetsLoadSeq;
    const request=(async()=>{const response=await api(`${ROOT}/assets?q=${encodeURIComponent(query)}&status=all&limit=60&offset=${offset}`);if(sequence!==state.assetsLoadSeq||query!==$('assetSearch').value)return;state.assets=append?state.assets.concat(response.items||[]):response.items||[];state.assetsTotal=response.total||state.assets.length;rememberSources(state.assets);renderAssets();})();assetLoads.set(key,request);
    try{return await request;}finally{if(assetLoads.get(key)===request)assetLoads.delete(key);}
  }
  const statusLabel=status=>({uploading:'上传中',uploaded:'待校验',probing:'媒体校验中',ready:'可剪辑',failed:'失败',deleted:'已删除',unprobed:'待校验',queued:'排队中',queued_gpu:'GPU 排队中',submitting:'正在提交',running:'处理中',recovery_required:'等待恢复',resume_queued:'等待核实恢复',preparing:'准备素材',starting:'准备制作',connecting:'连接制作服务',waiting_gpu_capacity:'等待制作资源',waiting_media:'等待预览处理',downloading:'下载素材',normalizing:'处理片段',concatenating:'拼接制作',rendering:'制作中',rendering_random:'制作中',rendering_intro:'制作片头',waiting_cover:'等待封面',removing_bgm:'处理背景音乐',uploading_output:'上传成片',verifying:'校验成片',verification:'校验成片',prefetching:'预下载素材',prefetched:'素材已就绪',completed:'制作完成',succeeded:'制作完成',unknown:'正在核实结果',cancelled:'已取消'}[status]||(typeof status==='string'&&/[\u3400-\u9fff]/.test(status)?status:status?'处理中':'等待状态'));
  function taskProgressText(task){
    const stage=task.stage||task.status,label=statusLabel(stage),progress=task.progress;
    if(Number.isFinite(progress)&&progress>=0)return `${label} · ${Math.round(Math.min(100,progress<=1?progress*100:progress))}%`;
    if(!progress||typeof progress!=='object'||Array.isArray(progress))return label;
    const number=value=>Number.isFinite(value)&&value>=0?value:null;
    const pick=(...keys)=>{for(const key of keys){const value=number(progress[key]);if(value!==null)return value;}return null;};
    const detail=[];
    const elapsed=pick('out_time_seconds'),duration=pick('duration_seconds');
    if(elapsed!==null&&duration>0)detail.push(`当前片段 ${seconds(elapsed*1000)} / ${seconds(duration*1000)} 秒`);
    if(['downloading','prefetching'].includes(stage)){
      const done=pick('download_bytes','downloaded_bytes','bytes_done'),total=pick('total_bytes','bytes_total');
      if(done!==null)detail.push(`已下载 ${bytes(done)}${total>0?` / ${bytes(total)}`:''}`);
    }
    if(['uploading','uploading_output'].includes(stage)){
      const done=pick('uploaded_bytes','bytes_done'),total=pick('upload_bytes_total','total_bytes','bytes_total');
      if(done!==null)detail.push(`已上传 ${bytes(done)}${total>0?` / ${bytes(total)}`:''}`);
    }
    const steps=pick('completed_steps'),totalSteps=pick('total_steps');
    if(steps!==null)detail.push(`已完成 ${Math.floor(steps)}${totalSteps>0?` / ${Math.floor(totalSteps)}`:''} 步`);
    // Runtime counters describe only the current operation and can restart for
    // every clip/file. Do not convert them (or metrics.percent) to job percent.
    return detail.length?`${label} · 当前阶段：${detail.join('；')}`:label;
  }
  function renderAssets(){
    $('assetCount').textContent=state.assetsTotal||state.assets.length;
    $('assetGrid').innerHTML=state.assets.map(asset=>`<article class="asset-card"><div class="asset-thumb">${asset.thumbnail_url?`<img src="${esc(asset.thumbnail_url)}" alt="" loading="lazy">`:'<span class="asset-symbol">▧</span>'}<span class="duration">${durationOf(asset)?`${seconds(durationOf(asset))} 秒`:esc(statusLabel(asset.status))}</span></div><div class="asset-meta"><strong title="${esc(asset.name)}">${esc(asset.name)}</strong><p>${esc(statusLabel(asset.status))} · ${bytes(asset.size_bytes||0)}</p>${asset.error_message?`<p class="danger">${esc(asset.error_message)}</p>`:''}<div class="asset-actions"><button class="button" data-preview-asset="${esc(asset.id)}" ${asset.status!=='ready'?'disabled':''}>预览</button><button class="button" data-pick-asset="${esc(asset.id)}" ${asset.status!=='ready'?'disabled':''}>用于剪辑</button>${asset.can_manage&&['uploaded','probing','failed'].includes(asset.status)?`<button class="button" data-probe-asset="${esc(asset.id)}">核实媒体校验</button>`:''}${asset.can_manage?`<button class="text-button" data-rename-asset="${esc(asset.id)}">改名</button><button class="text-button danger" data-delete-asset="${esc(asset.id)}">删除</button>`:''}</div></div></article>`).join('')||'<div class="empty">暂无素材，上传一个视频开始创作。</div>';
    $('assetPicker').innerHTML=state.assets.filter(asset=>asset.status==='ready').map(asset=>`<button data-pick-asset="${esc(asset.id)}"><strong>▧ ${esc(asset.name)}</strong><small>${seconds(durationOf(asset))} 秒 · ${asset.metadata.width} × ${asset.metadata.height}</small></button>`).join('')||'<div class="empty">暂无通过校验的素材，请先上传。</div>';
    $('loadMoreAssets').classList.toggle('hidden',state.assets.length>=state.assetsTotal);
  }
  function pickAsset(id){const asset=state.assets.find(entry=>entry.id===id);if(!asset||asset.status!=='ready')return;const old=state.project.asset_id;if(old&&old!==id&&state.project.dramas.some(drama=>drama.clips.some(clip=>clip.source_id===old)))return toast('当前时间线已使用原素材，请先删除对应素材片段或新建项目',true);changed(()=>{state.project.asset_id=id;});$('assetDialog').close();setView('editor');}
  async function previewAsset(id){const source=await ensureSource(id,true);if(demo){toast('模拟素材不含真实视频文件');return;}openMedia(source.name,source.preview_url,source.preview_expires_at||source.expires_at);}
  async function probeAsset(id){
    if(demo)return toast('模拟素材已通过校验');const source=await ensureSource(id,true);if(source.status==='ready'){await loadAssets();return toast('素材已通过媒体校验');}
    if(source.probe_task_id){const task=item(await api(`${ROOT}/tasks/${encodeURIComponent(source.probe_task_id)}`));if(['failed','unknown','recovery_required'].includes(task.status)){trackProbeRecovery(id,task.id);await resumeTaskRequest(task.id);toast('正在核实并恢复原校验任务');}else toast('媒体校验正在排队或处理中，请稍后刷新');}
    else{await api(`${ROOT}/catalog/probe`,{method:'POST',body:{source_ids:[id]}});toast('已加入媒体校验队列');}await loadAssets();
  }
  function expiryTime(value){return typeof value==='number'?(value<1e12?value*1000:value):new Date(value).getTime();}
  function openMedia(title,url,expires){if(!url)return toast('预览链接暂不可用，请稍后刷新重试',true);$('mediaTitle').textContent=title||'视频预览';$('mediaPlayer').src=url;$('mediaExpiry').textContent=expires?`链接有效期至 ${new Date(expiryTime(expires)).toLocaleString('zh-CN')}。过期后重新打开即可刷新。`:'临时签名链接，过期后请重新打开。';$('mediaDialog').showModal();}
  async function loadTasks(){
    if(demo){renderTasks();return;}if(state.tasksLoading)return state.tasksLoading;
    const request=(async()=>{const response=await api(`${ROOT}/tasks`);state.tasks=response.items||[];renderTasks();})();state.tasksLoading=request;
    try{return await request;}finally{if(state.tasksLoading===request)state.tasksLoading=null;}
  }
  const materialUploadLabels={queued:'等待上传',copying:'正在准备 COS 成片',submitting:'正在提交素材库',verifying:'正在核实素材 ID',succeeded:'已上传',failed:'上传失败',unknown_outcome:'结果待核实'};
  function materialUploadView(task){
    if(!task.output||!['completed','succeeded'].includes(task.status))return {detail:'',action:''};
    const pending=state.materialUploadSubmitting.has(String(task.id));
    const upload=task.material_upload;
    if(!upload && !pending)return {detail:'',action:`<button class="button small" data-upload-material-task="${esc(task.id)}">上传至素材库</button>`};
    const status=pending&&!upload?'queued':upload&&upload.status;
    const label=materialUploadLabels[status]||'状态待核实';
    const confirmed=status==='succeeded'&&upload&&upload.material_id;
    const message=confirmed?` · 素材 ID：${esc(upload.material_id)}`:'';
    const error=upload&&(upload.error||upload.error_message);
    const detail=`<p class="material-upload ${esc(status||'unknown_outcome')}" role="status">素材库：${esc(label)}${message}${error?` · ${esc(error)}`:''}</p>`;
    const action=!pending&&['failed','unknown_outcome'].includes(status)?`<button class="button small" data-refresh-material-task="${esc(task.id)}">刷新状态</button>`:'';
    return {detail,action};
  }
  function renderTasks(){
    $('taskCount').textContent=state.tasks.length;
    $('taskList').innerHTML=state.tasks.map(task=>{const material=materialUploadView(task);return `<article class="task-card"><div class="task-icon">▷</div><div class="task-info"><strong>${esc(task.drama_title||task.title||task.id)}</strong><p>${esc(task.id)} · ${task.created_at?new Date(expiryTime(task.created_at)).toLocaleString('zh-CN'):''}</p>${task.error_message?`<p class="danger">${esc(task.error_message)}</p>`:''}${task.output&&task.output.metadata?`<p>720 × 1280 · ${seconds(task.output.metadata.duration_ms)} 秒 · MP4</p>`:''}${material.detail}</div><div><div class="task-status ${esc(task.status)}">${esc(statusLabel(task.status))}</div><p class="micro">${esc(taskProgressText(task))}</p></div><div class="task-actions">${material.action}${task.output?`<button class="button small" data-preview-task="${esc(task.id)}">预览</button><button class="button small" data-download-task="${esc(task.id)}">下载</button>`:''}${['failed','unknown','recovery_required'].includes(task.status)?`<button class="button small" data-resume-task="${esc(task.id)}">核实并重试</button>`:''}</div></article>`;}).join('')||'<div class="empty">还没有制作任务。完成剪辑后，点击“制作成片”。</div>';
  }
  async function uploadMaterialTask(id){
    if(demo)return toast('模拟预览不会上传至素材库');
    const task=state.tasks.find(entry=>String(entry.id)===String(id));
    if(!task||!task.output||!['completed','succeeded'].includes(task.status)||task.material_upload||state.materialUploadSubmitting.has(String(id)))return;
    state.materialUploadSubmitting.add(String(id));renderTasks();
    try{
      const upload=item(await api(`${ROOT}/tasks/${encodeURIComponent(id)}/material-source`,{method:'POST',body:{}}));
      if(!upload||!upload.status)throw new Error('提交回执无效，结果需要核实');
      const current=state.tasks.find(entry=>String(entry.id)===String(id));if(current)current.material_upload=upload;
      if(upload.status==='succeeded'&&upload.material_id)toast(`素材已入库，素材 ID：${upload.material_id}`);
      else if(upload.status==='failed'||upload.status==='unknown_outcome')toast(upload.error||'入库结果需要核实，请刷新任务查看状态',true);
      else toast('已提交素材库，正在处理');
    }catch(error){
      const current=state.tasks.find(entry=>String(entry.id)===String(id));
      if(current&&(!error.status||error.status>=500))current.material_upload={status:'unknown_outcome',error:'提交回执未确认，请刷新任务核实'};
      toast(error.message,true);
    }finally{state.materialUploadSubmitting.delete(String(id));renderTasks();}
  }
  async function taskOutput(id,download){if(demo)return toast('模拟任务不会生成真实成片');const task=item(await api(`${ROOT}/tasks/${encodeURIComponent(id)}`));if(!task.output)return toast('成片尚未就绪');if(download){const url=task.output.download_url;if(!url)return toast('下载链接暂不可用',true);const link=document.createElement('a');link.href=url;link.rel='noopener noreferrer';link.target='_blank';link.download='';link.click();toast(task.output.expires_at?`下载链接有效至 ${new Date(expiryTime(task.output.expires_at)).toLocaleString('zh-CN')}`:'已打开临时下载链接');}else openMedia(task.drama_title,task.output.preview_url,task.output.expires_at||task.output.preview_expires_at);}
  function setCatalogStatus(message, error=false, busy=false) {
    $('catalogStatus').textContent=message;$('catalogStatus').classList.toggle('error',error);$('catalogStatus').setAttribute('aria-busy',String(busy));
  }
  function catalogCurrent(session, sequence=state.catalogLoadSeq) { return $('dramaDialog').open && session===state.catalogSessionSeq && sequence===state.catalogLoadSeq; }
  function pendingCurrent(intent) {
    const drama=state.catalog[state.selectedCatalog];
    return !!(intent && state.pendingAdd===intent && !intent.completed && $('dramaDialog').open && intent.session===state.catalogSessionSeq && intent.project===state.project && intent.projectSeq===state.projectLoadSeq && drama && String(drama.app_id)===String(intent.drama.app_id) && String(drama.content_id)===String(intent.drama.content_id));
  }
  function cancelPendingAdd(message='已取消自动添加。已排队的媒体校验会继续，可重新选择后添加。') {
    if(!state.pendingAdd)return;state.pendingAdd=null;renderEpisodes();setCatalogStatus(message);
  }
  function closeCatalogSession() { cancelPendingAdd();state.catalogSessionSeq++;state.catalogLoadSeq++; }
  function renderProducts() {
    const selected=$('productPicker').value;
    $('productPicker').innerHTML='<option value="">选择产品</option>'+(state.catalogProducts||[]).map(product=>`<option value="${esc(product.app_id||product.id)}">${esc(product.label||product.app||product.name||product.app_name||product.title||product.app_id)}</option>`).join('');
    if((state.catalogProducts||[]).some(product=>String(product.app_id||product.id)===selected))$('productPicker').value=selected;
  }
  async function openCatalog(button=$('addDrama')) {
    if(editingLocked())return toast('项目媒体信息尚未载入，请稍候');
    if(!$('dramaDialog').open){state.catalogSessionSeq++;$('dramaDialog').showModal();setCatalogStatus(`最多 ${state.config.max_dramas} 部短剧；选择剧集后可自动校验并添加。`);}
    const session=state.catalogSessionSeq;
    if(demo){$('productPicker').innerHTML='<option value="demo-product">模拟产品</option>';await searchDramas();return;}
    if(state.catalogProducts){renderProducts();$('productPicker').disabled=false;return;}
    setCatalogStatus('正在加载产品…',false,true);$('productPicker').disabled=true;
    return withBusy(`catalog-open:${session}`,button,'加载中…',async()=>{try{
      const response=await api(`${ROOT}/catalog/products`);state.catalogProducts=response.items||[];
      if(session!==state.catalogSessionSeq||!$('dramaDialog').open)return;renderProducts();setCatalogStatus('请选择产品，输入完整剧 ID 或至少 2 字的剧名前缀。');
    }catch(error){if(session===state.catalogSessionSeq&&$('dramaDialog').open)setCatalogStatus(`产品加载失败：${error.message}。关闭后重新打开可重试。`,true);throw error;}
    finally{if(session===state.catalogSessionSeq)$('productPicker').disabled=false;}});
  }
  async function searchDramas() {
    const appId=$('productPicker').value,query=$('dramaSearch').value.trim(),session=state.catalogSessionSeq,key=`catalog-search:${session}:${appId}:${query}`;
    if(state.catalogSearch&&state.catalogSearch.key===key&&state.catalogSearch.sequence===state.catalogLoadSeq)return state.catalogSearch.promise;
    cancelPendingAdd('已更换搜索条件，本次自动添加已取消。');
    const requestSeq=++state.catalogLoadSeq;state.selectedCatalog=null;state.catalog=[];state.episodes=[];renderEpisodes();
    $('catalogEpisodes').innerHTML='<div class="empty">选择左侧短剧，加载剧集。</div>';
    if(!appId){$('catalogDramas').innerHTML='<div class="empty">请先选择产品。</div>';setCatalogStatus('请先选择产品。');return;}
    if(!demo&&(!query||(Array.from(query).length<2&&!/^\d+$/.test(query)))){$('catalogDramas').innerHTML='<div class="empty">请输入完整剧 ID，或至少 2 字的剧名前缀后搜索。</div>';setCatalogStatus('请输入完整剧 ID，或至少 2 字的剧名前缀。');return;}
    if(query.length>100){setCatalogStatus('搜索内容最多 100 字。',true);return;}
    $('catalogDramas').innerHTML='<div class="empty">正在查找短剧…</div>';setCatalogStatus('正在查找短剧…',false,true);
    const request=withBusy(`${key}:${requestSeq}`,$('searchDramas'),'搜索中…',async()=>{
      try{
        const dramas=demo?[{app_id:appId,content_id:'demo-101',title:'风起时，遇见你'},{app_id:appId,content_id:'demo-102',title:'她的第二人生'}]:(await api(`${ROOT}/catalog/dramas?app_id=${encodeURIComponent(appId)}&q=${encodeURIComponent(query)}`)).items||[];
        if(!catalogCurrent(session,requestSeq))return;state.catalog=dramas;renderCatalog();setCatalogStatus(dramas.length?`找到 ${dramas.length} 部短剧，请选择所需剧集。`:'未找到短剧，请调整剧名或 ID。');
      }catch(error){if(catalogCurrent(session,requestSeq)){$('catalogDramas').innerHTML='<div class="empty">搜索失败，请重试。</div>';setCatalogStatus(`搜索失败：${error.message}`,true);}throw error;}
    });state.catalogSearch={key,sequence:requestSeq,promise:request};
    try{return await request;}finally{if(state.catalogSearch&&state.catalogSearch.promise===request)state.catalogSearch=null;}
  }
  function renderCatalog(){$('catalogDramas').innerHTML=state.catalog.map((drama,index)=>`<button data-catalog-index="${index}" class="${state.selectedCatalog===index?'active':''}"><strong>${esc(drama.title||drama.name)}</strong><small>ID ${esc(drama.content_id)}</small></button>`).join('')||'<div class="empty">未找到短剧</div>';}
  async function selectCatalog(index) {
    const drama=state.catalog[index];if(!drama)return;
    cancelPendingAdd('已切换短剧，本次自动添加已取消。');
    const session=state.catalogSessionSeq,requestSeq=++state.catalogLoadSeq;state.selectedCatalog=index;state.episodes=[];renderCatalog();renderEpisodes();
    $('catalogEpisodes').innerHTML='<div class="empty">正在加载剧集…</div>';setCatalogStatus(`正在加载“${drama.title||drama.name}”的剧集…`,false,true);
    try{
      const episodes=demo?[1,2,3,4].map(number=>({id:`${drama.content_id}-ep-${number}`,source_id:`${drama.content_id}-ep-${number}`,kind:'episode',name:`第 ${number} 集`,episode_number:number,app_id:drama.app_id,content_id:drama.content_id,status:'ready',metadata:{duration_ms:45000+number*7000,width:720,height:1280,has_audio:true}})):(await api(`${ROOT}/catalog/episodes?app_id=${encodeURIComponent(drama.app_id)}&content_id=${encodeURIComponent(drama.content_id)}`)).items||[];
      if(!catalogCurrent(session,requestSeq))return;state.episodes=episodes;rememberSources(episodes);renderEpisodes();setCatalogStatus(episodes.length?'勾选需要的剧集后点击添加；未就绪剧集将自动校验。':'该短剧暂无可用剧集。');
    }catch(error){if(catalogCurrent(session,requestSeq)){$('catalogEpisodes').innerHTML='<div class="empty">剧集加载失败，点击短剧可重试。</div>';setCatalogStatus(`剧集加载失败：${error.message}`,true);}throw error;}
  }
  function renderEpisodes() {
    const pending=state.pendingAdd,waiting=pendingCurrent(pending);
    const selected=new Set(waiting?pending.ids:Array.from(document.querySelectorAll('.episode-select:checked')).map(input=>input.value));
    const scrollTop=$('catalogEpisodes').scrollTop;
    $('catalogEpisodes').innerHTML=state.episodes.map(source=>{const progress=probeDisplay(source);return `<label class="episode-option"><input class="episode-select" type="checkbox" value="${esc(source.source_id||source.id)}" ${selected.has(source.source_id||source.id)?'checked':''} ${waiting?'disabled':''}><span>${esc(sourceTitle(source))}<small class="episode-status">${esc(progress.text)}</small>${progress.percent===null?'':`<progress class="episode-download-progress" max="100" value="${progress.percent}" aria-label="${esc(sourceTitle(source))} 下载进度 ${progress.percent}%"></progress>`}</span></label>`;}).join('')||'<div class="empty">暂无可用剧集</div>';
    $('catalogEpisodes').scrollTop=scrollTop;
    $('addEpisodes').disabled=waiting||!state.episodes.length;$('addEpisodes').setAttribute('aria-busy',String(waiting));
    $('cancelAddEpisodes').classList.toggle('hidden',!waiting);
    if(!waiting)$('addEpisodes').textContent='添加所选剧集';
  }
  function sourceReady(id){const source=state.sources[id];return !!(source&&source.status==='ready'&&durationOf(source));}
  function pendingProgress(intent) {
    if(!pendingCurrent(intent))return;
    const ready=intent.ids.filter(sourceReady).length;
    $('addEpisodes').disabled=true;$('addEpisodes').setAttribute('aria-busy','true');
    $('addEpisodes').textContent=intent.phase==='preparing'?'正在准备校验…':`校验中 ${ready}/${intent.ids.length}`;
    const pending=intent.ids.map(id=>state.sources[id]||{}).filter(source=>source.status!=='ready');
    const downloading=pending.filter(source=>source.probe_progress?.stage==='downloading').length;
    const queued=pending.filter(source=>['queued','queued_gpu'].includes(source.probe_progress?.status)||['queued','waiting_gpu_capacity','waiting_media'].includes(source.probe_progress?.stage)).length;
    setCatalogStatus(`媒体校验${intent.recovering?'恢复':''}：已就绪 ${ready}/${intent.ids.length}${downloading?`，下载中 ${downloading} 集`:''}${queued?`，排队 ${queued} 集`:''}。${intent.phase==='preparing'?'正在准备所选剧集。':''}完成后自动添加，可取消等待。`,false,true);
  }
  function failPendingAdd(intent,message) {
    if(!pendingCurrent(intent))return;state.pendingAdd=null;renderEpisodes();setCatalogStatus(`${message}。可点击“添加所选剧集”核实并重试。`,true);
  }
  function finishPendingAdd(intent) {
    if(!pendingCurrent(intent)||intent.phase==='preparing'||!intent.ids.every(sourceReady)||editingLocked())return false;
    const drama=intent.drama,existingDrama=state.project.dramas.find(entry=>String(entry.app_id)===String(drama.app_id)&&String(entry.content_id)===String(drama.content_id));
    if(!existingDrama&&state.project.dramas.length>=state.config.max_dramas){failPendingAdd(intent,`每批最多 ${state.config.max_dramas} 部短剧`);return false;}
    const existingSources=new Set((existingDrama&&existingDrama.clips||[]).map(clip=>clip.source_id)),newIds=intent.ids.filter(id=>!existingSources.has(id));
    if((existingDrama&&existingDrama.clips.length||0)+newIds.length>300){failPendingAdd(intent,'每部短剧最多 300 个片段，请减少所选剧集');return false;}
    intent.completed=true;state.pendingAdd=null;renderEpisodes();
    if(newIds.length)changed(()=>{let target=existingDrama;if(!target){target={app_id:drama.app_id,content_id:drama.content_id,title:drama.title||drama.name,clips:[]};state.project.dramas.push(target);}newIds.forEach(id=>target.clips.push(newClip(state.sources[id])));normalizeLast(target.clips);state.activeDrama=state.project.dramas.indexOf(target);});
    $('dramaDialog').close();toast(newIds.length?'已添加所选剧集，可在时间线上切割':'所选剧集已在项目中，无需重复添加');return true;
  }
  const unknownProbeSubmissions=new Set(),probeSubmissions=new Map(),taskResumeRequests=new Map(),hiddenProbeTasks=new Set();
  function resumeTaskRequest(taskId){
    if(taskResumeRequests.has(taskId))return taskResumeRequests.get(taskId);
    const request=api(`${ROOT}/tasks/${encodeURIComponent(taskId)}/resume`,{method:'POST',body:{}});taskResumeRequests.set(taskId,request);
    return request.finally(()=>{if(taskResumeRequests.get(taskId)===request)taskResumeRequests.delete(taskId);});
  }
  function trackProbeRecovery(sourceId,taskId){state.probeRecoveries.set(sourceId,{taskId});}
  async function prepareEpisodeSources(ids,intent) {
    const pending=[];
    await mapLimit(ids,3,async id=>{
      if(!pendingCurrent(intent)||sourceReady(id))return;
      let source=state.sources[id];
      try{
        const submitting=probeSubmissions.get(id);
        if(submitting){await submitting.promise;if(!pendingCurrent(intent))return;source=state.sources[id];if(sourceReady(id))return;}
        if(unknownProbeSubmissions.has(id)){
          source=await ensureSource(id,true);
          if(!pendingCurrent(intent))return;
          if(sourceReady(id)){unknownProbeSubmissions.delete(id);return;}
          if(!source.probe_task_id)throw new Error('前次校验提交结果尚在核实，暂不重复提交');
          unknownProbeSubmissions.delete(id);
        }
        if(!source||!source.probe_task_id){pending.push(id);return;}
        // Hidden shared tasks remain source-observable; readable owner tasks can
        // expose an unknown result even while their source is still "probing".
        if(source.status==='probing'&&hiddenProbeTasks.has(source.probe_task_id))return;
        let task;
        try{task=item(await api(`${ROOT}/tasks/${encodeURIComponent(source.probe_task_id)}`));}
        catch(error){if(error.status===403||error.status===404){if(source.status==='probing'){hiddenProbeTasks.add(source.probe_task_id);return;}throw new Error('无法核实此共享校验的原任务，请联系发起人或管理员；不会创建替代任务');}throw error;}
        if(!pendingCurrent(intent))return;
        if(['failed','unknown','recovery_required'].includes(task.status)){
          trackProbeRecovery(id,task.id);intent.recovering=true;
          try{await resumeTaskRequest(task.id);}
          catch(error){if(error.status!==409||error.code!=='task_not_resumable')throw error;const latest=item(await api(`${ROOT}/tasks/${encodeURIComponent(task.id)}`));if(['failed','unknown','recovery_required'].includes(latest.status))throw error;}
        }else if(task.status==='completed')await ensureSource(id,true);
        else if(task.status==='cancelled')throw new Error('原媒体校验已取消，请联系管理员核实');
      }catch(error){throw new Error(`${sourceTitle(source)}：${error.message}`);}
    });
    for(let offset=0;offset<pending.length&&pendingCurrent(intent);offset+=100){
      const batch=pending.slice(offset,offset+100);
      const submission={promise:null};batch.forEach(id=>probeSubmissions.set(id,submission));
      submission.promise=(async()=>{
        try{
          const result=await api(`${ROOT}/catalog/probe`,{method:'POST',body:{source_ids:batch}}),items=Array.isArray(result.items)?result.items:[];
          const accepted=items.filter(source=>batch.includes(source.source_id||source.id));rememberSources(accepted);
          if(batch.some(id=>!accepted.some(source=>(source.source_id||source.id)===id&&(source.probe_task_id||(source.status==='ready'&&durationOf(source))))))throw new Error('媒体校验提交回执不完整，结果需要核实');
        }
        catch(error){batch.forEach(id=>unknownProbeSubmissions.add(id));throw new Error(`${batch.map(id=>sourceTitle(state.sources[id])).join('、')}：${error.message}`);}
        finally{batch.forEach(id=>{if(probeSubmissions.get(id)===submission)probeSubmissions.delete(id);});}
      })();await submission.promise;
    }
    if(pendingCurrent(intent)){state.episodes=state.episodes.map(source=>state.sources[source.source_id||source.id]||source);renderEpisodes();}
  }
  async function refreshProbeRecoveries(intent) {
    await mapLimit(Array.from(state.probeRecoveries),3,async([sourceId,recovery])=>{
      const taskId=recovery.taskId;if(taskResumeRequests.has(taskId))return;
      try{
        const task=item(await api(`${ROOT}/tasks/${encodeURIComponent(taskId)}`));
        if(state.probeRecoveries.get(sourceId)!==recovery)return;
        if(['completed','failed','unknown','recovery_required','cancelled'].includes(task.status)){
          await ensureSource(sourceId,true);if(state.probeRecoveries.get(sourceId)!==recovery)return;state.probeRecoveries.delete(sourceId);
          if(task.status!=='completed'&&pendingCurrent(intent)&&intent.ids.includes(sourceId))failPendingAdd(intent,`${sourceTitle(state.sources[sourceId])}：${task.error_message||statusLabel(task.status)}`);
        }
      }catch(error){
        if(state.probeRecoveries.get(sourceId)!==recovery)return;
        if(error.status===403||error.status===404){state.probeRecoveries.delete(sourceId);if(pendingCurrent(intent)&&intent.ids.includes(sourceId))failPendingAdd(intent,`${sourceTitle(state.sources[sourceId])}：无法核实原校验，请联系发起人或管理员`);}
        // A different source's unavailable recovery must not block this selection.
      }
    });
  }
  async function refreshPendingAdd(intent=state.pendingAdd) {
    if(!pendingCurrent(intent)||intent.phase==='preparing')return;
    const ids=intent.ids.filter(id=>!sourceReady(id));
    await mapLimit(ids,3,async id=>{
      if(!pendingCurrent(intent))return;
      const source=await ensureSource(id,true);
      if(!pendingCurrent(intent)||sourceReady(id)||state.probeRecoveries.has(id))return;
      if(!['failed','probing'].includes(source.status))return;
      if(source.status==='probing'&&(!source.probe_task_id||hiddenProbeTasks.has(source.probe_task_id)))return;
      let reason=source.error_message||'媒体校验失败';
      if(source.probe_task_id){
        try{const task=item(await api(`${ROOT}/tasks/${encodeURIComponent(source.probe_task_id)}`));if(!['failed','unknown','recovery_required','cancelled'].includes(task.status))return;reason=task.error_message||statusLabel(task.status);}
        catch(error){if(error.status===403||error.status===404){if(source.status==='probing'){hiddenProbeTasks.add(source.probe_task_id);return;}reason='无法核实此共享校验的原任务，请联系发起人或管理员';}else throw error;}
      }
      failPendingAdd(intent,`${sourceTitle(source)}：${reason}`);
    });
    if(!pendingCurrent(intent))return;
    state.episodes=state.episodes.map(source=>state.sources[source.source_id||source.id]||source);renderEpisodes();
    if(!finishPendingAdd(intent))pendingProgress(intent);
  }
  async function addEpisodes() {
    if(state.pendingAdd)return;
    const ids=Array.from(document.querySelectorAll('.episode-select:checked')).map(input=>input.value),drama=state.catalog[state.selectedCatalog];
    if(!ids.length){setCatalogStatus('请勾选所需剧集后添加。',true);return;}if(!drama)return;
    if(editingLocked()){setCatalogStatus('项目媒体信息尚未载入，请稍候。',true);return;}
    const existingDrama=state.project.dramas.find(entry=>String(entry.app_id)===String(drama.app_id)&&String(entry.content_id)===String(drama.content_id));
    if(!existingDrama&&state.project.dramas.length>=state.config.max_dramas){setCatalogStatus(`每批最多 ${state.config.max_dramas} 部短剧`,true);return;}
    const existing=new Set((existingDrama&&existingDrama.clips||[]).map(clip=>clip.source_id));
    if((existingDrama&&existingDrama.clips.length||0)+ids.filter(id=>!existing.has(id)).length>300){setCatalogStatus('每部短剧最多 300 个片段，请减少所选剧集。',true);return;}
    const intent={project:state.project,projectSeq:state.projectLoadSeq,session:state.catalogSessionSeq,drama:{...drama},ids:[...new Set(ids)],phase:'preparing',completed:false};state.pendingAdd=intent;
    renderEpisodes();pendingProgress(intent);
    try{
      const unready=intent.ids.filter(id=>!sourceReady(id));if(unready.length)await prepareEpisodeSources(unready,intent);
      if(!pendingCurrent(intent))return;intent.phase='waiting';if(!finishPendingAdd(intent))pendingProgress(intent);
    }catch(error){failPendingAdd(intent,error.message);}
  }
  async function pollUpdates() {
    if(!state.ready||document.hidden)return;if(state.polling)return state.polling;
    const work=(async()=>{
      const hadRecoveries=state.probeRecoveries.size>0,intent=state.pendingAdd;
      const jobs=[(async()=>{try{if(hadRecoveries)await refreshProbeRecoveries(intent);await refreshPendingAdd(intent);}catch(error){if(pendingCurrent(intent))setCatalogStatus(`查询校验进度失败：${error.message}。稍后自动核实，也可取消等待。`,true);}})()];
      if(state.activeView==='tasks')jobs.push(loadTasks());
      if(state.activeView==='library'&&(hadRecoveries||state.assets.some(asset=>['uploading','uploaded','probing'].includes(asset.status))))jobs.push(loadAssets());
      await Promise.allSettled(jobs);
    })();state.polling=work;
    try{await work;}finally{if(state.polling===work)state.polling=null;}
  }
  function showInsert(){const asset=selectedAsset();if(!asset||asset.status!=='ready')return toast('请先选择已通过校验的素材');if(!currentDrama())return toast('请先添加短剧');$('insertName').textContent=asset.name;$('insertStart').value=0;$('insertEnd').value=(durationOf(asset)/1000).toFixed(3);$('insertDialog').showModal();}
  function confirmInsert(){const asset=selectedAsset();const start=Math.round(Number($('insertStart').value)*1000),end=Math.round(Number($('insertEnd').value)*1000);if(start<0||end<=start||end>durationOf(asset))return toast('素材保留范围无效',true);changed(()=>{const clips=currentDrama().clips;const selected=clips.findIndex(clip=>clip.id===state.selectedClip);const position=$('insertPosition').value;const index=position==='start'?0:position==='end'?clips.length:Math.max(0,selected)+(position==='after'?1:0);const clip=newClip(asset,{start_ms:start,end_ms:end});clips.splice(index,0,clip);normalizeLast(clips);state.selectedClip=clip.id;});$('insertDialog').close();}
  function uploadStore(){try{return JSON.parse(localStorage.getItem('ve-upload-sessions-v1')||'{}');}catch(_){return {};}}
  function storeUpload(fingerprint,value){try{const saved=uploadStore();if(value)saved[fingerprint]=value;else delete saved[fingerprint];localStorage.setItem('ve-upload-sessions-v1',JSON.stringify(saved));}catch(_){}}
  const fileFingerprint=file=>`${file.name}|${file.size}|${file.lastModified}`;
  function renderUploads(){$('uploadList').innerHTML=Array.from(state.uploads.values()).map(upload=>`<div class="upload-item"><span class="asset-icon">↑</span><div class="upload-info"><strong>${esc(upload.file.name)}</strong><small>${esc(upload.message)} · ${bytes(upload.file.size)}</small><progress max="100" value="${upload.percent||0}"></progress></div>${!upload.done?`<button class="button small" data-cancel-upload="${esc(upload.key)}">取消</button>`:''}</div>`).join('');}
  async function cosPut(url,headers,blob,upload,partNumber){
    return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();upload.requests.add(xhr);xhr.open('PUT',url);xhr.withCredentials=false;Object.entries(headers||{}).forEach(([name,value])=>xhr.setRequestHeader(name,value));xhr.upload.onprogress=event=>{if(event.lengthComputable){upload.progress[partNumber]=event.loaded;upload.percent=Math.min(99,100*(upload.completedBytes+Object.values(upload.progress).reduce((a,b)=>a+b,0))/upload.file.size);renderUploads();}};xhr.onload=()=>{upload.requests.delete(xhr);if(xhr.status>=200&&xhr.status<300)resolve(xhr.getResponseHeader('ETag'));else{const error=new Error(`COS 分片上传失败 (${xhr.status})`);error.status=xhr.status;reject(error);}};xhr.onerror=()=>{upload.requests.delete(xhr);reject(new Error('COS 上传网络中断；重新选择同一文件可续传'));};xhr.onabort=()=>{upload.requests.delete(xhr);reject(new Error('上传已取消'));};xhr.timeout=10*60*1000;xhr.ontimeout=()=>{upload.requests.delete(xhr);reject(new Error('分片上传超时，重新选择文件可续传'));};xhr.send(blob);});
  }
  async function uploadFile(file){
    if(demo)return toast('模拟预览不会上传文件，请在正式页面上传');if(!file.size||file.size>state.config.max_file_bytes)return toast(`${file.name}：文件应大于 0 且不超过 ${bytes(state.config.max_file_bytes)}`,true);
    const key=fileFingerprint(file);if(state.uploads.has(key)&&!state.uploads.get(key).done)return toast('此文件正在上传');
    const upload={key,file,requests:new Set(),progress:{},completedBytes:0,percent:0,message:'准备直传 COS…',done:false,cancelled:false};state.uploads.set(key,upload);renderUploads();
    let saved=uploadStore()[key];
    try{
      let session;
      if(saved&&saved.id){try{session=item(await api(`${ROOT}/uploads/${encodeURIComponent(saved.id)}`));}catch(error){if(error.status!==404)throw error;}}
      if(!session||['aborted','failed','expired'].includes(session.status)){if(state.config.intake_enabled===false&&!(saved&&saved.operation_id&&!session))throw new Error('新上传已暂停，已有上传仍可继续');saved={operation_id:!session&&saved&&saved.operation_id?saved.operation_id:uid()};storeUpload(key,saved);session=item(await api(`${ROOT}/uploads`,{method:'POST',body:{operation_id:saved.operation_id,name:file.name,size_bytes:file.size,content_type:file.type||'application/octet-stream'}}));storeUpload(key,{id:session.id,operation_id:saved.operation_id});}
      upload.sessionId=session.id;
      if(upload.cancelled){await api(`${ROOT}/uploads/${encodeURIComponent(session.id)}/abort`,{method:'POST',body:{}});throw new Error('上传已取消');}
      if(['completed','ready','probing'].includes(session.status)){upload.message='文件已上传，正在媒体校验';upload.percent=100;upload.done=true;storeUpload(key,null);renderUploads();await loadAssets();return;}
      session=item(await api(`${ROOT}/uploads/${encodeURIComponent(session.id)}/reconcile`,{method:'POST',body:{}}));
      const doneParts=new Set((session.uploaded_parts||[]).map(part=>Number(part.part_number)));const partSize=session.part_size||state.config.part_size;const count=session.part_count||Math.ceil(file.size/partSize);
      upload.completedBytes=Array.from(doneParts).reduce((sum,n)=>sum+Math.min(partSize,file.size-(n-1)*partSize),0);const pending=Array.from({length:count},(_,i)=>i+1).filter(n=>!doneParts.has(n));let cursor=0;let failure;
      upload.message=`上传中 · 已完成 ${doneParts.size}/${count} 分片`;renderUploads();
      const worker=async()=>{while(cursor<pending.length&&!upload.cancelled&&!failure){const n=pending[cursor++];const blob=file.slice((n-1)*partSize,Math.min(n*partSize,file.size));const contentMd5=md5Base64(await blob.arrayBuffer());let success=false;
        try{for(let attempt=0;attempt<3;attempt++){if(upload.cancelled)throw new Error('上传已取消');const signature=item(await api(`${ROOT}/uploads/${encodeURIComponent(session.id)}/parts/${n}/sign`,{method:'POST',body:{content_md5:contentMd5}}));try{await cosPut(signature.url,signature.headers,blob,upload,n);success=true;break;}catch(error){delete upload.progress[n];if(upload.cancelled||attempt===2||![0,401,403,408,429,500,502,503,504].includes(error.status||0))throw error;}}
          if(success){delete upload.progress[n];upload.completedBytes+=blob.size;doneParts.add(n);upload.percent=Math.min(99,100*upload.completedBytes/file.size);upload.message=`上传中 · 已完成 ${doneParts.size}/${count} 分片`;renderUploads();}
        }catch(error){failure=error;throw error;}
      }};
      const outcomes=await Promise.allSettled([worker(),worker(),worker()]);const rejected=outcomes.find(result=>result.status==='rejected');if(rejected)throw rejected.reason;if(upload.cancelled)throw new Error('上传已取消');
      upload.message='全部分片已上传，正在核对 COS 对象…';renderUploads();
      await api(`${ROOT}/uploads/${encodeURIComponent(session.id)}/complete`,{method:'POST',body:{}});storeUpload(key,null);upload.message='上传完成 · 媒体校验中';upload.percent=100;upload.done=true;renderUploads();await loadAssets();
    }catch(error){upload.done=true;upload.message=error.message;renderUploads();if(!upload.cancelled)toast(`${file.name}：${error.message}`,true);}
  }
  async function cancelUpload(key){const upload=state.uploads.get(key);if(!upload||upload.done)return;upload.cancelled=true;upload.requests.forEach(xhr=>xhr.abort());if(upload.sessionId)await api(`${ROOT}/uploads/${encodeURIComponent(upload.sessionId)}/abort`,{method:'POST',body:{}});storeUpload(key,null);upload.done=true;upload.message='上传已取消';renderUploads();}
  function seedDemo(){
    const asset={id:'demo-asset',source_id:'demo-asset',kind:'asset',name:'悬念开场 · 她终于回来了.mp4',status:'ready',can_manage:true,size_bytes:23445567,metadata:{duration_ms:8000,width:1080,height:1920,has_audio:true}};
    const sources=[asset,{id:'demo-asset-2',source_id:'demo-asset-2',kind:'asset',name:'情绪特写 · 城市夜色.mp4',status:'ready',can_manage:true,size_bytes:14873612,metadata:{duration_ms:12000,width:1920,height:1080,has_audio:true}}];
    for(const title of ['demo-101','demo-102'])for(let n=1;n<=3;n++)sources.push({id:`${title}-ep-${n}`,source_id:`${title}-ep-${n}`,kind:'episode',name:`第 ${n} 集`,episode_number:n,status:'ready',metadata:{duration_ms:60000,width:720,height:1280,has_audio:true}});
    state.assets=sources.filter(source=>source.kind==='asset');state.assetsTotal=2;rememberSources(sources);
    state.project={id:'demo-project',name:'悬念开场 · 批量剪辑 01',asset_id:asset.id,version:1,dramas:[{app_id:'demo-product',content_id:'demo-101',title:'风起时，遇见你',clips:[newClip(asset,{end_ms:5000,transition:'fade'}),newClip(state.sources['demo-101-ep-1'],{start_ms:2500,end_ms:14800}),newClip(state.sources['demo-101-ep-1'],{start_ms:26000,end_ms:41000,transition:'fade'}),newClip(state.sources['demo-101-ep-2'],{start_ms:4000,end_ms:22300})]},{app_id:'demo-product',content_id:'demo-102',title:'她的第二人生',clips:[newClip(asset,{end_ms:5000}),newClip(state.sources['demo-102-ep-1'],{end_ms:24000}),newClip(state.sources['demo-102-ep-2'],{start_ms:15000,end_ms:38000})]}]};
    state.projects=[clone(state.project)];state.selectedClip=state.project.dramas[0].clips[1].id;state.tasks=[{id:'demo-completed-task',drama_title:'风起时，遇见你',status:'completed',stage:'completed',progress:1,created_at:new Date().toISOString(),output:{metadata:{duration_ms:43800}}}];$('saveStatus').textContent='模拟草稿 · 仅本页';$('previewMessage').textContent='演示画面，正式素材校验后可播放';renderProjectPicker();renderAssets();renderTasks();renderEditor();
  }
  function bind(){
    const run=fn=>event=>{try{return Promise.resolve(fn(event)).catch(error=>toast(error.message,true));}catch(error){toast(error.message,true);return Promise.resolve();}};
    document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>setView(button.dataset.view)));
    document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>{if(button.dataset.close==='dramaDialog')closeCatalogSession();$(button.dataset.close).close();}));
    $('dramaDialog').addEventListener('close',closeCatalogSession);$('dramaDialog').addEventListener('cancel',closeCatalogSession);
    $('cancelAddEpisodes').addEventListener('click',()=>cancelPendingAdd());
    $('authButton').addEventListener('click',run(()=>demo?toast('模拟预览无需登录'):global.UiTopbar.handleAuthAction({auth:state.auth,api})));
    $('projectName').addEventListener('change',()=>changed(()=>{state.project.name=$('projectName').value.trim()||'未命名剪辑项目';}));
    $('projectPicker').addEventListener('change',run(event=>openProject(event.target.value)));
    $('retryProjectSources').addEventListener('click',run(()=>withBusy('retryProjectSources',$('retryProjectSources'),'正在载入…',()=>openProject(state.project.id))));
    $('newProject').addEventListener('click',run(()=>withBusy('newProject',$('newProject'),'正在新建…',async()=>{if(state.renderSubmitting)return;cancelPendingAdd();const sequence=++state.projectLoadSeq;state.projectOpening=sequence;syncEditorLocks();try{await flushDraft();if(sequence!==state.projectLoadSeq)return;stopPreview();clearTimeout(state.saveTimer);state.project={name:'未命名剪辑项目',asset_id:'',dramas:[]};state.activeDrama=0;state.selectedClip=null;state.undo=[];state.dirty=false;state.operationId=null;state.pendingCreate=null;state.renderOperation=null;state.sourceLoading=false;state.sourceLoadFailed=false;$('saveStatus').textContent='尚未保存';}finally{if(sequence===state.projectLoadSeq){state.projectOpening=0;renderEditor();}}})));
    $('saveProject').addEventListener('click',run(()=>withBusy('saveProject',$('saveProject'),'正在保存…',()=>saveProject())));$('renderProject').addEventListener('click',run(()=>withBusy('renderProject',$('renderProject'),'正在提交…',renderProject)));
    $('saveCopy').addEventListener('click',run(()=>withBusy('saveCopy',$('saveCopy'),'正在另存…',async()=>{cancelPendingAdd();if(state.saving)await state.saving.catch(()=>{});const copy=serializeProject();copy.name=(copy.name||'剪辑项目').slice(0,115)+' · 副本';state.project=copy;state.projectLoadSeq++;state.revision++;state.undo=[];state.operationId=null;state.pendingCreate=null;state.renderOperation=null;state.conflicted=false;state.dirty=true;renderEditor();await saveProject(true);})));
    $('undoEdit').addEventListener('click',()=>{if(!state.undo.length)return;stopPreview();const id=state.project.id,version=state.project.version;state.project={...state.undo.pop(),id,version};state.dirty=true;state.revision++;state.renderOperation=null;state.activeDrama=Math.max(0,Math.min(state.activeDrama,state.project.dramas.length-1));renderEditor();scheduleSave();});
    $('chooseAsset').addEventListener('click',run(()=>withBusy('chooseAsset',$('chooseAsset'),'正在加载…',async()=>{$('assetDialog').showModal();$('assetPicker').innerHTML='<div class="empty" role="status">正在加载共享素材…</div>';try{await loadAssets();}catch(error){$('assetPicker').innerHTML=`<div class="empty danger" role="status">素材加载失败：${esc(error.message)}。关闭后重新打开可重试。</div>`;throw error;}})));
    $('goUpload').addEventListener('click',()=>{$('assetDialog').close();setView('library');});
    $('addDrama').addEventListener('click',run(()=>openCatalog($('addDrama'))));$('addDramaBottom').addEventListener('click',run(()=>openCatalog($('addDramaBottom'))));$('searchDramas').addEventListener('click',run(searchDramas));$('productPicker').addEventListener('change',run(searchDramas));$('dramaSearch').addEventListener('keydown',run(event=>event.key==='Enter'?searchDramas():null));$('addEpisodes').addEventListener('click',run(addEpisodes));
    $('dramaList').addEventListener('click',event=>{const remove=event.target.closest('[data-remove-drama]');if(remove){changed(()=>{state.project.dramas.splice(Number(remove.dataset.removeDrama),1);state.activeDrama=Math.max(0,Math.min(state.activeDrama,state.project.dramas.length-1));});return;}const button=event.target.closest('[data-drama-index]');if(button){stopPreview();state.activeDrama=Number(button.dataset.dramaIndex);state.selectedClip=null;state.playMs=0;renderEditor();previewAt(0,false).catch(()=>{});}});
    $('catalogDramas').addEventListener('click',run(event=>{const button=event.target.closest('[data-catalog-index]');if(button)return selectCatalog(Number(button.dataset.catalogIndex));}));
    $('timeline').addEventListener('click',event=>{const button=event.target.closest('[data-clip-id]');if(button)selectClip(button.dataset.clipId);});
    let dragged=null;$('timeline').addEventListener('dragstart',event=>{const button=event.target.closest('[data-clip-id]');if(button){dragged=button.dataset.clipId;event.dataTransfer.setData('text/plain',dragged);event.dataTransfer.effectAllowed='move';}});$('timeline').addEventListener('dragover',event=>event.preventDefault());$('timeline').addEventListener('drop',event=>{event.preventDefault();const button=event.target.closest('[data-clip-id]');if(!button||!dragged)return;changed(()=>{const clips=currentDrama().clips;currentDrama().clips=move(clips,clips.findIndex(clip=>clip.id===dragged),clips.findIndex(clip=>clip.id===button.dataset.clipId));});dragged=null;});
    for(const [id,delta] of [['moveLeft',-1],['moveRight',1]])$(id).addEventListener('click',()=>{if(!selectedClip())return;changed(()=>{const clips=currentDrama().clips;const index=clips.findIndex(clip=>clip.id===state.selectedClip);currentDrama().clips=move(clips,index,index+delta);});});
    for(const [id,key,multiplier] of [['clipStart','start_ms',1000],['clipEnd','end_ms',1000],['trimStart','start_ms',1],['trimEnd','end_ms',1]])$(id).addEventListener('change',()=>{const value=Math.round(Number($(id).value)*multiplier);if(Number.isFinite(value))updateClip({[key]:value});});
    for(const [id,text] of [['trimStart','clipStart'],['trimEnd','clipEnd']])$(id).addEventListener('input',()=>{$(text).value=(Number($(id).value)/1000).toFixed(3);});
    document.querySelectorAll('[data-fit]').forEach(button=>button.addEventListener('click',()=>updateClip({fit:button.dataset.fit})));
    $('clipSound').addEventListener('change',()=>updateClip({mute:!$('clipSound').checked}));$('clipTransition').addEventListener('change',()=>updateClip({transition:$('clipTransition').value}));
    $('splitClip').addEventListener('click',run(()=>{const clip=selectedClip();if(!clip)return;const segment=layout(currentDrama().clips).find(entry=>entry.clip.id===clip.id);const at=clip.start_ms+state.playMs-segment.start;const result=split(currentDrama().clips,clip.id,at);changed(()=>{currentDrama().clips=result;});}));
    $('removeClip').addEventListener('click',()=>{if(!selectedClip())return;changed(()=>{currentDrama().clips=normalizeLast(currentDrama().clips.filter(clip=>clip.id!==state.selectedClip));});});
    $('removeEpisode').addEventListener('click',()=>{const clip=selectedClip();if(!clip)return;changed(()=>{currentDrama().clips=normalizeLast(currentDrama().clips.filter(entry=>entry.source_id!==clip.source_id));});});
    $('insertAsset').addEventListener('click',showInsert);$('confirmInsert').addEventListener('click',confirmInsert);
    $('applyCommon').addEventListener('click',run(()=>{const asset=selectedAsset();if(!$('commonPosition').value||!state.project.dramas.length)return;const assetClip=asset?newClip(asset):null;const dramas=applyCommon(state.project.dramas,assetClip,$('commonPosition').value,$('commonTransition').value,asset&&asset.source_id);changed(()=>{state.project.dramas=dramas;});toast('已应用公共设置，逐剧剪切点保持不变');}));
    $('playPreview').addEventListener('click',run(playPreview));$('playhead').addEventListener('input',()=>{stopPreview();state.playMs=Number($('playhead').value);updatePlayhead();previewAt(state.playMs,false).catch(error=>toast(error.message,true));});$('resetPlay').addEventListener('click',()=>{stopPreview();state.playMs=0;updatePlayhead();previewAt(0,false).catch(()=>{});});
    for(const id of ['previewA','previewB'])$(id).addEventListener('error',()=>{if($(id).src){stopPreview();$('previewPlaceholder').classList.remove('hidden');$('previewMessage').textContent='此视频暂无法在浏览器播放，请刷新预览链接或等待代理完成';}});
    $('mediaDialog').addEventListener('close',()=>{$('mediaPlayer').pause();$('mediaPlayer').removeAttribute('src');$('mediaPlayer').load();});
    $('refreshTasks').addEventListener('click',run(()=>withBusy('refreshTasks',$('refreshTasks'),'正在刷新…',loadTasks)));$('loadMoreAssets').addEventListener('click',run(()=>withBusy('loadMoreAssets',$('loadMoreAssets'),'正在加载…',()=>loadAssets(true))));let searchTimer;$('assetSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadAssets().catch(error=>toast(error.message,true)),300);});
    $('uploadButton').addEventListener('click',()=>$('fileInput').click());$('uploadDrop').addEventListener('click',()=>$('fileInput').click());$('uploadDrop').addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('fileInput').click();}});$('fileInput').addEventListener('change',()=>{Array.from($('fileInput').files).reduce((promise,file)=>promise.then(()=>uploadFile(file)),Promise.resolve());$('fileInput').value='';});
    for(const eventName of ['dragenter','dragover'])$('uploadDrop').addEventListener(eventName,event=>{event.preventDefault();$('uploadDrop').classList.add('drag-over');});for(const eventName of ['dragleave','drop'])$('uploadDrop').addEventListener(eventName,event=>{event.preventDefault();$('uploadDrop').classList.remove('drag-over');if(eventName==='drop')Array.from(event.dataTransfer.files).reduce((promise,file)=>promise.then(()=>uploadFile(file)),Promise.resolve());});
    document.addEventListener('click',run(async event=>{
      const button=event.target.closest('[data-pick-asset],[data-preview-asset],[data-probe-asset],[data-rename-asset],[data-delete-asset],[data-preview-task],[data-download-task],[data-resume-task],[data-upload-material-task],[data-refresh-material-task],[data-cancel-upload]');if(!button)return;
      const action=Object.keys(button.dataset).find(key=>['pickAsset','previewAsset','probeAsset','renameAsset','deleteAsset','previewTask','downloadTask','resumeTask','uploadMaterialTask','refreshMaterialTask','cancelUpload'].includes(key));
      const label={previewAsset:'正在加载…',previewTask:'正在加载…',downloadTask:'获取链接…',probeAsset:'正在核实…',resumeTask:'正在核实恢复…',uploadMaterialTask:'正在提交…',refreshMaterialTask:'正在刷新…',renameAsset:'正在保存…',deleteAsset:'正在移除…',cancelUpload:'正在取消…'}[action];
      return withBusy(`${action}:${button.dataset[action]}`,button,label,async()=>{
      if(button.dataset.pickAsset)return pickAsset(button.dataset.pickAsset);if(button.dataset.previewAsset)return previewAsset(button.dataset.previewAsset);if(button.dataset.probeAsset)return probeAsset(button.dataset.probeAsset);if(button.dataset.previewTask)return taskOutput(button.dataset.previewTask,false);if(button.dataset.downloadTask)return taskOutput(button.dataset.downloadTask,true);if(button.dataset.uploadMaterialTask)return uploadMaterialTask(button.dataset.uploadMaterialTask);if(button.dataset.refreshMaterialTask)return loadTasks();if(button.dataset.cancelUpload)return cancelUpload(button.dataset.cancelUpload);
      if(button.dataset.resumeTask){if(demo)return toast('模拟模式不会提交重试');await resumeTaskRequest(button.dataset.resumeTask);await loadTasks();return;}
      const id=button.dataset.renameAsset||button.dataset.deleteAsset;const asset=state.assets.find(entry=>entry.id===id);if(!asset)return;
      if(button.dataset.renameAsset){const name=prompt('素材名称',asset.name);if(!name||!name.trim())return;if(demo)asset.name=name.trim();else await api(`${ROOT}/assets/${encodeURIComponent(id)}`,{method:'PATCH',body:{name:name.trim()}});}
      else{if(!confirm(`从共享素材库移除“${asset.name}”？已有制作任务引用的文件仍会保留。`))return;if(demo)state.assets=state.assets.filter(entry=>entry.id!==id);else await api(`${ROOT}/assets/${encodeURIComponent(id)}`,{method:'DELETE'});}await loadAssets();
      });
    }));
    global.addEventListener('beforeunload',event=>{if(!demo&&(state.dirty||Array.from(state.uploads.values()).some(upload=>!upload.done))){event.preventDefault();event.returnValue='';}});
  }
  async function init(){
    bind();
    if(demo){state.auth={authenticated:true,user:{name:'创作体验',role:'admin',tenant_key:'demo'}};$('demoBanner').classList.remove('hidden');}
    else{try{state.auth=await api('/api/ui/topbar');}catch(error){state.auth={authenticated:false};toast(`登录信息获取失败：${error.message}`,true);}}
    await global.QuickNav.render({container:'#quickNav',auth:state.auth,activeKey:'videoEditing'});global.UiTopbar.render({auth:state.auth,userCard:'#userCard',authButton:'#authButton',refreshButton:'#refreshPage'});
    if(!state.auth.authenticated){$('loginGate').classList.remove('hidden');return;}
    if(demo){$('pageRoot').classList.remove('hidden');seedDemo();state.ready=true;return;}
    try{state.config={...state.config,...item(await api(`${ROOT}/config`))};const notices=[];if(state.config.intake_enabled===false||state.config.enabled===false)notices.push('新上传、媒体校验和制作提交已暂停；已有上传可续传，历史项目和成片仍可查看。');if(state.config.worker_configured===false)notices.push('制作服务尚未配置，暂不能提交 GPU 制作。');if(state.config.cos_configured===false)notices.push('COS 尚未配置，暂不能上传新素材。');$('configNotice').textContent=notices.join(' ');$('configNotice').classList.toggle('hidden',!notices.length);$('uploadLimits').textContent=`单个文件最大 ${bytes(state.config.max_file_bytes)} · 支持分片续传 · 视频直接进入 COS`;$('catalogStatus').textContent=`最多 ${state.config.max_dramas} 部短剧`;$('pageRoot').classList.remove('hidden');await Promise.all([loadAssets(),loadProjects(),loadTasks()]);renderEditor();state.ready=true;}
    catch(error){$('loginGate').innerHTML=`<strong>暂时无法打开视频剪辑</strong><p>${esc(error.message)}</p>`;$('loginGate').classList.remove('hidden');$('pageRoot').classList.add('hidden');}
    setInterval(pollUpdates,3000);
  }
  init().catch(error=>toast(error.message,true));
})(typeof window!=='undefined'?window:globalThis);
