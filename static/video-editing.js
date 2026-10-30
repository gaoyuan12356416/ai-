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
    result.splice(index, 1, { ...clone(clip), end_ms: point, transition: 'cut' }, { ...clone(clip), id: uid(), start_ms: point });
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
  function projectUnits(project) {
    const units=project.schema_version===2?project.units:(project.upgrade_units||project.dramas||[]);
    return (units||[]).map((unit,index)=>({id:unit.id||`unit_legacy_${index}`,series_code:String(unit.series_code||''),title:unit.title||unit.english_title||'',clips:clone(unit.clips||[])}));
  }
  function draftForSave(project) {
    return {schema_version:2,name:project.name,units:(project.dramas||projectUnits(project)).map(unit=>({id:unit.id,series_code:unit.series_code||'',title:unit.title||'',clips:clone(unit.clips||[])}))};
  }
  function effectiveBand(clip) {
    if(!clip.mirror||clip.subtitle_band===null)return null;
    const band=clip.subtitle_band||{top:960,bottom:1280};
    return {top:Math.max(0,Math.min(1278,Math.round(Number(band.top)/2)*2)),bottom:Math.max(2,Math.min(1280,Math.round(Number(band.bottom)/2)*2))};
  }
  function fitRectangle(width,height,fit) {
    const evenNear=(n,d)=>Math.max(2,2*Math.floor((n+d)/(2*d))),proportionalWidth=evenNear(1280*width,height),proportionalHeight=evenNear(720*height,width);
    if(fit==='cover'){const w=Math.max(720,proportionalWidth),h=Math.max(1280,proportionalHeight);return {x:-Math.floor((w-720)/4)*2,y:-Math.floor((h-1280)/4)*2,width:w,height:h};}
    const w=Math.min(720,proportionalWidth),h=Math.min(1280,proportionalHeight);return {x:Math.floor((720-w)/4)*2,y:Math.floor((1280-h)/4)*2,width:w,height:h};
  }
  const replicationTaskLabels={1:'多语种复刻',3:'竞品复刻',4:'爆款反转复刻'};
  const replicationAudioLabels={0:'无配音',1:'人工配音',2:'AI 配音'};
  const replicationCategories=['解说','二创','混剪','精剪','切片','image','Dramawave Post'];
  function replicationCategoryLabel(value){return value==null||value===''?'未指定（旧记录）':value==='image'?'图片（image）':String(value);}
  function replicationTimeMs(value){const match=/^(\d{2}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(String(value||''));return match?((+match[1]*3600 + +match[2]*60 + +match[3])*1000 + +(match[4]||'').padEnd(3,'0')):null;}
  function validateReplicationInput(payload,context){
    if(!context?.enabled)return context?.disabled_reason||'此成片暂不可复刻';
    if(![1,3,4].includes(payload.task_type)||![0,1,2].includes(payload.audio_type)||![1,2].includes(payload.target_material_type))return '请选择完整的制作选项';
    if(Object.prototype.hasOwnProperty.call(payload,'material_category')&&!replicationCategories.includes(payload.material_category))return '请选择有效的素材分类';
    if(!Array.isArray(payload.target_langs)||!payload.target_langs.length)return '请至少选择 1 个目标语种';
    const allowed=new Set((context.target_languages||[]).map(language=>language.code));
    if(payload.target_langs.some(code=>!allowed.has(code)))return '目标语种列表已变化，请重新打开后选择';
    if(context.can_change_operator&&!String(payload.operator||'').trim())return '请填写成品归属账号';
    if(!context.can_change_operator&&(!context.default_operator||context.operator_error))return context.operator_error||'素材创建人的归属账号尚未配置';
    if(payload.target_material_type===2){const start=replicationTimeMs(payload.descant_start_time),end=replicationTimeMs(payload.descant_end_time),duration=Number(context.source?.duration_ms);if(start===null||end===null)return '请填写解说起止时间，格式为 00:00:00，可附带毫秒';if(start>=end||!Number.isFinite(duration)||duration<=0||end>duration)return '解说开始时间须早于结束时间，且结束时间不能超过母素材时长';}
    return '';
  }
  function replicationReceipt(record){
    const states={pending:['等待提交','pending','请求已登记，尚未确认受理。'],submitting:['正在提交','pending','正在等待接口回执，请勿重复提交。'],accepted:[record?.upstream_status==='waiting_download'?'已受理 · 受理时：待转存':'已受理','accepted','此处仅展示受理回执，成品生成和素材库回推结果尚未查询。'],partial:['部分语种已受理','warning','仅有效语种已受理；不支持语种未创建制作任务。'],rejected:['提交失败','danger','本次请求未受理，请按失败原因处理。'],unknown_outcome:['结果待核实','warning','暂不能确认是否受理，请先核实记录，避免重复生产。']};
    const [label,tone,message]=states[record?.status]||['状态待核实','warning','尚未获得可确认的受理状态。'];return {label,tone,message};
  }
  const model = { replicationTimeMs,validateReplicationInput,replicationReceipt,projectUnits,draftForSave,effectiveBand,fitRectangle, FADE_MS, toFrame, layout, totalDuration, validateClips, split, move, applyCommon, md5Base64, probeDisplay };
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  if (!global.document) return;
  global.VideoEditingModel = model;
  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  const ROOT = '/api/video-editing';
  const state = { auth:null, config:{ max_file_bytes:8*1024**3, part_size:16*1024**2, max_dramas:20 }, assets:[], assetsTotal:0, sources:{}, projects:[], project:{schema_version:2,name:'未命名剪辑项目',dramas:[]}, activeDrama:0, selectedClip:null, undo:[], dirty:false, saving:null, saveTimer:null, revision:0, projectLoadSeq:0, tasks:[], uploads:new Map(), probeRecoveries:new Map(), playMs:0, playing:false, playTick:0, playOrigin:0, activeView:'editor', catalog:[], episodes:[], selectedCatalog:null, catalogLoadSeq:0, operationId:null, renderOperation:null, ready:false };
  const readRequests = new Map(), actions = new Map(), busyButtons = new WeakMap();
  Object.assign(state, { catalogSessionSeq:0, catalogProducts:null, pendingAdd:null, polling:null, assetsLoadSeq:0, projectsLoading:null, tasksLoading:null, projectOpening:0, sourceLoading:false, sourceLoadFailed:false, renderSubmitting:false, materialUploadSubmitting:new Set() });
  Object.assign(state,{libraryKind:'material',libraryResource:null,uploadResource:null,resourceSearchSeq:0,resourceDialogSeq:0,resourceResults:[],resourcePurpose:null,pickerKind:'free_episode',pickerItems:[],pickerSeq:0,selectedUnits:new Set(),renderIssues:[],insertSourceId:null,variantTask:null,variantSeq:0});
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
        if (!response.ok) { const error = new Error(payload.error || `请求失败 (${response.status})`); error.status=response.status; error.code=payload.code; error.payload=payload; throw error; }
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
  function editingLocked(){return !!(state.projectOpening||state.renderSubmitting);}
  function syncEditorLocks(){
    const locked=editingLocked();
    for(const id of ['projectName','chooseAsset','addDrama','addDramaBottom','insertAsset','applyCommon','clipStart','clipEnd','trimStart','trimEnd','clipSound','splitClip','removeClip','removeEpisode','moveLeft','moveRight','confirmInsert','clipMirror','protectSubtitles','subtitleTop','subtitleBottom','changeUnitResource']){const el=$(id);if(el&&!busyButtons.has(el))el.disabled=locked;}
    document.querySelectorAll('[data-fit]').forEach(button=>{button.disabled=locked;});
    $('undoEdit').disabled=locked||!state.undo.length;
    const clip=selectedClip(),drama=currentDrama();$('clipTransition').disabled=locked||!clip||clip===(drama&&drama.clips[drama.clips.length-1]);$('protectSubtitles').disabled=locked||!clip?.mirror;$('changeUnitResource').disabled=locked||!drama;
    $('renderProject').disabled=locked||actions.has('renderProject')||!state.project.dramas.length||state.config.intake_enabled===false||state.config.worker_configured===false;
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
  function selectedAsset() { return state.sources[state.insertSourceId] || null; }
  function newClip(source, overrides = {}) { return { id:uid(), source_id:source.source_id || source.id, start_ms:0, end_ms:durationOf(source), fit:'contain',mute:false,transition:'cut',mirror:false,subtitle_band:{top:960,bottom:1280},...overrides }; }
  function rememberSources(sources) { for (const source of sources || []) { const id=source.source_id||source.id;state.sources[id] = { ...source, _previewFetchedAt:Date.now() };if(source.status==='ready')state.probeRecoveries.delete(id); } }
  function snapshot() { state.undo.push(clone(state.project)); if (state.undo.length > 60) state.undo.shift(); }
  function changed(mutator) { if(editingLocked())return toast('正在载入或提交项目，请稍候');snapshot(); stopPreview(); mutator(); state.project.schema_version=2;state.project.issues=[];state.renderIssues=[]; state.revision++; state.dirty=true; state.renderOperation=null; renderEditor(); scheduleSave(); }
  function scheduleSave() { clearTimeout(state.saveTimer); $('saveStatus').textContent = demo ? '模拟草稿 · 仅本页' : '有未保存修改'; if (!demo) state.saveTimer=setTimeout(() => saveProject(false).catch(error => toast(`草稿未保存：${error.message}`, true)), 1000); }
  async function ensureSource(id, refresh = false) {
    const cached = state.sources[id];
    if (demo) return cached;
    const expires=cached&&(cached.preview_expires_at||cached.expires_at);
    if (!refresh && cached && cached.status === 'ready' && cached.preview_url && Date.now()-(cached._previewFetchedAt||0)<240000 && (!expires || expiryTime(expires) > Date.now()+30000)) return cached;
    const source = item(await api(`${ROOT}/sources/${encodeURIComponent(id)}`)); rememberSources([source]); return source;
  }
  function setView(view) {
    stopPreview();state.activeView=view;
    document.querySelectorAll('[data-view]').forEach(button=>button.classList.toggle('active',button.dataset.view===view));
    ['editor','library','tasks'].forEach(name=>$(`${name}View`).classList.toggle('hidden',name!==(view==='free'?'library':view)));
    if(view==='library'||view==='free'){
      state.libraryKind=view==='free'?'free_episode':'material';
      $('libraryTitle').textContent=view==='free'?'免费集':'共享素材库';
      $('libraryDescription').textContent=view==='free'?'按资源管理免费合集或视频，不要求填写集号。':'原素材直接上传 COS，有权限的同事均可使用。';
      $('uploadButton').textContent=view==='free'?'↑ 上传免费集':'↑ 上传本地素材';
      $('assetGrid').innerHTML='<div class="empty" role="status">正在加载视频…</div>';loadAssets().catch(error=>{$('assetGrid').innerHTML=`<div class="empty danger">${esc(error.message)}</div>`;toast(error.message,true);});
    }
    if(view==='tasks'){if(!state.tasks.length)$('taskList').innerHTML='<div class="empty" role="status">正在加载制作任务…</div>';loadTasks().catch(error=>toast(error.message,true));}
  }
  function adaptProject(project){project.dramas=projectUnits(project);return project;}
  function unitTitle(unit){return unit.title||unit.series_code||'未选择资源';}
  function currentIssues(){const unit=currentDrama();if(!unit)return [];const issues=[...(state.project.issues||[]),...state.renderIssues].filter(issue=>issue.unit_id===unit.id).map(issue=>issue.message);if(!unit.series_code)issues.unshift('请为当前单元选择目标资源；草稿仍可保存。');for(const clip of unit.clips){const source=state.sources[clip.source_id];if(source&&source.series_code&&unit.series_code&&source.series_code!==unit.series_code)issues.push(`${sourceTitle(source)}属于其他资源，请调整片段或资源标签。`);}return [...new Set(issues)];}

  function renderEditor() {
    $('projectName').value=state.project.name || ''; $('dramaCount').textContent=`${state.project.dramas.length} 部`;
    $('selectedAssetName').textContent=currentDrama()?.series_code?`选择 ${currentDrama().series_code} 的视频`:'先选择目标资源';$('changeUnitResource').disabled=!currentDrama();
    $('undoEdit').disabled=!state.undo.length;
    $('saveCopy').classList.toggle('hidden',!state.conflicted);
    $('renderProject').disabled=!state.project.dramas.length || state.config.intake_enabled===false || state.config.worker_configured===false;
    $('dramaList').innerHTML=state.project.dramas.map((drama,index)=>`<div class="drama-row ${index===state.activeDrama?'active':''}"><input type="checkbox" data-render-unit="${esc(drama.id)}" aria-label="选择制作 ${esc(unitTitle(drama))}" ${state.selectedUnits.has(drama.id)?'checked':''}><button data-drama-index="${index}"><strong>${esc(unitTitle(drama))}</strong><small>${esc(drama.series_code||'待关联资源')} · ${drama.clips.length} 片段</small></button><button class="remove-drama" data-remove-drama="${index}" title="移除此单元" aria-label="移除此单元">×</button></div>`).join('')||'<div class="empty">先添加目标资源，再选择视频。</div>';
        const drama=currentDrama(); const clips=drama && drama.clips || []; const duration=totalDuration(clips);
    if(!clips.length){$('canvasA').style.opacity=0;$('canvasB').style.opacity=0;$('previewPlaceholder').classList.remove('hidden');$('previewMessage').textContent=demo?'演示画面，正式素材校验后可播放':'添加素材与剧集后即可预览';$('previewExpiry').textContent='';}
    if (!clips.find(clip => clip.id === state.selectedClip)) state.selectedClip=clips.length ? clips[0].id : null;
    $('previewTitle').textContent=drama?unitTitle(drama):'选择资源后预览';
    $('timelineDuration').textContent=`预计 ${seconds(duration)} 秒 · ${clips.length} 片段`;
    const maxWidth=Math.max(105, Math.min(250, 800/Math.max(1,clips.length)));
    $('timeline').innerHTML=layout(clips).map(({clip,index,length,start}) => {
      const source=state.sources[clip.source_id]; const isAsset=source && source.kind==='asset';
      return `<button class="timeline-clip ${isAsset?'asset':''} ${clip.id===state.selectedClip?'selected':''}" draggable="true" data-clip-id="${esc(clip.id)}" style="width:${Math.max(105,Math.min(280,maxWidth+Math.log2(Math.max(1,length/1000))*7))}px" title="${esc(sourceTitle(source))}"><span class="clip-index">${time(start)}</span><strong>${isAsset?'▧':'▤'} ${esc(sourceTitle(source))}</strong><small>${seconds(clip.start_ms)}—${seconds(clip.end_ms)} 秒${clip.mute?' · 静音':''}${clip.mirror?' · 镜像':''}</small>${clip.transition==='fade'&&index<clips.length-1?'<span class="transition-mark" title="0.3 秒交叉淡化">⋈</span>':''}</button>`;
    }).join('') || '<div class="timeline-empty">添加剧集后开始剪辑</div>';
    const errors=[...currentIssues(),...(clips.length?validateClips(clips,state.sources):[])];
    $('validationErrors').textContent=errors.join('；'); $('validationErrors').classList.toggle('hidden',!errors.length);
    $('totalTime').textContent=time(duration); $('playhead').max=Math.max(1,duration); state.playMs=Math.min(state.playMs,Math.max(0,duration)); updatePlayhead(); renderInspector();syncEditorLocks();
  }
  function renderInspector() {
    const clip=selectedClip(); $('emptyInspector').classList.toggle('hidden',!!clip); $('clipInspector').classList.toggle('hidden',!clip);
    if (!clip) { $('clipKind').textContent='未选择'; return; }
    const source=state.sources[clip.source_id]; const isAsset=source && source.kind==='asset';
    $('clipKind').textContent=isAsset?(source.library_kind==='free_episode'?'免费集':'素材'):'在线剧集'; $('clipTitle').textContent=sourceTitle(source); $('sourceDuration').textContent=`原长 ${seconds(durationOf(source))} 秒`;
    $('clipStart').value=(clip.start_ms/1000).toFixed(3); $('clipEnd').value=(clip.end_ms/1000).toFixed(3);
    for(const id of ['trimStart','trimEnd']) $(id).max=durationOf(source)||clip.end_ms;
    $('trimStart').value=clip.start_ms; $('trimEnd').value=clip.end_ms; $('clipSound').checked=!clip.mute;
    $('clipTransition').value=clip.transition; $('clipTransition').disabled=clip===currentDrama().clips[currentDrama().clips.length-1];
    $('removeEpisode').classList.toggle('hidden',isAsset);
    document.querySelectorAll('[data-fit]').forEach(button=>button.classList.toggle('active',button.dataset.fit===clip.fit));
    $('clipMirror').checked=!!clip.mirror;$('protectSubtitles').checked=clip.subtitle_band!==null;$('protectSubtitles').disabled=!clip.mirror;const band=effectiveBand({...clip,mirror:true})||{top:960,bottom:1280};$('subtitleTop').value=band.top;$('subtitleBottom').value=band.bottom;$('subtitleControls').classList.toggle('hidden',!clip.mirror||clip.subtitle_band===null);renderBandOverlay();
  }
  function selectClip(id, seek = true) {
    stopPreview(); state.selectedClip=id; renderEditor();
    const segment=layout(currentDrama().clips).find(entry=>entry.clip.id===id);
    if (seek && segment) { state.playMs=segment.start; updatePlayhead(); previewAt(state.playMs,false).catch(error=>toast(error.message,true)); }
  }
  function updateClip(patch) { const clip=selectedClip(); if (!clip) return; changed(()=>Object.assign(clip,patch)); previewAt(state.playMs,false).catch(()=>{}); }
  function updatePlayhead() { renderBandOverlay(); $('playTime').textContent=time(state.playMs); $('playhead').value=state.playMs; }
  function stopPreview(preserveLayers=false) { previewLayers.forEach(layer=>{cancelLayer(layer);if(!preserveLayers)layer.active=false;});state.playing=false; previewGeneration++; cancelAnimationFrame(state.playTick); $('previewA').pause(); $('previewB').pause(); $('playPreview').textContent='▶'; $('playPreview').setAttribute('aria-label','播放时间线'); }
  let previewGeneration=0;
  async function previewAt(position,play){
    const generation=++previewGeneration,drama=currentDrama();if(!drama)return;
    const segments=layout(drama.clips).filter(segment=>position>=segment.start&&position<segment.end);renderBandOverlay();if(!segments.length)return;
    if(demo){$('previewMessage').textContent=`模拟预览 · ${sourceTitle(state.sources[segments[0].clip.source_id])}`;return;}
    const sources=await Promise.all(segments.map(segment=>ensureSource(segment.clip.source_id)));if(generation!==previewGeneration)return;
    const both=segments.length===2;
    for(let index=0;index<2;index++){
      const layer=layerAt(index),video=layer.video,segment=segments[index];if(!segment){video.pause();layer.active=false;layer.canvas.style.opacity=0;cancelLayer(layer);continue;}
      const source=sources[index],url=source.preview_url;layer.active=true;layer.clip=segment.clip;layer.source=source;layer.project=state.project;layer.unitId=drama.id;
      if(source.expires_at)$('previewExpiry').textContent=` 当前链接有效至 ${new Date(expiryTime(source.expires_at)).toLocaleTimeString('zh-CN')}，届时自动续签。`;
      if(!url){layer.active=false;cancelLayer(layer);video.removeAttribute('src');delete video.dataset.sourceId;delete video.dataset.previewUrl;video.load();$('previewPlaceholder').classList.remove('hidden');$('previewMessage').textContent='此来源暂未提供浏览器预览，可继续编辑和保存';video.pause();layer.canvas.style.opacity=0;continue;}
      if(video.dataset.sourceId!==segment.clip.source_id||video.dataset.previewUrl!==url){cancelLayer(layer);video.src=url;video.dataset.sourceId=segment.clip.source_id;video.dataset.previewUrl=url;}
      const localTime=toFrame(segment.clip.start_ms)/30+(position-segment.start)/1000;
      if(!Number.isFinite(video.currentTime)||Math.abs(video.currentTime-localTime)>(play?.22:.001)){try{video.currentTime=localTime;}catch(_){}}
      const fade=both?Math.min(1,Math.max(0,(position-segments[1].start)/FADE_MS)):1;layer.canvas.style.opacity=both?(index===0?1:fade):1;video.volume=segment.clip.mute?0:(both?(index===0?1-fade:fade):1);video.muted=segment.clip.mute;
      if(drawPreviewLayer(layer))$('previewPlaceholder').classList.add('hidden');
      if(play){video.play().catch(()=>{});startLayerFrames(layer);}else video.pause();
    }
  }
  async function playPreview() {
    if(state.playing) { stopPreview(true); return; }
    const drama=currentDrama(); if(!drama||!drama.clips.length) return toast('先添加剧集再预览');
    const errors=validateClips(drama.clips,state.sources); if(errors.length) return toast(errors[0],true);
    if(state.playMs>=totalDuration(drama.clips)) state.playMs=0;
    state.playing=true; state.playOrigin=performance.now()-state.playMs; $('playPreview').textContent='Ⅱ'; $('playPreview').setAttribute('aria-label','暂停时间线');
    let lastDraw=-1000;
    const tick=now=> { if(!state.playing)return; state.playMs=Math.min(totalDuration(drama.clips),now-state.playOrigin); updatePlayhead(); if(now-lastDraw>85){lastDraw=now;previewAt(state.playMs,true).catch(error=>{stopPreview();toast(error.message,true);});} if(state.playMs>=totalDuration(drama.clips)){stopPreview(true);return;} state.playTick=requestAnimationFrame(tick); };
    state.playTick=requestAnimationFrame(tick);
  }
  function serializeProject(project=state.project){return draftForSave(project);}
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
      state.project.id=saved.id;state.project.version=saved.version;state.project.issues=saved.issues||[];state.operationId=null;state.pendingCreate=null;state.conflicted=false;$('saveCopy').classList.add('hidden');
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
  function renderProjectPicker(){$('projectPicker').innerHTML='<option value="">打开项目…</option>'+state.projects.map(project=>`<option value="${esc(project.id)}">${esc(project.name)}</option>`).join('');$('projectPicker').value=state.project.id||'';}
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
      state.project=adaptProject(project);state.selectedUnits=new Set(project.dramas.map(unit=>unit.id));state.renderIssues=[];state.activeDrama=0;state.selectedClip=null;state.undo=[];state.dirty=false;state.operationId=null;state.pendingCreate=null;state.renderOperation=null;state.conflicted=false;state.sourceLoadFailed=false;
      const ids=[...new Set(project.dramas.flatMap(drama=>drama.clips.map(clip=>clip.source_id)).filter(Boolean))];
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
      const retainedIds=state.project.dramas.flatMap(drama=>drama.clips.map(clip=>clip.source_id)).filter(Boolean);
      state.sourceLoading=false;state.sourceLoadFailed=retainedIds.some(sourceId=>!durationOf(state.sources[sourceId]));
      $('saveStatus').textContent=`打开项目失败：${error.message}${state.sourceLoadFailed?'；当前项目媒体信息未载入，请点击重试载入':''}`;
    }throw error;}
    finally{if(requestSeq===state.projectLoadSeq){state.projectOpening=0;$('projectPicker').removeAttribute('aria-busy');syncEditorLocks();}}
  }
  async function renderProject(){
    const unitIds=state.project.dramas.filter(unit=>state.selectedUnits.has(unit.id)).map(unit=>unit.id);
    if(!unitIds.length)return toast('请勾选要制作的资源单元');
    if(demo){state.tasks=state.project.dramas.filter(unit=>unitIds.includes(unit.id)).map(unit=>({id:`demo-task-${unit.id}`,unit_id:unit.id,series_code:unit.series_code,drama_title:unitTitle(unit),status:'queued',stage:'模拟排队',created_at:new Date().toISOString()}));renderTasks();setView('tasks');return toast('模拟制作任务已生成，不会调用 GPU');}
    if(editingLocked())return;state.renderSubmitting=true;syncEditorLocks();const project=state.project;
    const submit=async pending=>{try{return await api(pending.path,{method:'POST',body:pending.body});}catch(error){if(error.status===409&&error.code==='project_version_conflict'){if(state.pendingRender===pending)state.pendingRender=null;state.renderOperation=null;error.message='项目版本已更新，请重新打开项目后再制作。';}throw error;}};
    try{
      await flushDraft();if(project!==state.project)return;
      let result;
      if(state.pendingRender){
        const pending=state.pendingRender;
        const prior=await submit(pending);
        if(state.pendingRender===pending)state.pendingRender=null;
        if(pending.project_id===project.id&&pending.body.expected_version===project.version&&canonical(pending.body.unit_ids)===canonical(unitIds))result=prior;
      }
      if(!result){const operation=state.renderOperation||(state.renderOperation=uid());const pending={project_id:project.id,path:`${ROOT}/projects/${encodeURIComponent(project.id)}/render`,body:{operation_id:operation,expected_version:project.version,unit_ids:[...unitIds]}};state.pendingRender=pending;result=await submit(pending);if(state.pendingRender===pending)state.pendingRender=null;}
      state.renderIssues=result.issues||(result.results||[]).filter(row=>row.status==='invalid').map(row=>({unit_id:row.unit_id,code:row.code,message:row.message}));
      state.renderOperation=null;
      const failed=new Set(state.renderIssues.map(issue=>issue.unit_id));state.selectedUnits=failed;
      if(state.renderIssues.length){toast(`已受理 ${(result.items||[]).length} 个单元；${failed.size} 个单元待调整，草稿已保留`,true);const first=state.project.dramas.findIndex(unit=>failed.has(unit.id));if(first>=0)state.activeDrama=first;}
      else{toast('已加入制作队列，各单元分别出片');setView('tasks');}
      await loadTasks();
    }finally{state.renderSubmitting=false;renderEditor();}
  }

  const assetLoads=new Map();
  async function loadAssets(append=false){
    if(demo){renderAssets();return;}
    const query=$('assetSearch').value,kind=state.libraryKind,resource=state.libraryResource?.series_code||'',offset=append?state.assets.length:0,key=`${kind}:${resource}:${query}:${offset}`;
    if(assetLoads.has(key))return assetLoads.get(key);const sequence=++state.assetsLoadSeq;
    const request=(async()=>{const response=await api(`${ROOT}/assets?q=${encodeURIComponent(query)}&library_kind=${kind}&series_code=${encodeURIComponent(resource)}&status=all&limit=60&offset=${offset}`);if(sequence!==state.assetsLoadSeq||kind!==state.libraryKind||query!==$('assetSearch').value||resource!==(state.libraryResource?.series_code||''))return;state.assets=append?state.assets.concat(response.items||[]):response.items||[];state.assetsTotal=response.total??state.assets.length;rememberSources(state.assets);renderAssets();})();assetLoads.set(key,request);
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
  function resourceLabel(resource){return resource?.series_code?`${resource.english_title||resource.title||'英文名待补充'} · ${resource.series_code}`:'待关联资源';}
  function renderAssets(){
    $(state.libraryKind==='free_episode'?'freeCount':'assetCount').textContent=demo?state.assets.filter(asset=>(asset.library_kind||'material')===state.libraryKind&&(!state.libraryResource||asset.series_code===state.libraryResource.series_code)).length:(state.assetsTotal??state.assets.length);
    $('libraryResourceFilter').textContent=state.libraryResource?resourceLabel(state.libraryResource):'按资源筛选';$('clearLibraryResource').classList.toggle('hidden',!state.libraryResource);
    $('uploadResourceLabel').textContent=state.uploadResource?resourceLabel(state.uploadResource):'新上传必选一个资源';
    const list=demo?state.assets.filter(asset=>(asset.library_kind||'material')===state.libraryKind&&(!state.libraryResource||asset.series_code===state.libraryResource.series_code)):state.assets;
    $('assetGrid').innerHTML=list.map(asset=>`<article class="asset-card"><div class="asset-thumb">${asset.thumbnail_url?`<img src="${esc(asset.thumbnail_url)}" alt="" loading="lazy">`:'<span class="asset-symbol">▧</span>'}<span class="duration">${durationOf(asset)?`${seconds(durationOf(asset))} 秒`:esc(statusLabel(asset.status))}</span></div><div class="asset-meta"><strong title="${esc(asset.name)}">${esc(asset.name)}</strong><p class="resource-tag">${esc(resourceLabel(asset))}</p><p>${esc(statusLabel(asset.status))} · ${bytes(asset.size_bytes||0)}</p>${asset.error_message?`<p class="danger">${esc(asset.error_message)}</p>`:''}<div class="asset-actions"><button class="button" data-preview-asset="${esc(asset.id)}" ${asset.status!=='ready'?'disabled':''}>预览</button><button class="button" data-use-asset="${esc(asset.id)}" ${asset.status!=='ready'?'disabled':''}>用于剪辑</button>${asset.can_manage&&['uploaded','probing','failed'].includes(asset.status)?`<button class="button" data-probe-asset="${esc(asset.id)}">核实媒体校验</button>`:''}${asset.can_manage?`<button class="text-button" data-tag-asset="${esc(asset.id)}">资源标签</button><button class="text-button" data-rename-asset="${esc(asset.id)}">改名</button><button class="text-button danger" data-delete-asset="${esc(asset.id)}">删除</button>`:''}</div></div></article>`).join('')||'<div class="empty">暂无匹配视频，可选择资源后上传。</div>';
    $('loadMoreAssets').classList.toggle('hidden',state.assets.length>=state.assetsTotal);
  }
  function pickAsset(id){const asset=state.sources[id];if(!asset||asset.status!=='ready'||!currentDrama())return;if(asset.series_code!==currentDrama().series_code)return toast('请选择当前资源的视频',true);state.insertSourceId=id;$('assetDialog').close();showInsert();}
  function useAsset(id){const asset=state.sources[id];if(!asset?.series_code)return toast('请先为此视频补充资源标签',true);let unit=state.project.dramas.find(unit=>unit.series_code===asset.series_code);changed(()=>{if(!unit){unit={id:uid(),series_code:asset.series_code,title:asset.english_title||asset.name,clips:[]};state.project.dramas.push(unit);state.selectedUnits.add(unit.id);}state.activeDrama=state.project.dramas.indexOf(unit);unit.clips.push(newClip(asset));normalizeLast(unit.clips);});setView('editor');}

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
    const request=(async()=>{const response=await api(`${ROOT}/tasks`);state.tasks=response.items||[];state.tasks.forEach(task=>{if(task.material_replication?.summary)reconcileReplicationOperation(task.id,[task.material_replication.summary]);});renderTasks();if(replication.taskId)updateReplicationForm();})();state.tasksLoading=request;
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
  const replication={taskId:null,context:null,selected:new Set(),sequence:0,loading:false,operations:new Map(),historyTaskId:null,historySequence:0};
  function replicationPath(id,historyOnly=false){return `${ROOT}/tasks/${encodeURIComponent(id)}/material-replication${historyOnly?'?history_only=1':''}`;}
  function replicationOperationKey(id){const user=state.auth?.user||{};return `video-editing:replication:${user.tenant_key||''}:${user.id||user.username||user.name||''}:${id}`;}
  function pendingReplication(id){
    if(replication.operations.has(String(id)))return replication.operations.get(String(id));
    try{const saved=JSON.parse(global.sessionStorage?.getItem(replicationOperationKey(id))||'null');if(saved?.payload?.operation_id&&saved.taskId===String(id)){saved.phase=['conflict','awaiting'].includes(saved.phase)?saved.phase:'unknown';replication.operations.set(String(id),saved);return saved;}}catch(error){/* Storage is optional; the in-memory operation remains authoritative. */}
    return null;
  }
  function saveReplicationOperation(id,operation){
    if(operation)replication.operations.set(String(id),operation);else replication.operations.delete(String(id));
    try{if(operation)global.sessionStorage?.setItem(replicationOperationKey(id),JSON.stringify(operation));else global.sessionStorage?.removeItem(replicationOperationKey(id));}catch(error){/* Browser storage may be unavailable. */}
  }
  function replicationAvailability(task){
    if(!['completed','succeeded'].includes(task.status))return {enabled:false,reason:'成片制作完成并入库后可复刻'};
    if(task.material_replication?.enabled)return {enabled:true,reason:''};
    return {enabled:false,reason:task.material_replication?.disabled_reason||(!task.material_upload?.material_id?'请先将成片上传至素材库':'复刻入口暂不可用，请刷新任务')};
  }
  function replicationCardView(task){
    const available=replicationAvailability(task),meta=task.material_replication||{},record=meta.summary;
    const button=`<button class="button small replication-launch" data-replicate-task="${esc(task.id)}" ${available.enabled?'':`disabled title="${esc(available.reason)}"`}>素材复刻</button>`;
    const history=meta.count?`<button class="text-button" data-replication-history="${esc(task.id)}">复刻记录 (${esc(meta.count)})</button>`:'';
    const detail=record?`<div class="replication-card-receipt"><span>复刻回执</span><strong class="${replicationReceipt(record).tone}">${esc(replicationReceipt(record).label)}</strong><span>${esc(replicationLanguageText(record.target_langs))} · 归属 ${esc(record.operator||'待核实')} · 素材分类：${esc(replicationCategoryLabel(record.material_category))}${record.historical?' · 历史导入记录':''}</span><span class="micro">成品与回推结果未查询</span>${history}</div>`:history?`<div class="replication-card-receipt">${history}</div>`:'';
    return {action:button,detail,hint:available.enabled?'':`<p class="micro replication-disabled">${esc(available.reason)}</p>`};
  }
  function replicationLanguageText(codes){return Array.isArray(codes)&&codes.length?codes.map(code=>{const language=replication.context?.target_languages?.find(entry=>entry.code===code);return language?`${language.name} ${code}`:String(code);}).join('、'):'无';}
  function replicationConfigurationText(record){return `${replicationTaskLabels[record.task_type]||'类型待核实'} · ${replicationAudioLabels[record.audio_type]||'配音待核实'} · ${Number(record.target_material_type)===2?`解说 ${record.descant_start_time||'待填写'}–${record.descant_end_time||'待填写'}`:'非解说'} · 素材分类：${replicationCategoryLabel(record.material_category)} · ${record.no_bgm?'母素材无背景音乐':'未标记无背景音乐'}`;}
  function replicationRecordHtml(record){
    const view=replicationReceipt(record),error=typeof record.error==='string'?record.error:record.error?JSON.stringify(record.error):'';
    const recordedAt=record.submitted_at||record.created_at,recordedTime=recordedAt?new Date(expiryTime(recordedAt)):null,timeText=recordedTime&&Number.isFinite(recordedTime.getTime())?recordedTime.toLocaleString('zh-CN'):'';
    return `<article class="replication-record"><div class="replication-record-heading"><strong class="${view.tone}">${esc(view.label)}</strong><span>${esc(timeText)}</span></div><p>${record.b_task_id?`任务号 ${esc(record.b_task_id)}`:'尚未获得制作任务号'} · 归属 ${esc(record.operator||'待核实')}${record.historical?' · 历史导入回执':''}</p><p>${esc(replicationConfigurationText(record))}</p><p>申请语种：${esc(replicationLanguageText(record.target_langs))}</p><p>有效语种：${['unknown_outcome','pending','submitting'].includes(record.status)?'待核实':esc(replicationLanguageText(record.effective_langs))}${record.unsupported_langs?.length?` · 不支持：${esc(replicationLanguageText(record.unsupported_langs))}`:''}</p>${error?`<p class="danger">${esc(error)}</p>`:''}<p>${esc(view.message)}</p>${record.operation_id?`<p class="micro">请求标识：${esc(record.operation_id)}</p>`:''}</article>`;
  }
  function replicationChoice(name,fallback){return document.querySelector(`[name="${name}"]:checked`)?.value||fallback;}
  function replicationPayload(){
    const payload={task_type:Number(replicationChoice('replicationTaskType','1')),audio_type:Number(replicationChoice('replicationAudioType','0')),target_material_type:Number(replicationChoice('replicationMaterialType','1')),material_category:$('replicationCategory').value,target_langs:[...replication.selected],no_bgm:$('replicationNoBgm').checked};
    if(replication.context?.can_change_operator)payload.operator=$('replicationOperator').value.trim();
    if(payload.target_material_type===2){payload.descant_start_time=$('replicationStart').value.trim();payload.descant_end_time=$('replicationEnd').value.trim();}
    return payload;
  }
  function replicationError(message){$('replicationError').textContent=message||'';$('replicationError').classList.toggle('hidden',!message);if(message)$('replicationError').scrollIntoView?.({block:'nearest'});}
  function renderReplicationLanguages(){
    const query=$('replicationSearch').value.trim().toLowerCase(),languages=replication.context?.target_languages||[];
    const visible=languages.filter(language=>`${language.name} ${language.code}`.toLowerCase().includes(query));
    $('replicationLanguages').innerHTML=visible.map(language=>`<label class="replication-language ${replication.selected.has(language.code)?'is-selected':''}"><input type="checkbox" data-replication-language="${esc(language.code)}" ${replication.selected.has(language.code)?'checked':''}><span>${esc(language.name)}</span><small>${esc(language.code)}</small></label>`).join('')||'<p class="micro">没有匹配的目标语种。</p>';
    $('replicationSelected').innerHTML=[...replication.selected].map(code=>`<button type="button" class="replication-chip" data-remove-replication-language="${esc(code)}" aria-label="移除 ${esc(code)}">${esc(replicationLanguageText([code]))} ×</button>`).join('')||'<span class="micro">尚未选择目标语种，可选择与母素材相同的语种。</span>';
    $('replicationLanguageCount').textContent=String(replication.selected.size);
  }
  function updateReplicationForm(){
    const payload=replicationPayload(),context=replication.context,operation=pendingReplication(replication.taskId),sending=operation?.phase==='sending';
    $('replicationFields').disabled=replication.loading||!context?.enabled||!!operation;
    $('replicationSubmit').disabled=replication.loading||!context?.enabled||!!operation;
    $('replicationSubmit').textContent=sending?'正在提交…':'提交素材复刻';
    $('replicationSubmit').setAttribute('aria-busy',String(sending));
    $('replicationNarration').classList.toggle('hidden',payload.target_material_type!==2);
    $('replicationSummaryTitle').textContent=`${replicationTaskLabels[payload.task_type]} · 1 条母素材 × ${payload.target_langs.length} 个目标语种`;
    $('replicationSummaryDetail').textContent=`${replicationConfigurationText(payload)} · 归属 ${context?.can_change_operator?payload.operator||'待填写':context?.default_operator||'待配置'} · ${replicationLanguageText(payload.target_langs)}`;
    const notes=[];if(payload.audio_type!==0)notes.push('有配音制作需具备对应语种的配音资源。');if([3,4].includes(payload.task_type)&&payload.audio_type===0)notes.push('竞品或爆款反转复刻选择无配音时，需完整字幕与剧集资源。');if(payload.target_material_type===2)notes.push('解说制作需目标语种支持解说。');notes.push('目标语种是否支持，以正式接口返回为准。');$('replicationSupport').textContent=notes.join(' ');
    $('replicationPending').classList.toggle('hidden',!operation);
    $('replicationPendingText').textContent=operation?`${sending?'正在等待受理回执':operation.phase==='awaiting'?'请求已登记，等待接口受理':'此请求结果尚待核实，已保留原参数'}。原请求：${replicationConfigurationText(operation.payload)} · ${replicationLanguageText(operation.payload.target_langs)} · 归属 ${operation.payload.operator||context?.default_operator||'素材创建人'}。请求标识：${operation.payload.operation_id}`:'';
    $('replicationReplay').disabled=!operation||operation.phase!=='unknown';
    $('replicationVerify').disabled=!operation||sending;
    document.querySelectorAll('[data-replication-choice]').forEach(input=>input.closest('label')?.classList.toggle('is-selected',input.checked));
  }
  function showReplicationReceipt(record,response={}){
    const conflict=response.conflict||record.conflict,reused=response.reused||record.reused,fields=response.conflict_fields||record.conflict_fields||[];
    const conflictValue=(field,value)=>field==='material_category'?replicationCategoryLabel(value):typeof value==='object'?JSON.stringify(value):value;
    let notice='';
    if(conflict)notice=`<div class="replication-conflict" role="alert"><strong>已存在的任务与本次参数不同</strong><p>以下展示旧任务，本次新参数未生效。</p>${fields.map(field=>`<p>${esc(({task_type:'复刻类型',audio_type:'配音方式',target_material_type:'成品类型',material_category:'素材分类',operator:'归属',no_bgm:'母素材无背景音乐',descant_start_time:'解说开始时间',descant_end_time:'解说结束时间'})[field.field]||field.field)}：原值 ${esc(conflictValue(field.field,field.previous))} → 本次 ${esc(conflictValue(field.field,field.requested))}</p>`).join('')}</div>`;
    else if(reused)notice='<p class="replication-conflict">已复用已有任务，本次未创建新任务。</p>';
    $('replicationReceipt').innerHTML=notice+replicationRecordHtml(record);$('replicationReceipt').classList.remove('hidden');
  }
  async function openReplication(id){
    if(demo)return toast('演示模式不可提交素材复刻');
    const sequence=++replication.sequence;replication.taskId=String(id);replication.context=null;replication.loading=true;replication.selected=new Set();
    for(const [name,value]of [['replicationTaskType','1'],['replicationAudioType','0'],['replicationMaterialType','1']])document.querySelectorAll(`[name="${name}"]`).forEach(input=>input.checked=input.value===value);
    $('replicationCategory').value='混剪';
    $('replicationOperator').value='';$('replicationStart').value='';$('replicationEnd').value='';$('replicationSearch').value='';$('replicationNoBgm').checked=false;$('replicationReceipt').classList.add('hidden');replicationError('');$('replicationSource').textContent='正在加载母素材与可用语种…';$('replicationRoleNote').textContent='';
    renderReplicationLanguages();updateReplicationForm();if(!$('replicationDialog').open)$('replicationDialog').showModal();$('replicationBody').scrollTop=0;
    try{
      const context=item(await api(replicationPath(id)));if(sequence!==replication.sequence||replication.taskId!==String(id))return;
      replication.context=context;const source=context.source||{};
      $('replicationSource').textContent=`${source.name||'母素材'} · 素材 ${source.material_id||'待入库'} · 剧资源 ${source.resource_id||'待关联'} · 源语种 ${source.source_language||'待确认'} · ${seconds(source.duration_ms)} 秒`;
      $('replicationOperator').value=context.default_operator||'';$('replicationOperator').readOnly=!context.can_change_operator;
      $('replicationRoleNote').textContent=context.operator_error|| (context.can_change_operator?'默认沿用素材创建人；管理员可修改成品归属账号。':'成品归属沿用素材创建人，只有管理员可修改。');
      if(!context.enabled)replicationError(context.disabled_reason||'此成片暂不可复刻');
      reconcileReplicationOperation(id,context.history||[]);renderReplicationLanguages();
    }catch(error){if(sequence===replication.sequence)replicationError(`复刻信息加载失败：${error.message}`);}
    finally{if(sequence===replication.sequence){replication.loading=false;updateReplicationForm();document.querySelector('[name="replicationTaskType"]:checked')?.focus({preventScroll:true});$('replicationBody').scrollTop=0;}}
  }
  function reconcileReplicationOperation(id,history){
    const operation=pendingReplication(id);if(!operation)return;
    const record=history.find(entry=>entry.operation_id===operation.payload.operation_id||(entry.operation_ids||[]).includes(operation.payload.operation_id));if(!record)return;
    if(['accepted','partial','rejected'].includes(record.status))saveReplicationOperation(id,null);
    else if(operation.phase!=='sending'&&operation.phase!=='conflict'){operation.phase=['pending','submitting'].includes(record.status)?'awaiting':'unknown';saveReplicationOperation(id,operation);}
    if(replication.taskId===String(id)){
      const reused=record.operation_id!==operation.payload.operation_id;
      const conflict_fields=reused?['task_type','audio_type','target_material_type','material_category','operator','no_bgm','descant_start_time','descant_end_time'].filter(field=>Object.prototype.hasOwnProperty.call(operation.payload,field)&&String(record[field]??'')!==String(operation.payload[field]??'')).map(field=>({field,previous:record[field],requested:operation.payload[field]})):[];
      showReplicationReceipt(record,{reused,conflict:conflict_fields.length>0,conflict_fields,...(operation.receiptFlags||{})});
    }
  }
  async function verifyReplication(){
    const id=replication.taskId,sequence=replication.sequence;
    return withBusy(`replication-verify:${id}`,$('replicationVerify'),'正在核实…',async()=>{
      const context=item(await api(replicationPath(id,true)));reconcileReplicationOperation(id,context.history||[]);
      if(sequence!==replication.sequence)return;
      if(pendingReplication(id))replicationError(pendingReplication(id).phase==='awaiting'?'请求已登记，仍在等待接口受理。请稍后核实记录。':'尚未获得最终受理回执。可继续核实记录；如需重放，只会使用已保留的原请求标识和参数。');else replicationError('');
      updateReplicationForm();await loadTasks();
    });
  }
  async function submitReplication(replay=false){
    const id=replication.taskId,sequence=replication.sequence;if(!id||replication.loading||!replication.context?.enabled)return;
    let operation=pendingReplication(id);if(['sending','conflict','awaiting'].includes(operation?.phase))return;
    if(operation&&!replay){replicationError('请先核实上一次请求，或使用“重放原请求”。');return;}
    if(!operation){
      const payload=replicationPayload(),error=validateReplicationInput(payload,replication.context);if(error){replicationError(error);return;}
      operation={taskId:String(id),phase:'sending',payload:{...clone(payload),operation_id:uid()}};
    }else if(!replay)return;
    operation.phase='sending';saveReplicationOperation(id,operation);replicationError('');updateReplicationForm();
    const Controller=global.AbortController||(typeof AbortController==='function'?AbortController:null),controller=Controller?new Controller():null;
    let timedOut=false;const timeout=controller?setTimeout(()=>{timedOut=true;controller.abort();},45000):null;
    try{
      const response=await api(replicationPath(id),{method:'POST',body:operation.payload,...(controller?{signal:controller.signal}:{})}),record=item(response);
      if(!record||!['pending','submitting','accepted','partial','rejected','unknown_outcome'].includes(record.status))throw new Error('提交回执无效，结果需要核实');
      const unresolved=['pending','submitting','unknown_outcome'].includes(record.status);operation.phase=['pending','submitting'].includes(record.status)?'awaiting':'unknown';
      if(response.reused||record.reused||response.conflict||record.conflict)operation.receiptFlags={reused:!!(response.reused||record.reused),conflict:!!(response.conflict||record.conflict),conflict_fields:response.conflict_fields||record.conflict_fields||[]};
      saveReplicationOperation(id,unresolved?operation:null);
      if(sequence===replication.sequence){showReplicationReceipt(record,response);if(response.conflict||record.conflict)replicationError('已展示旧任务及参数差异，本次新参数未生效。');}
      await loadTasks().catch(error=>toast(`受理回执已保存，任务列表刷新失败：${error.message}`,true));
    }catch(error){
      if(error.status===409){operation.phase='conflict';saveReplicationOperation(id,operation);if(sequence===replication.sequence)replicationError('请求标识与已登记参数冲突，草稿和原请求均已保留。请核实记录；不会自动更换请求标识或重新提交。');}
      else if(error.status&&error.status<500&&error.status!==408&&error.status!==429){saveReplicationOperation(id,null);if(sequence===replication.sequence)replicationError(`提交失败：${error.message}`);}
      else{operation.phase='unknown';saveReplicationOperation(id,operation);if(sequence===replication.sequence){showReplicationReceipt({...operation.payload,status:'unknown_outcome',operator:operation.payload.operator||replication.context.default_operator,effective_langs:[],unsupported_langs:[],error:'尚未获得可确认的受理回执'});replicationError(`${timedOut?'提交超时，受理结果尚未确认':error.message}。请先核实记录，系统不会自动重新提交。`);}}
    }finally{if(timeout!==null)clearTimeout(timeout);if(replication.taskId===String(id))updateReplicationForm();}
  }
  async function openReplicationHistory(id){
    if(demo)return toast('演示模式没有正式复刻记录');
    const sequence=++replication.historySequence;replication.historyTaskId=String(id);$('replicationHistoryList').innerHTML='<p class="empty">正在加载复刻记录…</p>';if(!$('replicationHistoryDialog').open)$('replicationHistoryDialog').showModal();
    try{const context=item(await api(replicationPath(id,true)));if(sequence!==replication.historySequence)return;const history=context.history||[];reconcileReplicationOperation(id,history);$('replicationHistoryList').innerHTML=history.map(replicationRecordHtml).join('')||'<p class="empty">暂无复刻记录。</p>';if(replication.taskId===String(id))updateReplicationForm();}
    catch(error){if(sequence===replication.historySequence)$('replicationHistoryList').innerHTML=`<p class="danger">${esc(error.message)}</p>`;}
  }
  function bindReplication(run){
    $('replicationForm').addEventListener('submit',run(event=>{event.preventDefault();return submitReplication();}));
    $('replicationForm').addEventListener('change',event=>{const input=event.target;if(input.dataset.replicationLanguage){if(input.checked)replication.selected.add(input.dataset.replicationLanguage);else replication.selected.delete(input.dataset.replicationLanguage);renderReplicationLanguages();}replicationError('');updateReplicationForm();});
    $('replicationForm').addEventListener('input',event=>{if(event.target.id==='replicationSearch')renderReplicationLanguages();else{replicationError('');updateReplicationForm();}});
    $('replicationSelected').addEventListener('click',event=>{const button=event.target.closest('[data-remove-replication-language]');if(!button||pendingReplication(replication.taskId))return;replication.selected.delete(button.dataset.removeReplicationLanguage);renderReplicationLanguages();updateReplicationForm();});
    $('replicationClearLanguages').addEventListener('click',()=>{replication.selected.clear();renderReplicationLanguages();updateReplicationForm();});
    $('replicationVerify').addEventListener('click',run(verifyReplication));$('replicationReplay').addEventListener('click',run(()=>submitReplication(true)));
    $('replicationViewHistory').addEventListener('click',run(()=>openReplicationHistory(replication.taskId)));$('replicationRefreshHistory').addEventListener('click',run(()=>openReplicationHistory(replication.historyTaskId)));
    $('replicationDialog').addEventListener('close',()=>{replication.sequence++;});
    $('replicationHistoryDialog').addEventListener('close',()=>{replication.historySequence++;});
  }
  function renderTasks(){
    $('taskCount').textContent=state.tasks.length;
    $('taskList').innerHTML=state.tasks.map(task=>{const material=materialUploadView(task),replica=replicationCardView(task);return `<article class="task-card"><div class="task-icon">▷</div><div class="task-info"><strong>${esc(task.drama_title||task.title||task.id)}</strong><p>${esc(task.id)} · ${task.created_at?new Date(expiryTime(task.created_at)).toLocaleString('zh-CN'):''}</p>${task.error_message?`<p class="danger">${esc(task.error_message)}</p>`:''}${task.output&&task.output.metadata?`<p>720 × 1280 · ${seconds(task.output.metadata.duration_ms)} 秒 · MP4</p>`:''}${material.detail}${replica.hint}</div><div><div class="task-status ${esc(task.status)}">${esc(statusLabel(task.status))}</div><p class="micro">${esc(taskProgressText(task))}</p></div><div class="task-actions">${material.action}${replica.action}${task.output?`<button class="button small" data-preview-task="${esc(task.id)}">预览</button><button class="button small" data-download-task="${esc(task.id)}">下载</button>`:''}${['failed','unknown','recovery_required'].includes(task.status)?`<button class="button small" data-resume-task="${esc(task.id)}">核实并重试</button>`:''}</div>${replica.detail}</article>`;}).join('')||'<div class="empty">还没有制作任务。完成剪辑后，点击“制作成片”。</div>';
  }
  async function uploadMaterialTask(id,variant=null){
    if(demo)return toast('模拟预览不会上传至素材库');
    const task=state.tasks.find(entry=>String(entry.id)===String(id));
    if(!task||!task.output||!['completed','succeeded'].includes(task.status)||task.material_upload||state.materialUploadSubmitting.has(String(id)))return;
    if(task.series_code&&!variant){await openMaterialVariants(task);return;}
    state.materialUploadSubmitting.add(String(id));renderTasks();
    try{
      const upload=item(await api(`${ROOT}/tasks/${encodeURIComponent(id)}/material-source`,{method:'POST',body:variant?{app_id:variant.app_id,content_id:variant.content_id}:{}}));
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
    return !!(intent && state.pendingAdd===intent && !intent.completed && $('dramaDialog').open && intent.session===state.catalogSessionSeq && intent.project===state.project && intent.projectSeq===state.projectLoadSeq && drama && String(drama.app_id)===String(intent.drama.app_id) && String(drama.content_id)===String(intent.drama.content_id)&&state.project.dramas.some(unit=>unit.id===intent.unit_id&&unit.series_code===intent.series_code));
  }
  function cancelPendingAdd(message='已取消自动添加。已排队的媒体校验会继续，可重新选择后添加。') {
    if(!state.pendingAdd)return;state.pendingAdd=null;renderEpisodes();setCatalogStatus(message);
  }
  function closeCatalogSession() { cancelPendingAdd();state.catalogSessionSeq++;state.catalogLoadSeq++; }
  async function openCatalog(button=$('onlineSources')) {
    const unit=currentDrama();if(!unit?.series_code)return toast('请先选择当前单元的目标资源');
    if(!$('dramaDialog').open){state.catalogSessionSeq++;$('dramaDialog').showModal();}
    state.catalogTargetUnit=unit.id;const session=state.catalogSessionSeq,sequence=++state.catalogLoadSeq;state.selectedCatalog=null;state.catalog=[];state.episodes=[];renderEpisodes();setCatalogStatus('正在加载该资源的产品和语言版本…',false,true);
    try{const response=demo?{items:[{app_id:'demo-product',content_id:'demo-101',title:unit.title,language:'en',country:'US'}]}:await api(`${ROOT}/catalog/resource-variants?series_code=${encodeURIComponent(unit.series_code)}`);if(!catalogCurrent(session,sequence))return;state.catalog=(response.items||[]).map(variant=>({...variant,series_code:unit.series_code}));renderCatalog();setCatalogStatus(state.catalog.length?'选择产品和语言版本，再勾选在线剧集。':'该资源暂无在线剧版本，仍可使用上传视频制作。');}catch(error){if(catalogCurrent(session,sequence))setCatalogStatus(`目录暂不可用：${error.message}。不影响草稿保存或已上传视频制作。`,true);}
  }

  function renderCatalog(){$('catalogDramas').innerHTML=state.catalog.map((drama,index)=>`<button data-catalog-index="${index}" class="${state.selectedCatalog===index?'active':''}"><strong>${esc(drama.title||drama.name)}</strong><small>${esc(drama.app||drama.app_id)} · ${esc(drama.language||'语言待确认')} · ${esc(drama.content_id)}</small></button>`).join('')||'<div class="empty">未找到短剧</div>';}
  async function selectCatalog(index) {
    const drama=state.catalog[index];if(!drama)return;
    cancelPendingAdd('已切换短剧，本次自动添加已取消。');
    const session=state.catalogSessionSeq,requestSeq=++state.catalogLoadSeq;state.selectedCatalog=index;state.episodes=[];renderCatalog();renderEpisodes();
    $('catalogEpisodes').innerHTML='<div class="empty">正在加载剧集…</div>';setCatalogStatus(`正在加载“${drama.title||drama.name}”的剧集…`,false,true);
    try{
      const episodes=demo?[1,2,3,4].map(number=>({id:`${drama.content_id}-ep-${number}`,source_id:`${drama.content_id}-ep-${number}`,kind:'episode',name:`第 ${number} 集`,episode_number:number,app_id:drama.app_id,content_id:drama.content_id,status:'ready',metadata:{duration_ms:45000+number*7000,width:720,height:1280,has_audio:true}})):(await api(`${ROOT}/catalog/episodes?app_id=${encodeURIComponent(drama.app_id)}&content_id=${encodeURIComponent(drama.content_id)}`)).items||[];
      if(!catalogCurrent(session,requestSeq))return;state.episodes=episodes.map(source=>({...source,series_code:source.series_code||drama.series_code}));rememberSources(state.episodes);renderEpisodes();setCatalogStatus(episodes.length?'勾选需要的剧集后点击添加；未就绪剧集将自动校验。':'该短剧暂无可用剧集。');
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
    const target=state.project.dramas.find(unit=>unit.id===intent.unit_id);if(!target)return false;
    if(target.clips.length+intent.ids.length>300){failPendingAdd(intent,'每个单元最多 300 个片段，请减少选择');return false;}
    intent.completed=true;state.pendingAdd=null;renderEpisodes();changed(()=>{intent.ids.forEach(id=>target.clips.push(newClip(state.sources[id])));normalizeLast(target.clips);state.activeDrama=state.project.dramas.indexOf(target);});$('dramaDialog').close();toast('已添加所选剧集，可继续插入或剪辑');return true;
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
    if(state.pendingAdd)return;const ids=Array.from(document.querySelectorAll('.episode-select:checked')).map(input=>input.value),drama=state.catalog[state.selectedCatalog],unit=state.project.dramas.find(entry=>entry.id===state.catalogTargetUnit);
    if(!ids.length){setCatalogStatus('请勾选所需剧集后添加。',true);return;}if(!drama||!unit)return;
    if(unit.clips.length+ids.length>300){setCatalogStatus('每个单元最多 300 个片段。',true);return;}
    const intent={project:state.project,projectSeq:state.projectLoadSeq,session:state.catalogSessionSeq,unit_id:unit.id,series_code:unit.series_code,drama:{...drama},ids:[...new Set(ids)],phase:'preparing',completed:false};state.pendingAdd=intent;renderEpisodes();pendingProgress(intent);
    try{const unready=intent.ids.filter(id=>!sourceReady(id));if(unready.length)await prepareEpisodeSources(unready,intent);if(!pendingCurrent(intent))return;intent.phase='waiting';if(!finishPendingAdd(intent))pendingProgress(intent);}catch(error){failPendingAdd(intent,error.message);}
  }

  async function pollUpdates() {
    if(!state.ready||document.hidden)return;if(state.polling)return state.polling;
    const work=(async()=>{
      const hadRecoveries=state.probeRecoveries.size>0,intent=state.pendingAdd;
      const jobs=[(async()=>{try{if(hadRecoveries)await refreshProbeRecoveries(intent);await refreshPendingAdd(intent);}catch(error){if(pendingCurrent(intent))setCatalogStatus(`查询校验进度失败：${error.message}。稍后自动核实，也可取消等待。`,true);}})()];
      if(state.activeView==='tasks')jobs.push(loadTasks());
      if(['library','free'].includes(state.activeView)&&(hadRecoveries||state.assets.some(asset=>['uploading','uploaded','probing'].includes(asset.status))))jobs.push(loadAssets());
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
  async function uploadFile(file,uploadContext={library_kind:state.libraryKind,resource:state.uploadResource}){
    if(demo)return toast('模拟预览不会上传文件，请在正式页面上传');if(!file.size||file.size>state.config.max_file_bytes)return toast(`${file.name}：文件应大于 0 且不超过 ${bytes(state.config.max_file_bytes)}`,true);
    const key=fileFingerprint(file);if(state.uploads.has(key)&&!state.uploads.get(key).done)return toast('此文件正在上传');
    const upload={key,file,requests:new Set(),progress:{},completedBytes:0,percent:0,message:'准备直传 COS…',done:false,cancelled:false};state.uploads.set(key,upload);renderUploads();
    let saved=uploadStore()[key];
    try{
      let session;
      if(saved&&saved.id){try{session=item(await api(`${ROOT}/uploads/${encodeURIComponent(saved.id)}`));}catch(error){if(error.status!==404)throw error;}}
      if(!session||['aborted','failed','expired'].includes(session.status)){if(state.config.intake_enabled===false&&!(saved&&saved.operation_id&&!session))throw new Error('新上传已暂停，已有上传仍可继续');const identity=saved?.resource?{library_kind:saved.library_kind,resource:saved.resource}:uploadContext;if(!identity.resource?.series_code)throw new Error('请选择上传视频的资源标签');saved={operation_id:!session&&saved&&saved.operation_id?saved.operation_id:uid(),...identity};storeUpload(key,saved);session=item(await api(`${ROOT}/uploads`,{method:'POST',body:{operation_id:saved.operation_id,name:file.name,size_bytes:file.size,content_type:file.type||'application/octet-stream',library_kind:saved.library_kind,series_code:saved.resource.series_code}}));storeUpload(key,{...saved,id:session.id});}
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
    const sources=[{id:'demo-asset',kind:'asset',library_kind:'material',series_code:'21481',english_title:'Love & Revenge of Malos',name:'悬念开场.mp4',status:'ready',can_manage:true,size_bytes:23445567,metadata:{duration_ms:8000,width:1080,height:1920,has_audio:true}},{id:'demo-free',kind:'asset',library_kind:'free_episode',series_code:'21481',english_title:'Love & Revenge of Malos',name:'免费集 1–5.mp4',status:'ready',can_manage:true,size_bytes:148736120,metadata:{duration_ms:360000,width:720,height:1280,has_audio:true}},{id:'demo-asset-2',kind:'asset',library_kind:'material',series_code:'21482',english_title:'Her Second Life',name:'情绪特写.mp4',status:'ready',can_manage:true,size_bytes:14873612,metadata:{duration_ms:12000,width:1920,height:1080,has_audio:true}}].map(source=>({...source,source_id:source.id}));
    rememberSources(sources);state.assets=sources;state.assetsTotal=3;$('freeCount').textContent=1;
    state.project=adaptProject({schema_version:2,id:'demo-project',name:'资源剪辑体验',version:1,units:[{id:'unit_demo1',series_code:'21481',title:'Love & Revenge of Malos',clips:[newClip(sources[0],{end_ms:5000,transition:'fade'}),newClip(sources[1],{start_ms:12000,end_ms:30000,mirror:true,subtitle_band:{top:960,bottom:1280}})]},{id:'unit_demo2',series_code:'21482',title:'Her Second Life',clips:[newClip(sources[2])]}]});
    state.selectedUnits=new Set(state.project.dramas.map(unit=>unit.id));state.projects=[clone(state.project)];state.selectedClip=state.project.dramas[0].clips[1].id;state.playMs=4700;
    state.tasks=[{id:'demo-completed-task',series_code:'21481',unit_id:'unit_demo1',drama_title:'Love & Revenge of Malos',status:'completed',stage:'completed',progress:1,created_at:new Date().toISOString(),output:{metadata:{duration_ms:22700}}}];$('saveStatus').textContent='模拟草稿 · 仅本页';$('previewMessage').textContent='演示画面，正式素材校验后可播放';renderProjectPicker();renderAssets();renderTasks();renderEditor();
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
    $('newProject').addEventListener('click',run(()=>withBusy('newProject',$('newProject'),'正在新建…',async()=>{if(state.renderSubmitting)return;cancelPendingAdd();const sequence=++state.projectLoadSeq;state.projectOpening=sequence;syncEditorLocks();try{await flushDraft();if(sequence!==state.projectLoadSeq)return;stopPreview();clearTimeout(state.saveTimer);state.project={schema_version:2,name:'未命名剪辑项目',dramas:[]};state.selectedUnits=new Set();state.renderIssues=[];state.activeDrama=0;state.selectedClip=null;state.undo=[];state.dirty=false;state.operationId=null;state.pendingCreate=null;state.renderOperation=null;state.sourceLoading=false;state.sourceLoadFailed=false;$('saveStatus').textContent='尚未保存';}finally{if(sequence===state.projectLoadSeq){state.projectOpening=0;renderEditor();}}})));
    $('saveProject').addEventListener('click',run(()=>withBusy('saveProject',$('saveProject'),'正在保存…',()=>saveProject())));$('renderProject').addEventListener('click',run(()=>withBusy('renderProject',$('renderProject'),'正在提交…',renderProject)));
    $('saveCopy').addEventListener('click',run(()=>withBusy('saveCopy',$('saveCopy'),'正在另存…',async()=>{cancelPendingAdd();if(state.saving)await state.saving.catch(()=>{});const copy=adaptProject(serializeProject());copy.name=(copy.name||'剪辑项目').slice(0,115)+' · 副本';state.project=copy;state.projectLoadSeq++;state.revision++;state.undo=[];state.operationId=null;state.pendingCreate=null;state.renderOperation=null;state.conflicted=false;state.dirty=true;renderEditor();await saveProject(true);})));
    $('undoEdit').addEventListener('click',()=>{if(!state.undo.length)return;stopPreview();const id=state.project.id,version=state.project.version;state.project={...state.undo.pop(),id,version};state.dirty=true;state.revision++;state.renderOperation=null;state.activeDrama=Math.max(0,Math.min(state.activeDrama,state.project.dramas.length-1));renderEditor();previewAt(state.playMs,false).catch(()=>{});scheduleSave();});
    $('chooseAsset').addEventListener('click',run(()=>openSources()));$('insertAsset').addEventListener('click',run(()=>openSources()));
    $('goUpload').addEventListener('click',()=>{$('assetDialog').close();setView(state.pickerKind==='free_episode'?'free':'library');});
    $('addDrama').addEventListener('click',()=>openResources({kind:'new-unit'}));$('addDramaBottom').addEventListener('click',()=>openResources({kind:'new-unit'}));$('changeUnitResource').addEventListener('click',()=>currentDrama()&&openResources({kind:'unit',unit_id:currentDrama().id}));
    $('onlineSources').addEventListener('click',run(()=>{$('assetDialog').close();return openCatalog();}));$('addEpisodes').addEventListener('click',run(addEpisodes));
    document.querySelectorAll('[data-source-kind]').forEach(button=>button.addEventListener('click',run(()=>{state.pickerKind=button.dataset.sourceKind;return openSources();})));
    $('insertSelectedSources').addEventListener('click',()=>{const ids=Array.from(document.querySelectorAll('.source-pick:checked')).map(input=>input.value);if(!ids.length)return toast('请先勾选视频');changed(()=>{const unit=currentDrama();ids.forEach(id=>unit.clips.push(newClip(state.sources[id])));normalizeLast(unit.clips);});$('assetDialog').close();});
        $('dramaList').addEventListener('click',event=>{const selection=event.target.closest('[data-render-unit]');if(selection){if(selection.checked)state.selectedUnits.add(selection.dataset.renderUnit);else state.selectedUnits.delete(selection.dataset.renderUnit);state.renderOperation=null;return;}const remove=event.target.closest('[data-remove-drama]');if(remove){changed(()=>{state.project.dramas.splice(Number(remove.dataset.removeDrama),1);state.activeDrama=Math.max(0,Math.min(state.activeDrama,state.project.dramas.length-1));});return;}const button=event.target.closest('[data-drama-index]');if(button){stopPreview();state.activeDrama=Number(button.dataset.dramaIndex);state.selectedClip=null;state.playMs=0;renderEditor();previewAt(0,false).catch(()=>{});}});
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
    $('confirmInsert').addEventListener('click',confirmInsert);
    $('applyCommon').addEventListener('click',run(()=>{if(!state.project.dramas.length)return;const dramas=applyCommon(state.project.dramas,null,'none',$('commonTransition').value,null);changed(()=>{state.project.dramas=dramas;});toast('已应用公共设置，逐剧剪切点保持不变');}));
    $('playPreview').addEventListener('click',run(playPreview));$('playhead').addEventListener('input',()=>{stopPreview();state.playMs=Number($('playhead').value);updatePlayhead();previewAt(state.playMs,false).catch(error=>toast(error.message,true));});$('resetPlay').addEventListener('click',()=>{stopPreview();state.playMs=0;updatePlayhead();previewAt(0,false).catch(()=>{});});
    for(const id of ['previewA','previewB'])$(id).addEventListener('error',()=>{if($(id).src){stopPreview();$('previewPlaceholder').classList.remove('hidden');$('previewMessage').textContent='此视频暂无法在浏览器播放，请刷新预览链接或等待代理完成';}});
    $('mediaDialog').addEventListener('close',()=>{$('mediaPlayer').pause();$('mediaPlayer').removeAttribute('src');$('mediaPlayer').load();});
    $('refreshTasks').addEventListener('click',run(()=>withBusy('refreshTasks',$('refreshTasks'),'正在刷新…',loadTasks)));$('loadMoreAssets').addEventListener('click',run(()=>withBusy('loadMoreAssets',$('loadMoreAssets'),'正在加载…',()=>loadAssets(true))));let searchTimer;$('assetSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadAssets().catch(error=>toast(error.message,true)),300);});
    $('uploadButton').addEventListener('click',()=>$('fileInput').click());$('uploadDrop').addEventListener('click',()=>$('fileInput').click());$('uploadDrop').addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('fileInput').click();}});$('fileInput').addEventListener('change',()=>{queueFiles(Array.from($('fileInput').files));$('fileInput').value='';});
    for(const eventName of ['dragenter','dragover'])$('uploadDrop').addEventListener(eventName,event=>{event.preventDefault();$('uploadDrop').classList.add('drag-over');});for(const eventName of ['dragleave','drop'])$('uploadDrop').addEventListener(eventName,event=>{event.preventDefault();$('uploadDrop').classList.remove('drag-over');if(eventName==='drop')queueFiles(Array.from(event.dataTransfer.files));});
    document.addEventListener('click',run(async event=>{
      const button=event.target.closest('[data-use-asset],[data-tag-asset],[data-pick-asset],[data-preview-asset],[data-probe-asset],[data-rename-asset],[data-delete-asset],[data-preview-task],[data-download-task],[data-resume-task],[data-upload-material-task],[data-refresh-material-task],[data-replicate-task],[data-replication-history],[data-cancel-upload]');if(!button)return;
      const action=Object.keys(button.dataset).find(key=>['useAsset','tagAsset','pickAsset','previewAsset','probeAsset','renameAsset','deleteAsset','previewTask','downloadTask','resumeTask','uploadMaterialTask','refreshMaterialTask','replicateTask','replicationHistory','cancelUpload'].includes(key));
      const label={previewAsset:'正在加载…',previewTask:'正在加载…',downloadTask:'获取链接…',probeAsset:'正在核实…',resumeTask:'正在核实恢复…',uploadMaterialTask:'正在提交…',refreshMaterialTask:'正在刷新…',renameAsset:'正在保存…',deleteAsset:'正在移除…',cancelUpload:'正在取消…'}[action];
      return withBusy(`${action}:${button.dataset[action]}`,button,label,async()=>{
      if(button.dataset.replicateTask)return openReplication(button.dataset.replicateTask);if(button.dataset.replicationHistory)return openReplicationHistory(button.dataset.replicationHistory);if(button.dataset.useAsset)return useAsset(button.dataset.useAsset);if(button.dataset.tagAsset)return openResources({kind:'tag',asset_id:button.dataset.tagAsset});if(button.dataset.pickAsset)return pickAsset(button.dataset.pickAsset);if(button.dataset.previewAsset)return previewAsset(button.dataset.previewAsset);if(button.dataset.probeAsset)return probeAsset(button.dataset.probeAsset);if(button.dataset.previewTask)return taskOutput(button.dataset.previewTask,false);if(button.dataset.downloadTask)return taskOutput(button.dataset.downloadTask,true);if(button.dataset.uploadMaterialTask)return uploadMaterialTask(button.dataset.uploadMaterialTask);if(button.dataset.refreshMaterialTask)return loadTasks();if(button.dataset.cancelUpload)return cancelUpload(button.dataset.cancelUpload);
      if(button.dataset.resumeTask){if(demo)return toast('模拟模式不会提交重试');await resumeTaskRequest(button.dataset.resumeTask);await loadTasks();return;}
      const id=button.dataset.renameAsset||button.dataset.deleteAsset;const asset=state.assets.find(entry=>entry.id===id);if(!asset)return;
      if(button.dataset.renameAsset){const name=prompt('素材名称',asset.name);if(!name||!name.trim())return;if(demo)asset.name=name.trim();else await api(`${ROOT}/assets/${encodeURIComponent(id)}`,{method:'PATCH',body:{name:name.trim()}});}
      else{if(!confirm(`从共享素材库移除“${asset.name}”？已有制作任务引用的文件仍会保留。`))return;if(demo)state.assets=state.assets.filter(entry=>entry.id!==id);else await api(`${ROOT}/assets/${encodeURIComponent(id)}`,{method:'DELETE'});}await loadAssets();
      });
    }));
    bindResources(run);bindMirror(run);bindReplication(run);
    global.addEventListener('beforeunload',event=>{if(!demo&&(state.dirty||Array.from(state.uploads.values()).some(upload=>!upload.done))){event.preventDefault();event.returnValue='';}});
  }
  function openResources(purpose){
    state.resourcePurpose={...purpose,project:state.project};state.resourceDialogSeq++;state.resourceSearchSeq++;state.resourceResults=[];$('resourceSearch').value='';$('resourceResults').innerHTML='<div class="empty">输入完整资源 ID，或英文剧名开头后搜索。</div>';$('resourceStatus').textContent='资源名称使用英语剧名称；名称待补充时仍可按 ID 选择。';$('resourceDialogTitle').textContent=({tag:'修改资源标签',filter:'按资源筛选',upload:'选择上传资源',unit:'更换目标资源','new-unit':'添加目标资源'})[purpose.kind]||'选择资源';$('resourceDialog').showModal();$('resourceSearch').focus();
  }
  async function searchResources(){
    const query=$('resourceSearch').value.trim(),session=state.resourceDialogSeq,sequence=++state.resourceSearchSeq;
    if(!query){$('resourceResults').innerHTML='<div class="empty">请输入资源 ID 或英文剧名。</div>';return;}
    $('resourceResults').innerHTML='<div class="empty" role="status">正在搜索资源…</div>';$('resourceStatus').textContent='正在搜索…';
    try{const response=demo?{items:[{series_code:'21481',english_title:'Love & Revenge of Malos',english_title_status:'matched'},{series_code:'21482',english_title:'Her Second Life',english_title_status:'matched'}]}:await api(`${ROOT}/catalog/resources?q=${encodeURIComponent(query)}`);if(session!==state.resourceDialogSeq||sequence!==state.resourceSearchSeq||!$('resourceDialog').open)return;state.resourceResults=response.items||[];$('resourceResults').innerHTML=state.resourceResults.map((resource,index)=>`<button data-select-resource="${index}"><strong>${esc(resource.english_title||'英文名待补充 / 核实')}</strong><small>资源 ID：${esc(resource.series_code)}</small></button>`).join('')||'<div class="empty">未找到资源，请使用准确 ID 或英文剧名开头。</div>';$('resourceStatus').textContent=`找到 ${state.resourceResults.length} 个资源，点击即可选择。`;}
    catch(error){if(session===state.resourceDialogSeq&&sequence===state.resourceSearchSeq){$('resourceResults').innerHTML='<div class="empty">资源查询暂不可用，可调整条件后重试。</div>';$('resourceStatus').textContent=error.message;}}
  }
  async function selectResource(index){
    const resource=state.resourceResults[index],purpose=state.resourcePurpose;if(!resource||!purpose)return;
    if(['new-unit','unit'].includes(purpose.kind)&&purpose.project!==state.project)return;
    if(purpose.kind==='new-unit'){
      if(state.project.dramas.length>=state.config.max_dramas)return toast(`每项目最多 ${state.config.max_dramas} 个单元`,true);
      changed(()=>{const unit={id:uid(),series_code:resource.series_code,title:resource.english_title||resource.series_code,clips:[]};state.project.dramas.push(unit);state.selectedUnits.add(unit.id);state.activeDrama=state.project.dramas.length-1;});
    }else if(purpose.kind==='unit'){
      const unit=state.project.dramas.find(entry=>entry.id===purpose.unit_id);if(!unit)return;cancelPendingAdd();changed(()=>{unit.series_code=resource.series_code;unit.title=resource.english_title||resource.series_code;});
    }else if(purpose.kind==='filter'){state.libraryResource=resource;await loadAssets();}
    else if(purpose.kind==='upload'){state.uploadResource=resource;renderAssets();}
    else if(purpose.kind==='tag'){
      if(demo){const source=state.sources[purpose.asset_id];Object.assign(source,{series_code:resource.series_code,english_title:resource.english_title});const row=state.assets.find(asset=>asset.id===purpose.asset_id);if(row)Object.assign(row,source);}
      else{const source=item(await api(`${ROOT}/assets/${encodeURIComponent(purpose.asset_id)}`,{method:'PATCH',body:{series_code:resource.series_code}}));rememberSources([source]);}
      await loadAssets();renderEditor();toast('资源标签已更新；已提交任务保持原样');
    }
    $('resourceDialog').close();if(purpose.afterSelect)purpose.afterSelect(resource);
  }
  async function openSources(){
    const unit=currentDrama();if(!unit?.series_code){toast('先选择当前制作单元的目标资源');if(unit)openResources({kind:'unit',unit_id:unit.id});else openResources({kind:'new-unit'});return;}
    if(!$('assetDialog').open)$('assetDialog').showModal();$('sourcePickerTitle').textContent=unitTitle(unit);$('sourcePickerResource').textContent=`资源 ID：${unit.series_code} · 可以多选并重复插入`;
    document.querySelectorAll('[data-source-kind]').forEach(button=>button.classList.toggle('active',button.dataset.sourceKind===state.pickerKind));
    const sequence=++state.pickerSeq,kind=state.pickerKind,unitId=unit.id,project=state.project;$('assetPicker').innerHTML='<div class="empty" role="status">正在加载该资源的视频…</div>';state.pickerItems=[];
    try{const response=demo?{items:Object.values(state.sources).filter(source=>source.kind==='asset'&&source.series_code===unit.series_code&&(source.library_kind||'material')===kind)}:await api(`${ROOT}/assets?library_kind=${kind}&series_code=${encodeURIComponent(unit.series_code)}&status=all&limit=100&offset=0`);if(sequence!==state.pickerSeq||project!==state.project||unitId!==currentDrama()?.id||!$('assetDialog').open)return;state.pickerItems=response.items||[];rememberSources(state.pickerItems);renderSourcePicker(response.total);}
    catch(error){if(sequence===state.pickerSeq)$('assetPicker').innerHTML=`<div class="empty danger">${esc(error.message)}，可以切换分组或重新打开重试。</div>`;}
  }
  function renderSourcePicker(total){
    $('assetPicker').innerHTML=state.pickerItems.map(source=>`<label class="source-option"><input type="checkbox" class="source-pick" value="${esc(source.id||source.source_id)}" ${source.status!=='ready'?'disabled':''}><span><strong>${esc(sourceTitle(source))}</strong><small>${esc(statusLabel(source.status))}${durationOf(source)?` · ${seconds(durationOf(source))} 秒`:''}</small></span><button class="button small" type="button" data-preview-asset="${esc(source.id)}" ${source.status!=='ready'?'disabled':''}>预览</button><button class="button small" type="button" data-pick-asset="${esc(source.id)}" ${source.status!=='ready'?'disabled':''}>截取插入</button></label>`).join('')||'<div class="empty">该资源暂无此类视频，可切换来源或前往上传。</div>';
    if(total>state.pickerItems.length)$('assetPicker').innerHTML+='<p class="micro">已展示前 100 条，可在对应栏目按名称查找并点击“用于剪辑”。</p>';
  }
  function queueFiles(files){
    if(!files.length)return;const context={library_kind:state.libraryKind,resource:state.uploadResource};const existing=uploadStore();
    const start=resource=>files.reduce((promise,file)=>promise.then(()=>uploadFile(file,{...context,resource})),Promise.resolve());
    if(!context.resource&&files.some(file=>!existing[fileFingerprint(file)]?.id&&!existing[fileFingerprint(file)]?.resource)){openResources({kind:'upload',afterSelect:start});return;}start(context.resource);
  }
  async function openMaterialVariants(task){
    const sequence=++state.variantSeq;state.variantTask=task;state.materialVariants=[];$('variantTitle').textContent=`${task.drama_title||task.series_code} · 选择入库归属`;$('variantSelect').innerHTML='<option value="">正在加载…</option>';$('variantStatus').textContent='选择此成片对应的来源产品和语言剧版本。';$('confirmVariant').disabled=true;$('variantDialog').showModal();
    try{const response=await api(`${ROOT}/catalog/resource-variants?series_code=${encodeURIComponent(task.series_code)}`);if(sequence!==state.variantSeq||!$('variantDialog').open)return;state.materialVariants=response.items||[];$('variantSelect').innerHTML='<option value="">选择来源产品 / 语言版本</option>'+state.materialVariants.map((variant,index)=>`<option value="${index}">${esc(variant.app||variant.app_id)} · ${esc(variant.language)} · ${esc(variant.country)} · ${esc(variant.content_id)}</option>`).join('');$('variantStatus').textContent=state.materialVariants.length?'英文资源名不代表视频语言，请选择成片实际对应版本。':'暂未找到语言版本，仍可预览和下载成片。';}
    catch(error){if(sequence===state.variantSeq)$('variantStatus').textContent=`目录暂不可用：${error.message}。仍可预览和下载成片。`;}
  }
  function bindResources(run){
    $('searchResources').addEventListener('click',run(searchResources));$('resourceSearch').addEventListener('keydown',run(event=>event.key==='Enter'?searchResources():null));
    $('resourceResults').addEventListener('click',run(event=>{const button=event.target.closest('[data-select-resource]');if(button)return withBusy('selectResource',button,'正在选择…',()=>selectResource(Number(button.dataset.selectResource)));}));
    $('resourceDialog').addEventListener('close',()=>{state.resourceDialogSeq++;state.resourceSearchSeq++;state.resourcePurpose=null;});
    $('libraryResourceFilter').addEventListener('click',()=>openResources({kind:'filter'}));$('clearLibraryResource').addEventListener('click',run(()=>{state.libraryResource=null;return loadAssets();}));$('chooseUploadResource').addEventListener('click',()=>openResources({kind:'upload'}));
    $('assetDialog').addEventListener('close',()=>{state.pickerSeq++;});
    $('variantDialog').addEventListener('close',()=>{state.variantSeq++;state.variantTask=null;});$('variantSelect').addEventListener('change',()=>{$('confirmVariant').disabled=$('variantSelect').value==='';});
    $('confirmVariant').addEventListener('click',run(()=>{const task=state.variantTask,variant=state.materialVariants[Number($('variantSelect').value)];if(!task||!variant||$('variantSelect').value==='')return;$('variantDialog').close();return uploadMaterialTask(task.id,variant);}));
  }

  const previewLayers=[];
  function layerAt(index){
    if(previewLayers[index])return previewLayers[index];const video=$(index?'previewB':'previewA'),canvas=$(index?'canvasB':'canvasA'),scratch=document.createElement('canvas');scratch.width=720;scratch.height=1280;canvas.width=720;canvas.height=1280;
    const layer={video,canvas,scratch,clip:null,source:null,active:false,frameRequest:null,raf:null,token:0};previewLayers[index]=layer;
    for(const event of ['loadeddata','seeked'])video.addEventListener(event,()=>{if(layer.active&&!video.seeking&&drawPreviewLayer(layer))$('previewPlaceholder').classList.add('hidden');});return layer;
  }
  function cancelLayer(layer){layer.token++;if(layer.frameRequest!==null&&layer.video.cancelVideoFrameCallback)layer.video.cancelVideoFrameCallback(layer.frameRequest);if(layer.raf!==null)cancelAnimationFrame(layer.raf);layer.frameRequest=null;layer.raf=null;}
  function drawPreviewLayer(layer){
    const {video,canvas,scratch,clip,source}=layer;if(!layer.active||!clip||layer.project!==state.project||layer.unitId!==currentDrama()?.id||state.activeView!=='editor'||video.seeking||!video.videoWidth||!video.videoHeight||video.readyState<2||(video.currentSrc&&video.currentSrc!==video.src))return false;
    const original=scratch.getContext?.('2d'),display=canvas.getContext?.('2d');if(!original||!display)return false;
    const geometry=source.preview_geometry||{},rect=geometry.content_rect||{x:0,y:0,width:geometry.width||video.videoWidth,height:geometry.height||video.videoHeight};
    const ratioX=video.videoWidth/(geometry.width||video.videoWidth),ratioY=video.videoHeight/(geometry.height||video.videoHeight);
    const width=geometry.display_width||source.metadata?.display_width||source.metadata?.width||rect.width,height=geometry.display_height||source.metadata?.display_height||source.metadata?.height||rect.height;
    const dest=fitRectangle(width,height,clip.fit);
    original.setTransform(1,0,0,1,0,0);original.fillStyle='#000';original.fillRect(0,0,720,1280);
    original.drawImage(video,rect.x*ratioX,rect.y*ratioY,rect.width*ratioX,rect.height*ratioY,dest.x,dest.y,dest.width,dest.height);
    display.setTransform(1,0,0,1,0,0);display.clearRect(0,0,720,1280);display.save();if(clip.mirror){display.translate(720,0);display.scale(-1,1);}display.drawImage(scratch,0,0);display.restore();
    const band=effectiveBand(clip);if(band&&band.bottom>band.top)display.drawImage(scratch,0,band.top,720,band.bottom-band.top,0,band.top,720,band.bottom-band.top);
    return true;
  }
  function startLayerFrames(layer){
    if(layer.frameRequest!==null||layer.raf!==null)return;const token=layer.token;
    const frame=()=>{layer.frameRequest=null;layer.raf=null;if(!layer.active||token!==layer.token)return;drawPreviewLayer(layer);if(!state.playing)return;if(layer.video.requestVideoFrameCallback)layer.frameRequest=layer.video.requestVideoFrameCallback(frame);else layer.raf=requestAnimationFrame(frame);};
    if(layer.video.requestVideoFrameCallback)layer.frameRequest=layer.video.requestVideoFrameCallback(frame);else layer.raf=requestAnimationFrame(frame);
  }
  function renderBandOverlay(){
    const clip=selectedClip(),band=clip&&effectiveBand(clip),overlay=$('subtitleOverlay');const layoutClip=currentDrama()&&layout(currentDrama().clips).find(segment=>segment.clip.id===clip?.id);const visible=!!band&&!!layoutClip&&state.playMs>=layoutClip.start&&state.playMs<layoutClip.end;
    overlay.classList.toggle('hidden',!visible);if(visible){overlay.style.top=`${band.top/1280*100}%`;overlay.style.height=`${(band.bottom-band.top)/1280*100}%`;}
  }
  function bindMirror(run){
    $('clipMirror').addEventListener('change',()=>{const clip=selectedClip();if(!clip)return;updateClip({mirror:$('clipMirror').checked,...(clip.subtitle_band===undefined?{subtitle_band:{top:960,bottom:1280}}:{})});});
    $('protectSubtitles').addEventListener('change',()=>updateClip({subtitle_band:$('protectSubtitles').checked?{top:960,bottom:1280}:null}));
    const setBand=()=>{const top=Math.round(Number($('subtitleTop').value)/2)*2,bottom=Math.round(Number($('subtitleBottom').value)/2)*2;if(!Number.isFinite(top)||!Number.isFinite(bottom)||top<0||bottom>1280||bottom<=top)return toast('字幕区域需在 0–1280 内，底边须大于顶边',true);updateClip({subtitle_band:{top,bottom}});};
    $('subtitleTop').addEventListener('change',setBand);$('subtitleBottom').addEventListener('change',setBand);
    let dragging=null;
    const overlay=$('subtitleOverlay');overlay.addEventListener('pointerdown',event=>{const clip=selectedClip(),band=clip&&effectiveBand(clip);if(!band||editingLocked())return;event.preventDefault();stopPreview(true);snapshot();dragging={clip,band:{...band},clientY:event.clientY,edge:event.target.dataset.bandEdge||'move',height:$('videoFrame').getBoundingClientRect().height,pointerId:event.pointerId};overlay.setPointerCapture?.(event.pointerId);});
    overlay.addEventListener('pointermove',event=>{if(!dragging||event.pointerId!==dragging.pointerId)return;const delta=Math.round((event.clientY-dragging.clientY)/dragging.height*1280/2)*2;let {top,bottom}=dragging.band;if(dragging.edge==='top')top=Math.max(0,Math.min(bottom-2,top+delta));else if(dragging.edge==='bottom')bottom=Math.min(1280,Math.max(top+2,bottom+delta));else{const height=bottom-top;top=Math.max(0,Math.min(1280-height,top+delta));bottom=top+height;}dragging.clip.subtitle_band={top,bottom};$('subtitleTop').value=top;$('subtitleBottom').value=bottom;renderBandOverlay();previewLayers.forEach(drawPreviewLayer);});
    const finish=event=>{if(!dragging||event.pointerId!==dragging.pointerId)return;overlay.releasePointerCapture?.(event.pointerId);dragging=null;state.project.schema_version=2;state.revision++;state.dirty=true;state.renderOperation=null;renderEditor();scheduleSave();};overlay.addEventListener('pointerup',finish);overlay.addEventListener('pointercancel',finish);
  }

  async function init(){
    bind();
    if(demo){state.auth={authenticated:true,user:{name:'创作体验',role:'admin',tenant_key:'demo'}};$('demoBanner').classList.remove('hidden');}
    else{try{state.auth=await api('/api/ui/topbar');}catch(error){state.auth={authenticated:false};toast(`登录信息获取失败：${error.message}`,true);}}
    await global.QuickNav.render({container:'#quickNav',auth:state.auth,activeKey:'videoEditing'});global.UiTopbar.render({auth:state.auth,userCard:'#userCard',authButton:'#authButton',refreshButton:'#refreshPage'});
    if(!state.auth.authenticated){$('loginGate').classList.remove('hidden');return;}
    if(demo){$('pageRoot').classList.remove('hidden');seedDemo();state.ready=true;return;}
    try{state.config={...state.config,...item(await api(`${ROOT}/config`))};const notices=[];if(state.config.intake_enabled===false||state.config.enabled===false)notices.push('新上传、媒体校验和制作提交已暂停；已有上传可续传，历史项目和成片仍可查看。');if(state.config.worker_configured===false)notices.push('制作服务尚未配置，暂不能提交 GPU 制作。');if(state.config.cos_configured===false)notices.push('COS 尚未配置，暂不能上传新素材。');$('configNotice').textContent=notices.join(' ');$('configNotice').classList.toggle('hidden',!notices.length);$('uploadLimits').textContent=`单个文件最大 ${bytes(state.config.max_file_bytes)} · 支持分片续传 · 视频直接进入 COS`;$('catalogStatus').textContent=`最多 ${state.config.max_dramas} 部短剧`;$('pageRoot').classList.remove('hidden');const initial=await Promise.allSettled([loadAssets(),loadProjects(),loadTasks()]);renderEditor();state.ready=true;const failed=initial.find(result=>result.status==='rejected');if(failed)toast(`部分列表暂未加载：${failed.reason.message}，仍可编辑和保存草稿`,true);}
    catch(error){$('loginGate').innerHTML=`<strong>暂时无法打开视频剪辑</strong><p>${esc(error.message)}</p>`;$('loginGate').classList.remove('hidden');$('pageRoot').classList.add('hidden');}
    setInterval(pollUpdates,3000);
  }
  init().catch(error=>toast(error.message,true));
})(typeof window!=='undefined'?window:globalThis);
