(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const STORAGE = 'facebook-page-management-preview-v3-20261019';
  const captionSample = '{name}\n{desc}\n\n立即观看：{url}\n#DramaWave #短剧';
  const pages = [
    {id:'demo-page-01',name:'DramaWave 短剧',owner:'内容运营 A',language:'中文',mark:'D',color:'',available:true},
    {id:'demo-page-02',name:'DramaWave 故事剧场',owner:'内容运营 A',language:'中文',mark:'剧',color:'green',available:true},
    {id:'demo-page-03',name:'DramaWave Romance',owner:'内容运营 B',language:'英语',mark:'R',color:'rose',available:true},
    {id:'demo-page-04',name:'DramaWave Español',owner:'内容运营 C',language:'西班牙语',mark:'E',color:'amber',available:true},
    {id:'demo-page-05',name:'DramaWave 精选',owner:'内容运营 B',language:'中文',mark:'D',color:'gray',available:false,authReason:'授权已过期，请重新授权（示例）'}
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
  const descriptions={
    '她的第二人生':'告别被安排的人生，她决定为自己重新开始。一场意外，让平凡生活迎来新的转折。',
    '雨夜之后':'雨夜的一次相遇，让两个陌生人的命运从此交织。',
    'The Last Promise':'An unexpected reunion brings a forgotten promise back to life.',
    'Volver a Empezar':'Una decisión inesperada cambia su destino para siempre.',
    'After the Rain':'After a chance encounter, two strangers discover a new beginning.'
  };
  materials.forEach(m=>{m.contentId='demo-content-'+m.id.replace('DEMO-','');m.description=descriptions[m.series];m.tag='none';});
  const statusLabels={published:'模拟已发布',scheduled:'模拟已预约',processing:'模拟处理中',failed:'模拟失败',cancelled:'模拟已取消'};
  const Links=FBPreviewLinks;
  const state={materialId:materials[0].id,pageIds:[],pageDrafts:{},template:'none',mode:'now',schedule:'',previewPage:''};
  const configuredPages=new Map();
  let currentView='compose',pendingMaterial=state.materialId,recordFilter='all',records=[],toastTimer,confirmSnapshot=null,taskSequence=100;
  let editor={pageId:null,selected:new Set(),original:''};
  const timers=new Set(),localObjects=new Set(),previewCreatedMs=Date.now();
  const bjLocal=(time=Date.now())=>new Date(time+8*3600000).toISOString().slice(0,16);
  const bjText=time=>bjLocal(time).replace('T',' ');
  const parseBeijing=str=>Date.parse(str+':00+08:00');
  const tomorrowTen=()=>bjLocal(Date.now()+24*3600000).slice(0,10)+'T10:00';
  state.schedule=tomorrowTen();
  const getMaterial=(id=state.materialId)=>materials.find(item=>item.id===id)||materials[0];
  const getPage=id=>pages.find(page=>page.id===id);
  const getTemplate=(id=state.template)=>templates.find(t=>t.id===id)||templates[0];
  const eligiblePages=()=>pages.filter(p=>configuredPages.has(p.id)&&p.available);
  const selectedPages=()=>eligiblePages().filter(p=>state.pageIds.includes(p.id));
  function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,3600);}
  function readStorage(key){try{return JSON.parse(localStorage.getItem(STORAGE+key)||'null');}catch{return null;}}
  function writeStorage(key,value){try{localStorage.setItem(STORAGE+key,JSON.stringify(value));return true;}catch{return false;}}
  function draftFor(id){
    if(!configuredPages.has(id))return null;
    if(!state.pageDrafts[id]){const c=configuredPages.get(id);state.pageDrafts[id]={captionTemplate:c.captionTemplate,version:c.version};}
    return state.pageDrafts[id];
  }
  function previewLink(page,material=getMaterial()){
    return page?Links.buildLink({page,material,taskId:'preview-'+page.id+'-'+material.id,ledgerId:1,createdMs:previewCreatedMs}):null;
  }
  function linkDetails(link){
    if(!link)return '<p class="field-note">此素材没有剧集关联，无法预览短链。</p>';
    return `<details class="link-details"><summary>查看长链参数 · 2310</summary><div class="link-details-body"><p>模拟归因；sub_user_id 900001 为示例发布人映射，不查询真实账号。</p><code class="long-url">${esc(link.longUrl)}</code><dl>${link.params.map(([key,value])=>`<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl></div></details>`;
  }
  function renderManagement(){
    const query=$('managementSearch').value.trim().toLocaleLowerCase(),filter=$('managementFilter').value;
    const list=pages.filter(p=>configuredPages.has(p.id)&&[p.name,p.id,p.language].join(' ').toLocaleLowerCase().includes(query)&&(filter==='all'||p.available===(filter==='available')));
    $('managementTotal').textContent=`${configuredPages.size} 个已配置 Page`;
    $('managementCount').textContent=`显示 ${list.length} / ${configuredPages.size} 个 Page`;
    $('managementTableWrap').hidden=!configuredPages.size;$('managementEmpty').hidden=Boolean(configuredPages.size);
    $('managementBody').innerHTML=list.length?list.map(p=>{const c=configuredPages.get(p.id);return `<tr><td><strong class="group-name">${esc(p.name)}</strong><small>${esc(p.id)}</small></td><td>${esc(p.language)}</td><td><span class="status-badge ${p.available?'published':'failed'}">${p.available?'授权正常':'授权异常'}</span><small>${p.available?'可选择发布':esc(p.authReason)}</small></td><td><p class="group-caption-excerpt">${esc(c.captionTemplate)}</p><small>版本 ${c.version}</small></td><td>${esc(c.updatedAt)}<small>北京时间 · 本地演示</small></td><td><button class="text-button" data-edit-page="${p.id}">编辑文案</button></td></tr>`;}).join(''):'<tr><td colspan="6"><div class="empty-state">没有匹配的 Page，请调整搜索或筛选。</div></td></tr>';
    $('managementBody').querySelectorAll('[data-edit-page]').forEach(b=>b.addEventListener('click',()=>openPageEditor(b.dataset.editPage)));
  }
  function openPageEditor(id=null){
    const config=id?configuredPages.get(id):null;
    editor={pageId:id,selected:new Set(id?[id]:[]),original:config?.captionTemplate||''};
    $('pageDialogTitle').textContent=id?'编辑 Page 文案':'创建 Page 配置';
    $('pageEditorSubtitle').textContent=id?`${getPage(id).name} · ${id}`:'可一次选择多个 Page，保存后分别建立独立配置。';
    $('pageCaption').value=editor.original;$('pagePickerSearch').value='';$('pageSaveError').hidden=true;
    $('candidateControls').hidden=Boolean(id);$('savePageConfig').textContent=id?'保存修改':'创建配置';
    $('editorPreviewMaterial').value=(materials.find(m=>!m.local&&m.language===(id?getPage(id).language:'中文'))||materials[0]).id;
    renderCandidates();updateEditorPreview();$('pageDialog').showModal();
  }
  function renderCandidates(){
    const query=$('pagePickerSearch').value.trim().toLocaleLowerCase();
    const list=pages.filter(p=>(editor.pageId?p.id===editor.pageId:true)&&[p.name,p.id,p.language].join(' ').toLocaleLowerCase().includes(query));
    $('pageCandidates').innerHTML=list.length?list.map(p=>{const existing=!editor.pageId&&configuredPages.has(p.id);return `<label class="candidate-page ${existing?'configured':''}"><input type="checkbox" value="${p.id}" aria-label="配置 ${esc(p.name)}" ${editor.selected.has(p.id)?'checked':''} ${existing||editor.pageId?'disabled':''}><span><strong>${esc(p.name)}</strong><small>${esc(p.id)} · ${esc(p.language)}</small></span><span class="status-badge ${existing?'cancelled':p.available?'published':'failed'}">${existing?'已配置':p.available?'授权正常':'授权异常'}</span></label>`;}).join(''):'<div class="empty-state">没有匹配的 Page。</div>';
    $('pageCandidates').querySelectorAll('input:not(:disabled)').forEach(input=>input.addEventListener('change',()=>{input.checked?editor.selected.add(input.value):editor.selected.delete(input.value);updateEditorPreview();}));
    $('candidateCount').textContent=`已选 ${editor.selected.size} 个 Page`;
  }
  function editorDirty(){return $('pageCaption').value!==editor.original||(!editor.pageId&&editor.selected.size>0);}
  function updateEditorPreview(){
    const material=getMaterial($('editorPreviewMaterial').value),page=getPage([...editor.selected][0])||pages[0],link=previewLink(page,material);
    const result=Links.render($('pageCaption').value,material,link);
    $('editorExpandedPreview').textContent=result.text||'填写左侧文案，查看宏参数替换后的效果。';
    $('editorPreviewError').textContent=result.error;$('editorPreviewError').hidden=!result.error||!$('pageCaption').value.trim();
    $('editorLinkDetails').innerHTML=linkDetails(link);
    $('pageCaptionCount').textContent=`模板 ${Links.bytes($('pageCaption').value)} / 16,000 字节 · 预览 ${result.bytes} / 5,000 字节`;
    $('pageDirtyStatus').textContent=editorDirty()?'有未保存修改':'尚未修改';
    $('candidateCount').textContent=`已选 ${editor.selected.size} 个 Page`;
    $('editorPreviewPageNote').textContent=`以 ${page.name} 展示归因示例；预览剧目不绑定配置。`;
    $('pageSaveError').hidden=true;
  }
  function closePageEditor(){if(editorDirty()&&!window.confirm('放弃尚未保存的 Page 配置？'))return;$('pageDialog').close();}
  function savePageConfig(){
    const value=$('pageCaption').value,syntaxError=Links.validateTemplate(value);
    const error=editor.selected.size?syntaxError:'请至少选择 1 个 Page。';
    if(error){$('pageSaveError').textContent=error;$('pageSaveError').hidden=false;return;}
    if(!editor.pageId&&[...editor.selected].some(id=>configuredPages.has(id))){$('pageSaveError').textContent='部分 Page 已配置，请重新选择。';$('pageSaveError').hidden=false;renderCandidates();return;}
    const next=new Map(configuredPages),stamp=bjText(Date.now());
    for(const id of editor.selected)next.set(id,{pageId:id,captionTemplate:value,version:(next.get(id)?.version||0)+1,updatedAt:stamp});
    if(!writeStorage(':page-configs',[...next.values()])){$('pageSaveError').textContent='浏览器未允许本地存储，配置尚未保存。当前输入保留，请重试。';$('pageSaveError').hidden=false;return;}
    configuredPages.clear();for(const [id,c] of next)configuredPages.set(id,c);
    // Existing publication drafts are snapshots; saving a template must not overwrite them.
    $('pageDialog').close();renderManagement();renderPages();update();toast(editor.pageId?'Page 文案已保存；当前发布草稿保持原样':`已创建 ${editor.selected.size} 个 Page 配置，可前往视频发布选择`);
  }
  function restore(){
    const savedConfigs=readStorage(':page-configs');
    if(Array.isArray(savedConfigs))for(const c of savedConfigs)if(c&&getPage(c.pageId)&&typeof c.captionTemplate==='string'&&!Links.validateTemplate(c.captionTemplate))configuredPages.set(c.pageId,{pageId:c.pageId,captionTemplate:c.captionTemplate,version:Number.isSafeInteger(c.version)?c.version:1,updatedAt:String(c.updatedAt||'本地保存')});
    const draft=readStorage(':draft');
    if(draft&&typeof draft==='object'){
      if(materials.some(m=>m.id===draft.materialId))state.materialId=draft.materialId;
      if(Array.isArray(draft.pageIds))state.pageIds=eligiblePages().filter(p=>draft.pageIds.includes(p.id)).map(p=>p.id);
      if(templates.some(t=>t.id===draft.template))state.template=draft.template;
      for(const p of pages){const d=draft.pageDrafts?.[p.id];if(configuredPages.has(p.id)&&d&&typeof d.captionTemplate==='string'&&Links.bytes(d.captionTemplate)<=16000)state.pageDrafts[p.id]={captionTemplate:d.captionTemplate,version:Number.isSafeInteger(d.version)?d.version:1};}
      if(['now','scheduled'].includes(draft.mode))state.mode=draft.mode;
      if(typeof draft.schedule==='string'&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(draft.schedule))state.schedule=draft.schedule;
      if(state.pageIds.includes(draft.previewPage))state.previewPage=draft.previewPage;
    }
    const saved=readStorage(':records');
    records=Array.isArray(saved)?saved.filter(r=>r&&typeof r.id==='string'&&r.id.startsWith('DEMO-FB-')&&getPage(r.pageId)&&typeof r.caption==='string'&&Object.hasOwn(statusLabels,r.status)).slice(0,200):[];
    records=records.map(r=>r.status==='processing'?{...r,status:'cancelled',error:'演示页面已刷新，本地处理模拟已结束。'}:r);
    taskSequence=Math.max(100,...records.map(r=>Number(r.link?.ledgerId)||0));
  }
  function persistRecords(){return writeStorage(':records',records.slice(0,200));}
  function materialMeta(record){return materials.find(m=>m.id===record.materialId)||record.material||materials[0];}
  function pageAvatar(page){return `<span class="page-avatar ${esc(page.color)}">${esc(page.mark)}<b class="avatar-f">f</b></span>`;}
  function badge(status){return `<span class="status-badge ${esc(status)}">${esc(statusLabels[status]||status)}</span>`;}
  function renderPages(){
    const query=$('pageSearch').value.trim().toLocaleLowerCase();
    const list=pages.filter(p=>configuredPages.has(p.id)&&[p.name,p.id,p.language].join(' ').toLocaleLowerCase().includes(query));
    $('pageGrid').innerHTML=list.length?list.map(p=>`<label class="page-option" title="${p.available?'选择此 Page':esc(p.authReason)}">${pageAvatar(p)}<span class="page-info"><strong>${esc(p.name)}</strong><small>${esc(p.language)} · ${p.available?'已配置文案':esc(p.authReason)}</small></span><input type="checkbox" aria-label="${esc(p.name)}" value="${p.id}" ${state.pageIds.includes(p.id)?'checked':''} ${p.available?'':'disabled'}></label>`).join(''):`<div class="empty-state">${configuredPages.size?'没有找到匹配的主页。':'暂无已配置 Page，请先前往 Page 管理创建。'}</div>`;
    $('pageGrid').querySelectorAll('input:not(:disabled)').forEach(input=>input.addEventListener('change',()=>{state.pageIds=input.checked?[...new Set([...state.pageIds,input.value])]:state.pageIds.filter(id=>id!==input.value);if(input.checked)draftFor(input.value);update();}));
    $('selectAllPages').disabled=!eligiblePages().length;
  }
  function update(){
    const active=selectedPages(),material=getMaterial(),template=getTemplate();
    state.pageIds=active.map(p=>p.id);
    if(!active.some(p=>p.id===state.previewPage))state.previewPage=active[0]?.id||'';
    const p=getPage(state.previewPage),draft=p?draftFor(p.id):null,link=previewLink(p,material);
    const result=draft?Links.render(draft.captionTemplate,material,link):{text:'选择已配置的 Page 后，在这里查看文案效果。',error:'',bytes:0};
    $('pageCount').textContent=`已选 ${active.length} 个主页`;
    $('selectedPagesSummary').textContent=active.length?'已选：'+active.map(p=>p.name).join('、'):'请选择至少 1 个已配置且可用的 Page。';
    $('selectAllPages').textContent=active.length&&active.length===eligiblePages().length?'取消全选':'全选可用';
    $('pageGrid').querySelectorAll('input').forEach(input=>input.checked=state.pageIds.includes(input.value));
    const options=active.length?active.map(p=>`<option value="${p.id}" ${state.previewPage===p.id?'selected':''}>${esc(p.name)}</option>`).join(''):'<option value="">请先选择发布 Page</option>';
    $('captionPage').innerHTML=options;$('previewPage').innerHTML=options;
    $('caption').disabled=!draft;$('caption').value=draft?.captionTemplate||'';
    $('restorePageCaption').disabled=!draft;$('sampleCaption').disabled=!draft;
    document.querySelectorAll('[data-macro-target="caption"] button').forEach(b=>b.disabled=!draft);
    const config=p?configuredPages.get(p.id):null;
    $('captionSource').textContent=draft?`模板版本 ${draft.version}${draft.captionTemplate!==config.captionTemplate?' · 本次文案与当前模板不同':''}${draft.version!==config.version?' · 模板已更新，可重新套用':''}`:'每个 Page 使用自己的发布文案';
    $('captionCount').textContent=`${result.bytes} / 5,000 字节（替换后）`;
    $('captionMacroError').textContent=result.error;$('captionMacroError').hidden=!result.error;
    $('previewName').textContent=p?.name||'请先选择 Page';$('previewAvatar').textContent=p?.mark||'f';$('previewAvatar').className=`page-avatar ${p?.color||''}`;
    $('previewCaption').textContent=result.text;$('previewPoster').src=material.poster;$('previewPoster').alt=material.series+' · 示例画面';$('previewDuration').textContent=material.duration;
    $('publishLinkDetails').innerHTML=p?linkDetails(link):'';
    $('templateNote').textContent=template.note;$('scheduleFields').hidden=state.mode!=='scheduled';$('scheduleAt').min=bjLocal(Date.now()+60000);
    $('summaryMaterial').textContent=material.series;$('summaryPages').textContent=`${active.length} 个 Page`;$('summaryTemplate').textContent=template.title;
    $('summaryTime').textContent=state.mode==='now'?'立即发布':state.schedule.replace('T',' ')+'（北京）';
    $('publishSummary').textContent=`1 条视频 → ${active.length} 个主页`;$('publishSummaryDetail').textContent=`${template.title} · 各 Page 独立文案`;
    $('submitLabel').textContent=state.mode==='now'?'模拟发布':'模拟预约';$('formError').hidden=true;
  }
  function showView(view){
    currentView=['compose','records','management'].includes(view)?view:'compose';
    const names={compose:'Facebook 发布',records:'发布记录',management:'Page 管理'};
    const descriptions={compose:'选择已配置的 Page，各自使用独立文案发布。',records:'按主页查看处理进度与发布结果。',management:'创建 Page 配置，设置每个主页的默认发布文案。'};
    ['compose','records','management'].forEach(key=>{$(key+'View').hidden=key!==currentView;$(key+'Tab').classList.toggle('active',key===currentView);$(key+'Tab').setAttribute('aria-selected',String(key===currentView));});
    document.title=names[currentView]+' · AI自动后台 · 交互预览';$('pageTitle').textContent=names[currentView];$('breadcrumbTitle').textContent=names[currentView];$('pageDescription').textContent=descriptions[currentView];
    QuickNav.setActive('#quickNav',{compose:'fbPublish',records:'fbRecords',management:'fbPages'}[currentView]);
    if(currentView==='records')renderRecords();if(currentView==='management')renderManagement();
    $('sidebar').classList.remove('open');history.replaceState(null,'','#'+currentView);
  }
  function validate(){
    let error='',target=$('pageSearch');
    if(!selectedPages().length)error='请至少选择 1 个已配置且可用的 Page。';
    for(const p of selectedPages()){
      const result=Links.render(draftFor(p.id).captionTemplate,getMaterial(),previewLink(p));
      if(result.error){error=p.name+'：'+result.error;state.previewPage=p.id;update();target=$('caption');break;}
    }
    if(!error&&state.mode==='scheduled'&&(!Number.isFinite(parseBeijing(state.schedule))||parseBeijing(state.schedule)<=Date.now())){error='请选择未来的预约时间（北京时间 UTC+8）。';target=$('scheduleAt');}
    if(error){$('formError').textContent=error;$('formError').hidden=false;toast(error);target.focus();return false;}
    return true;
  }
  function confirmPublish(){
    if(!validate())return;
    const createdMs=Date.now(),material=getMaterial(),batch=crypto.randomUUID(),scheduled=state.mode==='scheduled';
    const entries=selectedPages().map((p,index)=>{
      const taskId=crypto.randomUUID().replaceAll('-',''),ledgerId=String(taskSequence+index+1),draft=draftFor(p.id);
      const link=Links.buildLink({page:p,material,taskId,ledgerId,createdMs});
      return {id:'DEMO-FB-'+taskId,batch,materialId:material.id,material:material.local?{...material,url:undefined}:undefined,pageId:p.id,template:state.template,status:scheduled?'scheduled':'processing',mode:state.mode,time:scheduled?state.schedule.replace('T',' '):'立即发布',created:bjText(createdMs),createdAtUtc:new Date(createdMs).toISOString(),caption:Links.render(draft.captionTemplate,material,link).text,captionTemplate:draft.captionTemplate,templateVersion:draft.version,link,landingId:2310};
    });
    // The final short URL has a different length than a preview URL: validate again.
    const invalid=entries.find(r=>Links.render(r.captionTemplate,material,r.link).error);
    if(invalid){state.previewPage=invalid.pageId;update();$('formError').textContent=getPage(invalid.pageId).name+'：'+Links.render(invalid.captionTemplate,material,invalid.link).error;$('formError').hidden=false;return;}
    confirmSnapshot={entries,mode:state.mode,schedule:state.schedule};
    $('confirmTitle').textContent=scheduled?'确认模拟预约':'确认模拟发布';$('confirmPublish').textContent=$('confirmTitle').textContent;$('confirmPublish').disabled=false;
    $('confirmContent').innerHTML=`<dl class="confirm-summary"><div><dt>视频素材</dt><dd>${esc(material.name)}</dd></div><div><dt>发布时间</dt><dd>${scheduled?esc(state.schedule.replace('T',' '))+'（北京时间）':'立即发布'}</dd></div></dl>`+entries.map(r=>`<section class="confirm-page"><h3>${esc(getPage(r.pageId).name)} <small>模板版本 ${r.templateVersion}</small></h3><div class="confirm-caption">${esc(r.caption)}</div><p class="short-link-label">模拟短链 · 不可访问</p><code class="short-link">${esc(r.link?.shortUrl||'无剧集关联，不生成示例短链')}</code>${linkDetails(r.link)}</section>`).join('')+`<div class="confirm-notice">将创建 ${entries.length} 条本地演示任务。短链和归因均为模拟，不创建真实短链或 Facebook 帖子。</div>`;
    $('confirmDialog').showModal();
  }
  function executeSimulation(){
    if(!confirmSnapshot)return;
    if(confirmSnapshot.mode==='scheduled'&&parseBeijing(confirmSnapshot.schedule)<=Date.now()){$('confirmDialog').close();confirmSnapshot=null;validate();return;}
    const frozen=confirmSnapshot,created=frozen.entries;
    $('confirmPublish').disabled=true;
    if(!writeStorage(':records',[...created,...records].slice(0,200))){$('confirmPublish').disabled=false;toast('浏览器未允许保存演示任务，尚未创建；请重试。');return;}
    confirmSnapshot=null;taskSequence+=created.length;records.unshift(...created);$('confirmDialog').close();showView('records');window.scrollTo({top:0,behavior:'smooth'});
    toast(`已创建 ${created.length} 条独立演示任务`);
    if(frozen.mode==='now')created.forEach((record,index)=>{const timer=setTimeout(()=>{timers.delete(timer);const r=records.find(r=>r.id===record.id);if(!r||r.status!=='processing')return;r.status='published';r.time=bjText(Date.now());persistRecords();renderRecords();if($('detailDialog').open&&$('detailDialog').dataset.recordId===r.id)renderDetail(r.id);},1600+index*300);timers.add(timer);});
  }
  function reuseRecord(id){
    const r=records.find(r=>r.id===id);if(!r)return;
    if(!configuredPages.has(r.pageId)||!getPage(r.pageId)?.available){toast('该 Page 尚未配置或授权异常，无法重新编辑发布。');return;}
    state.materialId=materials.some(m=>m.id===r.materialId)?r.materialId:materials[0].id;state.pageIds=[r.pageId];state.previewPage=r.pageId;state.template=getTemplate(r.template).id;
    state.pageDrafts[r.pageId]={captionTemplate:r.captionTemplate,version:r.templateVersion};state.mode='now';syncForm();showView('compose');
  }
  function syncForm(){$('scheduleAt').value=state.schedule;document.querySelectorAll('[name=publishMode]').forEach(input=>input.checked=input.value===state.mode);renderMaterial();renderPages();renderTemplates();update();}
  function saveDraft(){const local=getMaterial().local,draft={...state,materialId:local?materials.find(m=>!m.local).id:state.materialId};toast(writeStorage(':draft',draft)?(local?'草稿已保存；本地视频需重新导入':'各 Page 的发布草稿已保存在当前浏览器'):'浏览器未允许本地存储，当前输入仍保留在页面中');}

  function renderMaterial() {
    const item = getMaterial();
    $('selectedMaterial').innerHTML = `<div class="selected-material"><button class="material-thumb icon-button" id="selectedVideoPreview" aria-label="预览所选素材"><img src="${esc(item.poster)}" alt="${esc(item.series)}示例画面"><span class="thumb-time">${esc(item.duration)}</span></button><div class="material-meta"><h3>${esc(item.name)}</h3><p>${esc(item.id)} · ${esc(item.size)}</p><div class="material-tags"><span class="tag blue">${esc(item.language)}</span><span class="tag">${esc(item.resolution)}</span><span class="tag">${esc(item.local ? '本地预览' : '示例素材')}</span></div></div><span class="selected-check">${icon('check')}</span></div>`;
    $('selectedVideoPreview').addEventListener('click',openVideo);
  }
  function renderTemplates() {
    $('templateGrid').innerHTML = templates.map(template => `<label class="template-option"><input type="radio" name="template" value="${template.id}" aria-label="${esc(template.title)}" ${state.template===template.id?'checked':''}><span class="template-icon">${icon(template.icon)}</span><strong>${esc(template.title)}</strong><small>${esc(template.subtitle)}</small></label>`).join('');
    $('templateGrid').querySelectorAll('input').forEach(input => input.addEventListener('change',() => { state.template = input.value; update(); }));
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
    $('detailContent').innerHTML=`<dl class="confirm-summary"><div><dt>任务 ID</dt><dd>${esc(record.id)}</dd></div><div><dt>当前状态</dt><dd>${badge(record.status)}</dd></div><div><dt>视频素材</dt><dd>${esc(material.name)}</dd></div><div><dt>发布主页</dt><dd>${esc(page.name)}</dd></div><div><dt>去重模板</dt><dd>${esc(getTemplate(record.template).title)}</dd></div><div><dt>创建时间</dt><dd>${esc(record.created)}（北京）</dd></div><div><dt>发布时间</dt><dd>${esc(record.time)}${record.mode==='scheduled'?'（北京）':''}</dd></div></dl><ol class="timeline">${timeline}</ol><h3>发布文案 · 模板版本 ${record.templateVersion}</h3><div class="confirm-caption">${esc(record.caption)}</div><p class="short-link-label">模拟短链 · 不可访问</p><code class="short-link">${esc(record.link?.shortUrl||"无剧集关联")}</code>${linkDetails(record.link)}`;
  }
  function openDetail(id) {renderDetail(id);$('detailDialog').showModal();}
  restore();
  UiTopbar.render({userCard:'#userCard',refreshButton:'#refreshPage',auth:{authenticated:true,user:{name:'演示运营员',role:'演示账号',tenant_key:'preview'}}});
  $('userCard').querySelector('.user-meta span').textContent='DramaWave 工作空间';
  const initialView=location.hash.slice(1)||document.body.dataset.initialView||'compose';
  QuickNav.render({container:'#quickNav',activeKey:{compose:'fbPublish',records:'fbRecords',management:'fbPages'}[initialView]||'fbPublish',auth:{authenticated:true,user:{role:'admin'}},onNavigate(item){if(item.key==='fbPublish')showView('compose');else if(item.key==='fbRecords')showView('records');else if(item.key==='fbPages')showView('management');else if(item.key==='materials')openMaterialPicker();else if(item.key==='about')$('infoDialog').showModal();else toast('本原型仅包含 Facebook 发布与 Page 管理');}});
  $('editorPreviewMaterial').innerHTML=materials.map(m=>`<option value="${m.id}">${esc(m.series)} · ${esc(m.language)}</option>`).join('');
  document.querySelectorAll('[data-macro-target]').forEach(bar=>{
    bar.innerHTML='<span>插入宏参数</span>'+[['name','剧名'],['desc','简介'],['url','短链']].map(([key,label])=>`<button type="button" class="macro-chip" data-token="{${key}}" aria-label="插入${label}宏">{${key}} ${label}</button>`).join('');
    bar.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{const input=$(bar.dataset.macroTarget);if(input.disabled)return;input.setRangeText(b.dataset.token,input.selectionStart,input.selectionEnd,'end');input.focus();input.dispatchEvent(new Event('input',{bubbles:true}));}));
  });
  syncForm();renderRecords();renderManagement();showView(initialView);
  for(const view of ['compose','records','management'])$(view+'Tab').addEventListener('click',()=>showView(view));
  $('managePages').addEventListener('click',()=>showView('management'));
  $('createPage').addEventListener('click',()=>openPageEditor());$('createFirstPage').addEventListener('click',()=>openPageEditor());
  $('managementSearch').addEventListener('input',renderManagement);$('managementFilter').addEventListener('change',renderManagement);
  $('pagePickerSearch').addEventListener('input',renderCandidates);
  $('selectCandidatePages').addEventListener('click',()=>{const matches=[...$('pageCandidates').querySelectorAll('input:not(:disabled)')],all=matches.length&&matches.every(i=>editor.selected.has(i.value));matches.forEach(i=>all?editor.selected.delete(i.value):editor.selected.add(i.value));renderCandidates();updateEditorPreview();});
  $('pageCaption').addEventListener('input',updateEditorPreview);$('editorPreviewMaterial').addEventListener('change',updateEditorPreview);
  $('editorSampleCaption').addEventListener('click',()=>{$('pageCaption').value=captionSample;updateEditorPreview();});
  $('savePageConfig').addEventListener('click',savePageConfig);$('closePageEditor').addEventListener('click',closePageEditor);$('cancelPageEdit').addEventListener('click',closePageEditor);
  $('pageDialog').addEventListener('cancel',event=>{event.preventDefault();closePageEditor();});
  $('pageDialog').addEventListener('click',event=>{if(event.target===$('pageDialog')){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom){event.stopImmediatePropagation();closePageEditor();}}});
  $('openMaterials').addEventListener('click',openMaterialPicker);$('pageSearch').addEventListener('input',renderPages);
  $('selectAllPages').addEventListener('click',()=>{const available=eligiblePages();state.pageIds=selectedPages().length===available.length?[]:available.map(p=>p.id);state.pageIds.forEach(draftFor);update();});
  $('caption').addEventListener('input',()=>{const d=draftFor(state.previewPage);if(d){d.captionTemplate=$('caption').value;update();}});
  for(const id of ['captionPage','previewPage'])$(id).addEventListener('change',()=>{state.previewPage=$(id).value;update();});
  $('restorePageCaption').addEventListener('click',()=>{const c=configuredPages.get(state.previewPage),d=draftFor(state.previewPage);if(!c||!d)return;if(d.captionTemplate!==c.captionTemplate&&!window.confirm('用此 Page 的最新模板替换本次文案？'))return;state.pageDrafts[state.previewPage]={captionTemplate:c.captionTemplate,version:c.version};update();});
  $('sampleCaption').addEventListener('click',()=>{const d=draftFor(state.previewPage);if(d){d.captionTemplate=captionSample;update();}});
  document.querySelectorAll('[name=publishMode]').forEach(input=>input.addEventListener('change',()=>{state.mode=input.value;update();}));
  $('scheduleAt').addEventListener('input',()=>{state.schedule=$('scheduleAt').value;update();});$('tomorrowButton').addEventListener('click',()=>{state.schedule=tomorrowTen();$('scheduleAt').value=state.schedule;update();});
  $('previewVideoButton').addEventListener('click',openVideo);$('materialSearch').addEventListener('input',renderLibrary);$('materialLanguage').addEventListener('change',renderLibrary);
  $('confirmMaterial').addEventListener('click',()=>{state.materialId=pendingMaterial;renderMaterial();update();$('materialDialog').close();toast('素材已更新，各 Page 文案保持独立');});
  $('uploadLocal').addEventListener('click',()=>$('localVideoInput').click());$('localVideoInput').addEventListener('change',()=>importLocal($('localVideoInput').files[0]));
  $('newPublish').addEventListener('click',()=>{showView('compose');window.scrollTo({top:0,behavior:'smooth'});});$('recordSearch').addEventListener('input',renderRecords);
  $('submitPublish').addEventListener('click',confirmPublish);$('confirmPublish').addEventListener('click',executeSimulation);$('saveDraft').addEventListener('click',saveDraft);
  $('requirementsButton').addEventListener('click',()=>$('infoDialog').showModal());$('menuButton').addEventListener('click',()=>$('sidebar').classList.toggle('open'));
  document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();showView('compose');});
  document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
  document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}}));
  $('videoDialog').addEventListener('close',()=>$('videoContent').innerHTML='');$('confirmDialog').addEventListener('close',()=>confirmSnapshot=null);
  $('resetDemo').addEventListener('click',()=>{if(!window.confirm('清空本版演示发布记录和已保存草稿？Page 配置会保留。'))return;if(!writeStorage(':records',[])){toast('本地存储不可用，未重置记录');return;}timers.forEach(clearTimeout);timers.clear();records=[];try{localStorage.removeItem(STORAGE+':draft');}catch{}recordFilter='all';$('recordSearch').value='';renderRecords();toast('演示记录和已保存草稿已重置，Page 配置保留');});
  window.addEventListener('hashchange',()=>showView(location.hash.slice(1)));window.addEventListener('beforeunload',()=>localObjects.forEach(url=>URL.revokeObjectURL(url)));
})();
