"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ui = require("../static/moboreels-library.js");

const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve=yes;reject=no;});
  return {promise,resolve,reject};
}
function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);},
    emit(type, event={}) {for(const fn of listeners.get(type)||[])fn(event);}
  };
}
// Execute the real boot function against an in-memory DOM and controllable network.
function pageHarness() {
  const elements = new Map(), requests = [], timers = new Map();
  let timerId=0;
  function element(id) {
    if(elements.has(id))return elements.get(id);
    let html="";
    const classes=new Set();
    const item=Object.assign(eventTarget(), {
      id,value:id==="statusFilter"?"1":id==="pageSize"?"50":"",textContent:"",disabled:false,open:false,dataset:{},buttons:[],htmlWrites:0,
      classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),toggle(name,on){if(on)classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)},
      querySelectorAll(selector){return selector==='[data-action="promotion"]'?this.buttons:[];},
      setAttribute(){},showModal(){this.open=true;},close(){this.open=false;this.emit("close");}
    });
    Object.defineProperty(item,"innerHTML",{get(){return html;},set(value){html=value;this.htmlWrites++;}});
    elements.set(id,item);
    return item;
  }
  const doc=Object.assign(eventTarget(),{readyState:"loading",visibilityState:"visible",getElementById:element});
  const win=Object.assign(eventTarget(),{
    document:doc,AbortController,location:{pathname:"/moboreels-library.html",assign(){},reload(){}},
    fetch(url,options) {
      const result=deferred();
      const request={url,options,aborted:false,reply(data,status=200){result.resolve({ok:status>=200&&status<300,status,json:async()=>JSON.parse(JSON.stringify(data))});},fail:result.reject};
      if(options.signal)options.signal.addEventListener("abort",()=>{request.aborted=true;result.reject(Object.assign(new Error("aborted"),{name:"AbortError"}));});
      requests.push(request);
      return result.promise;
    }
  });
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../static/moboreels-library.js"),"utf8"),{
    window:win,URL,URLSearchParams,console,
    setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)
  });
  doc.emit("DOMContentLoaded");
  return {element,doc,win,requests,timers,request(part){return requests.find(request=>request.url.includes(part));}};
}
const authorized={authenticated:true,user:{name:"测试用户",role:"user",permissions:{moboreels_library:true}}};

async function testLoadBehavior() {
  const page=pageHarness();
  assert.equal(page.requests.length,3,"auth, list and sync must start without waiting for each other");
  page.request("/dramas?").reply({total:1,list:[{story_id:"one",story_name:"已授权后可见",is_show:1}]});
  page.request("/sync-runs/latest").reply({run:null});
  await flush();
  assert.ok(!page.element("dramaRows").innerHTML.includes("已授权后可见"),"early data stays hidden before authentication");
  page.request("/ui/topbar").reply(authorized);
  await flush();
  assert.ok(page.element("dramaRows").innerHTML.includes("已授权后可见"));
  assert.equal(page.requests.length,3,"early successful reads must be consumed, not fetched twice");

  // Promotion completion must not replace rows or images.
  const button={dataset:{storyId:"one"},textContent:"生成推广链接"};
  page.element("dramaRows").buttons=[button];
  const writes=page.element("dramaRows").htmlWrites;
  const action={dataset:{action:"promotion",storyId:"one"}};
  page.doc.emit("click",{preventDefault(){},target:{closest:selector=>selector==="[data-action]"?action:null}});
  page.request("/one/promotion").reply({promotion:{status:"creating"}});
  await flush();
  assert.equal(button.textContent,"查看生成进度");
  assert.equal(page.element("dramaRows").htmlWrites,writes,"promotion state must preserve existing image nodes");

  // A rapid filter change aborts the old read, shares identical parameters and ignores the old result.
  page.element("languageFilter").value="3";page.element("languageFilter").emit("change");
  const obsolete=page.requests.at(-1);
  page.element("languageFilter").value="4";page.element("languageFilter").emit("change");
  const current=page.requests.at(-1), count=page.requests.length;
  page.element("filterForm").emit("submit",{preventDefault(){}});
  assert.equal(page.requests.length,count);
  assert.equal(obsolete.aborted,true);
  current.reply({total:1,list:[{story_id:"new",story_name:"最后筛选",is_show:1}]});
  await flush();
  assert.ok(page.element("dramaRows").innerHTML.includes("最后筛选"));

  // Refresh and visibility events share one sync read. Hidden tabs do not schedule polling.
  page.element("refreshBtn").emit("click");
  const sync=page.requests.at(-1);
  const syncCount=page.requests.filter(request=>request.url.endsWith("/sync-runs/latest")).length;
  page.doc.emit("visibilitychange");page.doc.emit("visibilitychange");
  assert.equal(page.requests.filter(request=>request.url.endsWith("/sync-runs/latest")).length,syncCount);
  page.doc.visibilityState="hidden";page.doc.emit("visibilitychange");
  assert.equal(sync.aborted,true);
  await flush();
  assert.equal(page.timers.size,0,"hidden tabs pause sync and promotion timers");
  page.doc.visibilityState="visible";page.doc.emit("visibilitychange");
  assert.equal(page.requests.filter(request=>request.url.endsWith("/sync-runs/latest")).length,syncCount+1);
  assert.equal(page.requests.filter(request=>(request.options.method||"GET")==="POST").length,0,"restoring a tab only reads promotion state");
  page.win.emit("pagehide");
  await flush();
  assert.equal(page.timers.size,0,"a canceled read must not restart polling after navigation");

  const manual=pageHarness();
  manual.request("/ui/topbar").reply(authorized);
  manual.request("/dramas?").reply({total:0,list:[]});
  await flush();
  const oldSync=manual.request("/sync-runs/latest");
  manual.element("syncBtn").emit("click");
  assert.equal(oldSync.aborted,true,"manual sync invalidates an older status read");
  const post=manual.requests.at(-1);
  assert.equal(post.options.method,"POST");
  post.reply({run:{id:"new-run",status:"running",stage:"fetching",fetched_pages:1,total_pages:12}},202);
  await flush();
  assert.match(manual.element("syncStatus").textContent,/获取剧目/);
  assert.equal(manual.element("syncBtn").disabled,true);
  const next=Array.from(manual.timers.values()).find(timer=>timer.delay===500);
  assert.ok(next,"manual sync schedules a prompt status refresh");
  const beforePoll=manual.requests.length;
  next.fn();manual.doc.emit("visibilitychange");
  assert.equal(manual.requests.length,beforePoll+1,"manual completion and visibility share the same status request");
  manual.win.emit("pagehide");await flush();
  assert.ok(Array.from(manual.timers.values()).every(timer=>timer.delay===2500),"only the transient toast may remain; polling is stopped");

  // Permission rejection cannot be undone by a late successful topbar response.
  for(const status of [401,403]) {
    const denied=pageHarness();
    denied.request("/dramas?").reply({error:status===401?"cookie_auth_required":"permission_denied"},status);
    await flush();
    denied.request("/ui/topbar").reply(authorized);
    await flush();
    assert.equal(denied.element("filterFields").disabled,true);
    assert.ok(!denied.element("accessNotice").classList.contains("hidden"));
    assert.equal(denied.timers.size,0);
  }
  const lateDenial=pageHarness();
  lateDenial.request("/ui/topbar").reply(authorized);
  lateDenial.request("/dramas?").reply({total:1,list:[{story_id:"one",story_name:"撤权后隐藏",is_show:1}]});
  await flush();
  lateDenial.request("/sync-runs/latest").reply({error:"permission_denied"},403);
  await flush();
  assert.equal(lateDenial.element("filterFields").disabled,true);
  assert.ok(!lateDenial.element("dramaRows").innerHTML.includes("撤权后隐藏"));
  assert.equal(lateDenial.timers.size,0);

  const earlyFailure=pageHarness();
  earlyFailure.request("/dramas?").fail(new Error("offline"));
  earlyFailure.request("/sync-runs/latest").fail(new Error("offline"));
  await flush();
  earlyFailure.request("/ui/topbar").reply(authorized);
  await flush();
  assert.match(earlyFailure.element("resultCount").textContent,/失败/);
  earlyFailure.win.emit("pagehide");
}

async function main() {
  assert.equal(ui.language(23), "未知语言（23）");
  assert.equal(ui.language(-1), "未提供语言");
  assert.equal(ui.fee(0), "未提供");
  assert.equal(ui.fee(-1), "未提供");
  assert.equal(ui.fee(9), "第 9 集");
  assert.equal(ui.safeUrl("javascript:alert(1)"), "");
  assert.equal(ui.safeUrl("data:text/html,test"), "");
  assert.equal(ui.safeUrl("https://user:secret@example.com"), "");
  assert.equal(ui.safeUrl("https://eng.moboreels.com/video/322BYS"), "https://eng.moboreels.com/video/322BYS");
  const cover="https://video-cdn3.cdreader.com//backend/cover.webp";
  assert.equal(ui.coverUrl(cover),cover+"?imageMogr2/thumbnail/120x168/format/webp/quality/80");
  for(const unchanged of [cover+"?sign=keep%2Fthis",cover+"?",cover.replace("video-cdn3.cdreader.com","other.example"),cover.replace("video-cdn3.cdreader.com","video-cdn3.cdreader.com.evil.example"),cover.replace("video-cdn3.cdreader.com","video-cdn3.cdreader.com:8443")])assert.equal(ui.coverUrl(unchanged),unchanged);
  assert.equal(ui.coverUrl("javascript:alert(1)"),"");
  for(let index=0;index<10;index++) {
    const coverHtml=ui.renderRow({story_id:"cover",img_src:cover,is_show:1},index);
    assert.ok(coverHtml.includes(`loading="${index<8?"eager":"lazy"}"`));
    assert.ok(coverHtml.includes('width="43" height="60" decoding="async"'));
    assert.ok(coverHtml.includes(`data-original-src="${cover}"`));
    assert.ok(coverHtml.includes("加载中"));
  }
  const placeholder={textContent:"加载中"}, classes=new Set();
  const fakeImage=Object.assign(eventTarget(),{dataset:{originalSrc:cover},src:ui.coverUrl(cover),complete:false,naturalWidth:0,removed:false,remove(){this.removed=true;},parentElement:{classList:{add:name=>classes.add(name)},querySelector:()=>placeholder}});
  ui.bindCoverImage(fakeImage);
  fakeImage.emit("error");
  assert.equal(fakeImage.src,cover,"a failed thumbnail must retry the original once");
  assert.equal(fakeImage.dataset.originalSrc,undefined);
  assert.equal(fakeImage.removed,false);
  fakeImage.emit("error");
  assert.equal(fakeImage.removed,true);
  assert.equal(placeholder.textContent,"封面不可用");
  const loadedImage=Object.assign(eventTarget(),{dataset:{},complete:true,naturalWidth:120,parentElement:fakeImage.parentElement});
  ui.bindCoverImage(loadedImage);
  assert.equal(classes.has("is-loaded"),true,"cached loaded covers must be displayed immediately");

  const reads=[];
  const readLoader=ui.createReadLoader((url,options)=>{const result=deferred();reads.push({url,options,...result});return result.promise;});
  const oldRead=readLoader.load("/old");
  assert.equal(oldRead,readLoader.load("/old"),"same parameters reuse the in-flight request");
  const newRead=readLoader.load("/new");
  assert.equal(reads[0].options.signal.aborted,true);
  reads[0].resolve({old:true});await oldRead;
  assert.equal(newRead,readLoader.load("/new"),"an older request settling must not clear the current request");
  readLoader.cancel();assert.equal(reads[1].options.signal.aborted,true);
  reads[1].resolve({new:true});await newRead;
  assert.match(ui.formatUTC("2026-11-12T01:00:00"), /09:00:00/);
  assert.match(ui.formatUTC("2026-11-12T01:00:00Z"), /09:00:00/);

  const row = {story_id:"123\" onclick=\"bad", story_name:"<img src=x onerror=alert(1)>", img_src:"javascript:bad()", is_show:1, lang_id:23};
  const html = ui.renderRow(row);
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes('src="javascript:'));
  assert.ok(html.includes("&lt;img"));
  assert.ok(html.includes("未知语言（23）"));
  const detail = ui.detailHtml({...row, introduce:"<script>alert(1)</script>", baidu_link:"<img src=x onerror=bad()>", baidu_link_code:"<b>oops</b>"});
  assert.ok(!detail.includes("<script>"));
  assert.ok(detail.includes("&lt;script&gt;"));
  const promotion = ui.promotionHtml({status:"succeeded", koc_code:"<script>bad</script>", deep_link:"javascript:bad()", home_link:"https://example.com/?a=%22", star_name:"<b>me</b>"},row,"me");
  assert.ok(!promotion.includes("<script>"));
  assert.ok(!promotion.includes("javascript:"));
  assert.ok(promotion.includes("&lt;b&gt;me&lt;/b&gt;"));
  assert.equal(ui.promotionButton({is_show:0}), "查看历史链接");
  assert.equal(ui.promotionButton({is_show:1,promotion_status:"succeeded"}), "查看推广链接");
  assert.equal(ui.promotionButton({is_show:1,promotion_status:"creating"}), "查看生成进度");
  assert.equal(ui.runModel({status:"queued"}).active,true);
  assert.ok(Number.isFinite(ui.runModel({status:"running",stage:"fetching",total_pages:0}).progress));
  assert.equal(ui.runModel({status:"failed",stage:"writing",total_rows:10,processed_rows:5}).active,false);
  assert.equal(ui.runModel({status:"succeeded",total_rows:100}).progress,100);
  assert.equal(ui.runModel({status:"running",stage:"verifying",total_rows:1200,verified:500}).detail,"已核对 500 / 1,200 条剧目");
  assert.equal(ui.runModel({status:"running",stage:"writing",total_rows:1200,processed_rows:0,compared_rows:1000}).detail,"已读取 1,000 条已有剧目，正在比对资料");
  assert.equal(ui.runModel({status:"running",stage:"writing",total_rows:1200,processed_rows:500,compared_rows:1000}).detail,"已处理 500 / 1,200 条剧目");
  assert.equal(ui.runModel({status:"running",stage:"writing",total_rows:1200,processed_rows:0}).detail,"已处理 0 / 1,200 条剧目");

  let calls = [], resolveCreate;
  const loader = ui.createPromotionLoader(async (url,options) => {
    calls.push([url,(options||{}).method||"GET"]);
    if (!options) return {promotion:null};
    return new Promise(resolve=>{resolveCreate=resolve;});
  });
  const first = loader({story_id:"one",is_show:1});
  const second = loader({story_id:"one",is_show:1});
  assert.equal(first,second,"double click must share the same in-flight request");
  await Promise.resolve();
  resolveCreate({promotion:{status:"succeeded",koc_code:"SAME"}});
  assert.equal((await first).promotion.koc_code,"SAME");
  assert.equal((await second).promotion.koc_code,"SAME");
  assert.deepEqual(calls.map(call=>call[1]),["GET","POST"]);

  for (const status of ["succeeded","creating","unknown"]) {
    calls=[];
    const cached = ui.createPromotionLoader(async (url,options)=>{calls.push(options);return {promotion:{status,koc_code:"KEEP"}};});
    const result = await cached({story_id:"cached",is_show:1},true);
    assert.equal(result.promotion.status,status);
    assert.equal(calls.length,1,"existing success/pending/unknown must never cause another POST");
    assert.equal(calls[0],undefined);
  }
  calls=[];
  const delisted=ui.createPromotionLoader(async (url,options)=>{calls.push(options);return {promotion:null};});
  await delisted({story_id:"offline",is_show:0});
  assert.equal(calls.length,1);
  assert.equal(calls[0],undefined,"delisted drama must only read historical links");

  calls=[];
  const retry=ui.createPromotionLoader(async (url,options)=>{calls.push((options||{}).method||"GET");return options?{promotion:{status:"succeeded"}}:{promotion:{status:"rejected"}};});
  await retry({story_id:"retry",is_show:1});
  assert.deepEqual(calls,["GET"],"opening a rejected result must not silently create again");
  calls=[];
  await retry({story_id:"retry",is_show:1},true);
  assert.deepEqual(calls,["GET","POST"],"explicit retry is only sent after checking current rejection");

  calls=[];
  const uncertain=ui.createPromotionLoader(async (url,options)=>{calls.push((options||{}).method||"GET");if(options)throw new Error("network interruption");return {promotion:null};});
  await assert.rejects(uncertain({story_id:"uncertain",is_show:1}),/network/);
  assert.deepEqual(calls,["GET","POST"],"uncertain creation must not automatically retry");
  await testLoadBehavior();
  console.log("Moboreels UI tests passed: thumbnails/fallback, parallel authorized startup, request cancellation/coalescing, polling visibility, local row updates, rendering safety and promotion idempotency.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
