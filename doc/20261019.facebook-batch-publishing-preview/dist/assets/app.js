(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const STORAGE = 'facebook-page-management-preview-v3-20261019';
  const dramaLanguages=['中文','英语','西班牙语'];
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
    {id:'template-01',title:'模板 01',subtitle:'待定义 · 暂不可用',icon:'grid',disabled:true,note:'构图变化模板占位示例。模板数量、名称与处理方式待确认；本页不处理视频。'},
    {id:'template-02',title:'模板 02',subtitle:'待定义 · 暂不可用',icon:'spark',disabled:true,note:'画面调整模板占位示例。模板数量、名称与处理方式待确认；本页不处理视频。'},
    {id:'template-03',title:'模板 03',subtitle:'待定义 · 暂不可用',icon:'copy',disabled:true,note:'组合处理模板占位示例。模板数量、名称与处理方式待确认；本页不处理视频。'}
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
  const BATCH_DRAFT=':batch-draft-v5';
  const state={materialIds:[],pageIds:[],pairKeys:[],pairDrafts:{},activePair:'',template:'none',mode:'now',schedule:'',intervalMinutes:0};
  const configuredPages=new Map();
  let currentView='compose',pendingMaterials=new Set(),recordFilter='all',records=[],toastTimer,confirmSnapshot=null,taskSequence=100;
  let editor={pageId:null,selected:new Set(),original:'',originalLanguage:''},deletingPageId=null;
  const timers=new Set(),localObjects=new Set(),previewCreatedMs=Date.now();
  const bjLocal=(time=Date.now())=>new Date(time+8*3600000).toISOString().slice(0,16);
  const bjText=time=>bjLocal(time).replace('T',' ');
  const parseBeijing=str=>Date.parse(str+':00+08:00');
  const tomorrowTen=()=>bjLocal(Date.now()+24*3600000).slice(0,10)+'T10:00';
  state.schedule=tomorrowTen();
  const getMaterial=id=>materials.find(m=>m.id===id)||materials[0];
  const getPage=id=>pages.find(p=>p.id===id);
  const getTemplate=(id=state.template)=>templates.find(t=>t.id===id)||templates[0];
  const selectedMaterials=()=>state.materialIds.map(id=>materials.find(m=>m.id===id)).filter(Boolean);
  const pairKey=(mid,pid)=>mid+'::'+pid;
  function pageBlockReason(p){const c=configuredPages.get(p.id);return !c?'尚未配置':!p.available?p.authReason:!c.dramaLanguage?'请先设置剧语言':selectedMaterials().length&&!selectedMaterials().some(m=>m.language===c.dramaLanguage)?`本批没有${c.dramaLanguage}素材`:'';}
  const eligiblePages=()=>pages.filter(p=>!pageBlockReason(p));
  const selectedPages=()=>eligiblePages().filter(p=>state.pageIds.includes(p.id));
  function pairFromKey(key){const [mid,pid]=String(key).split('::'),material=materials.find(m=>m.id===mid),page=getPage(pid),config=configuredPages.get(pid);return material&&page&&config?{key,material,page,config}:null;}
  function validPair(key){const pair=pairFromKey(key);return pair&&state.materialIds.includes(pair.material.id)&&state.pageIds.includes(pair.page.id)&&!pageBlockReason(pair.page)&&pair.material.language===pair.config.dramaLanguage;}
  const activePairs=()=>state.pairKeys.filter(validPair).map(pairFromKey);
  const currentPair=()=>activePairs().find(p=>p.key===state.activePair)||null;
  function reconcile(){
    state.materialIds=[...new Set(state.materialIds)].filter(id=>materials.some(m=>m.id===id));
    state.pageIds=selectedPages().map(p=>p.id);
    state.pairKeys=[...new Set(state.pairKeys)].filter(validPair);
    state.pairKeys.sort((a,b)=>state.materialIds.indexOf(pairFromKey(a).material.id)-state.materialIds.indexOf(pairFromKey(b).material.id)||state.pageIds.indexOf(pairFromKey(a).page.id)-state.pageIds.indexOf(pairFromKey(b).page.id));
    if(!state.pairKeys.includes(state.activePair))state.activePair=state.pairKeys[0]||'';
  }
  function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4200);}
  function readStorage(key){try{return JSON.parse(localStorage.getItem(STORAGE+key)||'null');}catch{return null;}}
  function writeStorage(key,value){try{localStorage.setItem(STORAGE+key,JSON.stringify(value));return true;}catch{return false;}}
  function draftForKey(key){const p=pairFromKey(key);if(!p)return null;let d=state.pairDrafts[key];if(!d||d.configId!==p.config.instanceId){d={captionTemplate:p.config.captionTemplate,version:p.config.version,configId:p.config.instanceId};state.pairDrafts[key]=d;}return d;}
  function previewLink(page,material){return page&&material?Links.buildLink({page,material,taskId:'preview-'+page.id+'-'+material.id,ledgerId:1,createdMs:previewCreatedMs}):null;}
  function restore(){
    const saved=readStorage(':page-configs');
    if(Array.isArray(saved))for(const c of saved)if(c&&getPage(c.pageId)&&typeof c.captionTemplate==='string'&&!Links.validateTemplate(c.captionTemplate))configuredPages.set(c.pageId,{...c,dramaLanguage:dramaLanguages.includes(c.dramaLanguage)?c.dramaLanguage:'',instanceId:c.instanceId||'legacy-'+c.pageId,version:Number.isSafeInteger(c.version)?c.version:1});
    const draft=readStorage(BATCH_DRAFT);
    if(draft&&typeof draft==='object'){
      state.materialIds=Array.isArray(draft.materialIds)?draft.materialIds:[];state.pageIds=Array.isArray(draft.pageIds)?draft.pageIds:[];
      state.pairKeys=Array.isArray(draft.pairKeys)?draft.pairKeys:[];state.activePair=typeof draft.activePair==='string'?draft.activePair:'';
      for(const [key,d] of Object.entries(draft.pairDrafts||{})){const p=pairFromKey(key);if(p&&d?.configId===p.config.instanceId&&typeof d.captionTemplate==='string'&&Links.bytes(d.captionTemplate)<=16000)state.pairDrafts[key]={...d};}
      state.pairKeys=state.pairKeys.filter(k=>state.pairDrafts[k]);
      if(['now','scheduled'].includes(draft.mode))state.mode=draft.mode;
      if(typeof draft.schedule==='string'&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(draft.schedule))state.schedule=draft.schedule;
      if(Number.isInteger(draft.intervalMinutes)&&draft.intervalMinutes>=0&&draft.intervalMinutes<=1440)state.intervalMinutes=draft.intervalMinutes;
    }
    const stored=readStorage(':records');records=Array.isArray(stored)?stored.filter(r=>r&&typeof r.id==='string'&&r.id.startsWith('DEMO-FB-')&&getPage(r.pageId)&&typeof r.caption==='string'&&Object.hasOwn(statusLabels,r.status)).slice(0,200):[];
    records=records.map(r=>r.status==='processing'?{...r,status:'cancelled',error:'演示页面已刷新，本地模拟处理已结束。'}:r);
    taskSequence=Math.max(100,...records.map(r=>Number(r.link?.ledgerId)||0));reconcile();
  }
  function persistRecords(){return writeStorage(':records',records.slice(0,200));}
  function materialMeta(record){return record.material||materials.find(m=>m.id===record.materialId)||materials[0];}
  function pageAvatar(p){return `<span class="page-avatar ${esc(p.color)}">${esc(p.mark)}<b class="avatar-f">f</b></span>`;}
  function badge(status){return `<span class="status-badge ${esc(status)}">${esc(statusLabels[status]||status)}</span>`;}
  function renderPages(){
    const query=$('pageSearch').value.trim().toLocaleLowerCase();
    const list=pages.filter(p=>configuredPages.has(p.id)&&[p.name,p.id,configuredPages.get(p.id).dramaLanguage].join(' ').toLocaleLowerCase().includes(query));
    $('pageGrid').innerHTML=list.length?list.map(p=>`<label class="page-option" title="${esc(pageBlockReason(p)||'加入目标 Page 范围')}">${pageAvatar(p)}<span class="page-info"><strong>${esc(p.name)}</strong><small>${esc(configuredPages.get(p.id).dramaLanguage||'剧语言待设置')} · ${esc(pageBlockReason(p)||'可参与本批匹配')}</small></span><input type="checkbox" value="${p.id}" aria-label="${esc(p.name)}" ${state.pageIds.includes(p.id)?'checked':''} ${pageBlockReason(p)?'disabled':''}></label>`).join(''):`<div class="empty-state">${configuredPages.size?'没有匹配的 Page。':'暂无已配置 Page，请先前往 Page 管理创建。'}</div>`;
    $('pageGrid').querySelectorAll('input:not(:disabled)').forEach(i=>i.addEventListener('change',()=>{state.pageIds=i.checked?[...state.pageIds,i.value]:state.pageIds.filter(id=>id!==i.value);update();}));
    $('selectAllPages').disabled=!eligiblePages().length;
  }
  function renderMaterial(){
    const list=selectedMaterials();
    $('selectedMaterial').innerHTML=list.length?list.map((m,index)=>`<div class="batch-material"><button class="batch-thumb" data-video="${m.id}" aria-label="预览 ${esc(m.name)}"><img src="${esc(m.poster)}" alt="${esc(m.series)}"><span>${esc(m.duration)}</span></button><div class="batch-material-meta"><strong>${esc(m.name)}</strong><small>${esc(m.id)} · ${esc(m.language)} · ${esc(m.size)}</small></div><div class="material-actions"><button class="icon-button" data-move="${m.id}" data-direction="-1" aria-label="上移 ${esc(m.name)}" ${index?'':'disabled'}>↑</button><button class="icon-button" data-move="${m.id}" data-direction="1" aria-label="下移 ${esc(m.name)}" ${index<list.length-1?'':'disabled'}>↓</button><button class="text-button danger-text" data-remove-material="${m.id}">移除</button></div></div>`).join(''):'<div class="empty-state">尚未选择素材，点击“选择素材”可一次勾选多条视频。</div>';
    $('materialCount').textContent=`已选 ${list.length} 条素材`;
    $('selectedMaterial').querySelectorAll('[data-video]').forEach(b=>b.addEventListener('click',()=>openVideo(b.dataset.video)));
    $('selectedMaterial').querySelectorAll('[data-remove-material]').forEach(b=>b.addEventListener('click',()=>removeMaterial(b.dataset.removeMaterial)));
    $('selectedMaterial').querySelectorAll('[data-move]').forEach(b=>b.addEventListener('click',()=>{const i=state.materialIds.indexOf(b.dataset.move),j=i+Number(b.dataset.direction);[state.materialIds[i],state.materialIds[j]]=[state.materialIds[j],state.materialIds[i]];update();}));
  }
  function removeMaterial(id){state.materialIds=state.materialIds.filter(x=>x!==id);for(const key of Object.keys(state.pairDrafts))if(key.startsWith(id+'::'))delete state.pairDrafts[key];update();}
  function renderPairings(){
    const pairs=activePairs(),unmatched=selectedMaterials().filter(m=>!pairs.some(p=>p.material.id===m.id));
    $('pairSummary').textContent=`${pairs.length} 个配对 · ${unmatched.length} 条素材未分配`;
    $('matchByLanguage').disabled=!selectedMaterials().length||!selectedPages().length;
    const oldFilter=$('pairPageFilter').value;$('pairPageFilter').innerHTML='<option value="all">全部 Page</option>'+selectedPages().map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');
    if([...$('pairPageFilter').options].some(o=>o.value===oldFilter))$('pairPageFilter').value=oldFilter;
    const language=$('pairLanguageFilter').value,pageFilter=$('pairPageFilter').value,onlyUnmatched=$('showUnmatched').checked;
    const visible=selectedMaterials().filter(m=>(language==='all'||m.language===language)&&(!onlyUnmatched||unmatched.includes(m))&&(pageFilter==='all'||pairs.some(p=>p.material.id===m.id&&p.page.id===pageFilter)));
    $('pairList').innerHTML=visible.length?visible.map(m=>{
      const matched=pairs.filter(p=>p.material.id===m.id),targets=selectedPages();
      return `<article class="pair-material" data-material-row="${m.id}"><div class="pair-material-heading"><div><strong>${esc(m.name)}</strong><small>${esc(m.id)} · 剧语言：${esc(m.language)}</small></div><span class="status-badge ${matched.length?'published':'failed'}">${matched.length?'已对应 '+matched.length+' 个 Page':'未分配'}</span></div><div class="pair-targets">${targets.length?targets.map(p=>{const key=pairKey(m.id,p.id),ok=m.language===configuredPages.get(p.id).dramaLanguage;return `<label class="pair-target ${ok?'':'pair-ineligible'}"><input type="checkbox" data-pair="${key}" aria-label="${esc(m.name+' → '+p.name)}" ${state.pairKeys.includes(key)?'checked':''} ${ok?'':'disabled'}><span>${esc(p.name)}<small>${esc(ok?configuredPages.get(p.id).dramaLanguage:'剧语言不匹配')}</small></span></label>`;}).join(''):'<p class="field-note">请先选择目标 Page。</p>'}</div><div class="pair-row-foot"><span>${matched.length?'可逐条取消或添加同语言 Page。':'此素材不会被静默跳过，请分配 Page 或移除素材。'}</span><button class="text-button" data-preview-material="${m.id}" ${matched.length?'':'disabled'}>编辑配对文案</button><button class="text-button danger-text" data-remove-material="${m.id}">移除素材</button></div></article>`;
    }).join(''):`<div class="empty-state">${selectedMaterials().length?'没有符合筛选条件的素材。':'选择素材和目标 Page 后，在这里建立对应关系。'}</div>`;
    $('pairList').querySelectorAll('[data-pair]:not(:disabled)').forEach(i=>i.addEventListener('change',()=>{if(i.checked){state.pairKeys.push(i.dataset.pair);draftForKey(i.dataset.pair);}else state.pairKeys=state.pairKeys.filter(k=>k!==i.dataset.pair);update();}));
    $('pairList').querySelectorAll('[data-remove-material]').forEach(b=>b.addEventListener('click',()=>removeMaterial(b.dataset.removeMaterial)));
    $('pairList').querySelectorAll('[data-preview-material]').forEach(b=>b.addEventListener('click',()=>{state.activePair=activePairs().find(p=>p.material.id===b.dataset.previewMaterial)?.key||'';update();$('captionSection').scrollIntoView({behavior:'smooth',block:'start'});}));
    $('pageTaskTotals').textContent=selectedPages().map(p=>`${p.name}：${pairs.filter(pair=>pair.page.id===p.id).length} 条`).join('；')||'尚未选择目标 Page';
  }
  function matchByLanguage(){
    if(state.pairKeys.length&&!window.confirm('重新匹配会恢复之前取消的同语言配对。继续？'))return;
    state.pairKeys=selectedMaterials().flatMap(m=>selectedPages().filter(p=>configuredPages.get(p.id).dramaLanguage===m.language).map(p=>pairKey(m.id,p.id)));
    state.pairKeys.forEach(draftForKey);update();toast(`已按剧语言匹配 ${state.pairKeys.length} 个配对，请核对后提交`);
  }
  function update(){
    reconcile();renderMaterial();renderPages();renderPairings();
    const pairs=activePairs(),pair=currentPair(),d=pair?draftForKey(pair.key):null,link=pair?previewLink(pair.page,pair.material):null;
    const result=d?Links.render(d.captionTemplate,pair.material,link):{text:'建立素材与 Page 的对应关系后，在这里预览每条文案。',error:'',bytes:0};
    $('pageCount').textContent=`已选 ${selectedPages().length} 个 Page`;
    $('selectedPagesSummary').textContent='选择目标范围后，点击下方“按剧语言匹配”建立配对。';
    $('selectAllPages').textContent=selectedPages().length&&selectedPages().length===eligiblePages().length?'取消全选':'全选可用';
    const options=pairs.length?pairs.map(p=>`<option value="${p.key}" ${state.activePair===p.key?'selected':''}>${esc(p.material.name+' → '+p.page.name)}</option>`).join(''):'<option value="">请先建立素材与 Page 配对</option>';
    $('captionPage').innerHTML=options;$('previewPage').innerHTML=options;
    $('caption').disabled=!d;$('caption').value=d?.captionTemplate||'';$('restorePageCaption').disabled=!d;$('sampleCaption').disabled=!d;
    document.querySelectorAll('[data-macro-target="caption"] button').forEach(b=>b.disabled=!d);
    $('captionSource').textContent=d?`模板版本 ${d.version} · 仅修改本配对${d.version!==pair.config.version?' · Page 模板已更新':''}`:'各素材与 Page 配对使用独立文案';
    $('captionCount').textContent=`${result.bytes} / 5,000 字节（替换后）`;$('captionMacroError').textContent=result.error;$('captionMacroError').hidden=!result.error;
    $('previewName').textContent=pair?.page.name||'请选择配对';$('previewAvatar').textContent=pair?.page.mark||'f';$('previewAvatar').className=`page-avatar ${pair?.page.color||''}`;
    $('previewCaption').textContent=result.text;$('previewVideoButton').hidden=!pair;
    if(pair){$('previewPoster').src=pair.material.poster;$('previewPoster').alt=pair.material.series+' · 示例画面';$('previewDuration').textContent=pair.material.duration;}
    $('publishLinkDetails').innerHTML=pair?linkDetails(link):'';
    $('templateNote').textContent=getTemplate().note;$('scheduleFields').hidden=state.mode!=='scheduled';$('scheduleAt').min=bjLocal(Date.now()+60000);
    $('summaryMaterial').textContent=`${selectedMaterials().length} 条素材`;$('summaryPages').textContent=`${new Set(pairs.map(p=>p.page.id)).size} 个 Page / ${pairs.length} 条任务`;$('summaryTemplate').textContent=getTemplate().title;
    $('summaryTime').textContent=(state.mode==='now'?'立即开始':state.schedule.replace('T',' ')+'（北京）')+` · 每 Page 间隔 ${state.intervalMinutes} 分钟`;
    $('publishSummary').textContent=`${selectedMaterials().length} 条素材 → ${pairs.length} 条任务`;$('publishSummaryDetail').textContent='按素材顺序逐 Page 排列 · 每配对独立文案和短链';
    $('submitLabel').textContent=state.mode==='now'?'模拟批量发布':'模拟批量预约';$('formError').hidden=true;
  }
  function showView(view){
    currentView=['compose','records','management'].includes(view)?view:'compose';const names={compose:'Facebook 批量发布',records:'发布记录',management:'Page 管理'},desc={compose:'多选素材与 Page，按剧语言建立对应关系，逐条确认发布。',records:'按素材与 Page 查看独立任务。',management:'设置每个 Page 的剧语言与默认文案。'};
    ['compose','records','management'].forEach(k=>{$(k+'View').hidden=k!==currentView;$(k+'Tab').classList.toggle('active',k===currentView);$(k+'Tab').setAttribute('aria-selected',String(k===currentView));});
    document.title=names[currentView]+' · AI自动后台 · 交互预览';$('pageTitle').textContent=names[currentView];$('breadcrumbTitle').textContent=names[currentView];$('pageDescription').textContent=desc[currentView];QuickNav.setActive('#quickNav',{compose:'fbPublish',records:'fbRecords',management:'fbPages'}[currentView]);
    if(currentView==='records')renderRecords();if(currentView==='management')renderManagement();$('sidebar').classList.remove('open');history.replaceState(null,'','#'+currentView);
  }
  function showError(message,target='pairingSection'){$('formError').textContent=message;$('formError').hidden=false;toast(message);$(target).scrollIntoView({behavior:'smooth',block:'center'});return false;}
  function validate(){
    if(!selectedMaterials().length)return showError('请至少选择 1 条素材。','materialSection');
    if(!selectedPages().length)return showError('请至少选择 1 个已配置、授权正常且剧语言匹配的 Page。','pagesSection');
    const pairs=activePairs(),unmatched=selectedMaterials().filter(m=>!pairs.some(p=>p.material.id===m.id));
    if(unmatched.length)return showError(`${unmatched.length} 条素材未分配 Page：${unmatched.map(m=>m.name).join('、')}。请建立对应关系或移除这些素材。`);
    if(!Number.isInteger(state.intervalMinutes)||state.intervalMinutes<0||state.intervalMinutes>1440)return showError('每个 Page 的发布间隔须为 0–1440 的整数分钟。','scheduleSection');
    if(state.mode==='scheduled'&&(!Number.isFinite(parseBeijing(state.schedule))||parseBeijing(state.schedule)<=Date.now()))return showError('请选择未来的预约时间（北京时间）。','scheduleSection');
    for(const p of pairs){const result=Links.render(draftForKey(p.key).captionTemplate,p.material,previewLink(p.page,p.material));if(result.error){state.activePair=p.key;update();return showError(p.material.name+' → '+p.page.name+'：'+result.error,'captionSection');}}
    return true;
  }
  function confirmPublish(){
    if(!validate())return;
    const createdMs=Date.now(),batch=crypto.randomUUID(),perPage=new Map(),start=state.mode==='scheduled'?parseBeijing(state.schedule):createdMs;
    const entries=activePairs().map((p,index)=>{
      const order=perPage.get(p.page.id)||0;perPage.set(p.page.id,order+1);
      const taskId=crypto.randomUUID().replaceAll('-',''),ledgerId=String(taskSequence+index+1),d=draftForKey(p.key),scheduled=state.mode==='scheduled'||order*state.intervalMinutes>0,plannedMs=start+order*state.intervalMinutes*60000;
      const link=Links.buildLink({page:p.page,material:p.material,taskId,ledgerId,createdMs});
      return {id:'DEMO-FB-'+taskId,batch,pairKey:p.key,materialId:p.material.id,material:{...p.material,url:undefined},pageId:p.page.id,pageDramaLanguage:p.config.dramaLanguage,configId:p.config.instanceId,template:state.template,status:scheduled?'scheduled':'processing',mode:scheduled?'scheduled':'now',time:scheduled?bjText(plannedMs):'立即发布',plannedAtUtc:new Date(plannedMs).toISOString(),pageOrder:order+1,intervalMinutes:state.intervalMinutes,created:bjText(createdMs),createdAtUtc:new Date(createdMs).toISOString(),caption:Links.render(d.captionTemplate,p.material,link).text,captionTemplate:d.captionTemplate,templateVersion:d.version,link,landingId:2310};
    });
    const invalid=entries.find(r=>Links.render(r.captionTemplate,r.material,r.link).error);
    if(invalid){state.activePair=invalid.pairKey;update();return showError(Links.render(invalid.captionTemplate,invalid.material,invalid.link).error,'captionSection');}
    const repeats=entries.filter(r=>records.some(old=>old.materialId===r.materialId&&old.pageId===r.pageId&&['published','processing','scheduled'].includes(old.status)));
    confirmSnapshot={entries,mode:state.mode,schedule:state.schedule,repeatCount:repeats.length};
    $('confirmTitle').textContent=`确认 ${entries.length} 条模拟任务`;$('confirmPublish').textContent='确认创建 '+entries.length+' 条任务';$('confirmPublish').disabled=Boolean(repeats.length);
    $('confirmContent').innerHTML=`<div class="confirm-notice">${selectedMaterials().length} 条素材 · ${perPage.size} 个 Page · ${entries.length} 个配对。每个配对使用独立文案、任务和模拟短链。</div>`+[...perPage.keys()].map(pid=>`<section class="confirm-page-group"><h3>${esc(getPage(pid).name)} · ${perPage.get(pid)} 条</h3>${entries.filter(r=>r.pageId===pid).map(r=>`<article class="confirm-page" data-confirm-pair="${r.pairKey}"><h4>${r.pageOrder}. ${esc(r.material.name)}</h4><p class="field-note">${esc(r.pageDramaLanguage)} · ${esc(r.time)} · 模板版本 ${r.templateVersion}</p><div class="confirm-caption">${esc(r.caption)}</div><p class="short-link-label">模拟短链 · 不可访问</p><code class="short-link">${esc(r.link.shortUrl)}</code>${linkDetails(r.link)}</article>`).join('')}</section>`).join('')+(repeats.length?`<div class="repeat-notice"><strong>发现 ${repeats.length} 个历史重复配对</strong><p>已有记录包含相同素材与 Page。继续将新增独立模拟任务，不会覆盖历史。</p><label><input type="checkbox" id="confirmRepeat"> 我确认本次仍需再次发布这些配对</label></div>`:'')+'<p class="confirm-notice">全部为本地模拟，不创建真实帖子或短链；预约任务不会自动执行。</p>';
    $('confirmRepeat')?.addEventListener('change',()=>$('confirmPublish').disabled=!$('confirmRepeat').checked);$('confirmDialog').showModal();
  }
  function executeSimulation(){
    if(!confirmSnapshot||confirmSnapshot.repeatCount&&!$('confirmRepeat')?.checked)return;
    if(confirmSnapshot.mode==='scheduled'&&parseBeijing(confirmSnapshot.schedule)<=Date.now()){$('confirmDialog').close();validate();return;}
    const frozen=confirmSnapshot,created=frozen.entries;$('confirmPublish').disabled=true;
    if(!writeStorage(':records',[...created,...records].slice(0,200))){$('confirmPublish').disabled=false;toast('本地存储不可用，任务尚未创建；请重试。');return;}
    confirmSnapshot=null;taskSequence+=created.length;records.unshift(...created);$('confirmDialog').close();showView('records');window.scrollTo({top:0,behavior:'smooth'});toast(`已创建 ${created.length} 条独立演示任务`);
    created.filter(r=>r.status==='processing').forEach((record,index)=>{const timer=setTimeout(()=>{timers.delete(timer);const r=records.find(r=>r.id===record.id);if(!r||r.status!=='processing')return;r.status='published';r.time=bjText(Date.now());persistRecords();renderRecords();if($('detailDialog').open&&$('detailDialog').dataset.recordId===r.id)renderDetail(r.id);},1200+index*200);timers.add(timer);});
  }
  function saveDraft(){const materialIds=state.materialIds.filter(id=>!getMaterial(id).local),pairKeys=state.pairKeys.filter(k=>materialIds.includes(pairFromKey(k)?.material.id)),pairDrafts=Object.fromEntries(Object.entries(state.pairDrafts).filter(([k])=>materialIds.includes(pairFromKey(k)?.material.id)));toast(writeStorage(BATCH_DRAFT,{...state,materialIds,pairKeys,pairDrafts})?'批量草稿已保存；本地导入视频需重新选择':'浏览器未允许保存，当前输入仍保留。');}
  function reuseRecord(id){const r=records.find(r=>r.id===id);if(!r||!configuredPages.has(r.pageId)||!getPage(r.pageId).available)return toast('此 Page 尚未配置或授权异常。');state.materialIds=[r.materialId];state.pageIds=[r.pageId];state.pairKeys=[pairKey(r.materialId,r.pageId)];state.activePair=state.pairKeys[0];state.pairDrafts[state.activePair]={captionTemplate:r.captionTemplate,version:r.templateVersion,configId:configuredPages.get(r.pageId).instanceId};state.mode='now';syncForm();showView('compose');}
  function syncForm(){$('scheduleAt').value=state.schedule;$('intervalMinutes').value=state.intervalMinutes;document.querySelectorAll('[name=publishMode]').forEach(i=>i.checked=i.value===state.mode);renderTemplates();update();}
  function renderTemplates(){$('templateGrid').innerHTML=templates.map(t=>`<label class="template-option ${t.disabled?'template-unavailable':''}"><input type="radio" name="template" value="${t.id}" ${state.template===t.id?'checked':''} ${t.disabled?'disabled':''}><span class="template-icon">${icon(t.icon)}</span><strong>${esc(t.title)}</strong><small>${esc(t.subtitle)}</small></label>`).join('');}
  function openMaterialPicker(){pendingMaterials=new Set(state.materialIds);$('materialSearch').value='';$('materialLanguage').value='all';renderLibrary();$('materialDialog').showModal();}
  function filteredMaterials(){const query=$('materialSearch').value.trim().toLocaleLowerCase(),language=$('materialLanguage').value;return materials.filter(m=>(language==='all'||m.language===language)&&[m.id,m.name,m.series].join(' ').toLocaleLowerCase().includes(query));}
  function renderLibrary(){
    const list=filteredMaterials();$('materialLibrary').innerHTML=list.length?list.map(m=>`<label class="library-item"><input type="checkbox" name="material" aria-label="${esc(m.name)}" value="${m.id}" ${pendingMaterials.has(m.id)?'checked':''}><img src="${esc(m.poster)}" alt="${esc(m.series)}"><div class="library-body"><strong>${esc(m.name)}</strong><p>${esc(m.id)} · ${esc(m.duration)} · ${esc(m.size)}</p><div class="material-tags"><span class="tag blue">${esc(m.language)}</span><span class="tag">${esc(m.format)}</span></div></div></label>`).join(''):'<div class="empty-state">没有匹配的素材。</div>';
    $('materialLibrary').querySelectorAll('input').forEach(i=>i.addEventListener('change',()=>{i.checked?pendingMaterials.add(i.value):pendingMaterials.delete(i.value);$('materialSelectionNote').textContent=`已选 ${pendingMaterials.size} 条素材（跨搜索与筛选保留）`;}));
    $('materialSelectionNote').textContent=`已选 ${pendingMaterials.size} 条素材（跨搜索与筛选保留）`;
  }
  async function importLocal(file){
    if(!file||file.type&&!file.type.startsWith('video/'))return toast('请选择视频文件');
    const url=URL.createObjectURL(file);localObjects.add(url);const video=document.createElement('video');video.preload='metadata';video.src=url;$('uploadLocal').disabled=true;
    try{await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('timeout')),10000);video.onloadedmetadata=()=>{clearTimeout(timer);resolve();};video.onerror=()=>{clearTimeout(timer);reject(new Error('video'));};});if(!Number.isFinite(video.duration)||video.duration<=0||!video.videoWidth)throw new Error('invalid');
      const total=Math.round(video.duration),id='LOCAL-'+crypto.randomUUID();materials.unshift({id,name:file.name,language:'本地视频',duration:`${String(Math.floor(total/60)).padStart(2,'0')}:${String(total%60).padStart(2,'0')}`,resolution:`${video.videoWidth} × ${video.videoHeight}`,size:(file.size/1024/1024).toFixed(1)+' MB',poster:'assets/poster-local.svg',series:file.name,format:'本地预览',local:true,url});pendingMaterials.add(id);$('materialSearch').value='';$('materialLanguage').value='all';renderLibrary();toast('视频已载入；未关联剧语言，不能参与自动匹配。');
    }catch{URL.revokeObjectURL(url);localObjects.delete(url);toast('浏览器无法读取该视频。');}finally{video.removeAttribute('src');video.load();$('uploadLocal').disabled=false;}
  }
  function openVideo(id){const m=getMaterial(id||currentPair()?.material.id);$('videoTitle').textContent=m.name;$('videoSubtitle').textContent=`${m.duration} · ${m.resolution} · ${m.local?'本地预览':'示例画面'}`;$('videoContent').innerHTML=m.local?`<video src="${esc(m.url)}" controls playsinline autoplay></video>`:`<div class="sample-video-poster"><img src="${esc(m.poster)}" alt="${esc(m.name)}"><div class="sample-video-note">素材画面示意<small>可导入本地视频预览，文件不会上传。</small></div></div>`;$('videoDialog').showModal();}
  function linkDetails(link){
    if(!link)return '<p class="field-note">此素材没有剧集关联，无法预览短链。</p>';
    return `<details class="link-details"><summary>查看长链参数 · 2310</summary><div class="link-details-body"><p>模拟归因；sub_user_id 900001 为示例发布人映射，不查询真实账号。</p><code class="long-url">${esc(link.longUrl)}</code><dl>${link.params.map(([key,value])=>`<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl></div></details>`;
  }
  function renderManagement(){
    const query=$('managementSearch').value.trim().toLocaleLowerCase(),filter=$('managementFilter').value;
    const list=pages.filter(p=>configuredPages.has(p.id)&&[p.name,p.id,configuredPages.get(p.id)?.dramaLanguage||''].join(' ').toLocaleLowerCase().includes(query)&&(filter==='all'||p.available===(filter==='available')));
    $('managementTotal').textContent=`${configuredPages.size} 个已配置 Page`;
    $('managementCount').textContent=`显示 ${list.length} / ${configuredPages.size} 个 Page`;
    $('managementTableWrap').hidden=!configuredPages.size;$('managementEmpty').hidden=Boolean(configuredPages.size);
    $('managementBody').innerHTML=list.length?list.map(p=>{const c=configuredPages.get(p.id);return `<tr><td><strong class="group-name">${esc(p.name)}</strong><small>${esc(p.id)}</small></td><td>${esc(c.dramaLanguage||'待设置')}<small>匹配素材的剧语言</small></td><td><span class="status-badge ${p.available?'published':'failed'}">${p.available?'授权正常':'授权异常'}</span><small>${p.available?'授权有效':esc(p.authReason)}</small></td><td><p class="group-caption-excerpt">${esc(c.captionTemplate)}</p><small>版本 ${c.version}</small></td><td>${esc(c.updatedAt)}<small>北京时间 · 本地演示</small></td><td><div class="page-row-actions"><button class="text-button" data-edit-page="${p.id}">编辑</button><button class="text-button danger-text" data-delete-page="${p.id}">删除</button></div></td></tr>`;}).join(''):'<tr><td colspan="6"><div class="empty-state">没有匹配的 Page，请调整搜索或筛选。</div></td></tr>';
    $('managementBody').querySelectorAll('[data-edit-page]').forEach(b=>b.addEventListener('click',()=>openPageEditor(b.dataset.editPage)));
    $('managementBody').querySelectorAll('[data-delete-page]').forEach(b=>b.addEventListener('click',()=>openDeletePage(b.dataset.deletePage)));
  }
  function openDeletePage(id){
    if(!configuredPages.has(id))return;
    deletingPageId=id;$('deletePageName').textContent=getPage(id).name;$('deletePageId').textContent=id;
    $('deletePageError').hidden=true;$('deletePageDialog').showModal();
  }
  function deletePageConfig(){
    const id=deletingPageId;if(!configuredPages.has(id))return;
    const next=[...configuredPages.values()].filter(c=>c.pageId!==id);
    if(!writeStorage(':page-configs',next)){$('deletePageError').textContent='本地存储不可用，尚未删除。请重试。';$('deletePageError').hidden=false;return;}
    configuredPages.delete(id);state.pageIds=state.pageIds.filter(p=>p!==id);for(const key of Object.keys(state.pairDrafts))if(key.endsWith('::'+id))delete state.pairDrafts[key];
    const saved=readStorage(BATCH_DRAFT);
    if(saved){saved.pageIds=(saved.pageIds||[]).filter(p=>p!==id);saved.pairKeys=(saved.pairKeys||[]).filter(k=>!k.endsWith('::'+id));for(const key of Object.keys(saved.pairDrafts||{}))if(key.endsWith('::'+id))delete saved.pairDrafts[key];writeStorage(BATCH_DRAFT,saved);}
    $('deletePageDialog').close();renderManagement();renderPages();update();toast('Page 配置已删除，历史发布记录保留');
  }
  function openPageEditor(id=null){
    const config=id?configuredPages.get(id):null;
    editor={pageId:id,selected:new Set(id?[id]:[]),original:config?.captionTemplate||'',originalLanguage:config?.dramaLanguage||''};
    $('pageDramaLanguage').value=editor.originalLanguage;
    $('pageDialogTitle').textContent=id?'编辑 Page 配置':'创建 Page 配置';
    $('pageEditorSubtitle').textContent=id?`${getPage(id).name} · ${id}`:'可一次选择多个 Page，保存后分别建立独立配置。';
    $('pageCaption').value=editor.original;$('pagePickerSearch').value='';$('pageSaveError').hidden=true;
    $('candidateControls').hidden=Boolean(id);$('savePageConfig').textContent=id?'保存修改':'创建配置';
    $('editorPreviewMaterial').value=(materials.find(m=>!m.local&&m.language===(config?.dramaLanguage||'中文'))||materials[0]).id;
    renderCandidates();updateEditorPreview();$('pageDialog').showModal();
  }
  function renderCandidates(){
    const query=$('pagePickerSearch').value.trim().toLocaleLowerCase();
    const list=pages.filter(p=>(editor.pageId?p.id===editor.pageId:true)&&[p.name,p.id,configuredPages.get(p.id)?.dramaLanguage||''].join(' ').toLocaleLowerCase().includes(query));
    $('pageCandidates').innerHTML=list.length?list.map(p=>{const existing=!editor.pageId&&configuredPages.has(p.id);return `<label class="candidate-page ${existing?'configured':''}"><input type="checkbox" value="${p.id}" aria-label="配置 ${esc(p.name)}" ${editor.selected.has(p.id)?'checked':''} ${existing||editor.pageId?'disabled':''}><span><strong>${esc(p.name)}</strong><small>${esc(p.id)}</small></span><span class="status-badge ${existing?'cancelled':p.available?'published':'failed'}">${existing?'已配置':p.available?'授权正常':'授权异常'}</span></label>`;}).join(''):'<div class="empty-state">没有匹配的 Page。</div>';
    $('pageCandidates').querySelectorAll('input:not(:disabled)').forEach(input=>input.addEventListener('change',()=>{input.checked?editor.selected.add(input.value):editor.selected.delete(input.value);updateEditorPreview();}));
    $('candidateCount').textContent=`已选 ${editor.selected.size} 个 Page`;
  }
  function editorDirty(){return $('pageCaption').value!==editor.original||$('pageDramaLanguage').value!==editor.originalLanguage||(!editor.pageId&&editor.selected.size>0);}
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
    const language=$('pageDramaLanguage').value;
    const error=!editor.selected.size?'请至少选择 1 个 Page。':!dramaLanguages.includes(language)?'请选择剧语言。':syntaxError;
    if(error){$('pageSaveError').textContent=error;$('pageSaveError').hidden=false;return;}
    if(!editor.pageId&&[...editor.selected].some(id=>configuredPages.has(id))){$('pageSaveError').textContent='部分 Page 已配置，请重新选择。';$('pageSaveError').hidden=false;renderCandidates();return;}
    const next=new Map(configuredPages),stamp=bjText(Date.now());
    for(const id of editor.selected)next.set(id,{pageId:id,captionTemplate:value,dramaLanguage:language,instanceId:next.get(id)?.instanceId||crypto.randomUUID(),version:(next.get(id)?.version||0)+1,updatedAt:stamp});
    if(!writeStorage(':page-configs',[...next.values()])){$('pageSaveError').textContent='浏览器未允许本地存储，配置尚未保存。当前输入保留，请重试。';$('pageSaveError').hidden=false;return;}
    configuredPages.clear();for(const [id,c] of next)configuredPages.set(id,c);
    // Existing publication drafts are snapshots; saving a template must not overwrite them.
    $('pageDialog').close();renderManagement();renderPages();update();toast(editor.pageId?'Page 文案已保存；当前发布草稿保持原样':`已创建 ${editor.selected.size} 个 Page 配置，可前往视频发布选择`);
  }
  function renderRecords() {
    $('recordTabCount').textContent=String(records.length);
    const count=status=>records.filter(record=>record.status===status).length;
    $('statsGrid').innerHTML=[['全部任务',records.length,'逐素材与 Page 独立任务'],['待发布 / 处理中',count('scheduled')+count('processing'),'模拟预约与处理流程'],['已完成',count('published'),'仅演示发布结果'],['需要关注',count('failed'),'查看错误说明']].map(([label,value,hint])=>`<div class="stat-card"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>`).join('');
    const filters=[['all','全部'],['scheduled','已预约'],['processing','处理中'],['published','已发布'],['failed','失败'],['cancelled','已取消']];
    $('recordFilters').innerHTML=filters.map(([id,label])=>`<button class="record-filter ${recordFilter===id?'active':''}" data-filter="${id}" aria-pressed="${recordFilter===id}">${label}</button>`).join('');
    $('recordFilters').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{recordFilter=button.dataset.filter;renderRecords();}));
    const query=$('recordSearch').value.trim().toLocaleLowerCase();
    const list=records.filter(record=>(recordFilter==='all'||record.status===recordFilter)&&[record.id,materialMeta(record).name,getPage(record.pageId).name].join(' ').toLocaleLowerCase().includes(query));
    $('recordsBody').innerHTML=list.length?list.map(record=>{const material=materialMeta(record),page=getPage(record.pageId);return `<tr><td><div class="table-material"><img src="${esc(material.poster)}" alt=""><div><strong>${esc(material.name)}</strong><small>${esc(record.id)}</small></div></div></td><td class="table-page">${esc(page.name)}<div class="subtle">${esc(page.owner)}</div></td><td>${esc(getTemplate(record.template).title)}</td><td>${badge(record.status)}</td><td class="table-time">${esc(record.time)}<small>${record.mode==='scheduled'?'预约时间 · UTC+8':'立即发布 · 演示'}</small></td><td><div class="row-actions"><button class="text-button" data-detail="${esc(record.id)}">详情</button>${record.status==='scheduled'?`<button class="text-button" data-cancel="${esc(record.id)}">取消预约</button>`:''}${record.status==='failed'?`<button class="text-button" data-reuse="${esc(record.id)}">重新编辑</button>`:''}</div></td></tr>`;}).join(''):'<tr><td colspan="6"><div class="empty-state">没有匹配的发布任务，请调整筛选条件。</div></td></tr>';
    $('recordsBody').querySelectorAll('[data-detail]').forEach(button=>button.addEventListener('click',()=>openDetail(button.dataset.detail)));
    $('recordsBody').querySelectorAll('[data-cancel]').forEach(button=>button.addEventListener('click',()=>{const record=records.find(item=>item.id===button.dataset.cancel);const next=records.map(r=>r.id===record.id?{...r,status:'cancelled'}:r);if(!writeStorage(':records',next.slice(0,200)))return toast('本地存储不可用，尚未取消');records=next;renderRecords();toast('已取消这条独立演示预约');}));
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
    $('detailContent').innerHTML=`<dl class="confirm-summary"><div><dt>任务 ID</dt><dd>${esc(record.id)}</dd></div><div><dt>当前状态</dt><dd>${badge(record.status)}</dd></div><div><dt>视频素材</dt><dd>${esc(material.name)}</dd></div><div><dt>发布主页</dt><dd>${esc(page.name)}</dd></div><div><dt>Page 剧语言</dt><dd>${esc(record.pageDramaLanguage||'旧版记录，未设置')}</dd></div><div><dt>Page 内顺序</dt><dd>${record.pageOrder||1} · 间隔 ${record.intervalMinutes||0} 分钟</dd></div><div><dt>去重模板</dt><dd>${esc(getTemplate(record.template).title)}</dd></div><div><dt>创建时间</dt><dd>${esc(record.created)}（北京）</dd></div><div><dt>发布时间</dt><dd>${esc(record.time)}${record.mode==='scheduled'?'（北京）':''}</dd></div></dl><ol class="timeline">${timeline}</ol><h3>发布文案 · 模板版本 ${record.templateVersion}</h3><div class="confirm-caption">${esc(record.caption)}</div><p class="short-link-label">模拟短链 · 不可访问</p><code class="short-link">${esc(record.link?.shortUrl||"无剧集关联")}</code>${linkDetails(record.link)}`;
  }
  function openDetail(id) {renderDetail(id);$('detailDialog').showModal();}
  restore();
  UiTopbar.render({userCard:'#userCard',refreshButton:'#refreshPage',auth:{authenticated:true,user:{name:'演示运营员',role:'演示账号',tenant_key:'preview'}}});
  $('userCard').querySelector('.user-meta span').textContent='DramaWave 工作空间';
  const initialView=location.hash.slice(1)||document.body.dataset.initialView||'compose';
  QuickNav.render({container:'#quickNav',activeKey:{compose:'fbPublish',records:'fbRecords',management:'fbPages'}[initialView]||'fbPublish',auth:{authenticated:true,user:{role:'admin'}},onNavigate(item){if(item.key==='fbPublish')showView('compose');else if(item.key==='fbRecords')showView('records');else if(item.key==='fbPages')showView('management');else if(item.key==='materials')openMaterialPicker();else if(item.key==='about')$('infoDialog').showModal();else toast('本原型仅包含 Facebook 批量发布与 Page 管理');}});
  $('pageDramaLanguage').innerHTML='<option value="">请选择剧语言</option>'+dramaLanguages.map(l=>`<option value="${l}">${l}</option>`).join('');
  $('editorPreviewMaterial').innerHTML=materials.filter(m=>!m.local).map(m=>`<option value="${m.id}">${esc(m.series)} · ${esc(m.language)}</option>`).join('');
  document.querySelectorAll('[data-macro-target]').forEach(bar=>{bar.innerHTML='<span>插入宏参数</span>'+[['name','剧名'],['desc','简介'],['url','短链']].map(([key,label])=>`<button type="button" class="macro-chip" data-token="{${key}}">{${key}} ${label}</button>`).join('');bar.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{const input=$(bar.dataset.macroTarget);if(input.disabled)return;input.setRangeText(b.dataset.token,input.selectionStart,input.selectionEnd,'end');input.focus();input.dispatchEvent(new Event('input',{bubbles:true}));}));});
  syncForm();renderRecords();renderManagement();showView(initialView);
  for(const view of ['compose','records','management'])$(view+'Tab').addEventListener('click',()=>showView(view));
  $('managePages').addEventListener('click',()=>showView('management'));
  $('createPage').addEventListener('click',()=>openPageEditor());$('createFirstPage').addEventListener('click',()=>openPageEditor());
  $('managementSearch').addEventListener('input',renderManagement);$('managementFilter').addEventListener('change',renderManagement);
  $('confirmDeletePage').addEventListener('click',deletePageConfig);$('deletePageDialog').addEventListener('close',()=>deletingPageId=null);
  $('pageDramaLanguage').addEventListener('change',()=>{const m=materials.find(m=>!m.local&&m.language===$('pageDramaLanguage').value);if(m)$('editorPreviewMaterial').value=m.id;updateEditorPreview();});
  $('pagePickerSearch').addEventListener('input',renderCandidates);
  $('selectCandidatePages').addEventListener('click',()=>{const matches=[...$('pageCandidates').querySelectorAll('input:not(:disabled)')],all=matches.length&&matches.every(i=>editor.selected.has(i.value));matches.forEach(i=>all?editor.selected.delete(i.value):editor.selected.add(i.value));renderCandidates();updateEditorPreview();});
  $('pageCaption').addEventListener('input',updateEditorPreview);$('editorPreviewMaterial').addEventListener('change',updateEditorPreview);$('editorSampleCaption').addEventListener('click',()=>{$('pageCaption').value=captionSample;updateEditorPreview();});
  $('savePageConfig').addEventListener('click',savePageConfig);$('closePageEditor').addEventListener('click',closePageEditor);$('cancelPageEdit').addEventListener('click',closePageEditor);
  $('pageDialog').addEventListener('cancel',e=>{e.preventDefault();closePageEditor();});
  $('pageDialog').addEventListener('click',e=>{if(e.target===$('pageDialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom){e.stopImmediatePropagation();closePageEditor();}}});
  $('openMaterials').addEventListener('click',openMaterialPicker);$('pageSearch').addEventListener('input',renderPages);
  $('selectAllPages').addEventListener('click',()=>{const available=eligiblePages();state.pageIds=selectedPages().length===available.length?[]:available.map(p=>p.id);update();});
  $('matchByLanguage').addEventListener('click',matchByLanguage);
  for(const id of ['pairLanguageFilter','pairPageFilter','showUnmatched'])$(id).addEventListener('change',renderPairings);
  $('caption').addEventListener('input',()=>{const d=draftForKey(state.activePair);if(d){d.captionTemplate=$('caption').value;update();}});
  for(const id of ['captionPage','previewPage'])$(id).addEventListener('change',()=>{state.activePair=$(id).value;update();});
  $('restorePageCaption').addEventListener('click',()=>{const p=currentPair(),d=p?draftForKey(p.key):null;if(!d)return;if(d.captionTemplate!==p.config.captionTemplate&&!window.confirm('用此 Page 的最新模板替换当前配对文案？'))return;delete state.pairDrafts[p.key];draftForKey(p.key);update();});
  $('sampleCaption').addEventListener('click',()=>{const d=draftForKey(state.activePair);if(d){d.captionTemplate=captionSample;update();}});
  document.querySelectorAll('[name=publishMode]').forEach(i=>i.addEventListener('change',()=>{state.mode=i.value;update();}));
  $('scheduleAt').addEventListener('input',()=>{state.schedule=$('scheduleAt').value;update();});$('intervalMinutes').addEventListener('input',()=>{state.intervalMinutes=$('intervalMinutes').value===''?NaN:Number($('intervalMinutes').value);update();});
  $('tomorrowButton').addEventListener('click',()=>{state.schedule=tomorrowTen();$('scheduleAt').value=state.schedule;update();});
  $('previewVideoButton').addEventListener('click',()=>openVideo(currentPair()?.material.id));$('materialSearch').addEventListener('input',renderLibrary);$('materialLanguage').addEventListener('change',renderLibrary);
  $('selectFilteredMaterials').addEventListener('click',()=>{const list=filteredMaterials(),all=list.length&&list.every(m=>pendingMaterials.has(m.id));list.forEach(m=>all?pendingMaterials.delete(m.id):pendingMaterials.add(m.id));renderLibrary();});
  $('clearMaterialSelection').addEventListener('click',()=>{pendingMaterials.clear();renderLibrary();});
  $('confirmMaterial').addEventListener('click',()=>{const previous=new Set(state.materialIds);state.materialIds=[...pendingMaterials];for(const key of Object.keys(state.pairDrafts))if(!state.materialIds.includes(pairFromKey(key)?.material.id))delete state.pairDrafts[key];update();$('materialDialog').close();toast('已选择 '+state.materialIds.length+' 条素材，请核对并匹配目标 Page');});
  $('uploadLocal').addEventListener('click',()=>$('localVideoInput').click());$('localVideoInput').addEventListener('change',async()=>{const files=[...$('localVideoInput').files];for(const file of files)await importLocal(file);$('localVideoInput').value='';});
  $('newPublish').addEventListener('click',()=>{showView('compose');window.scrollTo({top:0,behavior:'smooth'});});$('recordSearch').addEventListener('input',renderRecords);
  $('submitPublish').addEventListener('click',confirmPublish);$('confirmPublish').addEventListener('click',executeSimulation);$('saveDraft').addEventListener('click',saveDraft);
  $('requirementsButton').addEventListener('click',()=>$('infoDialog').showModal());$('menuButton').addEventListener('click',()=>$('sidebar').classList.toggle('open'));
  document.querySelector('.brand').addEventListener('click',e=>{e.preventDefault();showView('compose');});
  document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
  document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}}));
  $('videoDialog').addEventListener('close',()=>$('videoContent').innerHTML='');$('confirmDialog').addEventListener('close',()=>confirmSnapshot=null);
  $('resetDemo').addEventListener('click',()=>{if(!window.confirm('清空演示发布记录和批量草稿？Page 配置保留。'))return;if(!writeStorage(':records',[]))return toast('本地存储不可用，未重置');timers.forEach(clearTimeout);timers.clear();records=[];try{localStorage.removeItem(STORAGE+BATCH_DRAFT);}catch{}recordFilter='all';$('recordSearch').value='';renderRecords();toast('记录和已保存批量草稿已清空，Page 配置保留');});
  window.addEventListener('hashchange',()=>showView(location.hash.slice(1)));window.addEventListener('beforeunload',()=>localObjects.forEach(url=>URL.revokeObjectURL(url)));
})();
