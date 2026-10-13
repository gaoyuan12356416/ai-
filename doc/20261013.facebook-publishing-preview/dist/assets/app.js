(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const STORAGE = 'facebook-publishing-preview-v1-20261013';
  const captionSample = '她曾以为，人生只能按别人写好的剧本继续。\n直到这一次，她决定为自己重新开始。✨\n\n你会如何选择？一起看她的第二人生。\n#DramaWave #短剧 #她的第二人生';
  const pages = [
    {id:'demo-page-01',name:'DramaWave 短剧',owner:'内容运营 A',language:'中文',mark:'D',color:'',available:true},
    {id:'demo-page-02',name:'DramaWave 故事剧场',owner:'内容运营 A',language:'中文',mark:'剧',color:'green',available:true},
    {id:'demo-page-03',name:'DramaWave Romance',owner:'内容运营 B',language:'英语',mark:'R',color:'rose',available:true},
    {id:'demo-page-04',name:'DramaWave Español',owner:'内容运营 C',language:'西班牙语',mark:'E',color:'amber',available:true},
    {id:'demo-page-05',name:'DramaWave 精选',owner:'内容运营 B',language:'中文',mark:'D',color:'gray',available:false}
  ];
  const materials = [
    {id:'DEMO-26001',name:'她的第二人生 · 高光片段',language:'中文',duration:'02:18',resolution:'1080 × 1920',size:'48.6 MB',poster:'assets/poster-city.svg',series:'她的第二人生',format:'9:16',kind:'素材库'},
    {id:'DEMO-26002',name:'雨夜之后 · 第一次相遇',language:'中文',duration:'01:46',resolution:'1080 × 1920',size:'35.2 MB',poster:'assets/poster-rain.svg',series:'雨夜之后',format:'9:16',kind:'素材库'},
    {id:'DEMO-26003',name:'The Last Promise · Episode 01',language:'英语',duration:'03:24',resolution:'1920 × 1080',size:'76.8 MB',poster:'assets/poster-gold.svg',series:'The Last Promise',format:'16:9',kind:'素材库'},
    {id:'DEMO-26004',name:'Volver a Empezar · Capítulo 01',language:'西班牙语',duration:'02:32',resolution:'1080 × 1920',size:'52.4 MB',poster:'assets/poster-city.svg',series:'Volver a Empezar',format:'9:16',kind:'素材库'},
    {id:'DEMO-26005',name:'她的第二人生 · 反转时刻',language:'中文',duration:'01:12',resolution:'1080 × 1920',size:'27.1 MB',poster:'assets/poster-gold.svg',series:'她的第二人生',format:'9:16',kind:'素材库'},
    {id:'DEMO-26006',name:'After the Rain · A New Beginning',language:'英语',duration:'02:05',resolution:'1920 × 1080',size:'41.3 MB',poster:'assets/poster-rain.svg',series:'After the Rain',format:'16:9',kind:'素材库'}
  ];
  const templates = [
    {id:'none',title:'不去重',subtitle:'直接使用原视频',icon:'film',note:'直接使用原视频，不增加去重处理步骤。'},
    {id:'template-01',title:'模板 01',subtitle:'构图变化 · 示例',icon:'grid',note:'构图变化模板占位示例。模板数量、名称与处理方式待确认；本页不处理视频。'},
    {id:'template-02',title:'模板 02',subtitle:'画面调整 · 示例',icon:'spark',note:'画面调整模板占位示例。模板数量、名称与处理方式待确认；本页不处理视频。'},
    {id:'template-03',title:'模板 03',subtitle:'组合处理 · 示例',icon:'copy',note:'组合处理模板占位示例。模板数量、名称与处理方式待确认；本页不处理视频。'}
  ];
  const statusLabels = {published:'模拟已发布',scheduled:'模拟已预约',processing:'模拟处理中',failed:'模拟失败',cancelled:'模拟已取消'};
  const state = {materialId:materials[0].id,pageIds:[pages[0].id,pages[1].id],template:'none',caption:captionSample,mode:'now',schedule:'',previewPage:pages[0].id};
  let currentView = 'compose', pendingMaterial = state.materialId, recordFilter = 'all', records = [], toastTimer, confirmSnapshot = null, taskSequence = 100;
  const timers = new Set();
  const localObjects = new Set();
  const bjLocal = (time = Date.now()) => new Date(time + 8 * 3600000).toISOString().slice(0,16);
  const bjText = time => bjLocal(time).replace('T',' ');
  const parseBeijing = str => Date.parse(str + ':00+08:00');
  const tomorrowTen = () => bjLocal(Date.now() + 24 * 3600000).slice(0,10) + 'T10:00';
  state.schedule = tomorrowTen();
  const getMaterial = (id = state.materialId) => materials.find(item => item.id === id) || materials[0];
  const getPage = id => pages.find(page => page.id === id);
  const getTemplate = (id = state.template) => templates.find(template => template.id === id) || templates[0];
  const selectedPages = () => pages.filter(page => state.pageIds.includes(page.id) && page.available);
  function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; },3600); }
  function readStorage(key) { try { return JSON.parse(localStorage.getItem(STORAGE + key) || 'null'); } catch { return null; } }
  function writeStorage(key,value) { try { localStorage.setItem(STORAGE + key,JSON.stringify(value)); return true; } catch { return false; } }
  function seedRecords() {
    const base = Date.now();
    return [
      {id:'DEMO-FB-00105',materialId:materials[0].id,pageId:pages[0].id,template:'template-01',status:'scheduled',mode:'scheduled',time:tomorrowTen().replace('T',' '),created:bjText(base-600000),caption:captionSample},
      {id:'DEMO-FB-00104',materialId:materials[1].id,pageId:pages[1].id,template:'template-02',status:'processing',mode:'now',time:'立即发布',created:bjText(base-1800000),caption:'雨夜之后，一切悄悄改变。#DramaWave',seed:true},
      {id:'DEMO-FB-00103',materialId:materials[2].id,pageId:pages[2].id,template:'none',status:'published',mode:'now',time:bjText(base-3600000),created:bjText(base-3660000),caption:'One promise. A whole new beginning. #DramaWave'},
      {id:'DEMO-FB-00102',materialId:materials[0].id,pageId:pages[1].id,template:'none',status:'published',mode:'now',time:bjText(base-7200000),created:bjText(base-7260000),caption:captionSample},
      {id:'DEMO-FB-00101',materialId:materials[3].id,pageId:pages[3].id,template:'template-03',status:'failed',mode:'now',time:bjText(base-10800000),created:bjText(base-10860000),caption:'Cada historia merece un nuevo comienzo. #DramaWave',error:'示例错误：主页授权已过期，需重新授权后再发布。'}
    ];
  }
  function restore() {
    const draft = readStorage(':draft');
    if (draft && typeof draft === 'object') {
      if (materials.some(item => item.id === draft.materialId)) state.materialId = draft.materialId;
      if (Array.isArray(draft.pageIds)) state.pageIds = pages.filter(page => draft.pageIds.includes(page.id) && page.available).map(page => page.id);
      if (templates.some(item => item.id === draft.template)) state.template = draft.template;
      if (typeof draft.caption === 'string') state.caption = draft.caption.slice(0,5000);
      if (['now','scheduled'].includes(draft.mode)) state.mode = draft.mode;
      if (typeof draft.schedule === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(draft.schedule)) state.schedule = draft.schedule;
      setTimeout(() => toast('已恢复保存在当前浏览器的草稿'),500);
    }
    const saved = readStorage(':records');
    records = Array.isArray(saved) ? saved.filter(item => item && typeof item.id === 'string' && item.id.startsWith('DEMO-FB-') && typeof item.caption === 'string' && getPage(item.pageId) && Object.hasOwn(statusLabels,item.status)).slice(0,200) : seedRecords();
    // Browser reload never implies a remote result; interrupted simulations remain explicitly marked.
    records = records.map(record => record.status === 'processing' && !record.seed ? {...record,status:'cancelled',error:'演示页面已刷新，本地处理模拟已结束。'} : record);
  }
  function persistRecords() { writeStorage(':records',records.slice(0,200).map(record => ({...record,localUrl:undefined}))); }
  function materialMeta(record) { return materials.find(item => item.id === record.materialId) || record.material || materials[0]; }
  function pageAvatar(page) { return `<span class="page-avatar ${esc(page.color)}">${esc(page.mark)}<b class="avatar-f">f</b></span>`; }
  function badge(status) { return `<span class="status-badge ${esc(status)}">${esc(statusLabels[status] || status)}</span>`; }
  function renderMaterial() {
    const item = getMaterial();
    $('selectedMaterial').innerHTML = `<div class="selected-material"><button class="material-thumb icon-button" id="selectedVideoPreview" aria-label="预览所选素材"><img src="${esc(item.poster)}" alt="${esc(item.series)}示例画面"><span class="thumb-time">${esc(item.duration)}</span></button><div class="material-meta"><h3>${esc(item.name)}</h3><p>${esc(item.id)} · ${esc(item.size)}</p><div class="material-tags"><span class="tag blue">${esc(item.language)}</span><span class="tag">${esc(item.resolution)}</span><span class="tag">${esc(item.local ? '本地预览' : '示例素材')}</span></div></div><span class="selected-check">${icon('check')}</span></div>`;
    $('selectedVideoPreview').addEventListener('click',openVideo);
  }
  function renderPages() {
    const search = $('pageSearch').value.trim().toLocaleLowerCase();
    const matches = pages.filter(page => [page.name,page.owner,page.id,page.language].join(' ').toLocaleLowerCase().includes(search));
    $('pageGrid').innerHTML = matches.length ? matches.map(page => `<label class="page-option" title="${page.available ? esc(page.name) : '演示：授权失效，当前不可选择'}">${pageAvatar(page)}<span class="page-info"><strong>${esc(page.name)}</strong><small>${page.available ? '<i class="status-dot"></i>' : ''}${esc(page.available ? page.owner+' · '+page.language : '授权失效 · 不可发布')}</small></span><input type="checkbox" value="${page.id}" aria-label="${esc(page.name)}" ${state.pageIds.includes(page.id)?'checked':''} ${page.available?'':'disabled'}></label>`).join('') : '<div class="empty-state">没有找到匹配的主页，试试其他关键词。</div>';
    $('pageGrid').querySelectorAll('input').forEach(input => input.addEventListener('change',() => {
      state.pageIds = input.checked ? [...new Set([...state.pageIds,input.value])] : state.pageIds.filter(id => id !== input.value);
      update();
    }));
  }
  function renderTemplates() {
    $('templateGrid').innerHTML = templates.map(template => `<label class="template-option"><input type="radio" name="template" value="${template.id}" aria-label="${esc(template.title)}" ${state.template===template.id?'checked':''}><span class="template-icon">${icon(template.icon)}</span><strong>${esc(template.title)}</strong><small>${esc(template.subtitle)}</small></label>`).join('');
    $('templateGrid').querySelectorAll('input').forEach(input => input.addEventListener('change',() => { state.template = input.value; update(); }));
  }
  function update() {
    const active = selectedPages(), material = getMaterial(), template = getTemplate();
    $('pageCount').textContent = `已选 ${active.length} 个主页`;
    $('selectedPagesSummary').textContent = active.length ? '已选：'+active.map(page=>page.name).join('、') : '请选择至少 1 个可发布的主页。';
    $('selectAllPages').textContent = active.length === pages.filter(page=>page.available).length ? '取消全选' : '全选可用';
    $('pageGrid').querySelectorAll('input').forEach(input=>{input.checked=state.pageIds.includes(input.value);});
    if (!active.some(page=>page.id===state.previewPage)) state.previewPage = active[0]?.id || '';
    $('previewPage').innerHTML = active.length ? active.map(page=>`<option value="${page.id}" ${state.previewPage===page.id?'selected':''}>${esc(page.name)} · 预览</option>`).join('') : '<option value="">请先选择发布主页</option>';
    const preview = getPage(state.previewPage);
    $('previewName').textContent = preview?.name || '主页名称';
    $('previewAvatar').textContent = preview?.mark || 'f';
    $('previewAvatar').className = `page-avatar ${preview?.color || ''}`;
    $('previewCaption').textContent = state.caption || '填写发布文案，在这里预览帖子效果。';
    $('previewPoster').src = material.poster;
    $('previewPoster').alt = material.series + ' · 示例画面';
    $('previewDuration').textContent = material.duration;
    $('templateNote').textContent = template.note;
    $('captionCount').textContent = `${state.caption.length.toLocaleString()} / 5,000`;
    $('scheduleFields').hidden = state.mode !== 'scheduled';
    $('scheduleAt').min = bjLocal(Date.now()+60000);
    $('summaryMaterial').textContent = material.series;
    $('summaryPages').textContent = `${active.length} 个 Facebook 主页`;
    $('summaryTemplate').textContent = template.title + (template.id!=='none'?' · 待确认':'');
    $('summaryTime').textContent = state.mode === 'now' ? '立即发布' : (state.schedule.replace('T',' ') || '待选择')+'（北京）';
    $('publishSummary').textContent = `1 条视频 → ${active.length} 个主页`;
    $('publishSummaryDetail').textContent = `${template.title} · ${state.mode==='now'?'立即发布':state.schedule.replace('T',' ')+'（北京）'}`;
    $('submitLabel').textContent = state.mode==='now' ? '模拟发布' : '模拟预约';
    $('formError').hidden = true;
  }
  function showView(view) {
    currentView = view;
    const composing = view === 'compose';
    $('composeView').hidden = !composing; $('recordsView').hidden = composing;
    $('composeTab').classList.toggle('active',composing); $('recordsTab').classList.toggle('active',!composing);
    $('composeTab').setAttribute('aria-selected',String(composing)); $('recordsTab').setAttribute('aria-selected',String(!composing));
    QuickNav.setActive('#quickNav',composing?'fbPublish':'fbRecords');
    if (!composing) renderRecords();
    $('sidebar').classList.remove('open');
    history.replaceState(null,'',composing?'#compose':'#records');
  }
  function openMaterialPicker() { pendingMaterial = state.materialId; $('materialSearch').value=''; $('materialLanguage').value='all'; renderLibrary(); $('materialDialog').showModal(); }
  function renderLibrary() {
    const query = $('materialSearch').value.trim().toLocaleLowerCase(), language = $('materialLanguage').value;
    const list = materials.filter(item=>(language==='all'||item.language===language)&&[item.id,item.name,item.series].join(' ').toLocaleLowerCase().includes(query));
    $('materialLibrary').innerHTML = list.length ? list.map(item=>`<label class="library-item"><input type="radio" name="material" aria-label="${esc(item.name)}" value="${esc(item.id)}" ${item.id===pendingMaterial?'checked':''}><img src="${esc(item.poster)}" alt="${esc(item.series)}示例画面"><div class="library-body"><strong>${esc(item.name)}</strong><p>${esc(item.id)} · ${esc(item.duration)} · ${esc(item.size)}</p><div class="material-tags"><span class="tag blue">${esc(item.language)}</span><span class="tag">${esc(item.format)}</span><span class="tag">${esc(item.local?'本地预览':'示例素材')}</span></div></div></label>`).join('') : '<div class="empty-state">没有找到匹配的素材，请更换搜索词或语言。</div>';
    $('materialLibrary').querySelectorAll('input').forEach(input=>input.addEventListener('change',()=>{ pendingMaterial=input.value; $('materialSelectionNote').textContent='已选：'+getMaterial(input.value).name; }));
    $('materialSelectionNote').textContent = '已选：'+getMaterial(pendingMaterial).name;
  }
  async function importLocal(file) {
    if (!file) return;
    if (file.type && !file.type.startsWith('video/')) { toast('请选择视频文件'); return; }
    const url = URL.createObjectURL(file); localObjects.add(url);
    const video = document.createElement('video'); video.preload='metadata'; video.src=url;
    $('uploadLocal').disabled=true;
    try {
      await new Promise((resolve,reject)=>{ const timer=setTimeout(()=>reject(new Error('metadata timeout')),10000); video.onloadedmetadata=()=>{clearTimeout(timer);resolve();}; video.onerror=()=>{clearTimeout(timer);reject(new Error('unsupported file'));}; });
      if (!Number.isFinite(video.duration)||video.duration<=0||!video.videoWidth) throw new Error('invalid video');
      const total=Math.round(video.duration),duration=`${String(Math.floor(total/60)).padStart(2,'0')}:${String(total%60).padStart(2,'0')}`;
      const id='LOCAL-'+Date.now();
      materials.unshift({id,name:file.name,language:'本地视频',duration,resolution:`${video.videoWidth} × ${video.videoHeight}`,size:(file.size/1024/1024).toFixed(1)+' MB',poster:'assets/poster-local.svg',series:file.name,format:video.videoWidth>video.videoHeight?'横版':'竖版',kind:'本地预览',local:true,url});
      pendingMaterial=id; $('materialSearch').value=''; $('materialLanguage').value='all'; renderLibrary(); toast('本地视频已载入，可播放预览；文件未上传');
    } catch { URL.revokeObjectURL(url);localObjects.delete(url);toast('浏览器无法读取该视频，请选择可播放的视频文件'); }
    finally { video.removeAttribute('src');video.load();$('uploadLocal').disabled=false;$('localVideoInput').value=''; }
  }
  function openVideo() {
    const item=getMaterial();
    $('videoTitle').textContent=item.name;
    $('videoSubtitle').textContent=`${item.duration} · ${item.resolution} · ${item.local?'本地文件，仅浏览器预览':'示例素材画面'}`;
    $('videoContent').innerHTML=item.local?`<video src="${esc(item.url)}" controls playsinline autoplay></video>`:`<div class="sample-video-poster"><img src="${esc(item.poster)}" alt="${esc(item.name)}示例画面"><div class="sample-video-note">这是素材画面的占位示意<small>点击“更换素材 → 导入本地视频”，即可体验真实播放；本页不执行去重处理。</small></div></div>`;
    $('videoDialog').showModal();
  }
  function validate() {
    let error='',target=null;
    if (!selectedPages().length) {error='请至少选择 1 个可发布的 Facebook 主页。';target=$('pageSearch');}
    else if (!state.caption.trim()) {error='请填写发布文案。';target=$('caption');}
    else if (state.mode==='scheduled' && (!Number.isFinite(parseBeijing(state.schedule))||parseBeijing(state.schedule)<=Date.now())) {error='请选择未来的预约时间，时间按北京时间（UTC+8）计算。';target=$('scheduleAt');}
    if (error) { $('formError').textContent=error;$('formError').hidden=false;toast(error);target?.focus();return false; }
    return true;
  }
  function confirmPublish() {
    if (!validate()) return;
    confirmSnapshot={...state,pageIds:[...state.pageIds]};
    const scheduled=state.mode==='scheduled';
    $('confirmTitle').textContent=scheduled?'确认模拟预约':'确认模拟发布';
    $('confirmPublish').textContent=scheduled?'确认模拟预约':'确认模拟发布';
    $('confirmPublish').disabled=false;
    $('confirmContent').innerHTML=`<dl class="confirm-summary"><div><dt>视频素材</dt><dd>${esc(getMaterial().name)}</dd></div><div><dt>发布主页</dt><dd>${selectedPages().map(page=>esc(page.name)).join('<br>')}</dd></div><div><dt>去重方式</dt><dd>${esc(getTemplate().title)}${state.template==='none'?'':'（占位示例，待确认）'}</dd></div><div><dt>发布时间</dt><dd>${scheduled?esc(state.schedule.replace('T',' '))+'（北京时间）':'立即发布'}</dd></div></dl><div class="confirm-caption">${esc(state.caption)}</div><div class="confirm-notice">将创建 ${selectedPages().length} 条本地演示任务，分别展示各主页的发布结果。不会联系 Facebook 或上传视频。</div>`;
    $('confirmDialog').showModal();
  }
  function executeSimulation() {
    if (!confirmSnapshot) return;
    if (confirmSnapshot.mode==='scheduled' && parseBeijing(confirmSnapshot.schedule)<=Date.now()) { $('confirmDialog').close();confirmSnapshot=null;validate();return; }
    $('confirmPublish').disabled=true;
    const frozen=confirmSnapshot;confirmSnapshot=null;
    const material=getMaterial(frozen.materialId), scheduled=frozen.mode==='scheduled', batch=Date.now();
    const created=frozen.pageIds.filter(id=>getPage(id)?.available).map((pageId,index)=>({id:`DEMO-FB-${batch}-${++taskSequence}`,batch,materialId:material.id,material:material.local?{...material,url:undefined}:undefined,pageId,template:frozen.template,status:scheduled?'scheduled':'processing',mode:frozen.mode,time:scheduled?frozen.schedule.replace('T',' '):'立即发布',created:bjText(batch),caption:frozen.caption,index}));
    records.unshift(...created); persistRecords(); $('confirmDialog').close();showView('records');window.scrollTo({top:0,behavior:'smooth'});
    toast(scheduled?`已创建 ${created.length} 条演示预约，时间到后不会真实执行`:`已创建 ${created.length} 条演示任务，正在模拟发布流程`);
    if (!scheduled) created.forEach((record,index)=>{
      const timer=setTimeout(()=>{timers.delete(timer);const current=records.find(item=>item.id===record.id);if(!current||current.status!=='processing')return;current.status='published';current.time=bjText(Date.now());persistRecords();renderRecords();if($('detailDialog').open && $('detailDialog').dataset.recordId===record.id)renderDetail(record.id);if(index===created.length-1)toast('本次模拟发布已完成，可查看逐主页结果');},2800+index*700);
      timers.add(timer);
    });
  }
  function renderRecords() {
    $('recordTabCount').textContent=String(records.length);
    const count=status=>records.filter(record=>record.status===status).length;
    $('statsGrid').innerHTML=[['全部任务',records.length,'逐主页独立任务'],['待发布 / 处理中',count('scheduled')+count('processing'),'模拟预约与处理流程'],['已完成',count('published'),'仅演示发布结果'],['需要关注',count('failed'),'查看错误说明']].map(([label,value,hint])=>`<div class="stat-card"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>`).join('');
    const filters=[['all','全部'],['scheduled','已预约'],['processing','处理中'],['published','已发布'],['failed','失败'],['cancelled','已取消']];
    $('recordFilters').innerHTML=filters.map(([id,label])=>`<button class="record-filter ${recordFilter===id?'active':''}" data-filter="${id}" aria-pressed="${recordFilter===id}">${label}</button>`).join('');
    $('recordFilters').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{recordFilter=button.dataset.filter;renderRecords();}));
    const query=$('recordSearch').value.trim().toLocaleLowerCase();
    const list=records.filter(record=>(recordFilter==='all'||record.status===recordFilter)&&[record.id,materialMeta(record).name,getPage(record.pageId).name].join(' ').toLocaleLowerCase().includes(query));
    $('recordsBody').innerHTML=list.length?list.map(record=>{const material=materialMeta(record),page=getPage(record.pageId);return `<tr><td><div class="table-material"><img src="${esc(material.poster)}" alt=""><div><strong>${esc(material.name)}</strong><small>${esc(record.id)}</small></div></div></td><td class="table-page">${esc(page.name)}<div class="subtle">${esc(page.owner)}</div></td><td>${esc(getTemplate(record.template).title)}</td><td>${badge(record.status)}</td><td class="table-time">${esc(record.time)}<small>${record.mode==='scheduled'?'预约时间 · UTC+8':'立即发布 · 演示'}</small></td><td><div class="row-actions"><button class="text-button" data-detail="${esc(record.id)}">详情</button>${record.status==='scheduled'?`<button class="text-button" data-cancel="${esc(record.id)}">取消预约</button>`:''}${record.status==='failed'?`<button class="text-button" data-reuse="${esc(record.id)}">重新编辑</button>`:''}</div></td></tr>`;}).join(''):'<tr><td colspan="6"><div class="empty-state">没有匹配的发布任务，请调整筛选条件。</div></td></tr>';
    $('recordsBody').querySelectorAll('[data-detail]').forEach(button=>button.addEventListener('click',()=>openDetail(button.dataset.detail)));
    $('recordsBody').querySelectorAll('[data-cancel]').forEach(button=>button.addEventListener('click',()=>{const record=records.find(item=>item.id===button.dataset.cancel);record.status='cancelled';persistRecords();renderRecords();toast('已取消这条本地演示预约');}));
    $('recordsBody').querySelectorAll('[data-reuse]').forEach(button=>button.addEventListener('click',()=>reuseRecord(button.dataset.reuse)));
  }
  function renderDetail(id) {
    const record=records.find(item=>item.id===id);if(!record)return;
    $('detailDialog').dataset.recordId=id;
    const material=materialMeta(record),page=getPage(record.pageId);
    let timeline='';
    if(record.status==='scheduled')timeline='<li class="done"><strong>模拟预约已创建</strong><small>已记录选择的时间与主页</small></li><li><strong>等待预约时间</strong><small>本页不会自动联系平台或执行真实任务</small></li>';
    if(record.status==='processing')timeline='<li class="done"><strong>模拟任务已创建</strong><small>视频、主页、文案已保存</small></li><li class="done"><strong>视频准备</strong><small>此步骤仅展示流程，不运行视频处理</small></li><li><strong>模拟发布中</strong><small>'+ (record.seed?'初始示例状态，用于查看进度样式':'即将展示模拟完成结果') +'</small></li>';
    if(record.status==='published')timeline='<li class="done"><strong>模拟任务已创建</strong><small>视频、主页、文案已保存</small></li><li class="done"><strong>视频准备完成（模拟）</strong><small>'+esc(record.template==='none'?'直接使用原视频':'固定模板处理步骤示意')+'</small></li><li class="done"><strong>发布完成（模拟）</strong><small>没有真实 Facebook 帖子或外部链接</small></li>';
    if(record.status==='failed')timeline='<li class="done"><strong>模拟任务已创建</strong></li><li><strong>发布失败（示例）</strong><small>'+esc(record.error)+'</small></li>';
    if(record.status==='cancelled')timeline='<li class="done"><strong>模拟任务已创建</strong></li><li class="done"><strong>本地演示已取消</strong><small>'+esc(record.error||'由操作者取消预约')+'</small></li>';
    $('detailContent').innerHTML=`<dl class="confirm-summary"><div><dt>任务 ID</dt><dd>${esc(record.id)}</dd></div><div><dt>当前状态</dt><dd>${badge(record.status)}</dd></div><div><dt>视频素材</dt><dd>${esc(material.name)}</dd></div><div><dt>发布主页</dt><dd>${esc(page.name)}</dd></div><div><dt>去重模板</dt><dd>${esc(getTemplate(record.template).title)}</dd></div><div><dt>创建时间</dt><dd>${esc(record.created)}（北京）</dd></div><div><dt>发布时间</dt><dd>${esc(record.time)}${record.mode==='scheduled'?'（北京）':''}</dd></div></dl><ol class="timeline">${timeline}</ol><div class="confirm-caption">${esc(record.caption)}</div>`;
  }
  function openDetail(id) {renderDetail(id);$('detailDialog').showModal();}
  function reuseRecord(id) {const record=records.find(item=>item.id===id);if(!record)return;state.materialId=materials.some(item=>item.id===record.materialId)?record.materialId:materials[0].id;state.pageIds=[record.pageId];state.template=getTemplate(record.template).id;state.caption=record.caption;state.mode='now';syncForm();showView('compose');toast('已带入原配置，修改后可创建新的演示任务');}
  function syncForm() { $('caption').value=state.caption;$('scheduleAt').value=state.schedule;document.querySelectorAll('[name=publishMode]').forEach(input=>{input.checked=input.value===state.mode;});renderMaterial();renderPages();renderTemplates();update(); }
  function saveDraft() {const local=getMaterial().local;const draft={...state,materialId:local?materials.find(item=>!item.local).id:state.materialId};const saved=writeStorage(':draft',draft);toast(saved?(local?'草稿已保存；本地视频不会保存，刷新后请重新导入':'草稿已保存在当前浏览器，刷新后可继续编辑'):'浏览器未允许本地存储，当前草稿仍保留在页面中');}
  restore();
  UiTopbar.render({userCard:'#userCard',refreshButton:'#refreshPage',auth:{authenticated:true,user:{name:'演示运营员',role:'演示账号',tenant_key:'preview'}}});
  // Offline prototype data only. Production auth and navigation endpoints are intentionally not called.
  $('userCard').querySelector('.user-meta span').textContent='DramaWave 工作空间';
  QuickNav.render({container:'#quickNav',activeKey:location.hash==='#records'?'fbRecords':'fbPublish',auth:{authenticated:true,user:{role:'admin'}},onNavigate(item){if(item.key==='fbPublish')showView('compose');else if(item.key==='fbRecords')showView('records');else if(item.key==='materials')openMaterialPicker();else if(item.key==='about')$('infoDialog').showModal();else toast('YouTube 为现有功能入口，本次原型聚焦 Facebook 发布');}});
  syncForm();renderRecords();if(location.hash==='#records')showView('records');
  $('openMaterials').addEventListener('click',openMaterialPicker);
  $('pageSearch').addEventListener('input',renderPages);
  $('selectAllPages').addEventListener('click',()=>{const available=pages.filter(page=>page.available);state.pageIds=selectedPages().length===available.length?[]:available.map(page=>page.id);update();});
  $('caption').addEventListener('input',()=>{state.caption=$('caption').value;update();});
  $('sampleCaption').addEventListener('click',()=>{state.caption=captionSample;$('caption').value=state.caption;update();toast('已填入示例文案，可继续修改');});
  document.querySelectorAll('[name=publishMode]').forEach(input=>input.addEventListener('change',()=>{state.mode=input.value;update();}));
  $('scheduleAt').addEventListener('input',()=>{state.schedule=$('scheduleAt').value;update();});
  $('tomorrowButton').addEventListener('click',()=>{state.schedule=tomorrowTen();$('scheduleAt').value=state.schedule;update();});
  $('previewPage').addEventListener('change',()=>{state.previewPage=$('previewPage').value;update();});
  $('previewVideoButton').addEventListener('click',openVideo);
  $('materialSearch').addEventListener('input',renderLibrary);$('materialLanguage').addEventListener('change',renderLibrary);
  $('confirmMaterial').addEventListener('click',()=>{state.materialId=pendingMaterial;renderMaterial();update();$('materialDialog').close();toast('素材已更新');});
  $('uploadLocal').addEventListener('click',()=>$('localVideoInput').click());$('localVideoInput').addEventListener('change',()=>importLocal($('localVideoInput').files[0]));
  $('composeTab').addEventListener('click',()=>showView('compose'));$('recordsTab').addEventListener('click',()=>showView('records'));$('newPublish').addEventListener('click',()=>{showView('compose');window.scrollTo({top:0,behavior:'smooth'});});
  $('recordSearch').addEventListener('input',renderRecords);
  $('submitPublish').addEventListener('click',confirmPublish);$('confirmPublish').addEventListener('click',executeSimulation);$('saveDraft').addEventListener('click',saveDraft);
  $('requirementsButton').addEventListener('click',()=>$('infoDialog').showModal());
  $('menuButton').addEventListener('click',()=>$('sidebar').classList.toggle('open'));
  document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();showView('compose');});
  document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
  document.querySelectorAll('dialog').forEach(dialog=>{dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}});});
  $('videoDialog').addEventListener('close',()=>{$('videoContent').innerHTML='';});
  $('confirmDialog').addEventListener('close',()=>{confirmSnapshot=null;});
  $('resetDemo').addEventListener('click',()=>{timers.forEach(clearTimeout);timers.clear();records=seedRecords();persistRecords();try{localStorage.removeItem(STORAGE+':draft');}catch{}recordFilter='all';$('recordSearch').value='';renderRecords();toast('演示记录和已保存草稿已重置，当前编辑内容保留');});
  window.addEventListener('beforeunload',()=>localObjects.forEach(url=>URL.revokeObjectURL(url)));
})();
