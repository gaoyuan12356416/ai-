// Pure rendering checks: no browser, network, credentials or production writes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const DramaJobRuntime = require("../static/drama-job-runtime.js");

const root = path.resolve(__dirname, "..");
let checks = 0;
for (const name of ["index.html", "drama-synthesis.html"]) {
  const text = fs.readFileSync(path.join(root, "static", name), "utf8");
  for (const script of text.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
    new vm.Script(script[1], { filename: name });
  }
  const row = text.match(/function buildJobRow\(job\) \{[\s\S]*?\n    \}/)[0];
  const materials = text.match(/function selectableMaterials\(job, includeCover\) \{[\s\S]*?\n    \}/)[0];
  assert.ok(row.includes("</tr>"));
  const context = vm.createContext({
    DramaJobRuntime,
    state: { selectedJobIds: new Set() },
    formatJobElapsed: () => "1秒", formatRange: () => "1-3",
    statusClass: () => "done", previewCell: () => "",
    formatBeijingTime: () => "2026-08-27",
  });
  vm.runInContext(materials + "\n" + row, context);
  const base = { job_id: "a".repeat(32), app_id: "1479", status: "done", progress: 100 };
  const render = extra => context.buildJobRow({ ...base, ...extra });
  const actions = ["copyMaterialUrl", "createShortLink", "openYoutubePublish"];
  for (const field of ["output_video_url", "output_video_no_bgm_url", "output_random_template_url"]) {
    const html = render({ [field]: "https://example.test/video.mp4" });
    for (const action of actions) {
      assert.ok(html.includes(`event.stopPropagation(); ${action}('${base.job_id}')`));
    }
    assert.ok(html.includes('class="actions-inline"'));
    assert.equal((html.match(/>生成短链<|>发布到 YouTube<|>复制素材 URL</g) || []).length, 3);
    checks++;
  }
  const cover = render({ cover_16x9_url: "https://example.test/cover.jpg" });
  assert.ok(cover.includes("copyMaterialUrl("));
  assert.ok(!cover.includes("createShortLink("));
  assert.ok(!cover.includes("openYoutubePublish("));
  assert.match(cover, /disabled>生成短链<\/button>/);
  assert.match(cover, /disabled>发布到 YouTube<\/button>/);
  checks++;
  for (const status of ["queued", "rendering", "failed"]) {
    const html = render({ status, output_video_url: "https://example.test/video.mp4" });
    actions.forEach(action => assert.ok(!html.includes(action + "(")));
    assert.ok(html.includes("viewJob("));
    assert.ok(html.includes("downloadAll("));
    checks++;
  }
  assert.ok(text.includes("正在读取 CPU 模板目录…"));
  assert.ok(!text.includes("正在读取香港 GPU 模板目录…"));
  assert.ok(text.includes("els.jobDetailActions.innerHTML = job.status ==="));
  checks++;
}
async function checkRuntime() {
  const now = Date.parse("2026-08-28T08:05:00Z");
  const job = {job_id:"test", status:"rendering", created_at:"2026-08-28 04:00:00", elapsed_seconds:1};
  assert.equal(DramaJobRuntime.elapsed(job, now), "4小时5分0秒");
  assert.equal(DramaJobRuntime.elapsed({...job, remote_runtime:{first_started_at:"2026-08-28T03:00:00Z"}, active_started_at:"2026-08-28 08:00:00"}, now), "5小时5分0秒");
  checks += 2;
  const done = {...job, status:"done", active_finished_at:"2026-08-28 08:00:00", updated_at:"2026-08-28 09:00:00"};
  assert.equal(DramaJobRuntime.elapsed(done, now + 86400000), "4小时0分0秒");
  assert.equal(DramaJobRuntime.date("2026-08-28T12:00:00+08:00").getTime(), DramaJobRuntime.date(job.created_at).getTime());
  checks += 2;
  const unknown = DramaJobRuntime.progressHtml({...job, remote_progress:{stage_label:"拼接全集", stage_percent:null, detail:"正在拼接"}, cover_16x9_url:"https://example.test/cover"});
  assert.ok(!unknown.includes('class="progress"'));
  assert.ok(unknown.includes("封面已完成"));
  const escaped = DramaJobRuntime.progressHtml({...job, remote_progress:{stage_label:"<script>", stage_percent:30, detail:'<img src=x onerror="bad()">'}});
  assert.ok(!escaped.includes("<script>"));
  assert.ok(!escaped.includes("<img"));
  checks += 2;

  let tick, clock=now, reads=0, allowed=true;
  class FakeDate extends Date {static now(){return clock;}}
  const context = vm.createContext({module:{exports:{}}, Date:FakeDate,
    setInterval:fn=>{tick=fn;return 1;}, clearInterval:()=>{}});
  vm.runInContext(fs.readFileSync(path.join(root,"static/drama-job-runtime.js"),"utf8"),context);
  const elapsedCell = {textContent:"", getAttribute:()=>job.job_id};
  const doc = {visibilityState:"visible", querySelectorAll:()=>[elapsedCell]};
  const runtime = context.module.exports;
  const dispose = runtime.install({getJobs:()=>[job],refresh:async()=>{reads++;},canRead:()=>allowed,document:doc});
  await tick();
  assert.equal(reads,1);
  assert.equal(elapsedCell.textContent,"4小时5分0秒");
  clock+=1000; await tick(); assert.equal(reads,1);
  clock+=10000; await tick(); assert.equal(reads,2);
  checks += 2;
  doc.visibilityState="hidden";clock+=10000;await tick();assert.equal(reads,2);
  doc.visibilityState="visible";allowed=false;await tick();assert.equal(reads,2);
  checks++;
  dispose();
}
checkRuntime().then(()=>console.log(JSON.stringify({ ok: true, checks, pages: 2, browser_calls: 0, network_calls: 0 })))
  .catch(error=>{console.error(error);process.exitCode=1;});
