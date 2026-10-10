"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const item = {id: 1, current_version: 6, status: "enabled", name: "Test <template>", config: {group_ids: [62], schedule: {mode: "fixed", times: ["12:00"]}}};
const flush = () => new Promise(resolve => setImmediate(resolve));
async function fixture(storage = new Map(), enabled = true) {
  const nodes = new Map();
  const html = fs.readFileSync(path.join(root, "static/fb-auto-publish-templates.html"), "utf8");
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const classes = new Set((match[0].match(/class="([^"]*)"/)?.[1] || "").split(/\s+/));
    nodes.set(match[1], {value: "", innerHTML: "", textContent: "", disabled: false, readOnly: false, events: {}, open: false,
      classList: {add(x) {classes.add(x);}, remove(x) {classes.delete(x);}, contains(x) {return classes.has(x);}, toggle(x, yes) {yes ? classes.add(x) : classes.delete(x);}},
      addEventListener(k, fn) {this.events[k] = fn;}, contains() {return true;}, focus() {},
      showModal() {this.open = true;}, close() {this.open = false;},
    });
  }
  const byId = id => {assert(nodes.has(id), id); return nodes.get(id);};
  const calls = [];
  let respond = async () => {throw new Error("network lost");};
  let ready, serial = 0;
  const localItem = {...item, status: enabled ? "enabled" : "disabled"};
  const ui = {API_BASE: "/api/admin/fb-auto-publish", byId,
    positiveId: v => Number.isInteger(Number(v)) && Number(v) > 0 ? Number(v) : 0,
    templateVersion: v => v.current_version, operationId: () => "operation-" + ++serial,
    escapeHtml: v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c])),
    showToast() {}, boot: ({onReady}) => (ready = onReady({user: {user_id: "tester"}})),
    api: async url => url.endsWith("/groups") ? {summary: {}} : {items: [localItem], total: 1},
  };
  const window = {FBAutoPublishUI: ui, setTimeout, clearTimeout,
    sessionStorage: {getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
  };
  const fetch = async (url, options) => {calls.push({url, payload: JSON.parse(options.body)}); return respond();};
  vm.runInNewContext(fs.readFileSync(path.join(root, "static/fb-auto-publish-templates.js"), "utf8"), {window, URLSearchParams, AbortController, fetch});
  await ready;
  const fire = async (id, event, extra = {}) => {byId(id).events[event]({preventDefault() {}, ...extra}); await flush();};
  const open = () => fire("templateRows", "click", {target: {closest: () => ({dataset: {templateId: "1", action: "material"}})}});
  return {byId, calls, fire, open, window, setResponse: fn => {respond = fn;}};
}
async function main() {
  const storage = new Map();
  const page = await fixture(storage);
  assert(page.byId("templateRows").innerHTML.includes("爆款素材发布"));
  await page.open();
  assert(page.byId("materialDialog").open);
  await page.fire("materialForm", "submit");
  assert.equal(page.calls.length, 0);
  page.byId("hitMaterialId").value = "1,2";
  await page.fire("materialForm", "submit");
  assert.equal(page.calls.length, 0);
  page.byId("hitMaterialId").value = "7360836";
  await page.fire("materialForm", "submit");
  assert.equal(page.calls.length, 1);
  assert(page.byId("hitMaterialId").readOnly);
  assert(page.byId("materialStatus").textContent.includes("尚未确认"));
  const original = page.calls[0].payload;
  const refreshed = await fixture(storage);
  await refreshed.open();
  assert.equal(refreshed.byId("hitMaterialId").value, "7360836");
  let resolve;
  refreshed.setResponse(() => new Promise(done => {resolve = done;}));
  await refreshed.fire("materialForm", "submit");
  await refreshed.fire("materialForm", "submit");
  assert.equal(refreshed.calls.length, 1);
  assert.deepEqual(refreshed.calls[0].payload, original);
  assert(refreshed.byId("materialClose").disabled);
  resolve({ok: true, status: 202, json: async () => ({ok: true, run_id: 88, queued: 2, skipped: 1, skipped_pages: [{page_id: "<script>", reason: "page_paused"}]})});
  await flush(); await flush();
  assert(refreshed.byId("materialStatus").textContent.includes("2 个 Page 已加入发布队列"));
  assert(refreshed.byId("materialSkippedRows").innerHTML.includes("&lt;script&gt;"));
  assert(refreshed.byId("materialSkippedRows").innerHTML.includes("Page 已暂停"));
  assert(refreshed.byId("materialRunLink").href.endsWith("run_id=88"));
  await refreshed.fire("materialForm", "submit");
  assert.equal(refreshed.calls.length, 1);
  await refreshed.fire("materialNew", "click");
  assert.equal(refreshed.byId("hitMaterialId").value, "");
  assert(!refreshed.byId("hitMaterialId").readOnly);
  refreshed.byId("hitMaterialId").value = "123";
  refreshed.setResponse(async () => ({ok: false, status: 409, json: async () => ({code: "fb_auto_version_conflict", message: "模板已更新，请刷新列表"})}));
  await refreshed.fire("materialForm", "submit");
  assert(!refreshed.byId("hitMaterialId").readOnly);
  assert.equal(storage.size, 0);
  assert(refreshed.byId("materialStatus").textContent.includes("模板已更新"));
  refreshed.setResponse(async () => ({ok: false, status: 400, json: async () => ({code: "invalid_request", message: "Audit failure after accepted request"})}));
  await refreshed.fire("materialForm", "submit");
  assert.equal(storage.size, 1, "Proxy 400 after sidecar commit must retain the operation ID");
  const auditRequest = refreshed.calls.at(-1).payload;
  await refreshed.fire("materialForm", "submit");
  assert.deepEqual(refreshed.calls.at(-1).payload, auditRequest);
  refreshed.setResponse(async () => ({ok: false, status: 409, json: async () => ({code: "fb_auto_material_probe_failed", message: "素材时长探测失败"})}));
  await refreshed.fire("materialForm", "submit");
  assert(!refreshed.byId("hitMaterialId").readOnly);
  assert.equal(storage.size, 0);
  refreshed.setResponse(async () => ({ok: false, status: 503, json: async () => ({code: "fb_auto_material_probe_failed", message: "素材时长探测超时，尚未创建运行"})}));
  await refreshed.fire("materialForm", "submit");
  assert(!refreshed.byId("hitMaterialId").readOnly);
  const disabled = await fixture(new Map(), false);
  await disabled.open();
  assert(!disabled.byId("materialDialog").open);
  const brokenStorage = await fixture();
  await brokenStorage.open();
  brokenStorage.window.sessionStorage.setItem = () => {throw new Error("disabled storage");};
  brokenStorage.byId("hitMaterialId").value = "123";
  await brokenStorage.fire("materialForm", "submit");
  assert.equal(brokenStorage.calls.length, 0);
  console.log("FB hit-material UI passed: validation, disabled template, storage fence, double-click, lost-response reload, same-key retry, safe skipped rows, result and new round");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
