// Exercise the actual browser script's async fetch/error/render contract.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

(async()=>{
  const cell={dataset:{xTagId:'1'},innerHTML:''},summary={innerHTML:''};
  let timer,cleared=0,mode='gateway_timeout',click;
  const context={window:{},document:{
    addEventListener:(event,handler)=>{if(event==='click')click=handler;},
    querySelector:()=>null,querySelectorAll:()=>[cell],getElementById:()=>summary,
  },AbortController,Intl,Date,setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{cleared++;},
    fetch:async(url,options)=>{
      assert.equal(options.credentials,'same-origin');assert.ok(options.signal);
      if(mode==='hang')return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('abort'),{name:'AbortError'}))));
      if(mode==='gateway_timeout')return {ok:false,status:504,json:async()=>{throw new SyntaxError('HTML is not JSON');}};
      return {ok:true,status:200,json:async()=>({items:{'1':{status:'complete',revenue_cents:12345,audiences:[{name:'女性',share_bp:10000}],combinations:[]}},meta:{start:'2026-07-23',end:'2026-11-09',source_generated_at:'2026-11-10T02:20:00Z'}})};
    }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/x-tag-revenue.js'),'utf8'),context);
  await context.window.XTagRevenue.refresh();
  assert.match(summary.innerHTML,/画像读取超时，请稍后重试/);assert.match(cell.innerHTML,/data-xtr-retry/);
  assert.doesNotMatch(cell.innerHTML,/HTML is not JSON|正在读取画像/);
  mode='hang';const pending=context.window.XTagRevenue.refresh();timer();await pending;
  assert.match(summary.innerHTML,/画像读取超时，请稍后重试/);
  mode='ok';click({target:{closest:()=>({hasAttribute:name=>name==='data-xtr-retry'})}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(cell.innerHTML,/女性/);assert.match(cell.innerHTML,/100\.00%/);assert.doesNotMatch(cell.innerHTML,/\$123\.45/);
  assert.doesNotMatch(cell.innerHTML,/data-xtr-retry|正在读取画像/);assert.equal(cleared,3);
  console.log('PASS: HTML 504, pending-request timeout, retry recovery, timer cleanup');
})().catch(error=>{console.error(error);process.exitCode=1;});
