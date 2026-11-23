(function(root){
  'use strict';
  const API='/api/moboreels', DIMS={platform:'发布渠道',publisher:'发布人',story:'剧目',promotion:'推广口令',account:'发布账号',day:'日期'};
  const METRICS=[['clicks','点击量'],['order_recharge_count','充值订单数'],['order_recharge_usd','订单充值'],['order_refund_usd','订单退款'],['advertising_revenue_usd','广告收益'],['total_revenue_usd','合计净收入'],['links_clicked','有点击的链接'],['automated','预览及自动访问']];
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const isMoney=k=>k.endsWith('_usd');
  function format(value,key){
    if(value===null||value===undefined)return '—';
    const n=Number(value);if(!Number.isFinite(n))return '—';
    if(!isMoney(key))return n.toLocaleString('zh-CN');
    if(n!==0&&Math.abs(n)<0.000001)return n>0?'＜$0.000001':'−＜$0.000001';
    return (n<0?'−':'')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:Math.abs(n)>0&&Math.abs(n)<0.01?6:2});
  }
  function beijing(value){if(!value)return '尚未同步';const d=new Date(value);return Number.isFinite(+d)?d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间':'未知';}
  function range(days=7,now=new Date()){
    const end=new Date(+now+8*3600000).toISOString().slice(0,10);
    return {date_from:new Date(Date.parse(end+'T00:00:00Z')-(days-1)*86400000).toISOString().slice(0,10),date_to:end};
  }
  function validate(from,to){
    if(!/^\d{4}-\d{2}-\d{2}$/.test(from)||!/^\d{4}-\d{2}-\d{2}$/.test(to))throw Error('请选择完整日期');
    if(!Number.isFinite(Date.parse(from))||!Number.isFinite(Date.parse(to))||from>to||to>range(1).date_to||(Date.parse(to)-Date.parse(from))/86400000>=31)throw Error('请选择不晚于今天、最多 31 天的日期范围');
    return {date_from:from,date_to:to};
  }
  function available(data,key){
    if(key==='clicks'||key==='automated'||key==='links_clicked')return data.date_to>=data.click_coverage_from;
    if(key==='advertising_revenue_usd')return data.complete_ad_days>0;
    if(key==='total_revenue_usd')return data.complete_order_days>0||data.complete_ad_days>0;
    return data.complete_order_days>0;
  }
  function value(data,row,key){
    const day=row.dimensions?.find(d=>d.key==='day')?.value;
    if(day&&day!=='unknown'){
      const coverage=data.trend.find(r=>r.date===day);
      const known=coverage&&(key==='clicks'||key==='automated'||key==='links_clicked'?coverage.clicks_covered:key==='advertising_revenue_usd'?coverage.ads_synced:key==='total_revenue_usd'?coverage.orders_synced&&coverage.ads_synced:coverage.orders_synced);
      if(!known)return null;
    }
    return available(data,key)?row[key]:null;
  }
  // Compare successful timestamps, even if the tab never observed the running state.
  function syncChanged(oldValue,newValue){return !!newValue&&oldValue!==newValue;}
  function csvCell(v){let s=String(v??'');if(/^\s*[=+@]/.test(s)||/^[\t\r]/.test(s)||(/^\s*-/.test(s)&&!/^-[0-9]+(?:\.[0-9]+)?$/.test(s)))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';}
  function csv(data){
    const header=[...data.group_by.map(d=>DIMS[d]),...METRICS.map(m=>m[1]+(isMoney(m[0])?' (USD)':''))];
    const rows=data.list.map(r=>[...r.dimensions.map(d=>d.label),...METRICS.map(([k])=>value(data,r,k))]);
    rows.push([...data.group_by.map((_,i)=>i===0?'合计':''),...METRICS.map(([k])=>value(data,data.summary,k))]);
    rows.push([],['查询日期',data.date_from,data.date_to],['最近收入同步',data.last_success_at||'未同步'],['覆盖日期数','订单 '+data.complete_order_days,'广告 '+data.complete_ad_days,'所选 '+data.requested_days],['口径','点击按北京时间；收入按供应方来源日期，时区未注明；未同步日期不视为零；共用口令收入不重复分摊']);
    return '\ufeff'+[header,...rows].map(r=>r.map(csvCell).join(',')).join('\r\n');
  }
  async function boot(){
    const doc=root.document,el={};
    ['quickNav','userCard','loginBtn','refreshBtn','scopeLabel','freshness','message','filters','fields','dateFrom','dateTo','platform','publisher','search','groups','resetBtn','content','period','queriedAt','coverage','summary','trendMetric','rankMetric','trend','ranking','tableHead','tableBody','tableFoot','pageSize','pageSummary','pageNumber','prev','next','exportBtn','syncBtn','syncStatus','syncDetail','syncError'].forEach(id=>el[id]=doc.getElementById(id));
    const state={auth:{},allowed:false,data:null,query:null,page:1,sort:'total_revenue_usd',desc:true,seq:0,controller:null,timer:null,lastSuccess:null,run:null,polling:false,starting:false,active:true};
    function message(text,error=false){el.message.textContent=text;el.message.hidden=!text;el.message.classList.toggle('error',error);}
    function deny(){state.allowed=false;state.seq++;state.controller?.abort();clearTimeout(state.timer);state.data=null;el.content.hidden=true;el.fields.disabled=true;el.refreshBtn.disabled=true;el.syncBtn.disabled=true;el.freshness.textContent='收入同步状态待授权';el.scopeLabel.textContent='无访问权限';el.syncDetail.textContent='登录并获得权限后查看';message('请登录并确认拥有 MoboReels 收益报表权限。',true);}
    async function api(url,options={}){
      const response=await root.fetch(url,{credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},...options});
      const data=await response.json().catch(()=>({}));
      if(response.status===401||response.status===403)deny();
      if(!response.ok)throw Error(data.message||'读取失败，请稍后重试（'+response.status+'）');
      return data;
    }
    function setDates(days){const r=range(days);el.dateFrom.value=r.date_from;el.dateTo.value=r.date_to;el.dateFrom.max=el.dateTo.max=range(1).date_to;}
    el.groups.innerHTML=Object.entries(DIMS).map(([key,label])=>`<label class="group-choice"><input type="checkbox" name="dimension" value="${key}" ${['platform','publisher'].includes(key)?'checked':''}>${label}</label>`).join('');
    function filters(){
      const group=Array.from(el.groups.querySelectorAll('input:checked')).map(n=>n.value);
      if(!group.length||group.length>3)throw Error('请选择 1 至 3 个分组维度');
      return {...validate(el.dateFrom.value,el.dateTo.value),group_by:group.join(','),platform:el.platform.value,publisher:el.publisher.value,q:el.search.value.trim(),view:'effects'};
    }
    function bars(target,rows,key){
      const max=Math.max(0,...rows.map(r=>Math.abs(Number(r.value)||0)));
      target.innerHTML=rows.length?rows.map(r=>`<div class="bar-row"><span class="bar-label" title="${esc(r.label)}">${esc(r.label)}</span><div class="bar-track"><div class="bar-fill" style="width:${r.value===null?0:max?Math.abs(Number(r.value))/max*100:0}%"></div></div><span class="bar-value" title="${esc(r.value??'未同步')}">${r.value===null?'未同步':esc(format(r.value,key))}</span></div>`).join(''):'<p class="muted">当前筛选暂无数据</p>';
    }
    function charts(){
      const d=state.data;if(!d)return;const key=el.trendMetric.value;
      bars(el.trend,d.trend.map(r=>({label:r.date,value:(key==='clicks'?r.clicks_covered:key==='order_recharge_count'?r.orders_synced:r.orders_synced&&r.ads_synced)?r[key]:null})),key);
      const rank=el.rankMetric.value;
      bars(el.ranking,[...d.list].sort((a,b)=>Number(b[rank])-Number(a[rank])).slice(0,10).map(r=>({label:r.dimensions.map(x=>x.label).join(' · '),value:value(d,r,rank)})),rank);
    }
    function cells(row){return METRICS.map(([key])=>`<td class="number" title="${esc(value(state.data,row,key)??'尚未同步')}">${esc(format(value(state.data,row,key),key))}</td>`).join('');}
    function table(){
      const d=state.data;if(!d)return;const size=Number(el.pageSize.value),pages=Math.max(1,Math.ceil(d.list.length/size));state.page=Math.min(state.page,pages);
      const rows=[...d.list].sort((a,b)=>{
        if(state.sort==='day'){
          const dayA=a.dimensions.find(x=>x.key==='day')?.value,dayB=b.dimensions.find(x=>x.key==='day')?.value;
          const knownA=!!dayA&&dayA!=='unknown',knownB=!!dayB&&dayB!=='unknown';
          if(knownA!==knownB)return knownA?-1:1;
          return knownA?(state.desc?-1:1)*dayA.localeCompare(dayB):0;
        }
        return (state.desc?-1:1)*(Number(a[state.sort])-Number(b[state.sort]));
      });
      const heading=(key,label,cls='')=>`<th${cls?` class="${cls}"`:''} aria-sort="${state.sort===key?(state.desc?'descending':'ascending'):'none'}"><button type="button" data-sort="${key}">${label}${state.sort===key?(state.desc?' ↓':' ↑'):''}</button></th>`;
      el.tableHead.innerHTML='<tr>'+d.group_by.map(k=>k==='day'?heading(k,DIMS[k]):`<th>${DIMS[k]}</th>`).join('')+METRICS.map(([key,label])=>heading(key,label,'number')).join('')+'</tr>';
      el.tableBody.innerHTML=rows.slice((state.page-1)*size,state.page*size).map(r=>'<tr>'+r.dimensions.map(x=>`<td class="dim">${esc(x.label)}${x.key==='story'&&x.value!==x.label?`<small>${esc(x.value)}</small>`:''}</td>`).join('')+cells(r)+'</tr>').join('')||`<tr><td colspan="${d.group_by.length+METRICS.length}" class="empty">当前日期和筛选范围暂无记录；请结合上方同步覆盖日期查看。</td></tr>`;
      el.tableFoot.innerHTML=`<tr><td colspan="${d.group_by.length}">全部筛选结果合计</td>${cells(d.summary)}</tr>`;
      el.pageSummary.textContent=`共 ${d.list.length} 组 · 每页 ${size} 组`;el.pageNumber.textContent=`${state.page} / ${pages}`;el.prev.disabled=state.page<=1;el.next.disabled=state.page>=pages;
    }
    function render(){
      const d=state.data;el.content.hidden=false;el.scopeLabel.textContent=d.scope.label+' · 金额 USD';el.freshness.textContent='最近收入同步：'+beijing(d.last_success_at);
      el.period.textContent=d.date_from+' 至 '+d.date_to+' · '+d.group_by.map(k=>DIMS[k]).join(' × ');el.queriedAt.textContent='收益读取：'+beijing(d.financial_queried_at||d.queried_at)+' · 点击查询：'+beijing(d.queried_at);
      const notes=[`订单已同步 ${d.complete_order_days}/${d.requested_days} 天，广告已同步 ${d.complete_ad_days}/${d.requested_days} 天（广告最新来源日 ${d.ad_data_as_of||'未同步'}）。`, '点击按北京时间；收入按 Mobo 来源日期。每小时整点自动同步收入，广告通常 T+1。'];
      if(d.complete_order_days<d.requested_days||d.complete_ad_days<d.requested_days)notes.unshift('收入部分日期尚未同步，下方合计仅含已采集收入，不能当作完整期间收入。');
      if(d.date_from<d.click_coverage_from)notes.push('点击统计从 '+d.click_coverage_from+' 开始，更早日期未覆盖。');
      if(Number(d.excluded_shared.total_revenue_usd)!==0)notes.push('本次渠道筛选未包含共用或待归属收入 '+format(d.excluded_shared.total_revenue_usd,'total_revenue_usd')+'，可切换“全部渠道”核对。');
      if(d.summary.unclassified_order_count)notes.push(`另有 ${d.summary.unclassified_order_count} 笔未分类订单，金额 ${format(d.summary.unclassified_order_usd,'unclassified_order_usd')}，未计入净收入。`);
      if(d.summary.unmatched_order_count||d.summary.unmatched_ad_count)notes.push('部分收入未匹配本地推广记录，已单独按待归属维度保留。');
      el.coverage.textContent=notes.join(' ');
      const hints={clicks:'有效短链跳转次数',order_recharge_count:'订单笔数，不是付费人数',order_recharge_usd:'已采集充值金额',order_refund_usd:'已采集退款金额',advertising_revenue_usd:'供应方广告收益 · T+1',total_revenue_usd:'充值 − 退款 + 广告；非机构佣金'};
      el.summary.innerHTML=METRICS.slice(0,6).map(([key,label])=>`<article class="metric ${key==='total_revenue_usd'?'primary':''}"><h3 class="metric-label">${label}${isMoney(key)?' · USD':''}</h3><p class="metric-value" title="${esc(value(d,d.summary,key)??'尚未同步')}">${esc(format(value(d,d.summary,key),key))}</p><p class="metric-note">${hints[key]}</p></article>`).join('');
      const selected=el.publisher.value;el.publisher.innerHTML='<option value="">全部发布人</option>'+d.publishers.map(p=>`<option value="${esc(p.value)}">${esc(p.label)}</option>`).join('');el.publisher.value=selected;
      charts();table();
    }
    function load(force=false){
      if(!state.allowed||!state.query)return Promise.resolve();
      const url=API+'/reports?'+new URLSearchParams(state.query);
      if(!force&&state.pending?.url===url)return state.pending.promise;
      const seq=++state.seq;state.controller?.abort();state.controller=new AbortController();el.refreshBtn.disabled=true;el.exportBtn.disabled=true;el.content.setAttribute('aria-busy','true');message('正在查询点击与收入…');
      const request={url};state.pending=request;
      request.promise=(async()=>{
      try{const d=await api(url,{signal:state.controller.signal});if(!state.allowed||seq!==state.seq)return;if(d.view!=='effects'||!Array.isArray(d.list))throw Error('报表格式异常');state.data=d;state.lastSuccess=d.last_success_at;render();message('');}
      catch(e){if(e.name!=='AbortError'&&seq===state.seq){state.data=null;el.content.hidden=true;message(e.message,true);}}
      finally{if(state.pending===request)state.pending=null;if(seq===state.seq){el.refreshBtn.disabled=!state.allowed;el.exportBtn.disabled=!state.data;el.content.setAttribute('aria-busy','false');}}
      })();return request.promise;
    }
    function schedule(){clearTimeout(state.timer);if(state.allowed&&state.active&&doc.visibilityState!=='hidden')state.timer=setTimeout(poll,state.run&&['queued','running'].includes(state.run.status)?3000:30000);}
    async function poll(){
      if(!state.allowed||!state.active||state.polling||doc.visibilityState==='hidden')return;state.polling=true;
      try{const d=await api(API+'/report-sync-runs/latest');if(!state.allowed||!state.active)return;const prior=state.run;state.run=d.run;
        const changed=syncChanged(state.lastSuccess,d.last_success_at)||(prior&&d.run&&prior.updated_at!==d.run.updated_at&&d.run.status==='failed');
        if(changed){state.lastSuccess=d.last_success_at;await load(true);}
        const r=d.run,active=r&&['queued','running'].includes(r.status);el.syncBtn.disabled=state.starting||!!active;
        el.syncStatus.textContent=active?'正在同步…':r?.status==='failed'?'最近同步部分失败':'每小时整点自动更新（北京时间）';
        el.syncDetail.textContent=r?`最近任务：${r.date_from} 至 ${r.date_to}；${(r.parts||[]).filter(p=>p.status==='succeeded').length}/${(r.parts||[]).length} 分项完成。最近成功：${beijing(d.last_success_at)}`:'尚无同步任务';el.syncError.textContent=r?.error||'';
      }catch(e){if(state.allowed)el.syncError.textContent='同步状态读取失败：'+e.message;}finally{state.polling=false;schedule();}
    }
    async function apply(){try{
      const query=filters();
      if(query.group_by!==state.query?.group_by){state.sort=query.group_by.split(',').includes('day')?'day':'total_revenue_usd';state.desc=true;}
      state.query=query;state.page=1;await load();
    }catch(e){message(e.message,true);}}
    el.filters.addEventListener('submit',e=>{e.preventDefault();apply();});
    el.groups.addEventListener('change',e=>{if(el.groups.querySelectorAll('input:checked').length>3){e.target.checked=false;message('最多选择 3 个分组维度');}});
    doc.querySelectorAll('[data-days]').forEach(btn=>btn.addEventListener('click',()=>{setDates(Number(btn.dataset.days));apply();}));
    el.resetBtn.addEventListener('click',()=>{setDates(7);el.platform.value=el.publisher.value=el.search.value='';el.groups.querySelectorAll('input').forEach(n=>n.checked=['platform','publisher'].includes(n.value));apply();});
    el.refreshBtn.addEventListener('click',()=>{load();poll();});
    el.trendMetric.addEventListener('change',charts);el.rankMetric.addEventListener('change',charts);el.pageSize.addEventListener('change',()=>{state.page=1;table();});
    el.prev.addEventListener('click',()=>{state.page=Math.max(1,state.page-1);table();});el.next.addEventListener('click',()=>{state.page++;table();});
    el.tableHead.addEventListener('click',e=>{const key=e.target.dataset.sort;if(!key)return;state.desc=state.sort===key?!state.desc:true;state.sort=key;state.page=1;table();});
    el.exportBtn.addEventListener('click',()=>{if(!state.data)return;const url=URL.createObjectURL(new Blob([csv(state.data)],{type:'text/csv;charset=utf-8'}));const a=doc.createElement('a');a.href=url;a.download=`MoboReels_社媒效果_${state.data.date_from}_${state.data.date_to}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
    el.syncBtn.addEventListener('click',async()=>{if(!state.allowed||state.starting)return;let period;try{period=validate(el.dateFrom.value,el.dateTo.value);}catch(e){message(e.message,true);return;}state.starting=true;el.syncBtn.disabled=true;try{const d=await api(API+'/report-sync-runs',{method:'POST',body:JSON.stringify(period)});state.run=d.run;el.syncError.textContent='';await poll();}catch(e){el.syncError.textContent=e.message;}finally{state.starting=false;el.syncBtn.disabled=!state.allowed||['queued','running'].includes(state.run?.status);schedule();}});
    el.loginBtn.addEventListener('click',()=>root.UiTopbar?root.UiTopbar.handleAuthAction({auth:state.auth,api,afterLogout:()=>root.location.reload()}):root.location.assign('/api/auth/feishu/login?next='+encodeURIComponent(root.location.pathname)));
    root.addEventListener('pagehide',()=>{state.active=false;clearTimeout(state.timer);state.controller?.abort();});
    root.addEventListener('pageshow',()=>{state.active=true;if(state.allowed){load();poll();}});
    doc.addEventListener('visibilitychange',()=>{if(doc.visibilityState==='hidden')clearTimeout(state.timer);else if(state.allowed){load();poll();}});
    setDates(7);
    try{state.auth=await api('/api/ui/topbar');const u=state.auth.user;state.allowed=!!(state.auth.authenticated&&u&&(u.is_admin||u.role==='admin'||u.permissions?.moboreels_report));
      root.UiTopbar?.render({auth:state.auth,userCard:el.userCard,authButton:el.loginBtn});
      root.QuickNav?.render({container:el.quickNav,activeKey:'moboreelsReport',auth:state.auth,onNavigate:item=>{if(item?.href)root.location.assign(item.href);}}).catch(()=>{});
      if(!state.allowed){deny();return;}el.fields.disabled=false;el.refreshBtn.disabled=false;await apply();await poll();
    }catch(e){deny();message(e.message,true);}
    return state;
  }
  const exported={format,range,validate,available,syncChanged,csv,esc,boot};
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
  if(root.document)boot();
})(typeof window!=='undefined'?window:globalThis);
