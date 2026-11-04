// Render actual run rows and task rows with API-shaped fixtures; no X requests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag, options = {}) {
    this.tagName = tag;
    this.children = [];
    this.text = options.text == null ? "" : String(options.text);
    this.dataset = options.dataset || {};
    this.className = options.className || "";
  }
  appendChild(child) { this.children.push(child); return child; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.text = String(value); this.children = []; }
}
const elements = new Map();
const byId = id => {
  if (!elements.has(id)) elements.set(id, new Element("div"));
  return elements.get(id);
};
const ui = {
  byId, initShell: () => {},
  clear: node => { node.children = []; node.text = ""; },
  element: (tag, options) => new Element(tag, options),
  appendTextCell: (row, value) => row.appendChild(new Element("td", { text: value })),
  positiveId: value => Number(value) > 0 ? String(value) : "",
  objectValue: value => value && typeof value === "object" ? value : {},
  formatTime: value => value || "—", formatNumber: value => value == null ? "—" : String(value),
  statusKind: value => value,
  statusBadge: (label, kind) => new Element("span", { text: label, className: kind }),
};
const context = {
  window: { XAutoPublish: ui },
  document: { createTextNode: text => new Element("text", { text }) },
  URL, console,
};
let source = fs.readFileSync(path.join(__dirname, "../static/x-auto-publish-runs.js"), "utf8");
source = source.replace("  void ui.initShell({",
  "  window.testRuns = { state, renderRuns, renderRunTasks, renderEvents };\n  void ui.initShell({");
vm.runInNewContext(source, context);
const page = context.window.testRuns;
const emptyCounts = {
  published: 0, no_candidate: 0, skipped: 0, failed: 0, canceled: 0,
  unknown: 0, pending: 0, ready: 0, retry_wait: 0, waiting_result: 0, active: 0,
};
const run = {
  id: 446, template_name: "非会员号，消耗优先", template_version: 6,
  trigger_type: "auto", status: "running", task_count: 85,
  completed_task_count: 75, attention_task_count: 3, published_task_count: 65,
  task_counts: { ...emptyCounts, published: 65, no_candidate: 10, failed: 3, pending: 4, waiting_result: 3 },
};
page.state.runs = [run];
page.renderRuns();
let row = byId("runRows").children[0];
assert.equal(row.children[3].children[0].textContent, "已发布 65 / 85");
assert.equal(row.children[3].children[1].textContent, "7 个未结束");
assert.equal(row.children[4].textContent, "未结束");
assert.equal(row.children[5].textContent,
  "已发布 65；无符合条件素材 10；失败 3；待处理 4；等待发布结果 3");
assert.ok(!row.textContent.includes("75 / 85 完成"));

page.state.runs = [{ ...run, display_status: "needs_review" }];
page.renderRuns();
assert.equal(byId("runRows").children[0].children[4].textContent, "结果待确认");

// An old/incomplete response must never reinterpret completed=75 as posts=75.
page.state.runs = [{ ...run, task_counts: undefined, published_task_count: null }];
page.renderRuns();
row = byId("runRows").children[0];
assert.equal(row.children[3].children[0].textContent, "已发布 — / 85");
assert.ok(row.children[3].textContent.includes("已处理 75（含无素材或跳过）"));
assert.ok(!row.textContent.includes("已发布 75"));

page.renderRunTasks([
  { account_id: 79, status: "retry_wait", execution_queue_id: 5819, unknown_outcome: false },
  { account_id: 46, status: "retry_wait", execution_queue_id: 5393, unknown_outcome: true },
  { account_id: 45, status: "retry_wait", unknown_outcome: false },
]);
const taskRows = byId("runTaskRows").children;
assert.equal(taskRows[0].children[4].textContent, "等待发布结果");
assert.equal(taskRows[1].children[4].textContent, "结果待确认");
assert.equal(taskRows[2].children[4].textContent, "等待重试");
assert.equal(taskRows[1].children[4].children[0].className, "unknown");
page.renderEvents([{ event_type: "first" }, { event_type: "latest" }], {
  events_total: 29513, events_limit: 200, events_has_more: true,
});
assert.equal(byId("runEvents").children[0].textContent,
  "仅显示最近 2 条，共 29513 条；完整历史保留在审计记录中。");
assert.equal(byId("runEvents").children.length, 3);
page.renderEvents([{ event_type: "only" }], { events_total: 1, events_has_more: false });
assert.equal(byId("runEvents").children.length, 1);
assert.ok(!byId("runEvents").textContent.includes("仅显示"));
console.log("PASS: run publication counts, exclusive outcomes, unknown and queued-result rendering");
