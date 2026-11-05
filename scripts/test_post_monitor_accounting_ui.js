"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PIPELINE_PLAYWRIGHT_PACKAGE||'playwright');
const root=path.resolve(__dirname,'..'),output=process.env.PIPELINE_UI_OUTPUT||path.join(root,'output/playwright');
const sample={generated_at:'2026-11-05T03:00:00Z',collected_at:'2026-11-05T03:00:00Z',scope:'all',allowed_channels:['fb'],queue_state_version:1,totals:{waiting:0,executing:0,scheduled:0,abnormal:160},channels:[{channel:'fb',stage:'prepare',waiting:0,scheduled:0,executing:0,completed:0,abnormal:160,abnormal_reasons:{canonical_task_not_claimable:160},reason_labels:{canonical_task_not_claimable:'原任务领取被拒绝'}}],sources:[],backlog:{available:true,unfinished:198,instantiated_unfinished:191,unqueued:7,warnings:[],channels:[{channel:'fb',unfinished:198,instantiated_unfinished:191,unqueued:7,known_unqueued:7,scheduled:0,closed:3,closed_unqueued:2,states:{blocked:160,failed:11,unknown:20},reasons:[{code:'canonical_task_not_claimable',reason:'原任务领取被拒绝',count:160}],plan_sample:[{plan_key:'due:123',account_id:'<script>unsafe</script>',due_at:'2026-11-04T01:00:00Z'}]}]}};
const task={source_key:'fb:auto:1',id:1,source_id:1,channel:'fb',stage:'prepare',status:'failed',effective_state:'blocked',effective_reason:'原任务领取被拒绝',effective_code:'canonical_task_not_claimable',account_label:'test',owner_name:'test',created_at:'2026-11-04T01:00:00Z'};
sample.backlog.channels[0].plans_oldest_at='2026-09-11T20:07:00Z';
sample.backlog.channels[0].plans_latest_at='2026-09-14T09:14:00Z';
let releaseTasks;
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');const send=data=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
 if(url.pathname==='/api/ui/topbar')return send({authenticated:true,user:{is_admin:true,name:'test'}});
 if(url.pathname==='/api/navigation')return send({items:[]});
 if(url.pathname==='/api/pipeline-monitor/summary')return send(sample);
 if(url.pathname==='/api/pipeline-monitor/workers')return send({items:[],processes:[]});
 if(url.pathname==='/api/pipeline-monitor/tasks'){
  if(releaseTasks===undefined){releaseTasks=()=>send({items:[task],total:1});return;}
  return send({items:[task],total:1});
 }
 const file=path.join(root,'static',url.pathname.slice(1));
 if(!file.startsWith(path.join(root,'static')+path.sep)||!fs.existsSync(file)){res.writeHead(404);return res.end();}
 res.writeHead(200,{'Content-Type':{'.js':'text/javascript','.css':'text/css','.html':'text/html'}[path.extname(file)]||'text/plain'});res.end(fs.readFileSync(file));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({headless:true,executablePath:process.env.PIPELINE_CHROMIUM_EXECUTABLE});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
  await page.goto(origin+'/pipeline-monitor.html');await page.waitForFunction(()=>document.querySelector('#pmTotals').textContent.includes('160'));
  assert.equal(await page.locator('#pmBacklog').count(),0);
  assert.doesNotMatch(await page.locator('#pmContent').innerText(),/历史未完成与计划缺口/);
  assert.doesNotMatch(await page.locator('#pmTaskRows').innerText(),/原任务领取被拒绝/);
  releaseTasks();await page.waitForFunction(()=>document.querySelector('#pmTaskRows').textContent.includes('原任务领取被拒绝'));
  assert.match(await page.locator('#pmTaskRows').innerText(),/受阻[\s\S]*原状态 failed[\s\S]*原任务领取被拒绝/);
  assert.match(await page.locator('#pmChannelRows').innerText(),/领取被拒绝/);
  assert.match(await page.locator('#pmTotals').innerText(),/待执行\s*0/);
  fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'monitor.png'),fullPage:true});
  sample.backlog.unfinished=null;sample.backlog.unqueued=null;sample.backlog.channels[0].unqueued=null;
  sample.backlog.channels[0].unfinished=null;sample.backlog.warnings=['缺少冻结计划证据，不按零统计'];
  sample.totals.waiting=2;
  await page.locator('#pmRefresh').click();await page.waitForFunction(()=>document.querySelector('[data-stat-key="waiting"] .pm-stat-value').textContent==='2');
  assert.equal(await page.locator('#pmBacklog').count(),0);
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
  console.log('accounting browser: removed history panel, zero waiting, normalized status, independent summary rendering, refresh and mobile layout passed');
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
