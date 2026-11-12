"use strict";
const assert = require("node:assert/strict");
const ui = require("../static/moboreels-library.js");

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
  console.log("Moboreels UI tests passed: rendering safety, local time, progress, link reuse, concurrency and uncertain outcomes.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
