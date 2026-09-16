"use strict";

// Exercise the real page functions with an isolated DOM and fake API responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static/fb-post-ad-delete.js"), "utf8");
const page = fs.readFileSync(path.join(root, "static/fb-post-ad-delete.html"), "utf8");

function harness(respond = () => { throw new Error("Unexpected API call"); }) {
  const elements = new Map();
  const phaseInputs = ["creative", "ad", "video"].map(value => ({ value, checked: value === "video" }));
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", innerHTML: "", disabled: false, open: false, style: {},
      classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, removeAttribute() {},
      showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
    });
    return elements.get(id);
  }
  const requests = [];
  const storage = new Map();
  const context = vm.createContext({
    document: { getElementById: element, querySelectorAll: selector => selector === "[data-phase]" ? phaseInputs : [], addEventListener() {} },
    window: { crypto: { randomUUID: () => "12345678-1234-4234-8234-123456789012" }, addEventListener() {} },
    location: { origin: "https://unit.invalid" }, AbortController, URLSearchParams, URL,
    sessionStorage: { setItem: (key, value) => storage.set(key, value), getItem: key => storage.get(key), removeItem: key => storage.delete(key) },
    setTimeout: () => 1, clearTimeout() {},
    fetch: async (url, options) => {
      requests.push({ url, options });
      const result = await respond(url, options);
      return { ok: true, status: 200, text: async () => JSON.stringify(result) };
    },
  });
  const hook = "globalThis.ui = { state, diagnosticDetails, videoAccountResults, videoAccountDetails, renderObjects, renderJob, openConfirmation, confirmExecution, sendExecution, canExecute, canRecheck, selectedEligible };";
  const marker = '  document.addEventListener("DOMContentLoaded", init);';
  assert.ok(source.includes(marker));
  vm.runInContext(source.replace(marker, hook), context);
  return { ui: context.ui, element, requests, phaseInputs };
}

function pair(account_id, status, result = {}) {
  return { account_id, status, result: { delete_mode: "ad_account_video", ...result } };
}
function video(overrides = {}) {
  return { kind: "video", key: "video:301", object_id: "301", status: "failed", account_ids: ["101", "102"], ...overrides };
}
function job(overrides = {}) {
  return {
    job_id: "job1", preview_id: "preview1", status: "partial", page: 1, total: 1, products: [], dramas: [], blockers: [],
    objects: [video()], summary: { total: 1, failed: 1 }, phase_results: { video: { total: 1, failed: 1 } }, ...overrides,
  };
}
function authorize(ui, data) {
  Object.assign(ui.state, { job: data, activeJobId: data.job_id, permitted: true, productsLoaded: true, formGeneration: 0, boundGeneration: 0 });
}

test("current account progress takes precedence over an older aggregate result", () => {
  const { ui } = harness();
  const rows = ui.videoAccountResults(video({
    result: { account_results: [pair("101", "failed"), pair("102", "pending")] },
    video_account_results: [pair("101", "deleted"), pair("102", "in_progress")],
  }));
  assert.deepEqual(JSON.parse(JSON.stringify(rows)).map(row => row.status), ["deleted", "in_progress"]);
});

test("saved account results support empty current progress and are deduplicated by account", () => {
  const { ui } = harness();
  const item = video({ video_account_results: [], result: { account_results: [null, pair("101", "pending"), pair("101", "deleted"), pair("102", "failed")] } });
  const original = JSON.stringify(item);
  const rows = ui.videoAccountResults(item);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].status, "deleted");
  assert.equal(JSON.stringify(item), original);
  assert.equal(ui.videoAccountResults({ kind: "ad", video_account_results: [pair("101", "deleted")] }).length, 0);
});

test("one Video remains one table row while each account has its own status", () => {
  const { ui, element } = harness();
  ui.renderObjects(job({ objects: [video({ result: { delete_mode: "ad_account_video", account_results: [pair("101", "deleted"), pair("102", "failed", { message: "No permission" })] } })] }));
  const html = element("detailBody").innerHTML;
  assert.equal((html.match(/data-object-key="video:301"/g) || []).length, 1);
  assert.ok(html.includes("账户素材已删除"));
  assert.ok(html.includes("账户 101") && html.includes("账户 102"));
  assert.ok(html.includes("已删除 1 / 2") && html.includes("失败 1"));
  assert.ok(html.includes("此账户与 Video 组合已成功，后续执行跳过"));
  assert.ok(html.includes("No permission"));
  assert.ok(element("detailCount").textContent.startsWith("共 1 个对象"));
});

test("pending, active, blocked and uncertain pair outcomes remain distinct", () => {
  const { ui } = harness();
  const html = ui.videoAccountDetails([pair("101", "pending"), pair("102", "in_progress"), pair("103", "unknown"), pair("104", "blocked")]);
  for (const phrase of ["已删除 0 / 4", "待执行 1", "执行中 1", "结果待核实 1", "已阻止 1"]) assert.ok(html.includes(phrase));
  assert.ok(!html.includes("此账户与 Video 组合已成功"));
});

test("pair diagnostics show the account endpoint and actual delivery User identity", () => {
  const { ui } = harness();
  const html = ui.diagnosticDetails(pair("101", "failed", {
    delete_account_id: "101", delete_endpoint: "/act_101/advideos?video_id=301", credential_kind: "user", credential_user_id: "803", credential_relation: "ad_source_user",
    detail: { code: 200, error_subcode: 10, error_user_title: "Permission", error_user_msg: "Review account", fbtrace_id: "trace1" },
  }));
  for (const phrase of ["广告账户中的视频素材", "/act_101/advideos?video_id=301", "本条记录的请求身份：用户身份", "投放记录用户", "内部用户 ID", "803", "200 / 10", "Review account", "trace1"]) assert.ok(html.includes(phrase));
});

test("fallback user labels and neutral request identity survive failed and unknown results", () => {
  const { ui } = harness();
  for (const relation of ["configured_user", "frozen_user_fallback", "fallback"]) {
    const html = ui.diagnosticDetails(pair("101", "unknown", { credential_kind: "user", credential_relation: relation }));
    assert.ok(html.includes("原冻结候选用户") && html.includes("本条记录的请求身份"));
  }
});

test("all account messages and safe identity fields are escaped while raw credential fields are ignored", () => {
  const { ui } = harness();
  const attack = '<img src=x onerror="alert(1)">';
  const html = ui.videoAccountDetails([pair(attack, "failed", {
    message: attack, credential_kind: "user", credential_user_id: attack, access_token: "NEVER_RENDER", token: "NEVER_RENDER",
    detail: { error_user_title: attack, error_user_msg: attack, page_access_token: "NEVER_RENDER" },
  })]);
  assert.ok(html.includes("&lt;img"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("NEVER_RENDER"));
});

test("request paths cannot render URLs or credential query strings", () => {
  const { ui } = harness();
  for (const endpoint of ["https://example.invalid/NEVER_RENDER", "/act_101/advideos?access_token=NEVER_RENDER"]) {
    assert.ok(!ui.diagnosticDetails(pair("101", "failed", { delete_endpoint: endpoint })).includes("NEVER_RENDER"));
  }
});

test("legacy Page results retain their historical mode and distinct Page and user IDs", () => {
  const { ui, element } = harness();
  ui.renderObjects(job({ objects: [video({ status: "deleted", result: { delete_mode: "video_id_direct", credential_kind: "page", credential_page_id: "901", credential_fb_user_id: "902", credential_user_id: "803", credential_relation: "creative_page" } })] }));
  const html = element("detailBody").innerHTML;
  for (const phrase of ["历史记录 · Video ID 直删", "Page 身份", "Meta 用户 ID", "内部用户 ID", "不代表视频真实所有者"]) assert.ok(html.includes(phrase));
  assert.ok(!html.includes("账户素材已删除"));
});

test("historical Video outcomes without mode are not relabelled as account deletion", () => {
  const { ui, element } = harness();
  ui.renderObjects(job({ objects: [video({ status: "deleted", result: { success: true } })] }));
  const html = element("detailBody").innerHTML;
  assert.ok(html.includes("历史 Video 结果") && html.includes("删除成功"));
  assert.ok(!html.includes("账户素材已删除"));
});

test("current pair progress precedes preserved historical Page request diagnostics", () => {
  const { ui, element } = harness();
  ui.renderObjects(job({ objects: [video({ result: { delete_mode: "video_id_direct", credential_kind: "page", credential_page_id: "901" }, video_account_results: [pair("101", "in_progress")] })] }));
  const html = element("detailBody").innerHTML;
  assert.ok(html.indexOf("广告账户结果") < html.indexOf("查看历史 Video ID 请求记录"));
  assert.ok(html.includes("Page 身份") && html.includes("执行中 1"));
});

test("successful pairs do not expand the parent Video execution count", () => {
  const { ui } = harness();
  const data = job({ objects: [video({ result: { account_results: [pair("101", "deleted"), pair("102", "failed")] } })] });
  authorize(ui, data);
  assert.equal(ui.selectedEligible(), 1);
  assert.equal(ui.canExecute(), true);
  assert.equal(ui.canRecheck(), false);
});

test("ordinary confirmation submits only frozen preview, phases and the request ID", async () => {
  const data = job();
  const h = harness((url, options) => options.method === "POST" ? { job_id: data.job_id, run_id: "run1" } : url.endsWith("/jobs") ? { items: [] } : data);
  authorize(h.ui, data);
  await h.ui.openConfirmation();
  assert.equal(h.element("confirmDialog").open, true);
  const help = h.element("confirmExecutionHelp").textContent;
  for (const phrase of ["Video 数量按 ID 去重", "关联广告账户中的视频素材", "单账户失败后继续", "成功的账户与 Video 组合跳过"]) assert.ok(help.includes(phrase));
  await h.ui.confirmExecution();
  const writes = h.requests.filter(row => row.options.method === "POST");
  assert.equal(writes.length, 1);
  assert.ok(writes[0].url.endsWith("/jobs/job1/execute"));
  assert.deepEqual(JSON.parse(writes[0].options.body), { preview_id: "preview1", phases: ["video"], request_id: "12345678-1234-4234-8234-123456789012" });
});

test("HTML keeps the ordinary three phases and the retired navigation version", () => {
  assert.equal((page.match(/type="checkbox"/g) || []).length, 3);
  assert.ok(page.includes("quick-nav.js?v=20260915retired"));
  assert.ok(page.includes("fb-post-ad-delete.css?v=20260915-meta-video-account-delete"));
  assert.ok(page.includes("fb-post-ad-delete.js?v=20260916-meta-video-token-routing"));
  assert.ok(!page.includes("Video 阶段删除视频对象"));
  assert.ok(page.includes("广告账户视频素材"));
});

test("queue rules distinguish publishing users from actual default token owners", () => {
  const { ui } = harness();
  for (const [flag, relation, label] of [["1", "product_default_user", "产品默认 Token"], ["-1", "publish_queue_user", "发布用户自己的 Token"]]) {
    const html = ui.diagnosticDetails(pair("101", "failed", { credential_kind: "user", credential_user_id: "999",
      credential_source_user_id: "803", credential_product_id: "456", credential_publish_queue_id: "500",
      credential_default_token: flag, credential_relation: relation, token: "never-render-this" }));
    for (const phrase of [label, "发布队列 ID", "500", "Token 所属产品 ID", "456", "源广告发布用户 ID", "803", "999", "使用默认 Token"]) assert.ok(html.includes(phrase));
    assert.ok(html.includes(flag === "1" ? "<dd>是</dd>" : "<dd>否</dd>"));
    assert.ok(!html.includes("never-render-this"));
  }
});

test("failed selection details retain the intended queue identity", () => {
  const { ui } = harness();
  const html = ui.diagnosticDetails(pair("101", "failed", { detail: { credential_relation: "product_default_user",
    credential_publish_queue_id: '<img src=x>', credential_default_token: "1", credential_user_id: "999" } }));
  assert.ok(html.includes("产品默认 Token") && html.includes("999"));
  assert.ok(html.includes("&lt;img src=x&gt;") && !html.includes("<img"));
});
