// Pure rendering checks: no browser, network, credentials or production writes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

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
    state: { selectedJobIds: new Set() },
    formatJobElapsed: () => "1秒", formatRange: () => "1-3",
    statusClass: () => "done", previewCell: () => "",
    formatBeijingTime: () => "2026-08-27",
  });
  vm.runInContext(materials + "\n" + row, context);
  const preview = text.match(/function previewCell\(url, type, label\) \{[\s\S]*?\n    \}/)[0];
  vm.runInContext(preview, context);
  const base = { job_id: "a".repeat(32), app_id: "1479", status: "done", progress: 100 };
  const render = extra => context.buildJobRow({ ...base, ...extra });
  const outputFields = [
    ["concat_video", "output_video_url"],
    ["no_bgm_video", "output_video_no_bgm_url"],
    ["random_template_video", "output_random_template_url"],
    ["cover_16x9", "cover_16x9_url"],
  ];
  const detailResults = text.match(/const detailResultCards = (\[[\s\S]*?\]);/)[1] + '.join("")';
  const resultCard = text.match(/function resultCard\(title, url, type\) \{[\s\S]*?\n    \}/)[0];
  vm.runInContext(resultCard, context);
  for (let mask = 0; mask < 16; mask++) {
    for (const ready of [false, true]) {
      const outputs = Object.fromEntries(outputFields.map(([key], i) => [key, !!(mask & (1 << i))]));
      const urls = Object.fromEntries(outputFields.map(([, field]) => [field, ready ? `https://example.test/${field}` : ""]));
      const job = { ...base, ...urls, outputs };
      const html = render(job).match(/<div class="preview-grid">([\s\S]*?)<\/td>/)[1];
      const count = Object.values(outputs).filter(Boolean).length;
      assert.equal((html.match(/class="preview-card/g) || []).length, count);
      assert.equal((html.match(/待生成/g) || []).length, ready ? 0 : count);
      context.job = job;
      const detail = vm.runInContext(detailResults, context);
      assert.equal((detail.match(/class="card"/g) || []).length, count);
      for (const [key, field] of outputFields) {
        if (ready) {
          assert.equal(html.includes(urls[field]), outputs[key]);
          assert.equal(detail.includes(urls[field]), outputs[key]);
        }
      }
      checks++;
    }
  }
  for (const outputs of [undefined, null, {}]) {
    assert.ok(!render({ outputs }).includes('class="preview-card'));
    checks++;
  }
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
console.log(JSON.stringify({ ok: true, checks, pages: 2, browser_calls: 0, network_calls: 0 }));
