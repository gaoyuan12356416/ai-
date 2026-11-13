"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const ui=require("../static/moboreels-report.js");
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function eventTarget(){const events=new Map();return {addEventListener(type,fn){if(!events.has(type))events.set(type,[]);events.get(type).push(fn);},emit(type,event={}){for(const fn of events.get(type)||[])fn(event);}};}
function harness(){
  const elements=new Map(),requests=[],timers=new Map();let timerId=0;
  function element(id){
    if(elements.has(id))return elements.get(id);
    const classes=new Set(),node=Object.assign(eventTarget(),{id,value:id==="groupBy"?"promotion":"",textContent:"",innerHTML:"",disabled:false,
      classList:{add:value=>classes.add(value),remove:value=>classes.delete(value),toggle(value,on){if(on)classes.add(value);else classes.delete(value);},contains:value=>classes.has(value)}});
    elements.set(id,node);return node;
  }
  const document=Object.assign(eventTarget(),{readyState:"loading",visibilityState:"visible",getElementById:element});
  const window=Object.assign(eventTarget(),{document,AbortController,location:{pathname:"/moboreels-report.html",assign(){},reload(){}},fetch(url,options){
    const result=deferred();
    const request={url,options,aborted:false,reply(data,status=200){result.resolve({status,ok:status>=200&&status<300,json:async()=>JSON.parse(JSON.stringify(data))});},fail:result.reject};
    if(options.signal)options.signal.addEventListener("abort",()=>{request.aborted=true;result.reject(Object.assign(new Error("abort"),{name:"AbortError"}));});
    requests.push(request);return result.promise;
  }});
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../static/moboreels-report.js"),"utf8"),{window,URLSearchParams,console,setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)});
  document.emit("DOMContentLoaded");
  return {element,requests,timers,document,window,request(part){return requests.find(request=>request.url.includes(part));}};
}
const auth={authenticated:true,user:{name:"报表用户",permissions:{moboreels_report:true}}};
const metrics={order_recharge_usd:"12345.6789",order_refund_usd:"45.67",order_net_usd:"12300.0089",advertising_revenue_usd:"0.000001",unclassified_order_usd:"8.25",unmatched_attribution_usd:"2.45",order_recharge_count:18,order_refund_count:2,unclassified_order_count:1};
function report(overrides={}){return Object.assign({scope:{kind:"owner",label:"仅本人推广"},date_from:"2026-10-13",date_to:"2026-11-12",group_by:"promotion",total:1,list:[Object.assign({story_id:"123",story_name:"测试短剧",koc_code:"CODE",attribution_status:"matched",owner_name:"报表用户"},metrics)],summary:metrics,currency:"USD",source_timezone:"unspecified",ad_data_as_of:"2026-11-12"},overrides);}

async function interactions(){
  const page=harness();
  assert.equal(page.requests.length,3,"auth, report and sync reads start in parallel");
  page.request("/reports?").reply(report());page.request("/report-sync-runs/latest").reply({run:null});await flush();
  assert.ok(!page.element("reportRows").innerHTML.includes("CODE"));
  assert.ok(!page.element("summaryCards").innerHTML.includes("12,345"));
  page.request("/ui/topbar").reply(auth);await flush();
  assert.equal(page.requests.length,3,"startup consumes earlier reads without requesting twice");
  assert.ok(page.element("reportRows").innerHTML.includes("CODE"));
  assert.ok(page.element("summaryCards").innerHTML.includes("$12,300.0089"));
  assert.equal(page.element("scopeLabel").textContent,"范围：仅本人推广");
  assert.equal(page.element("filterFields").disabled,false);

  page.element("groupBy").value="day";page.element("groupBy").emit("change");
  const outdated=page.requests.at(-1);
  page.element("groupBy").value="story";page.element("searchInput").value='id&code=<x>';page.element("groupBy").emit("change");
  const current=page.requests.at(-1),length=page.requests.length;
  page.element("filterForm").emit("submit",{preventDefault(){}});
  assert.equal(page.requests.length,length,"identical reads coalesce");
  assert.equal(outdated.aborted,true);
  const url=new URL(current.url,"https://test.invalid");
  assert.equal(url.searchParams.get("group_by"),"story");assert.equal(url.searchParams.get("q"),'id&code=<x>');
  assert.equal(url.searchParams.get("page_size"),"50");
  current.reply(report({total:101,group_by:"story",summary:Object.assign({},metrics,{order_recharge_usd:"999999.00"})}));await flush();
  assert.ok(page.element("summaryCards").innerHTML.includes("$999,999.00"),"summary must use backend totals, not page sums");
  assert.equal(page.element("dimensionHeading").textContent,"短剧");
  assert.equal(page.element("nextPage").disabled,false);
  page.element("nextPage").emit("click");
  assert.equal(new URL(page.requests.at(-1).url,"https://test.invalid").searchParams.get("page_index"),"2");
  page.requests.at(-1).reply(report({total:101,group_by:"story"}));await flush();
  assert.equal(page.element("pageNumber").textContent,"2 / 3");

  // Invalid ranges do not query or start a synchronization.
  page.element("dateFrom").value="2026-01-01";page.element("dateTo").value="2026-02-01";
  const beforeInvalid=page.requests.length;
  page.element("filterForm").emit("submit",{preventDefault(){}});page.element("syncBtn").emit("click");
  assert.equal(page.requests.length,beforeInvalid);assert.match(page.element("filterError").textContent,/31/);
  page.element("dateFrom").value="2026-01-01";page.element("dateTo").value="2026-01-31";
  page.element("syncBtn").emit("click");
  const post=page.requests.at(-1);
  assert.equal(post.options.method,"POST");assert.deepEqual(JSON.parse(post.options.body),{date_from:"2026-01-01",date_to:"2026-01-31"});
  const run={id:"run/one",status:"running",stage:"fetching",date_from:"2026-01-01",date_to:"2026-01-31",total_parts:32,completed_parts:0,parts:[{kind:"orders",status:"running",fetched_pages:2,total_pages:10,total:0,processed:0,verified:0}]};
  post.reply({run,reused:false},202);await flush();
  assert.match(page.element("progressText").textContent,/2 \/ 10 页/);assert.equal(page.element("syncBtn").disabled,true);
  const refresh=Array.from(page.timers.values()).find(timer=>timer.delay===500);assert.ok(refresh);refresh.fn();
  assert.ok(page.requests.at(-1).url.endsWith("/report-sync-runs/run%2Fone"));
  const count=page.requests.length;page.document.emit("visibilitychange");assert.equal(page.requests.length,count,"only one status request is active");
  const active=page.requests.at(-1);
  page.document.visibilityState="hidden";page.document.emit("visibilitychange");await flush();
  assert.equal(active.aborted,true);assert.ok(Array.from(page.timers.values()).every(timer=>timer.delay===3000),"only a transient toast remains");
  page.document.visibilityState="visible";page.document.emit("visibilitychange");
  page.requests.at(-1).reply({run:Object.assign({},run,{status:"succeeded",stage:"completed",completed_parts:32,verified:500,finished_at:"2026-11-13T01:00:00Z"})});await flush();
  const afterSync=page.requests.at(-1);assert.ok(afterSync.url.includes("/reports?"),"successful sync refreshes the report");
  const zero=Object.fromEntries(Object.entries(metrics).map(([key,value])=>[key,typeof value==="number"?0:"0"]));
  afterSync.reply(report({total:0,list:[],summary:zero,ad_data_as_of:null}));await flush();
  assert.match(page.element("reportRows").innerHTML,/查询已完成.*暂无记录/);
  assert.match(page.element("adCoverage").textContent,/尚无已同步日期/);
  assert.match(page.element("lastSuccess").textContent,/01:00:00 UTC/);
  page.window.emit("pagehide");await flush();

  for(const status of [401,403]){
    const denied=harness();denied.request("/reports?").reply({error:status===401?"cookie_auth_required":"permission_denied"},status);await flush();
    denied.request("/ui/topbar").reply(auth);await flush();
    assert.equal(denied.element("filterFields").disabled,true);assert.ok(!denied.element("summaryCards").innerHTML.includes("$0.00"));assert.equal(denied.timers.size,0);
  }
  const onlyLibrary=harness();onlyLibrary.request("/reports?").reply(report());onlyLibrary.request("/report-sync-runs/latest").reply({run:null});onlyLibrary.request("/ui/topbar").reply({authenticated:true,user:{permissions:{moboreels_library:true}}});await flush();
  assert.equal(onlyLibrary.element("filterFields").disabled,true,"library permission does not grant financial report access");
  assert.ok(!onlyLibrary.element("reportRows").innerHTML.includes("CODE"));
  const admin=harness();admin.request("/ui/topbar").reply({authenticated:true,user:{role:"admin"}});admin.request("/reports?").reply(report({scope:{kind:"institution",label:"全机构"}}));admin.request("/report-sync-runs/latest").reply({run:null});await flush();
  assert.equal(admin.element("scopeLabel").textContent,"范围：全机构");admin.window.emit("pagehide");
  const failed=harness();failed.request("/reports?").fail(new Error("offline"));failed.request("/report-sync-runs/latest").fail(new Error("offline"));await flush();failed.request("/ui/topbar").reply(auth);await flush();
  assert.ok(failed.element("reportRows").innerHTML.includes("读取失败"));assert.ok(!failed.element("summaryCards").innerHTML.includes("$0.00"),"failed responses must not manufacture zero revenue");failed.window.emit("pagehide");
}

async function main(){
  assert.equal(ui.money("12345678901234567890.123456789"),"$12,345,678,901,234,567,890.123456789");
  assert.equal(ui.money("-3.0100"),"-$3.01");assert.equal(ui.money("-0.0000"),"$0.00");assert.equal(ui.money("0.000001"),"$0.000001");
  for(const value of [null,undefined,"","NaN","<script>",true,"1e+9"])assert.equal(ui.money(value),"—");
  assert.equal(ui.formatCount(1234),"1,234");assert.equal(ui.formatCount(null),"—");
  assert.deepEqual(ui.defaultRange(new Date("2026-11-13T23:00:00Z")),{date_from:"2026-10-13",date_to:"2026-11-12"});
  assert.deepEqual(ui.defaultRange(new Date("2024-03-01T00:00:00Z")),{date_from:"2024-01-30",date_to:"2024-02-29"});
  assert.deepEqual(ui.validateRange("2026-01-01","2026-01-31",new Date("2026-11-13")),{date_from:"2026-01-01",date_to:"2026-01-31"});
  for(const [from,to] of [["2026-01-01","2026-02-01"],["2026-02-29","2026-03-01"],["2026-11-12","2026-11-11"],["2026-11-13","2026-11-14"],["","2026-11-13"]])assert.throws(()=>ui.validateRange(from,to,new Date("2026-11-13T12:00:00Z")));
  const bad=Object.assign({},metrics,{koc_code:"<img src=x onerror=bad>",story_name:"<script>bad</script>",owner_name:'" onclick="bad',unknown_states:[{source_status_text:"<svg onload=bad>",classification_reason:"<script>"}]});
  const row=ui.rowHtml(bad);assert.ok(!row.includes("<script>"));assert.ok(!row.includes("<img"));assert.ok(row.includes("&lt;svg"));
  const day=ui.rowHtml(Object.assign({},metrics,{date:"2026-11-12"}),"day");assert.ok(day.includes("2026-11-12"));assert.ok(day.includes("时区未注明"));
  assert.ok(!ui.summaryHtml(null).includes("$0.00"));
  assert.equal(ui.progressModel({status:"running",stage:"fetching",total_parts:1,completed_parts:0,parts:[{status:"running",kind:"orders",fetched_pages:1,total_pages:2}]}).percent,25);
  assert.equal(ui.progressModel({status:"succeeded",total_parts:32,verified:100}).percent,100);
  assert.equal(ui.progressModel({status:"failed",total_parts:32,completed_parts:10}).active,false);
  assert.ok(Number.isFinite(ui.progressModel({status:"running",stage:"writing",total:0}).percent));
  assert.ok(ui.partsHtml([{kind:"ad",date:"<svg>",status:"failed",error:"<script>"}]).includes("&lt;script&gt;"));
  await interactions();
  console.log("Moboreels report UI tests passed: exact USD decimals, date boundaries, source dates, scoped auth, parallel startup, stale-query cancellation, real empty/error states and asynchronous synchronization.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
