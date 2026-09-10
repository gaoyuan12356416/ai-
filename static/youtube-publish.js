/* YouTube automatic publishing. Task outcomes and permissions are server-owned. */
(() => {
  'use strict';
  const BASE = '/api/youtube-auto-publish';
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const chars = value => Array.from(String(value ?? '')).length;
  const bytes = value => new TextEncoder().encode(String(value ?? '')).length;
  const uid = () => crypto.randomUUID();
  function safeUrl(value, local = false) {
    const raw = String(value || '');
    if (local && raw.startsWith('blob:')) return raw;
    try { const u = new URL(raw, location.origin); return raw && ['http:','https:'].includes(u.protocol) ? u.href : ''; } catch (_) { return ''; }
  }
  const paths = {plus:'M12 5v14M5 12h14',search:'m21 21-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0',x:'m6 6 12 12M6 18 18 6',check:'m5 12 4 4L19 6',upload:'M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4',image:'M4 3h16v18H4zM4 16l5-5 4 4 3-3 4 4M8 7h.01',arrow:'M5 12h14m-6-6 6 6-6 6',play:'m9 5 10 7-10 7z',spark:'m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7z',retry:'M3 11a9 9 0 1 1 2.5 7M3 4v7h7',warning:'m12 3 10 18H2zM12 9v5m0 3v.1',video:'M3 5h13v14H3zM16 10l5-3v10l-5-3',folder:'M3 6h7l2 3h9v11H3z',chevron:'m9 5 7 7-7 7',info:'M12 11v6m0-10v.1M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0'};
  const icon = name => `<svg class="ui-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
  const statuses = {queued:['等待执行','blue'],queued_generation:['等待生成封面','blue'],generating:['封面生成中','blue'],review:['待审核封面','amber'],enqueue_pending:['等待上传发布','blue'],enqueue_failed:['提交发布失败','red'],uploading:['上传发布中','blue'],processing:['视频处理中','blue'],published:['已发布','green'],generation_failed:['封面生成失败','red'],thumbnail_failed:['封面设置失败','red'],upload_failed:['视频上传失败','red'],processing_failed:['视频处理失败','red'],publish_failed:['视频发布失败','red'],comment_failed:['首评失败','amber'],failed:['执行失败','red']};
  const phaseLabels = {cover:'准备封面',upload:'私享上传视频',thumbnail:'设置最终封面',processing:'等待视频处理',public:'公开视频',comment:'发送首条评论',complete:'发布完成',pending:'等待发布'};
  const activeStatuses = ['queued','queued_generation','generating','enqueue_pending','uploading','processing'];
  const badge = status => `<span class="badge badge-${statuses[status]?.[1] || 'gray'}"><span class="status-dot"></span>${esc(statuses[status]?.[0] || status)}</span>`;
  const image = (url, alt, cls = '', local = false) => safeUrl(url, local) ? `<img class="${cls}" src="${esc(safeUrl(url, local))}" alt="${esc(alt)}" loading="lazy" decoding="async"/>` : `<span class="${cls} task-placeholder" aria-label="暂无封面">${icon('image')}</span>`;
  const time = value => { if (!value) return '—'; if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value).toLocaleString('zh-CN',{hour12:false}); const d = new Date(value); return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('zh-CN',{hour12:false}); };
  const state = {auth:null,bootstrap:null,tasks:[],counts:{},total:0,bounded:false,limit:200,materials:[],source:null,search:'',status:'all',tab:'all',listError:'',listRefreshError:'',listLoading:false,listLoaded:false,ready:false,channels:{items:[],loaded:false,loading:false,error:'',loadedAt:0}};
  let draft = null, stack = [], modalSerial = 0, lastFocus = null, busy = false, openingTask = false, listSerial = 0, listAbort = null, materialSerial = 0, pollTimer = null, searchTimer = null, materialTimer = null, channelPromise = null, channelSerial = 0, initSerial = 0, initBusy = false, pollInFlight = false;
  const carets = new Map();
  const taskCache = new Map(), taskRevisions = new Map(), taskReads = new Map();
  const task = id => taskCache.get(String(id)) || state.tasks.find(t => String(t.id) === String(id));
  const top = () => stack.at(-1);
  const versions = t => Array.isArray(t.versions) ? t.versions : [];
  const currentCover = t => t.cover_url || versions(t).find(v => Number(v.number) === Number(t.current_version))?.url || t.material?.thumbnail_url;
  async function api(path, options = {}) {
    const isPost = String(options.method || 'GET').toUpperCase() === 'POST';
    const controller = new AbortController(), timeoutMs = isPost ? 45000 : 12000;
    const externalSignal = options.signal;
    let timedOut = false;
    const cancel = () => controller.abort();
    if (externalSignal?.aborted) cancel(); else externalSignal?.addEventListener('abort',cancel,{once:true});
    const timer = setTimeout(() => { timedOut = true; controller.abort(); },timeoutMs);
    try {
      const response = await fetch(path.startsWith('/api/') ? path : BASE + path, {credentials:'same-origin',cache:'no-store',...options,signal:controller.signal,headers:{'Accept':'application/json',...(options.body ? {'Content-Type':'application/json'} : {}),...options.headers}});
      let data;
      try { data = await response.json(); }
      catch (parseError) {
        if (controller.signal.aborted) throw parseError;
        if (!response.ok) data = {};
        else { const error = new Error(isPost ? '服务端响应不完整，提交结果待确认。请保留当前内容后重试核对。' : '服务端响应不完整，请重试。'); error.apiError = true; error.uncertain = isPost; throw error; }
      }
      if (!data || typeof data !== 'object') { const error = new Error('服务端响应格式无效，请重试。'); error.apiError = true; error.uncertain = isPost; throw error; }
      if (!response.ok || data.ok === false) {
        const raw = data.error;
        const error = new Error(data.message || (typeof raw === 'string' ? raw : raw?.message) || (response.status === 401 ? '登录已失效，请先重新登录。当前填写内容仍保留。' : `请求失败（${response.status}），请重试。`));
        error.apiError = true; error.status = response.status; error.code = typeof raw === 'object' ? raw?.code : data.code || raw; error.uncertain = response.status >= 500 && isPost; throw error;
      }
      return data;
    } catch (cause) {
      if (cause.apiError) throw cause;
      const error = new Error(timedOut ? (isPost ? '提交请求超时，结果待确认。请保留当前内容，重新提交核对时将使用同一操作标识。' : '请求超时（12 秒），请重试。') : controller.signal.aborted ? '请求已取消。' : '网络连接失败，请检查网络后重试。');
      error.code = timedOut ? 'request_timeout' : controller.signal.aborted ? 'request_cancelled' : 'network_error'; error.uncertain = isPost; throw error;
    } finally { clearTimeout(timer); externalSignal?.removeEventListener('abort',cancel); }
  }
  const post = (path, body) => api(path,{method:'POST',body:JSON.stringify(body)});
  function toast(message, kind = 'success') { const node = document.createElement('div'); node.className = `toast toast-${kind}`; node.setAttribute('role','status'); node.innerHTML = `${icon(kind === 'success' ? 'check' : 'warning')}<span>${esc(message)}</span>`; $('#toast-root').replaceChildren(node); setTimeout(() => node.remove(),4800); }
  function rememberCaret(el) { if (el?.matches?.('#draft-title,#draft-description,#draft-comment,#default-description')) carets.set(el.id,[el.selectionStart,el.selectionEnd]); }
  function resolve(template, source) {
    const missing = [], unknown = [], used = [];
    const value = String(template ?? '').replace(/\{+[^{}\r\n]*\}+/g, token => {
      const key = token.slice(1,-1);
      if (!['url','desc','name'].includes(key)) { unknown.push(token); return token; }
      used.push(key); const replacement = source?.[`macro_${key}`];
      if (replacement === undefined || replacement === null || replacement === '') { missing.push(key); return token; }
      return String(replacement).trim();
    });
    return {value:value.trim(),unknown:[...new Set(unknown)],missing:[...new Set(missing)],used:[...new Set(used)]};
  }
  const limit = field => field === 'title' ? 100 : field === 'description' ? 5000 : 1000;
  const count = (field,value) => field === 'description' ? bytes(value) : chars(value);
  function validateText(field, template, source, settings = false) {
    const r = resolve(template,source);
    if (r.unknown.length) return `不支持宏参数 ${r.unknown.join('、')}，仅支持 {url}、{desc}、{name}。`;
    if (field !== 'comment' && !r.value.trim()) return `请填写${field === 'title' ? '视频标题' : '视频描述'}。`;
    if (!settings && source) {
      const required = r.missing.filter(key => key !== 'url');
      if (required.length) return `所选素材缺少 ${required.map(k => `{${k}}`).join('、')}，请更换素材。`;
      if (r.missing.includes('url') && !source.long_url && !(source.source_job_id && source.content_id)) return '素材缺少生成跳转链接所需的源剧集关联，请更换素材。';
    }
    if (!r.missing.length && count(field,r.value) > limit(field)) return `替换后的${field === 'title' ? '标题' : field === 'description' ? '描述' : '首评'}不能超过 ${limit(field)} ${field === 'description' ? 'UTF-8 字节' : '字符'}。`;
    return '';
  }
  function macroPreview(field, template, source, settings = false) {
    const r = resolve(template,source), warning = String(template || '').trim() ? validateText(field,template,source,settings) : '', pending = r.missing.length > 0;
    let visible = r.value;
    if (r.missing.includes('url') && source) visible = visible.replace(/\{url\}/g,'[提交时生成跳转链接]');
    const hint = warning || (pending ? (settings ? '宏参数将在新建发布时，按所选素材替换并校验。' : source ? '跳转链接将在提交时生成，最终长度由服务端校验。' : '选择素材后预览宏参数替换结果。') : '');
    return `<div class="macro-preview${warning ? ' has-warning' : ''}" data-macro-preview="${settings ? 'settings' : field}"><div class="macro-preview-header"><span>${settings ? '默认描述模板预览' : '发布预览' + (source ? ' · ' + esc(source.name) : '')}</span><span>${pending ? '待替换' : count(field,r.value) + '/' + limit(field) + (field === 'description' ? ' 字节' : ' 字符')}</span></div><div class="macro-preview-body">${esc(visible || (field === 'comment' ? '未填写，发布时将跳过首评。' : '尚未填写'))}</div>${hint ? `<div class="macro-warning" role="status">${esc(hint)}</div>` : ''}</div>`;
  }
  function macroControls(field, template, settings = false) {
    const id = settings ? 'default-description' : `draft-${field}`;
    return `<div class="macro-toolbar"><span>插入宏参数</span>${['url','desc','name'].map(key => `<button type="button" class="macro-chip" data-action="insert-macro" data-target="${id}" data-macro="${key}">{${key}}</button>`).join('')}</div><span class="macro-source">{url} 剧集跳转链接 · {desc} 剧情简介 · {name} 剧名</span>${macroPreview(field,template,settings ? null : draft?.material,settings)}`;
  }
  function linkHelp(m) { return `<details class="macro-link-help"><summary>查看 {url} 链接规则与长链</summary><div class="macro-link-details"><p>{url} 沿用剧集合成中 YouTube 发布的长链及跳转链接生成规则。</p>${m ? `<span class="field-hint">发布文案中的链接</span><code class="macro-link-value">${esc(m.macro_url || '提交时生成跳转链接')}</code><span class="field-hint">目标长链</span><code class="macro-link-value">${esc(m.long_url || '提交时由服务端根据源剧集关联生成')}</code><div class="macro-link-meta"><span>源剧集 ID</span><code>${esc(m.content_id || '未关联')}</code><span>源合成任务 ID</span><code>${esc(m.source_job_id || '未关联')}</code></div>` : '<p class="field-hint">选择素材后查看对应链接与来源关联。</p>'}</div></details>`; }
  function refreshPreview(field, settings = false) { const input = $('#' + (settings ? 'default-description' : 'draft-' + field)); const el = $(`[data-macro-preview="${settings ? 'settings' : field}"]`); if (input && el) el.outerHTML = macroPreview(field,input.value,settings ? null : draft?.material,settings); }
  function upsert(t, mutation = false) { if (!t?.id) throw new Error('服务端未返回有效任务，请刷新任务列表确认。'); const key = String(t.id); taskCache.set(key,t); if (mutation) taskRevisions.set(key,(taskRevisions.get(key) || 0) + 1); const i = state.tasks.findIndex(x => String(x.id) === key); if (i >= 0) state.tasks[i] = t; return t; }
  function showSource() { const source = state.source || state.bootstrap?.source; const el = $('#source-message'); el.classList.toggle('hidden',source?.configured !== false); el.innerHTML = source?.configured === false ? '<strong>素材筛选规则待配置</strong><span>已预留素材查询配置，配置完成后即可选择视频素材。</span>' : ''; }
  function renderList() {
    const c = state.counts || {}, all = Number(c.all ?? state.total ?? 0), reviewed = Number(c.review ?? 0), published = Number(c.published ?? 0), failed = Number(c.failed ?? 0);
    $('#stats').innerHTML = [['全部发布任务',all,'video','blue','当前权限范围内的全部任务'],['待审核封面',reviewed,'image','amber','确认封面后自动进入发布'],['视频已公开',published,'check','green','包含视频已公开、首评待处理'],['需要处理',failed,'warning','red','按失败阶段单独处理']].map(([label,n,glyph,color,caption]) => `<div class="stat-card"><div class="stat-top"><span class="stat-label">${label}</span><span class="stat-icon stat-icon-${color}">${icon(glyph)}</span></div><div class="stat-number">${n}</div><div class="stat-caption">${caption}</div></div>`).join('');
    $('#task-tabs').innerHTML = [['all','全部任务',all],['review','待审核',reviewed],['running','进行中',Number(c.running || 0)],['published','已发布',published],['failed','异常任务',failed]].map(([id,label,n]) => `<button class="tab ${state.tab === id ? 'active' : ''}" type="button" data-action="tab" data-id="${id}" role="tab" aria-selected="${state.tab === id}">${label}<span>${n}</span></button>`).join('');
    $('#task-table').setAttribute('aria-busy',String(state.listLoading));
    if (!state.listLoaded) {
      $('#stats').querySelectorAll('.stat-number').forEach(el => { el.textContent = '—'; });
      $('#task-tabs').querySelectorAll('.tab span').forEach(el => { el.textContent = '—'; });
      $('#task-count').textContent = state.listError ? '任务加载失败' : '正在加载任务…'; $('#footer-total').textContent = '等待任务数据';
      if (!state.listError) { $('#task-table').innerHTML = '<tr><td colspan="6"><div class="empty-state task-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><h3>正在加载发布任务</h3><p>可以先新建发布，任务数据将在加载完成后展示。</p></div></td></tr>'; return; }
    }
    if (state.listLoaded) { $('#task-count').textContent = state.listRefreshError ? '自动刷新失败，稍后重试（保留上次数据）' : `共 ${state.total} 条任务${state.bounded ? ' · 最近 ' + state.limit + ' 条内查询' : ''}`; $('#footer-total').textContent = `已显示 ${state.tasks.length} 条 / 共 ${state.total} 条${state.bounded ? '（最近 ' + state.limit + ' 条）' : ''}`; }
    if (state.listError) { $('#task-table').innerHTML = `<tr><td colspan="6"><div class="empty-state table-error">${icon('warning')}<h3>任务加载失败</h3><p>${esc(state.listError)}</p><button class="btn btn-secondary" data-action="reload-tasks">重新加载</button></div></td></tr>`; return; }
    $('#task-table').innerHTML = state.tasks.length ? state.tasks.map(t => `<tr><td><div class="task-cell"><button type="button" class="task-thumb" data-action="details" data-id="${esc(t.id)}" aria-label="查看任务详情">${image(currentCover(t),'任务封面')}<span>${icon('play')}</span></button><div><button type="button" class="task-title" data-action="details" data-id="${esc(t.id)}">${esc(t.title || t.title_template || '待处理发布任务')}</button><div class="task-subtitle">${esc(t.id)} · ${esc(t.material?.name || '—')}</div></div></div></td><td><div class="channel-cell"><span class="channel-avatar">YT</span><div><strong>${esc(t.channel?.name || '—')}</strong><span class="task-subtitle">${esc(t.channel?.language || '')}</span></div></div></td><td><span class="source-label">${icon(t.cover_source === 'ai' ? 'spark' : 'upload')}${t.cover_source === 'ai' ? 'AI 生成' : '本地上传'}</span></td><td>${badge(t.status)}${t.status === 'comment_failed' ? '<div class="task-subtitle status-subtitle">视频已公开</div>' : t.status === 'thumbnail_failed' ? '<div class="task-subtitle status-subtitle">视频保持私享</div>' : ''}</td><td><div class="date-cell">${esc(time(t.created_at))}</div></td><td><div class="task-actions">${t.can_review ? `<button class="btn btn-primary btn-sm" data-action="review" data-id="${esc(t.id)}">审核封面</button>` : `<button class="btn btn-${t.can_retry ? 'secondary' : 'ghost'} btn-sm" data-action="details" data-id="${esc(t.id)}">${t.can_retry ? '处理异常' : '查看详情'}</button>`}${t.can_review ? `<button class="icon-btn" data-action="details" data-id="${esc(t.id)}" aria-label="查看详情">${icon('chevron')}</button>` : ''}</div></td></tr>`).join('') : `<tr><td colspan="6"><div class="empty-state">${icon('folder')}<h3>${state.search || state.status !== 'all' || state.tab !== 'all' ? '没有找到匹配的任务' : '暂无发布任务'}</h3><p>${state.source?.configured === false ? '素材筛选规则待配置，配置完成后即可新建发布。' : '点击「新建发布」，选择素材与频道后开始。'}</p></div></td></tr>`;
  }
  async function loadTasks(quiet = false) {
    const serial = ++listSerial;
    listAbort?.abort(); listAbort = new AbortController();
    state.listLoading = true; if (!quiet || !state.listLoaded) { state.listError = ''; renderList(); }
    try { const data = await api('/tasks?' + new URLSearchParams({search:state.search,status:state.status !== 'all' ? state.status : state.tab}),{signal:listAbort.signal}); if (serial !== listSerial) return; if (!Array.isArray(data.items)) throw new Error('任务数据格式无效，请重试。'); state.tasks = data.items; state.total = Number(data.total ?? state.tasks.length); state.counts = data.counts || {}; state.bounded = data.bounded === true; state.limit = Number(data.limit || 200); state.listError = ''; state.listRefreshError = ''; state.listLoaded = true; }
    catch (error) { if (serial !== listSerial) return; if (!quiet || !state.listLoaded) state.listError = error.message; else state.listRefreshError = error.message; }
    finally { if (serial === listSerial) { state.listLoading = false; if (!quiet || !state.listLoaded || !state.listError) renderList(); } }
  }
  function channelsValid() { return state.channels.loaded && !state.channels.loading && !state.channels.error; }
  async function loadChannels(force = false) {
    if (channelPromise) return channelPromise;
    if (!force && state.channels.loaded && Date.now() - state.channels.loadedAt < 60000) return state.channels.items;
    const serial = ++channelSerial;
    state.channels.loading = true; state.channels.error = ''; state.channels.loaded = false; renderModals();
    channelPromise = (async () => {
      try { const data = await api('/channels'); if (serial !== channelSerial) return; if (!Array.isArray(data.channels)) throw new Error('频道数据格式无效，请重试。'); state.channels.items = data.channels; state.channels.loaded = true; state.channels.loadedAt = Date.now(); state.bootstrap.channels = data.channels; return data.channels; }
      catch (error) { if (serial === channelSerial) { state.channels.error = error.message; state.channels.loaded = false; } }
      finally { if (serial === channelSerial) { state.channels.loading = false; channelPromise = null; renderModals(); } }
    })();
    return channelPromise;
  }
  async function loadTask(id) { const key = String(id), revision = taskRevisions.get(key) || 0, serial = (taskReads.get(key) || 0) + 1; taskReads.set(key,serial); const data = await api('/tasks/' + encodeURIComponent(id)); if ((taskRevisions.get(key) || 0) !== revision || taskReads.get(key) !== serial) { if (taskCache.has(key)) return taskCache.get(key); } return upsert(data.task); }
  async function loadMaterials(view) {
    const serial = ++materialSerial; view.loading = true; view.error = ''; renderModals();
    try { const data = await api('/materials?' + new URLSearchParams({search:view.search || ''})); if (serial !== materialSerial || !stack.includes(view)) return; state.materials = Array.isArray(data.items) ? data.items : []; state.source = {configured:data.configured,message:data.message}; view.loading = false; showSource(); renderModals(); }
    catch (error) { if (serial !== materialSerial || !stack.includes(view)) return; view.loading = false; view.error = error.message; renderModals(); }
  }
  function newDraft() { return {operationId:uid(),material:null,channelId:'',title:'',description:state.bootstrap.settings?.default_description || '',comment:'',coverSource:'ai',requirements:'',cover:null,errors:{},pendingPayload:null}; }
  function openModal(type, data = {}) { if (!stack.length) lastFocus = document.activeElement; stack.push({type,key:++modalSerial,...data}); renderModals(true); }
  function releaseCover(cover) { if (cover?.preview) URL.revokeObjectURL(cover.preview); }
  function closeModal() { if (busy) return; const v = stack.pop(); if (v?.type === 'publish') { releaseCover(draft?.cover); draft = null; } if (v?.manualCover) releaseCover(v.manualCover); renderModals(true); if (!stack.length) lastFocus?.focus(); }
  function closeAll() { stack.forEach(v => releaseCover(v.manualCover)); stack = []; releaseCover(draft?.cover); draft = null; renderModals(); }
  function shell(v,title,subtitle,body,footer = '',large = false) { return `<div class="modal-overlay${top() !== v ? ' modal-behind' : ''}" data-modal-key="${v.key}" ${top() !== v ? 'inert aria-hidden="true"' : ''}><section class="modal${large ? ' modal-lg' : ''}" role="dialog" aria-modal="${top() === v}" aria-labelledby="modal-title-${v.key}" tabindex="-1"><header class="modal-header"><div><h2 class="modal-title" id="modal-title-${v.key}">${title}</h2><p class="modal-subtitle">${subtitle}</p></div><button class="icon-btn close-modal" data-action="close-modal" aria-label="关闭弹窗">${icon('x')}</button></header><div class="modal-body">${v.error ? `<div class="note-box note-error inline-error" role="alert">${esc(v.error)}</div>` : ''}${body}</div>${footer ? `<footer class="modal-footer">${footer}</footer>` : ''}</section></div>`; }
  const label = (text,required = false,id = '') => `<label class="field-label"${id ? ` for="${id}"` : ''}>${text}${required ? '<span class="required">*</span>' : ''}</label>`;
  const fieldError = field => draft?.errors[field] ? `<span class="error-message" role="alert">${esc(draft.errors[field])}</span>` : '';
  function coverInput(cover, kind) { return `${cover ? `<div class="upload-preview-row">${image(cover.preview,'本地封面预览','cover-preview',true)}<div><strong>${esc(cover.file.name)}</strong><p class="field-hint">确认提交后上传封面图片</p><button class="btn btn-secondary btn-sm" data-action="upload-${kind}">更换图片</button></div></div>` : `<button class="upload-zone" data-action="upload-${kind}">${icon('upload')}<strong>点击选择封面图片</strong><span>支持 JPG、PNG，最大 2 MB · 推荐 16:9</span></button>`}<input id="${kind}-cover-file" type="file" accept="image/jpeg,image/png" data-file="${kind}" hidden/>`; }
  function publishView(v) {
    const m = draft.material, channels = channelsValid() ? state.channels.items : [];
    const channelPlaceholder = state.channels.loading ? '正在加载频道…' : state.channels.error ? '频道加载失败，请重试' : '请选择 YouTube 频道';
    const channelHint = state.channels.loading ? '<span class="field-hint channel-loading" role="status"><span class="loading-spinner small" aria-hidden="true"></span>正在读取已授权频道，其他内容可继续填写。</span>' : state.channels.error ? `<div id="channel-state" class="channel-error" role="alert"><span>${esc(state.channels.error)}</span><button class="btn btn-secondary btn-sm" type="button" data-action="retry-channels">重试加载频道</button></div>` : `<span class="field-hint">${channels.length ? '频道授权与可发布状态由服务端校验' : '暂无已授权频道，请联系管理员完成频道授权'}</span>`;
    const fields = ['title','description','comment'].map(field => {
      const title = {title:'视频标题',description:'视频描述',comment:'首条评论 <span class="optional-label">选填</span>'}[field];
      return `<div class="field span-2">${label(title,field !== 'comment','draft-' + field)}${field === 'title' ? `<input id="draft-title" class="input ${draft.errors.title ? 'invalid' : ''}" data-field="title" value="${esc(draft.title)}" placeholder="例如：{name} | Watch the full story"/>` : `<textarea id="draft-${field}" class="textarea ${draft.errors[field] ? 'invalid' : ''}" data-field="${field}" rows="${field === 'description' ? 3 : 2}" placeholder="${field === 'comment' ? '可填写互动引导文案，留空则不发送首评' : '请输入视频描述'}">${esc(draft[field])}</textarea>`}${fieldError(field)}${field === 'description' ? '<span class="field-hint">已带入默认描述，可为本次发布单独修改。</span>' : field === 'comment' ? '<span class="field-hint">视频公开后自动发送；留空会跳过此步骤。</span>' : ''}${macroControls(field,draft[field])}</div>`;
    });
    const body = `${draft.pendingPayload ? '<div class="note-box note-amber inline-error">上次提交结果待确认。请重新点击提交，系统会使用同一操作标识核对，不会重复创建任务。</div>' : ''}<div class="form-grid"><div class="field">${label('发布素材',true)}<button class="selection-summary ${draft.errors.material ? 'invalid' : ''}" data-action="choose-material">${m ? `${image(m.thumbnail_url,'素材缩略图','mini-cover')}<span><strong>${esc(m.name)}</strong><small>${esc(m.language || '—')} · ${esc(m.duration || '—')}</small></span><span class="selection-link">更换</span>` : `<span class="selection-icon">${icon('folder')}</span><span>选择一条视频素材</span>${icon('chevron')}`}</button>${fieldError('material')}<span class="field-hint">${state.source?.configured === false ? '素材筛选规则待配置' : '仅展示符合筛选范围的素材'}</span></div><div class="field">${label('发布频道',true,'draft-channel')}<select id="draft-channel" class="select ${draft.errors.channelId ? 'invalid' : ''}" data-field="channelId" ${!channelsValid() ? 'disabled' : ''} aria-busy="${state.channels.loading}"><option value="">${channelPlaceholder}</option>${channels.map(c => `<option value="${esc(c.id)}" ${String(draft.channelId) === String(c.id) ? 'selected' : ''} ${c.eligible === false ? 'disabled' : ''}>${esc(c.name)}${c.language ? ' · ' + esc(c.language) : ''}${c.eligible === false ? '（' + esc(c.reason || '当前不可发布') + '）' : ''}</option>`).join('')}</select>${fieldError('channelId')}${channelHint}</div><div class="field span-2">${linkHelp(m)}</div>${fields[0]}<div class="field span-2">${label('封面来源',true)}<div class="source-options">${[['ai','AI 生成','按要求生成，审核后再发布','spark'],['local','本地上传','使用已准备好的封面图片','upload']].map(([value,title,desc,glyph]) => `<button class="source-option ${draft.coverSource === value ? 'selected' : ''}" data-action="cover-source" data-id="${value}" aria-pressed="${draft.coverSource === value}"><span class="source-option-icon">${icon(glyph)}</span><span><strong>${title}</strong><small>${desc}</small></span><span class="source-radio"></span></button>`).join('')}</div></div>${draft.coverSource === 'ai' ? `<div class="field span-2 ai-prompt-field">${label('封面要求',true,'draft-requirements')}<textarea id="draft-requirements" class="textarea ${draft.errors.requirements ? 'invalid' : ''}" data-field="requirements" rows="3" maxlength="2000" placeholder="请描述人物、场景、风格、文字及构图要求，系统将生成 16:9 横版封面。">${esc(draft.requirements)}</textarea>${fieldError('requirements')}<span class="field-hint">生成封面后发送飞书审核提醒，最终确认后才会上传视频。</span></div>` : `<div class="field span-2">${label('封面图片',true)}${coverInput(draft.cover,'draft')}${fieldError('cover')}</div>`}${fields[1]}${fields[2]}</div>`;
    return shell(v,`${icon('video')}新建发布`,'选择素材与频道，准备好这条视频的发布信息。',body,`<span class="footer-hint">${icon('info')}${draft.coverSource === 'ai' ? 'AI 封面需审核后发布' : '确认后进入上传发布流程'}</span><div class="inline-actions"><button class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" data-action="submit-publish">${icon(draft.coverSource === 'ai' ? 'spark' : 'upload')}${draft.coverSource === 'ai' ? '提交并生成封面' : '确认并上传'}</button></div>`,true);
  }
  function pickerView(v) {
    const configured = state.source?.configured !== false;
    const body = `<div class="picker-toolbar"><div class="search-input-wrap">${icon('search')}<input class="input" id="material-search" value="${esc(v.search || '')}" placeholder="搜索素材名称或 ID" aria-label="搜索素材"/></div></div>${!configured ? '<div class="note-box note-amber"><strong>素材筛选规则待配置</strong><span>已预留素材查询配置，配置完成后展示符合条件的视频素材。</span></div>' : ''}<div class="material-grid">${v.loading ? '<div class="empty-state material-empty">正在加载素材…</div>' : state.materials.length ? state.materials.map(m => `<article class="material-card ${String(v.selected?.id) === String(m.id) ? 'selected' : ''}"><button class="material-select" data-action="select-material" data-id="${esc(m.id)}" aria-pressed="${String(v.selected?.id) === String(m.id)}"><div class="material-cover">${image(m.thumbnail_url,m.name)}<span class="material-duration">${esc(m.duration || '—')}</span><span class="material-check">${String(v.selected?.id) === String(m.id) ? icon('check') : ''}</span></div><div class="material-info"><strong>${esc(m.name)}</strong><span class="material-meta">${esc(m.id)} · ${esc(m.language || '—')}</span><span class="material-meta">${esc(m.size || '')}</span></div></button><button class="btn btn-ghost btn-sm material-preview" data-action="preview-material" data-id="${esc(m.id)}">${icon('play')}预览素材</button></article>`).join('') : `<div class="empty-state material-empty">${icon('folder')}<h3>${configured ? '暂无符合条件的素材' : '素材筛选规则待配置'}</h3><p>${configured ? '请修改关键词或稍后重试。' : '当前无法选择素材，配置完成后自动使用新的素材筛选范围。'}</p>${v.error ? '<button class="btn btn-secondary" data-action="reload-materials">重新加载</button>' : ''}</div>`}</div>`;
    return shell(v,'选择发布素材','每个任务选择一条视频素材。',body,`<span class="footer-hint">${v.selected ? '已选择：' + esc(v.selected.name) : '尚未选择素材'}</span><div class="inline-actions"><button class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" data-action="confirm-material" ${!v.selected || !configured || v.loading ? 'disabled' : ''}>确认选择</button></div>`,true);
  }
  function previewView(v) { const m = v.material; return shell(v,'素材预览',esc(m.name),`${safeUrl(m.url) ? `<video class="material-video" controls preload="metadata" ${safeUrl(m.thumbnail_url) ? `poster="${esc(safeUrl(m.thumbnail_url))}"` : ''} src="${esc(safeUrl(m.url))}"></video>` : '<div class="note-box note-amber">该素材暂无可用的视频预览链接。</div>'}<div class="summary-grid"><div class="summary-item"><span>素材名称</span><strong>${esc(m.name)}</strong></div><div class="summary-item"><span>素材 ID</span><strong>${esc(m.id)}</strong></div><div class="summary-item"><span>时长 / 大小</span><strong>${esc(m.duration || '—')} / ${esc(m.size || '—')}</strong></div><div class="summary-item"><span>语言</span><strong>${esc(m.language || '—')}</strong></div></div>`,'<button class="btn btn-secondary" data-action="close-modal">返回素材列表</button>',true); }
  function reviewView(v) {
    const t = task(v.id); if (!t) return shell(v,'审核封面','正在加载任务…','');
    if (!t.can_review || t.status !== 'review') return detailsView(v);
    const version = versions(t).find(x => Number(x.number) === Number(v.viewVersion || t.current_version)) || versions(t).at(-1), isCurrent = Number(version?.number) === Number(t.current_version);
    let inline = '';
    if (v.mode === 'reject') inline = `<div class="review-inline-panel"><div class="split-row"><strong>修改意见</strong><span class="badge badge-amber">必填</span></div><textarea id="reject-reason" class="textarea" maxlength="2000" rows="3" placeholder="请说明需要调整的人物、背景、文字或构图。">${esc(v.feedback || '')}</textarea><div class="inline-actions"><button class="btn btn-secondary btn-sm" data-action="cancel-review-mode">取消</button><button class="btn btn-primary btn-sm" data-action="confirm-reject">确认打回并重做</button></div></div>`;
    if (v.mode === 'manual') inline = `<div class="review-inline-panel"><strong>手动替换封面</strong>${coverInput(v.manualCover,'manual')}<div class="inline-actions"><button class="btn btn-secondary btn-sm" data-action="cancel-review-mode">取消</button><button class="btn btn-primary btn-sm" data-action="confirm-manual" ${!v.manualCover ? 'disabled' : ''}>确认替换并上传</button></div></div>`;
    return shell(v,'审核封面','确认视觉效果，或提出修改意见后重新生成。',`<div class="review-task-heading"><div><strong>${esc(t.title)}</strong><span class="task-subtitle">${esc(t.channel?.name)} · ${esc(t.id)}</span></div>${badge(t.status)}</div><div class="review-layout"><div><div class="review-image">${image(version?.url || currentCover(t),'封面预览')}<span class="image-corner-badge">${isCurrent ? '当前封面' : '历史版本'} V${esc(version?.number || t.current_version)}</span></div><div class="review-image-meta"><span>16:9 横版封面</span><span>V${esc(version?.number || t.current_version)} · ${esc(time(version?.created_at))}</span></div><div class="note-box note-blue">${icon('info')}<span>通过即为最终发布确认。系统将先私享上传视频，设置封面并完成处理后再公开。</span></div>${inline}</div><aside class="review-side"><h3>封面要求</h3><p class="review-requirements">${esc(t.requirements || '本地上传封面')}</p><h3>版本记录 <span class="task-subtitle">${versions(t).length} 个版本</span></h3><div class="version-list">${[...versions(t)].reverse().map(x => `<button class="version-item ${Number(x.number) === Number(version?.number) ? 'selected' : ''}" data-action="view-version" data-id="${esc(x.number)}">${image(x.url,'封面版本 ' + x.number,'version-thumb')}<span><strong>V${esc(x.number)}${Number(x.number) === Number(t.current_version) ? ' · 当前版本' : ''}</strong><small>${esc(x.feedback || '首次生成')}</small><small>${esc(time(x.created_at))}</small></span></button>`).join('')}</div><div class="note-box"><strong>当前素材</strong><span>${esc(t.material?.name)}</span><span>${esc(t.material?.duration || '—')} · ${esc(t.channel?.language || '—')}</span></div></aside></div>`,`<span class="footer-hint">${isCurrent ? '通过后将自动进入上传发布流程' : '正在查看历史版本，切回当前版本后处理'}</span><div class="review-actions"><button class="btn btn-secondary" data-action="reject" ${!isCurrent ? 'disabled' : ''}>${icon('retry')}打回重做</button><button class="btn btn-secondary" data-action="manual" ${!isCurrent ? 'disabled' : ''}>${icon('upload')}手动上传</button><button class="btn btn-primary" data-action="approve" ${!isCurrent || v.mode ? 'disabled' : ''}>${icon('check')}通过</button></div>`,true);
  }
  function copyField(t, field, title) { const template = t[field + '_template']; return `<h3>${title}</h3>${template && /\{(?:url|desc|name)\}/.test(template) ? `<div class="detail-template"><span class="field-hint">提交时的文案模板</span><p>${esc(template)}</p><span class="field-hint">服务端替换后固定的发布内容</span></div>` : ''}<p>${esc(t[field] || (field === 'comment' ? '未填写，已跳过' : '—'))}</p>`; }
  function timeline(t) { return `<div class="timeline">${(Array.isArray(t.steps) ? t.steps : []).map((s,i) => { const cls = {success:'complete',completed:'complete',complete:'complete',done:'complete',running:'active',active:'active',failed:'error',error:'error',skipped:'skipped'}[s.status] || ''; return `<div class="timeline-step ${cls}"><span class="timeline-dot">${cls === 'complete' ? icon('check') : cls === 'error' ? icon('x') : i + 1}</span><div><strong>${esc(s.label)}</strong><p>${esc(s.message || (s.status === 'pending' ? '等待执行' : ''))}</p></div><span class="step-result">${esc({success:'完成',completed:'完成',complete:'完成',done:'完成',running:'执行中',active:'执行中',failed:'失败',error:'失败',skipped:'已跳过',pending:'待执行'}[s.status] || s.status)}</span></div>`; }).join('') || '<p class="field-hint">暂无执行阶段记录</p>'}</div>`; }
  function detailsView(v) {
    const t = task(v.id); if (!t) return shell(v,'发布任务详情','正在加载任务…','');
    const notification = t.notification;
    return shell(v,'发布任务详情','查看封面、发布文案与每个处理阶段。',`<div class="review-task-heading"><div><strong>${esc(t.title || t.title_template)}</strong><span class="task-subtitle">${esc(t.id)} · 创建于 ${esc(time(t.created_at))}</span></div>${badge(t.status)}</div><div class="detail-layout"><div>${image(currentCover(t),'任务封面预览','detail-cover')}<div class="summary-grid"><div class="summary-item"><span>发布频道</span><strong>${esc(t.channel?.name)}</strong></div><div class="summary-item"><span>封面来源</span><strong>${t.cover_source === 'ai' ? 'AI 生成' : '本地上传'}</strong></div><div class="summary-item"><span>素材</span><strong>${esc(t.material?.name)}</strong></div><div class="summary-item"><span>当前阶段</span><strong>${esc(phaseLabels[t.phase] || statuses[t.status]?.[0] || t.status)}</strong></div></div><div class="detail-copy">${copyField(t,'title','视频标题')}${copyField(t,'description','视频描述')}${copyField(t,'comment','首条评论 <span class="optional-label">选填</span>')}</div>${linkHelp(t.material)}</div><div>${t.error?.message ? `<div class="note-box note-error"><strong>${esc(statuses[t.status]?.[0] || '任务异常')}</strong><span>${esc(t.error.message)}</span></div>` : ''}${['queued','queued_generation','generating','generation_failed','review'].includes(t.status) ? `<div class="generation-summary"><span class="generation-icon">${icon(t.status === 'generation_failed' ? 'warning' : 'spark')}</span><div><strong>${t.status === 'review' ? '当前封面 V' + esc(t.current_version) + ' 待审核' : esc(statuses[t.status]?.[0])}</strong><p>${esc(t.requirements || '')}</p>${t.status === 'generating' ? '<div class="progress-track"><span class="progress-fill indeterminate"></span></div>' : ''}</div></div>` : ''}${notification ? `<div class="note-box ${notification.status === 'failed' ? 'note-amber' : 'note-blue'}"><strong>飞书审核提醒</strong><span>${esc(notification.message || {sent:'已发送审核提醒',pending:'等待发送提醒',failed:'提醒发送失败，请在当前页面完成审核',none:'尚未发送审核提醒'}[notification.status] || notification.status)}</span></div>` : ''}<h3 class="section-heading">发布进度</h3>${timeline(t)}${safeUrl(t.video_url) ? `<a class="btn btn-secondary" href="${esc(safeUrl(t.video_url))}" target="_blank" rel="noopener noreferrer">打开 YouTube 视频${icon('arrow')}</a>` : ''}</div></div>`,`<span class="footer-hint">${activeStatuses.includes(t.status) ? '执行中，任务状态会自动更新' : '发布结果以各阶段记录为准'}</span><div class="inline-actions"><button class="btn btn-secondary" data-action="close-modal">关闭</button>${t.can_review ? '<button class="btn btn-primary" data-action="details-review">审核封面</button>' : t.can_retry ? `<button class="btn btn-primary" data-action="retry">${icon('retry')}${t.status === 'comment_failed' ? '仅重试首评' : t.status === 'generation_failed' ? '重新生成封面' : '重试失败阶段'}</button>` : ''}</div>`,true);
  }
  function settingsView(v) { return shell(v,'默认描述设置','新建发布时自动带入，已提交任务不会受影响。',`<div class="field">${label('默认视频描述',true,'default-description')}<textarea id="default-description" class="textarea" rows="6">${esc(v.value)}</textarea><span class="field-hint">当前租户共用的默认文案。支持宏参数，发布时按所选素材替换并校验最终长度。</span>${macroControls('description',v.value,true)}</div>`,'<button class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" data-action="save-settings">保存默认描述</button>'); }
  function renderModals(focus = false) {
    const root = $('#modal-root'); if (!stack.length) { root.innerHTML = ''; document.body.style.overflow = ''; return; }
    const offsets = [...root.querySelectorAll('.modal-body')].map(el => el.scrollTop), activeId = document.activeElement?.id;
    let selection; try { selection = [document.activeElement.selectionStart,document.activeElement.selectionEnd]; } catch (_) {}
    const views = {publish:publishView,picker:pickerView,preview:previewView,review:reviewView,details:detailsView,settings:settingsView};
    root.innerHTML = stack.map(v => views[v.type](v)).join(''); document.body.style.overflow = 'hidden';
    root.querySelectorAll('.modal-body').forEach((el,i) => { el.scrollTop = offsets[i] || 0; });
    if (busy) root.querySelectorAll('button,input,textarea,select').forEach(el => { el.disabled = true; });
    if (draft?.pendingPayload && top()?.type === 'publish') root.lastElementChild.querySelectorAll('input,textarea,select,button:not([data-action="submit-publish"]):not([data-action="close-modal"])').forEach(el => { el.disabled = true; });
    if (activeId && $('#' + activeId)) { const el = $('#' + activeId); el.focus({preventScroll:true}); if (selection && typeof selection[0] === 'number') { try { el.setSelectionRange(...selection); } catch (_) {} } }
    else if (focus) (root.lastElementChild.querySelector('input:not([type=file]),textarea,select,button:not(.close-modal):not([disabled])') || root.lastElementChild.querySelector('.modal'))?.focus({preventScroll:true});
  }
  async function readFile(file,kind) {
    if (!file || busy) return; const view = top();
    try {
      if (!['image/jpeg','image/png'].includes(file.type)) throw new Error('请选择 JPG 或 PNG 格式的图片。');
      if (file.size > 2 * 1024 * 1024) throw new Error('图片不能超过 2 MB，请压缩后重新选择。');
      if (!file.size) throw new Error('图片文件为空，请重新选择。');
      const preview = URL.createObjectURL(file), img = new Image();
      try { await new Promise((ok,no) => { img.onload = ok; img.onerror = () => no(new Error('无法解析图片，请选择有效的 JPG 或 PNG 文件。')); img.src = preview; }); } catch (error) { URL.revokeObjectURL(preview); throw error; }
      if (!stack.includes(view)) { URL.revokeObjectURL(preview); return; }
      const cover = {file,preview,asset:null};
      if (kind === 'draft') { releaseCover(draft.cover); draft.cover = cover; delete draft.errors.cover; } else { releaseCover(view.manualCover); view.manualCover = cover; }
      view.error = ''; renderModals(); if (Math.abs(img.width / img.height - 16 / 9) > .08) toast('图片已加载并保留原比例，建议使用 16:9 横版封面。','info');
    } catch (error) { if (stack.includes(view)) { view.error = error.message; renderModals(); } }
  }
  async function uploadCover(cover) {
    if (cover.asset) return cover.asset;
    const data = await new Promise((ok,no) => { const reader = new FileReader(); reader.onload = () => ok(String(reader.result).split(',')[1]); reader.onerror = () => no(new Error('图片读取失败，请重新选择。')); reader.readAsDataURL(cover.file); });
    const result = await post('/covers',{file_name:cover.file.name,data});
    if (!result.asset?.id) throw new Error('封面上传未返回有效图片，请重试。'); cover.asset = result.asset; return cover.asset;
  }
  function validateDraft() {
    const e = {};
    if (!draft.material || state.source?.configured === false) e.material = state.source?.configured === false ? '素材筛选规则待配置，暂无法发布。' : '请选择一条视频素材。';
    if (!channelsValid()) e.channelId = state.channels.loading ? '频道正在加载，请加载完成后选择频道。' : '请先成功加载并选择发布频道。';
    else if (!draft.channelId || !state.channels.items.some(c => String(c.id) === String(draft.channelId) && c.eligible !== false)) e.channelId = '请选择可发布的 YouTube 频道。';
    ['title','description','comment'].forEach(f => { const error = validateText(f,draft[f],draft.material); if (error) e[f] = error; });
    const selectedChannel = state.channels.items.find(c => String(c.id) === String(draft.channelId));
    if (draft.comment.trim() && selectedChannel?.comment_eligible === false) e.comment = '该频道尚未获得首评权限，请留空首评或完成频道授权。';
    if (draft.coverSource === 'ai' && !draft.requirements.trim()) e.requirements = '请填写封面要求。';
    if (draft.coverSource === 'local' && !draft.cover) e.cover = '请选择封面图片。';
    draft.errors = e; return Object.keys(e).length === 0;
  }
  async function submitPublish() {
    if (busy || (!draft.pendingPayload && !validateDraft())) { renderModals(); $('.error-message')?.scrollIntoView({block:'center'}); return; }
    const v = top(); busy = true; v.error = ''; renderModals();
    try {
      let payload = draft.pendingPayload;
      if (!payload) {
        const asset = draft.coverSource === 'local' ? await uploadCover(draft.cover) : null;
        payload = {operation_id:draft.operationId,material_id:draft.material.id,channel_id:draft.channelId,title_template:draft.title,description_template:draft.description,comment_template:draft.comment,cover_source:draft.coverSource,requirements:draft.requirements.trim(),cover_asset_id:asset?.id || ''};
      }
      let data;
      try { data = await post('/tasks',payload); if (!data.task?.id) { const error = new Error('服务端未返回有效任务，提交结果待确认。请保持原内容重试核对。'); error.uncertain = true; throw error; } } catch (error) { if (error.uncertain) draft.pendingPayload = payload; else draft.pendingPayload = null; throw error; }
      const t = upsert(data.task,true); busy = false; closeAll(); openModal('details',{id:t.id}); toast('发布任务已创建。'); void loadTasks(true);
    } catch (error) { v.error = error.message; }
    finally { busy = false; renderModals(); }
  }
  async function openTask(id,type = 'details',version = null) {
    if (openingTask) return; openingTask = true;
    try { const t = await loadTask(id); openModal(type === 'review' && t.can_review ? 'review' : 'details',{id:t.id,viewVersion:version || t.current_version,version:t.current_version}); if (version && Number(version) !== Number(t.current_version)) { top().error = '此审核链接对应历史版本。请查看当前版本后再处理。'; renderModals(); } }
    catch (error) { toast(error.message,'warning'); }
    finally { openingTask = false; }
  }
  async function review(action) {
    const v = top(), t = task(v.id); if (busy || !t?.can_review) return;
    if (Number(v.viewVersion || t.current_version) !== Number(t.current_version)) { v.error = '请切换到当前封面版本后再处理。'; renderModals(); return; }
    if (action === 'reject' && !v.feedback?.trim()) { v.error = '请填写修改意见后再打回重做。'; renderModals(); $('#reject-reason')?.focus(); return; }
    if (action === 'manual' && !v.manualCover) { v.error = '请先选择替代封面并确认预览。'; renderModals(); return; }
    busy = true; v.error = ''; renderModals();
    try {
      const asset = action === 'manual' ? await uploadCover(v.manualCover) : null;
      const data = await post('/tasks/' + encodeURIComponent(t.id) + '/review',{action,version:Number(v.version || t.current_version),feedback:v.feedback?.trim() || '',cover_asset_id:asset?.id || ''});
      upsert(data.task,true); releaseCover(v.manualCover); v.manualCover = null; v.mode = ''; v.type = 'details'; busy = false; renderModals(); toast(action === 'reject' ? '已打回重做，生成新封面后将再次提醒审核。' : '封面已确认，任务进入上传发布流程。'); void loadTasks(true);
    } catch (error) {
      v.error = error.message;
      if (error.status === 409) { try { const latest = await loadTask(t.id); v.version = latest.current_version; v.viewVersion = latest.current_version; v.mode = ''; v.error = '当前封面已更新或已被处理，已重新加载最新状态。请重新查看后操作。'; } catch (refreshError) { v.error += ' 最新状态加载失败：' + refreshError.message; } }
    } finally { busy = false; renderModals(); }
  }
  async function retryTask() { const v = top(), t = task(v.id); if (busy || !t?.can_retry) return; busy = true; v.error = ''; renderModals(); try { const data = await post('/tasks/' + encodeURIComponent(t.id) + '/retry',{}); upsert(data.task,true); busy = false; renderModals(); toast('已提交失败阶段重试。'); void loadTasks(true); } catch (error) { v.error = error.message; } finally { busy = false; renderModals(); } }
  async function saveSettings() { const v = top(); if (busy || !state.bootstrap.can_manage_settings) return; const error = validateText('description',v.value,null,true); if (error) { v.error = error; renderModals(); return; } busy = true; v.error = ''; renderModals(); try { const data = await post('/settings',{default_description:v.value}); state.bootstrap.settings = data.settings || {default_description:v.value}; busy = false; closeModal(); toast('默认描述已保存。'); } catch (error) { v.error = error.message; } finally { busy = false; renderModals(); } }
  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (pollInFlight) return;
      pollInFlight = true;
      try {
        if (!state.ready || document.hidden) return;
        if (!state.listLoading) await loadTasks(true);
        const v = top();
        if (v && ['details','review'].includes(v.type) && !busy) {
          try { const old = task(v.id); const updated = await loadTask(v.id); if (!stack.includes(v)) return; if (v.type === 'details') renderModals(); else if (!v.mode && Number(updated.current_version) !== Number(v.version)) { v.error = '任务封面已更新，请查看最新版本后审核。'; v.version = updated.current_version; v.viewVersion = updated.current_version; renderModals(); } else if (old?.status !== updated.status || !updated.can_review) renderModals(); } catch (_) {}
        }
      } finally { pollInFlight = false; if (state.ready) schedulePoll(); }
    },6000);
  }
  document.addEventListener('click', event => {
    const b = event.target.closest('[data-action]'); if (!b || b.disabled || busy) return;
    const action = b.dataset.action, id = b.dataset.id, v = top();
    if (action === 'retry-init') init();
    else if (action === 'retry-channels') void loadChannels(true);
    else if (action === 'close-modal') closeModal();
    else if (action === 'tab') { state.tab = id; state.status = 'all'; $('#status-filter').value = 'all'; loadTasks(); }
    else if (action === 'reload-tasks') loadTasks();
    else if (action === 'reload-materials') loadMaterials(v);
    else if (action === 'insert-macro') { const input = $('#' + b.dataset.target); if (!input || input.disabled) return; const [start,end] = carets.get(input.id) || [input.value.length,input.value.length]; input.setRangeText(`{${b.dataset.macro}}`,Math.min(start,input.value.length),Math.min(end,input.value.length),'end'); input.focus({preventScroll:true}); rememberCaret(input); input.dispatchEvent(new Event('input',{bubbles:true})); }
    else if (action === 'choose-material') { openModal('picker',{selected:draft.material,search:'',loading:true}); loadMaterials(top()); }
    else if (action === 'select-material') { v.selected = state.materials.find(m => String(m.id) === String(id)); renderModals(); }
    else if (action === 'confirm-material') { draft.material = v.selected; if (!draft.title.trim()) draft.title = '{name}'; delete draft.errors.material; closeModal(); }
    else if (action === 'preview-material') { const material = state.materials.find(m => String(m.id) === String(id)); if (material) openModal('preview',{material}); }
    else if (action === 'cover-source') { draft.coverSource = id; renderModals(); }
    else if (action === 'upload-draft') $('#draft-cover-file')?.click();
    else if (action === 'upload-manual') $('#manual-cover-file')?.click();
    else if (action === 'submit-publish') submitPublish();
    else if (action === 'review' || action === 'details') openTask(id,action);
    else if (action === 'details-review') { const t = task(v.id); v.type = 'review'; v.version = t.current_version; v.viewVersion = t.current_version; renderModals(true); }
    else if (action === 'view-version') { v.viewVersion = Number(id); if (v.viewVersion === Number(task(v.id)?.current_version)) v.version = v.viewVersion; v.mode = ''; v.error = ''; renderModals(); }
    else if (action === 'reject' || action === 'manual') { v.mode = action; v.error = ''; renderModals(); if (action === 'reject') $('#reject-reason')?.focus(); }
    else if (action === 'cancel-review-mode') { v.mode = ''; v.error = ''; renderModals(); }
    else if (action === 'approve') review('approve');
    else if (action === 'confirm-reject') review('reject');
    else if (action === 'confirm-manual') review('manual');
    else if (action === 'retry') retryTask();
    else if (action === 'save-settings') saveSettings();
  });
  document.addEventListener('input', event => { const el = event.target; if (el.dataset.field && draft) { draft[el.dataset.field] = el.value; delete draft.errors[el.dataset.field]; if (['title','description','comment'].includes(el.dataset.field)) refreshPreview(el.dataset.field); } if (el.id === 'default-description') { top().value = el.value; refreshPreview('description',true); } if (el.id === 'reject-reason') top().feedback = el.value; if (el.id === 'material-search') { const v = top(); v.search = el.value; clearTimeout(materialTimer); materialTimer = setTimeout(() => { if (stack.includes(v)) loadMaterials(v); },280); } rememberCaret(el); });
  document.addEventListener('change', event => { const el = event.target; if (el.dataset.field && draft) draft[el.dataset.field] = el.value; if (el.dataset.file) readFile(el.files[0],el.dataset.file); });
  ['keyup','mouseup','select','focusout'].forEach(name => document.addEventListener(name,event => rememberCaret(event.target)));
  document.addEventListener('keydown', event => {
    if (!stack.length) return; if (event.key === 'Escape') { event.preventDefault(); closeModal(); return; }
    if (event.key !== 'Tab') return;
    const elements = [...$('#modal-root').lastElementChild.querySelectorAll('button:not([disabled]),input:not([disabled]):not([type=file]),textarea:not([disabled]),select:not([disabled]),a[href],summary')].filter(el => el.getClientRects().length);
    if (!elements.length) { event.preventDefault(); return; } const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && (document.activeElement === first || !elements.includes(document.activeElement))) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && (document.activeElement === last || !elements.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
  });
  $('#new-publish').addEventListener('click', () => { if (!state.ready || busy || state.bootstrap.enabled === false) return; draft = newDraft(); openModal('publish'); void loadChannels(); });
  $('#settings-button').addEventListener('click', () => { if (state.bootstrap?.can_manage_settings && !busy) openModal('settings',{value:state.bootstrap.settings?.default_description || ''}); });
  $('#task-search').addEventListener('input', event => { state.search = event.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(() => loadTasks(),300); });
  $('#status-filter').addEventListener('change', event => { state.status = event.target.value; state.tab = 'all'; loadTasks(); });
  // Refresh stays available even while the very first auth request is pending.
  if (!$('#refreshPage').dataset.topbarBound) {
    $('#refreshPage').dataset.topbarBound = '1';
    $('#refreshPage').addEventListener('click', () => location.reload());
  }
  async function init() {
    if (initBusy) return;
    const serial = ++initSerial; initBusy = true; state.ready = false;
    clearTimeout(pollTimer); listAbort?.abort();
    $('#page-message').className = 'note-box note-blue'; $('#page-message').textContent = '正在加载发布工作台…';
    try {
      state.auth = await api('/api/ui/topbar');
      if (serial !== initSerial) return;
      UiTopbar.render({auth:state.auth,userCard:'#userCard',authButton:'#authButton',refreshButton:'#refreshPage'});
      if (!$('#authButton').dataset.youtubeAuthBound) { $('#authButton').dataset.youtubeAuthBound = '1'; $('#authButton').addEventListener('click', () => UiTopbar.handleAuthAction({auth:state.auth,api})); }
      // QuickNav paints its cached/default menu synchronously. Its refresh is independent.
      try { void Promise.resolve(QuickNav.render({container:'#quickNav',auth:state.auth,activeKey:'youtubeAutoPublish'})).catch(() => {}); } catch (_) {}
      if (!state.auth.authenticated) { $('#page-message').innerHTML = '<strong>请先登录</strong><span>使用飞书登录后查看 YouTube 发布任务。</span>'; return; }
      const bootstrap = await api('/bootstrap?include_channels=0');
      if (serial !== initSerial) return;
      if (!bootstrap.settings || !bootstrap.source) throw new Error('工作台配置响应不完整，请重试。');
      state.bootstrap = bootstrap; state.source = bootstrap.source; state.ready = true;
      if (bootstrap.channels_loaded === true && Array.isArray(bootstrap.channels)) state.channels = {items:bootstrap.channels,loaded:true,loading:false,error:'',loadedAt:Date.now()};
      $('#new-publish').disabled = state.bootstrap.enabled === false;
      if (state.bootstrap.enabled === false) $('#new-publish').title = 'YouTube 自动发布暂未启用';
      $('#page-message').classList.add('hidden'); $('#page-content').classList.remove('hidden'); $('#settings-button').hidden = !state.bootstrap.can_manage_settings; showSource();
      void loadTasks();
      const params = new URLSearchParams(location.search); if (params.get('task_id')) void openTask(params.get('task_id'),'review',Number(params.get('version')) || null);
      schedulePoll();
    } catch (error) { if (serial !== initSerial) return; $('#page-message').className = 'note-box note-error'; $('#page-message').innerHTML = `<strong>${error.status === 403 ? '暂无 YouTube 自动发布权限' : '发布工作台加载失败'}</strong><span>${esc(error.message)}</span><button type="button" class="btn btn-secondary btn-sm boot-retry" data-action="retry-init">重新加载工作台</button>`; }
    finally { if (serial === initSerial) initBusy = false; }
  }
  window.addEventListener('pagehide', () => { clearTimeout(pollTimer); releaseCover(draft?.cover); stack.forEach(v => releaseCover(v.manualCover)); });
  init();
})();
