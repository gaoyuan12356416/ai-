(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const API = '/api/socialkit-analytics';
  const labels = {date:'日期',team_id:'团队',product:'产品',platform:'平台',account:'账号',drama:'剧目',country:'收入国家',device_os:'设备系统',material_type:'素材类型',creation_source:'短链来源',language:'账号语言',account_country:'账号国家',reason:'发布方式',task_status:'任务状态'};
  const moneyKeys = new Set(['revenue','refunds','net_revenue','dnu_revenue']);
  const percentKeys = new Set(['success_rate','install_rate','pay_rate']);
  const publicationMetrics = ['tasks','planned','success','failed','unfulfilled','success_rate'];
  const effectMetrics = ['revenue','net_revenue','dnu_revenue','refunds','clicks','views','installs','conversions','dnu_conversions','link_count','install_rate','pay_rate'];
  const metricLabels = {revenue:'收入 (USD)',refunds:'退款 (USD)',net_revenue:'收入减退款 (USD)',dnu_revenue:'新用户收入 (USD)',clicks:'落地页点击',views:'落地页访问',installs:'安装',conversions:'充值人数汇总',dnu_conversions:'新用户充值人数汇总',tasks:'任务数',planned:'计划内容',success:'成功发布',failed:'失败字段合计',unfulfilled:'未计入成功',success_rate:'计划完成率',install_rate:'安装／点击',pay_rate:'充值／安装',link_count:'有指标短链数'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const format = (value,key) => !finite(value) ? '—' : (moneyKeys.has(key) ? '$' : '') + Number(value).toLocaleString('en-US',{minimumFractionDigits:moneyKeys.has(key)?2:0,maximumFractionDigits:moneyKeys.has(key)||percentKeys.has(key)?2:0}) + (percentKeys.has(key)?'%':'');
  const stamp = value => { const date = new Date(value); return value && Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) + ' 北京时间' : '未提供'; };
  const utc = date => date.toISOString().slice(0,10);
  const defaultMetric = () => state.view === 'publications' ? 'success' : 'revenue';
  const visibleMetrics = () => state.view === 'effects' ? effectMetrics : state.view === 'publications' ? publicationMetrics : ['revenue','success','clicks','installs','conversions','refunds','dnu_revenue','planned','tasks','views'];
  const state = {view:'overview',options:null,report:null,filters:{},groups:['platform'],applied:null,dirty:false,busy:false,allowed:false,auth:null,requestId:0,controller:null,exportController:null,exporting:false};
  function message(text,kind='') { const node=$('#page-message'); node.textContent=text; node.className='analytics-message'+(kind?' '+kind:''); node.hidden=!text; }
  function controls() {
    const usable=state.allowed&&!state.busy;
    $('#filter-fields').disabled=!state.allowed;
    $('#apply-filters').disabled=!usable; $('#refreshPage').disabled=state.busy;
    $('#export-report').disabled=!usable||state.dirty||!state.report||state.exporting;
    $('#previous-page').disabled=!usable||state.dirty||!state.report||state.report.pagination.page<=1;
    $('#next-page').disabled=!usable||state.dirty||!state.report||state.report.pagination.page>=state.report.pagination.pages;
    $('#ranking-metric').disabled=!usable||state.dirty; $('#page-size').disabled=!usable||state.dirty;
    $('#report-content').setAttribute('aria-busy',String(state.busy));
    $('#report-content').classList.toggle('is-dirty',state.dirty);
    $('#filter-hint').textContent=state.dirty?'筛选有修改，请应用后查看对应结果。':'每小时整点更新当天及近期数据；收入按 UTC 日统计，北京时间 08:00 切日。';
    document.querySelectorAll('[data-view]').forEach(b=>b.disabled=!state.allowed||state.busy);
  }
  function dirty() { state.dirty=true; state.requestId++; state.controller?.abort(); state.exportController?.abort(); state.busy=false; controls(); }
  function denied(error) {
    state.allowed=false; state.report=null; state.options=null; state.applied=null; state.filters={}; state.requestId++;
    state.controller?.abort(); state.exportController?.abort(); state.busy=false; $('#report-content').classList.add('hidden'); $('#dimension-filters').replaceChildren(); $('#group-choices').replaceChildren();
    for(const id of ['kpi-grid','report-head','report-body','trend-chart','platform-chart','product-chart','ranking-chart','conversion-chart']) $('#'+id).replaceChildren();
    $('#scope-label').textContent='报表权限不可用';
    if(error.status===401) { state.auth={...state.auth,authenticated:false}; UiTopbar.render({auth:state.auth,userCard:'#userCard',authButton:'#authButton'}); }
    message(error.message||'暂无 SocialKit 报表权限。','error'); controls();
  }
  async function request(url,{signal,raw=false}={}) {
    const response=await fetch(url,{credentials:'same-origin',cache:'no-store',signal,headers:{Accept:raw?'text/csv':'application/json'}});
    if(!response.ok) { let data={};try{data=await response.json();}catch(_){} const error=new Error(data.message||(response.status===401?'请使用飞书登录后查看报表。':response.status===403?'暂无 SocialKit 报表权限，请联系管理员。':'数据读取失败，请稍后刷新。'));error.status=response.status;throw error; }
    if(raw) {if(!/text\/csv/i.test(response.headers.get('Content-Type')||''))throw new Error('导出格式异常。');return response.blob();}
    return response.json();
  }
  function allowedDimensions() {return state.options?.allowed_dimensions?.[state.view]||['date','team_id','product','platform','account','drama'];}
  function filterSummary(key) {const selected=state.filters[key]||[]; return selected.length?'已选 '+selected.length+' 项':'全部';}
  function renderFilterList(key,query='') {
    const container=document.querySelector('[data-options="'+key+'"]');if(!container)return;
    const selected=new Set(state.filters[key]||[]),all=state.options.options[key]||[];
    const matching=all.filter(item=>!query||(item.label+' '+(item.detail||item.value)).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    const shown=matching.slice(0,150);
    container.innerHTML=shown.map(item=>`<label class="filter-option"><input type="checkbox" data-filter="${key}" value="${esc(item.value)}" ${selected.has(item.value)?'checked':''}><span>${esc(item.label)}${item.detail?`<small class="filter-id"> · ${esc(item.detail)}</small>`:''}</span></label>`).join('')||'<p class="filter-empty">没有匹配项</p>';
    container.parentElement.querySelector('.filter-option-count').textContent=matching.length>150?'显示前 150 项，共 '+matching.length+' 项；输入名称或 ID 继续筛选。':'共 '+matching.length+' 项';
  }
  function renderFilters() {
    const dimensions=allowedDimensions();
    $('#dimension-filters').innerHTML=dimensions.filter(k=>k!=='date').map(key=>`<details class="multi-filter"><summary><span>${esc(labels[key])}</span><strong data-summary="${key}">${filterSummary(key)}</strong></summary><div class="filter-popover"><input type="search" data-filter-search="${key}" placeholder="搜索${esc(labels[key])}" aria-label="搜索${esc(labels[key])}"><div data-options="${key}" class="filter-option-list"></div><div class="filter-option-count"></div><button type="button" class="filter-clear" data-clear-filter="${key}">清除此筛选</button></div></details>`).join('');
    dimensions.filter(k=>k!=='date').forEach(k=>renderFilterList(k));
    $('#group-choices').innerHTML=dimensions.map(key=>`<label class="group-choice"><input type="checkbox" value="${key}" ${state.groups.includes(key)?'checked':''}><span>${esc(labels[key])}</span></label>`).join('');
    updateGroupLimit();
    $('#view-hint').textContent={overview:'共同维度并列查看收入与发布；两者日期口径不同',effects:'收入国家、设备、素材与短链来源支持组合筛选',publications:'按账号语言、国家、发布方式与任务状态查看执行'}[state.view];
  }
  function updateGroupLimit(){document.querySelectorAll('#group-choices input').forEach(node=>node.disabled=!node.checked&&state.groups.length>=3);}
  function collect() {
    const params=new URLSearchParams({view:state.view,start:$('#date-start').value,end:$('#date-end').value,version:state.options.version,page:'1',page_size:$('#page-size').value,sort:defaultMetric(),direction:'desc',ranking_metric:defaultMetric()});
    for(const dim of state.groups)params.append('group_by',dim);
    for(const [key,values]of Object.entries(state.filters))for(const value of values)params.append(key,value);
    if($('#report-search').value.trim())params.set('search',$('#report-search').value.trim());
    return params;
  }
  async function fetchReport(params) {
    const id=++state.requestId; state.controller?.abort(); const controller=new AbortController();state.controller=controller;
    state.busy=true;controls();message('正在读取报表…');
    try {
      const report=await request(API+'/report?'+params,{signal:controller.signal});
      if(id!==state.requestId)return;
      state.report=report;state.applied=new URLSearchParams(params);state.dirty=false;renderReport();message('');
    }catch(error){if(error.name==='AbortError')return;if(error.status===401||error.status===403){denied(error);return;}if(id===state.requestId)message(error.message+(error.status===409?' 请点击“读取最新数据”。':''),'error');}
    finally{if(id===state.requestId){state.busy=false;controls();}}
  }
  function range(kind) {
    const fallback=new Date();
    let end=new Date((state.options?.range?.end||utc(fallback))+'T00:00:00Z');
    if(kind==='today'||kind==='yesterday'){end=new Date(utc(fallback)+'T00:00:00Z');if(kind==='yesterday')end.setUTCDate(end.getUTCDate()-1);}
    const start=new Date(end);
    if(!['today','yesterday'].includes(kind))start.setUTCDate(start.getUTCDate()-Number(kind)+1);
    $('#date-start').value=utc(start);$('#date-end').value=utc(end);dirty();
  }
  async function latest({initial=false}={}) {
    if(state.busy)return;state.busy=true;controls();message('正在读取最新快照…');
    try{
      const data=await request(API+'/options');
      state.options=data;state.allowed=true;
      $('#scope-label').textContent='数据范围：全部 SocialKit 团队（已授权用户可见）';
      if(initial||!$('#date-end').value){const end=new Date((data.range?.end||utc(new Date()))+'T00:00:00Z');const start=new Date(end);start.setUTCDate(start.getUTCDate()-13);$('#date-start').value=utc(start);$('#date-end').value=utc(end);}
      const allowed=new Set(allowedDimensions());for(const k of Object.keys(state.filters))if(!allowed.has(k))delete state.filters[k];
      renderFilters();state.busy=false;controls();await fetchReport(collect());
    }catch(error){state.busy=false;if(error.status===401||error.status===403)denied(error);else{message(error.message,'error');controls();}}
  }
  function empty(title){return `<div class="chart-empty"><strong>${esc(title)}</strong></div>`;}
  function selectMetrics(id,selected) {$('#'+id).innerHTML=visibleMetrics().map(key=>`<option value="${key}">${esc(metricLabels[key])}</option>`).join('');$('#'+id).value=visibleMetrics().includes(selected)?selected:defaultMetric();}
  function renderTrend(){
    const r=state.report,key=$('#trend-metric').value,points=r.trend,values=points.filter(p=>finite(p[key])).map(p=>Number(p[key]));
    $('#trend-description').textContent=publicationMetrics.includes(key)?'按账号当地排期日期统计':'按 UTC 效果日期统计';
    $('#trend-caption').textContent=points.some(p=>!finite(p[key]))?'缺失日期留空，不按零处理。':'每小时更新近期数据，每天回算前 14 个完整 UTC 日期；当天数据仍会回传。';
    if(!values.length){$('#trend-chart').innerHTML=empty('所选范围尚无已同步数据');return;}
    const w=780,h=250,left=68,right=20,top=20,bottom=40,minimum=Math.min(0,...values),maximum=Math.max(1,...values),spread=maximum-minimum;
    const x=i=>left+(points.length===1?(w-left-right)/2:i*(w-left-right)/(points.length-1)),y=v=>top+(maximum-v)/spread*(h-top-bottom);
    let svg=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(metricLabels[key])}每日趋势"><title>${esc(metricLabels[key])}每日趋势</title>`;
    for(let n=0;n<5;n++){const v=minimum+spread*n/4,py=y(v);svg+=`<line x1="${left}" y1="${py}" x2="${w-right}" y2="${py}" stroke="#e5ebf5"/><text x="${left-10}" y="${py+4}" text-anchor="end">${esc(Math.abs(v)>=1000?(v/1000).toFixed(1)+'k':v.toFixed(moneyKeys.has(key)?1:0))}</text>`;}
    const segments=[];let segment=[];points.forEach((p,i)=>{if(finite(p[key]))segment.push([x(i),y(Number(p[key]))]);else if(segment.length){segments.push(segment);segment=[];}});if(segment.length)segments.push(segment);
    segments.forEach(part=>{if(part.length>1)svg+=`<path d="${part.map(([px,py],i)=>(i?'L':'M')+px+','+py).join(' ')}" stroke="#4d7ed5" stroke-width="2.5" fill="none"/>`;});
    points.forEach((p,i)=>{if(i%Math.max(1,Math.ceil(points.length/7))===0||i===points.length-1)svg+=`<text x="${x(i)}" y="${h-13}" text-anchor="middle">${esc(p.date.slice(5))}</text>`;if(finite(p[key]))svg+=`<circle class="trend-dot" cx="${x(i)}" cy="${y(Number(p[key]))}" r="3.5" tabindex="0"><title>${esc(p.date+' · '+format(p[key],key))}</title></circle>`;});
    $('#trend-chart').innerHTML=svg+'</svg>';
  }
  function bars(id,rows,key,dimension){
    const available=rows.filter(r=>finite(r[key])),sum=available.reduce((n,r)=>n+Math.max(0,Number(r[key])),0);
    if(!available.length){$('#'+id).innerHTML=empty('暂无已同步数据');return;}
    $('#'+id).innerHTML=available.map(r=>{const percentage=sum?Math.max(0,Number(r[key]))/sum*100:0;return `<div class="distribution-row"><div class="distribution-copy"><span>${esc(r.labels[dimension])}</span><strong>${format(r[key],key)} · ${percentage.toFixed(1)}%</strong></div><div class="distribution-track"><span style="width:${percentage}%"></span></div></div>`;}).join('');
  }
  function ranking(){const key=$('#ranking-metric').value,rows=state.report.ranking,max=Math.max(1,...rows.map(r=>Number(r[key])||0));$('#ranking-caption').textContent=state.report.group_by.map(k=>labels[k]).join(' × ')+' · '+metricLabels[key];$('#ranking-chart').innerHTML=rows.length?rows.map((r,i)=>`<div class="ranking-row"><span class="ranking-position">${i+1}</span><div class="ranking-content"><div class="ranking-copy"><span title="${esc(state.report.group_by.map(k=>r.labels[k]).join(' · '))}">${esc(state.report.group_by.map(k=>r.labels[k]).join(' · '))}</span><strong>${format(r[key],key)}</strong></div><div class="ranking-track"><span style="width:${Math.max(0,(Number(r[key])||0)/max*100)}%"></span></div></div>`).join(''):empty('当前筛选没有分组数据');}
  function renderReport(){
    const r=state.report,m=r.meta;$('#report-content').classList.remove('hidden');$('#report-content').setAttribute('aria-labelledby','tab-'+state.view);
    $('#report-period').textContent=m.start+' 至 '+m.end+' · '+({overview:'总览',effects:'收入效果',publications:'发布执行'}[state.view]);
    $('#report-updated').textContent='报表更新：'+stamp(m.generated_at)+' · 收入源更新时间：'+stamp(m.source_updated_at);$('#report-version').textContent='快照版本：'+m.version;$('#report-version').className='version-tag';
    const warnings=[];const today=utc(new Date());if(state.view!=='publications'&&m.start<=today&&m.end>=today)warnings.push('当前 UTC 日期的数据尚未完整，收入会随回传更新；北京时间 08:00 切日。');if(m.stale)warnings.push('最近一次更新未成功，正在显示上一份成功快照。');if(m.partial)warnings.push('所选范围含未同步日期；合计仅包含已同步数据。');
    const unmatched=r.totals.unmatched_revenue_cents;if(Number(r.totals.unmatched_rows)||Number(r.totals.unmatched_links)||Number(unmatched))warnings.push('当前筛选有 '+format(r.totals.unmatched_links,'link_count')+' 个未归属短链键（'+format(r.totals.unmatched_rows,'tasks')+' 条明细），点击 '+format(r.totals.unmatched_clicks||0,'clicks')+'、安装 '+format(r.totals.unmatched_installs||0,'installs')+'、收入 '+format(Number(unmatched||0)/100,'revenue')+'；已保留在总盘和未归属分组。');
    if(m.latest_version&&m.latest_version!==m.version)warnings.push('已有更新版本，当前查看的是固定快照；点击“读取最新数据”切换。');
    $('#quality-message').textContent=warnings.join(' ');$('#quality-message').hidden=!warnings.length;
    const kpis=state.view==='publications'?['success','planned','tasks','unfulfilled','failed','success_rate']:state.view==='effects'?['revenue','net_revenue','dnu_revenue','refunds','clicks','views','installs','conversions']:['revenue','net_revenue','success','installs','conversions','dnu_revenue','refunds','clicks'];
    $('#kpi-grid').innerHTML=kpis.map(key=>`<article class="kpi-card"><span class="kpi-label">${esc(metricLabels[key])}</span><strong class="kpi-value">${format(r.totals[key],key)}</strong><span class="kpi-hint">${publicationMetrics.includes(key)?'按账号当地排期日期':key==='conversions'?'每日人数累加 · 跨日不去重':'按 UTC 效果日期'}</span></article>`).join('');
    selectMetrics('trend-metric',$('#trend-metric').value);selectMetrics('ranking-metric',r.parameters.ranking_metric);renderTrend();ranking();
    const metric=defaultMetric();$('#platform-chart-title').textContent=state.view==='publications'?'平台成功发布分布':'平台收入分布';$('#product-chart-title').textContent=state.view==='publications'?'产品成功发布分布':'产品收入分布';bars('platform-chart',r.breakdowns.platform,metric,'platform');bars('product-chart',r.breakdowns.product,metric,'product');
    const stages=state.view==='publications'?['planned','success','unfulfilled']:['clicks','installs','conversions'];$('#conversion-title').textContent=state.view==='publications'?'计划执行概览':'效果总量';$('#conversion-description').textContent=state.view==='publications'?'未计入成功不等于发布失败':'各环节非同一批用户的逐人追踪';const maximum=Math.max(1,...stages.map(k=>Number(r.totals[k])||0));$('#conversion-chart').innerHTML=stages.map(key=>`<div class="conversion-stage"><div class="conversion-stage-head"><span>${esc(metricLabels[key])}</span><strong>${format(r.totals[key],key)}</strong></div><div class="conversion-track"><span style="width:${Math.max(0,(Number(r.totals[key])||0)/maximum*100)}%;background:#6c94cf"></span></div></div>`).join('');
    const cols=state.view==='publications'?['tasks','planned','success','failed','unfulfilled','success_rate']:state.view==='effects'?['revenue','refunds','net_revenue','dnu_revenue','clicks','views','installs','conversions','link_count']:['revenue','success','planned','clicks','installs','conversions','refunds'];
    const allCols=[...r.group_by,...cols];$('#report-head').innerHTML='<tr>'+allCols.map(k=>`<th class="${cols.includes(k)?'number-column':''}"><button type="button" data-sort="${k}" class="${r.parameters.sort===k?'sorted':''}">${esc(labels[k]||metricLabels[k])}${r.parameters.sort===k?(r.parameters.direction==='desc'?' ↓':' ↑'):''}</button></th>`).join('')+'</tr>';
    $('#report-body').innerHTML=r.rows.length?r.rows.map(row=>'<tr>'+r.group_by.map(k=>`<td class="dimension-cell"><strong>${esc(row.labels[k])}</strong>${['account','drama'].includes(k)?`<small>${esc(k==='account'?row.external_account_id:row.drama_content_id)}</small>`:''}</td>`).join('')+cols.map(k=>`<td class="number-column">${format(row[k],k)}</td>`).join('')+'</tr>').join(''):`<tr><td class="empty-table" colspan="${allCols.length}">${m.partial?'当前筛选没有已同步的分组数据':'当前筛选没有记录'}<small>调整日期、搜索或筛选条件。</small></td></tr>`;
    $('#table-description').textContent='按 '+r.group_by.map(k=>labels[k]).join(' × ')+' 分组；导出与当前筛选使用同一快照。';$('#page-summary').textContent='共 '+r.pagination.total.toLocaleString('en-US')+' 组';$('#page-number').textContent=r.pagination.page+' / '+r.pagination.pages;$('#page-size').value=String(r.pagination.page_size);controls();
  }
  async function exportReport(){if(!state.applied||state.dirty||state.exporting)return;const id=state.requestId,params=new URLSearchParams(state.applied);state.exportController=new AbortController();state.exporting=true;controls();try{const blob=await request(API+'/export.csv?'+params,{raw:true,signal:state.exportController.signal});if(id!==state.requestId||!state.allowed||state.dirty)return;const link=document.createElement('a'),url=URL.createObjectURL(blob);link.href=url;link.download='SocialKit_'+params.get('start')+'_'+params.get('end')+'.csv';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){if(error.name==='AbortError')return;if(error.status===401||error.status===403)denied(error);else message(error.message,'error');}finally{state.exporting=false;controls();}}
  $('#report-filters').addEventListener('submit',event=>{event.preventDefault();if(!state.groups.length){message('至少选择一个分组维度。','error');return;}void fetchReport(collect());});
  $('#dimension-filters').addEventListener('change',event=>{const node=event.target,key=node.dataset.filter;if(!key)return;const values=new Set(state.filters[key]||[]);node.checked?values.add(node.value):values.delete(node.value);state.filters[key]=[...values];document.querySelector('[data-summary="'+key+'"]').textContent=filterSummary(key);dirty();});
  $('#dimension-filters').addEventListener('input',event=>{if(event.target.dataset.filterSearch)renderFilterList(event.target.dataset.filterSearch,event.target.value);});
  $('#dimension-filters').addEventListener('click',event=>{const key=event.target.dataset.clearFilter;if(key){state.filters[key]=[];renderFilterList(key);document.querySelector('[data-summary="'+key+'"]').textContent='全部';dirty();}});
  $('#group-choices').addEventListener('change',()=>{state.groups=[...document.querySelectorAll('#group-choices input:checked')].map(n=>n.value);updateGroupLimit();dirty();});
  ['date-start','date-end','report-search'].forEach(id=>$('#'+id).addEventListener('input',dirty));
  document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>range(button.dataset.range)));
  document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{state.view=button.dataset.view;const allowed=new Set(allowedDimensions());state.groups=state.groups.filter(k=>allowed.has(k));if(!state.groups.length)state.groups=['platform'];for(const k of Object.keys(state.filters))if(!allowed.has(k))delete state.filters[k];document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));renderFilters();void fetchReport(collect());}));
  $('#reset-filters').addEventListener('click',()=>{state.filters={};state.groups=['platform'];$('#report-search').value='';range('14');renderFilters();void fetchReport(collect());});
  $('#refreshPage').addEventListener('click',()=>void latest());$('#export-report').addEventListener('click',()=>void exportReport());$('#trend-metric').addEventListener('change',renderTrend);
  $('#ranking-metric').addEventListener('change',()=>{const p=new URLSearchParams(state.applied);p.set('ranking_metric',$('#ranking-metric').value);void fetchReport(p);});
  $('#page-size').addEventListener('change',()=>{const p=new URLSearchParams(state.applied);p.set('page_size',$('#page-size').value);p.set('page','1');void fetchReport(p);});
  for(const [id,delta]of [['previous-page',-1],['next-page',1]])$('#'+id).addEventListener('click',()=>{const p=new URLSearchParams(state.applied);p.set('page',String(state.report.pagination.page+delta));void fetchReport(p);});
  $('#report-head').addEventListener('click',event=>{const key=event.target.closest('[data-sort]')?.dataset.sort;if(!key||state.dirty||state.busy)return;const p=new URLSearchParams(state.applied);p.set('direction',p.get('sort')===key&&p.get('direction')==='desc'?'asc':'desc');p.set('sort',key);p.set('page','1');void fetchReport(p);});
  $('#authButton').addEventListener('click',()=>{const auth=state.auth;if(auth?.authenticated)denied({status:401,message:'正在退出登录…'});void UiTopbar.handleAuthAction({auth,api:async(path,init)=>{const response=await fetch(path,{credentials:'same-origin',...init});return response.json();}});});
  async function init(){try{state.auth=await request('/api/ui/topbar');UiTopbar.render({auth:state.auth,userCard:'#userCard',authButton:'#authButton'});await Promise.resolve(QuickNav.render({container:'#quickNav',auth:state.auth,activeKey:'socialkitAnalytics'})).catch(()=>{});await latest({initial:true});}catch(error){message(error.message,'error');}}
  void init();
})();
