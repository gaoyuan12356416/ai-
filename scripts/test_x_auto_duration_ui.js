// Exercise the real editor payload and save path with an isolated API stub.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const fields = new Map();
const field = id => {
  if (!fields.has(id)) fields.set(id, { value: "", checked: false });
  return fields.get(id);
};
const requests = [];
const timers = new Map();
let timerId = 0;
const ui = {
  pageIdFromQuery: () => "9", byId: field, initShell: () => {},
  setText: (node, value) => { node.textContent = value; }, showToast: () => {},
  API_BASE: "/api/admin/x-auto-posts",
  api: async (url, options) => { requests.push({ url, options }); return { item: { id: 9 } }; },
  readItem: result => result.item,
};
const context = {
  window: { XAutoPublish: ui, location: { href: "" } },
  document: { getElementById: field }, console, AbortController,
  setTimeout: (callback, ms) => { assert.equal(ms, 30000); timers.set(++timerId, callback); return timerId; },
  clearTimeout: id => timers.delete(id),
};
context.location = context.window.location;
let source = fs.readFileSync(path.join(__dirname, "../static/x-auto-publish-template.js"), "utf8");
source = source.replace("  void ui.initShell({", "  window.testEditor = { state, buildPayload, saveTemplate };\n  void ui.initShell({");
vm.runInNewContext(source, context);
const editor = context.window.testEditor;
editor.state.selectedAccountIds.add("101");
editor.state.version = 6;
Object.entries({
  templateName: "Duration regression", bodyTemplate: "{{drama_name}}\n{{desc}}",
  metricWindowDays: "1", dramaLaunchWindowDays: "0", cooldownDays: "0",
  materialDurationMin: "60", materialDurationMax: "", randomDailyCount: "2", platform: "0",
  dramaSortBy: "spend", dramaSortDirection: "desc", materialSortBy: "spend", materialSortDirection: "desc",
}).forEach(([id, value]) => { field(id).value = value; });
field("scheduleModeRandom").checked = true;
assert.equal(editor.buildPayload().material_rule.duration_max_seconds, null);
for (const value of ["601", "1800", "14400", "86400"]) {
  field("materialDurationMax").value = value;
  assert.equal(editor.buildPayload().material_rule.duration_max_seconds, Number(value));
}
for (const value of ["0", "-1", "1.5", "NaN", "59", "9007199254740992"]) {
  field("materialDurationMax").value = value;
  assert.throws(() => editor.buildPayload());
}
(async () => {
  field("materialDurationMax").value = "";
  await editor.saveTemplate({ preventDefault() {} });
  assert.equal(requests.length, 1);
  const saved = JSON.parse(requests[0].options.body);
  assert.equal(saved.material_rule.duration_min_seconds, 60);
  assert.equal(saved.material_rule.duration_max_seconds, null);
  assert.equal(saved.expected_version, 6);
  assert.equal(context.window.location.href, "/x-auto-publish-templates.html");
  assert.equal(timers.size, 0);
  context.location.href = "";
  editor.state.dirty = true;
  ui.api = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
  const pendingSave = editor.saveTemplate({ preventDefault() {} });
  assert.equal(field("saveTemplate").disabled, true);
  assert.equal(editor.state.busy, true);
  timers.values().next().value();
  await pendingSave;
  assert.equal(field("saveTemplate").disabled, false);
  assert.equal(editor.state.busy, false);
  assert.equal(editor.state.dirty, true);
  assert.equal(field("templateName").value, "Duration regression");
  assert.match(field("formStatus").textContent, /结果尚未确认/);
  assert.equal(context.location.href, "");
  assert.equal(timers.size, 0);
  ui.api = async () => { throw new Error("template version changed"); };
  await editor.saveTemplate({ preventDefault() {} });
  assert.equal(field("formStatus").textContent, "template version changed");
  assert.equal(field("saveTemplate").disabled, false);
  assert.equal(timers.size, 0);
  console.log("PASS: bounds, successful save, timeout, retained draft and request error");
})().catch(error => { console.error(error); process.exitCode = 1; });
