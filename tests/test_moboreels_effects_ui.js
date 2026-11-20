'use strict';
const assert=require('node:assert/strict');
const {range,format,available,syncChanged,csv,esc}=require('../static/moboreels-effects.js');
assert.deepEqual(range(7,new Date('2026-11-19T20:00:00Z')),{date_from:'2026-11-14',date_to:'2026-11-20'});
assert.equal(format(null,'total_revenue_usd'),'—');
assert.equal(format('0.00125','advertising_revenue_usd'),'$0.00125');
assert.equal(format('0.00000001','advertising_revenue_usd'),'＜$0.000001');
assert.equal(syncChanged('2026-11-19T02:00:00Z','2026-11-20T02:02:21Z'),true);
assert.equal(syncChanged('same','same'),false);
const d={date_from:'2026-11-14',date_to:'2026-11-20',click_coverage_from:'2026-11-13',complete_order_days:0,complete_ad_days:0,requested_days:7,group_by:['story'],list:[{dimensions:[{label:'=HYPERLINK("bad")'}],clicks:3,total_revenue_usd:'0.000000001'}],summary:{clicks:3}};
assert.equal(available(d,'total_revenue_usd'),false);
assert.equal(available(d,'clicks'),true);
assert.match(csv(d),/"'=HYPERLINK/);
d.complete_ad_days=1;
assert.match(csv(d),/"0.000000001"/);
assert.equal(esc('<script>'), '&lt;script&gt;');
console.log('Mobo effects UI coverage, refresh and export checks passed');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function eventTarget(){const events=new Map();return {addEventListener(t,f){if(!events.has(t))events.set(t,[]);events.get(t).push(f);},emit(t,e={}){for(const f of events.get(t)||[])f(e);}};}
function harness(){
  const nodes=new Map(),requests=[],timers=new Map();let timerId=0;
  const dimensions=['platform','publisher','story','promotion','account','day'].map(value=>({value,checked:['platform','publisher'].includes(value)}));
  function element(id){if(nodes.has(id))return nodes.get(id);const node=Object.assign(eventTarget(),{value:({pageSize:'50',trendMetric:'clicks',rankMetric:'total_revenue_usd'})[id]||'',disabled:false,hidden:id==='content',innerHTML:'',textContent:'',setAttribute(){},querySelectorAll(q){return q==='input:checked'?dimensions.filter(d=>d.checked):dimensions;},classList:{toggle(){}}});nodes.set(id,node);return node;}
  const document=Object.assign(eventTarget(),{visibilityState:'visible',getElementById:element,querySelectorAll:()=>[]});
  const window=Object.assign(eventTarget(),{document,location:{pathname:'/moboreels-report.html',assign(){},reload(){}},fetch(url,options){let yes,no;const promise=new Promise((a,b)=>{yes=a;no=b;});const request={url,aborted:false,reply(data,status=200){yes({status,ok:status>=200&&status<300,json:async()=>JSON.parse(JSON.stringify(data))});},fail:no};options.signal?.addEventListener('abort',()=>{request.aborted=true;no(Object.assign(Error('aborted'),{name:'AbortError'}));});requests.push(request);return promise;}});
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/moboreels-effects.js'),'utf8'),{window,console,URLSearchParams,AbortController,setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)});
  return {element,requests,timers,document,window};
}
function fixture(){const summary={clicks:12,order_recharge_count:2,order_recharge_usd:'12.00000000',order_refund_usd:'0',advertising_revenue_usd:'0.37',total_revenue_usd:'12.37',links_clicked:3,automated:4};return {view:'effects',currency:'USD',scope:{label:'机构全部'},date_from:'2026-11-14',date_to:'2026-11-20',group_by:['platform','publisher'],list:[{...summary,dimensions:[{key:'platform',value:'x',label:'X'},{key:'publisher',value:'owner',label:'<Owner>'}]}],summary,trend:[{...summary,date:'2026-11-19',orders_synced:true,ads_synced:true,clicks_covered:true}],publishers:[{value:'owner',label:'Owner'}],excluded_shared:{total_revenue_usd:'0'},click_coverage_from:'2026-11-13',complete_ad_days:6,complete_order_days:6,requested_days:7,last_success_at:'2026-11-20T02:02:21Z',queried_at:'2026-11-20T12:00:00+08:00',ad_data_as_of:'2026-11-19'};}
(async()=>{
  const p=harness();assert.equal(p.requests.length,1,'wait for authorization before financial requests');
  p.requests[0].reply({authenticated:true,user:{is_admin:true}});await flush();
  assert.match(p.requests.at(-1).url,/view=effects/);p.requests.at(-1).reply(fixture());await flush();
  assert.match(p.element('summary').innerHTML,/\$12.37/);assert.match(p.element('tableBody').innerHTML,/&lt;Owner&gt;/);assert.equal(p.element('content').hidden,false);
  p.requests.at(-1).reply({run:{status:'succeeded',updated_at:'one'},last_success_at:'2026-11-20T02:02:21Z'});await flush();
  const before=p.requests.length;const timer=[...p.timers.values()][0];timer.fn();
  p.requests.at(-1).reply({run:{status:'succeeded',updated_at:'two'},last_success_at:'2026-11-20T03:00:00Z'});await flush();
  assert.equal(p.requests.length,before+2,'completed background sync refreshes data without seeing running status');
  p.requests.at(-1).reply(fixture());await flush();
  p.element('refreshBtn').emit('click');const old=p.requests.findLast(r=>r.url.includes('/reports?'));p.element('refreshBtn').emit('click');assert.equal(old.aborted,true);
  const latest=p.requests.findLast(r=>r.url.includes('/reports?'));latest.reply(fixture());await flush();
  assert.equal(p.element('content').hidden,false);p.requests.findLast(r=>r.url.includes('/report-sync-runs/')).reply({run:null});await flush();
  p.element('refreshBtn').emit('click');p.requests.findLast(r=>r.url.includes('/reports?')).reply({error:'permission_denied'},403);await flush();assert.equal(p.element('content').hidden,true);assert.equal(p.element('fields').disabled,true);
  const denied=harness();denied.requests[0].reply({authenticated:true,user:{permissions:{moboreels_library:true}}});await flush();assert.equal(denied.requests.length,1);assert.equal(denied.element('content').hidden,true);
  console.log('Mobo effects interaction checks passed: authorization, rendering, stale reads, missed sync completion, revocation');
})().catch(e=>{console.error(e);process.exitCode=1;});
