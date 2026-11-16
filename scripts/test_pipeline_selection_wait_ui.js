"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/pipeline-monitor.js"), "utf8");
const elements = new Map();
const document = { getElementById(id) { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); } };
const context = { document, location: { origin: "https://monitor.example" } };
vm.createContext(context);
vm.runInContext(source.slice(0, source.indexOf("  async function refresh()"))
  + "\nglobalThis.monitor = { columns: CHANNEL_COLUMNS, abnormalDetails, renderTasks };})();", context);
const row = { waiting: 79, waiting_selection: 79, abnormal: 0, abnormal_reasons: {}, reason_labels: {} };
const waiting = context.monitor.columns.find(column => column.key === "waiting").cell(row);
assert.match(waiting, /^79<small>等待选材 79<\/small>$/);
assert.equal(context.monitor.columns.find(column => column.key === "abnormal").cell(row), "0");
assert.equal(context.monitor.abnormalDetails({ abnormal_reasons: { held: 1 }, reason_labels: { held: "任务未完成，原错误码：held" } }), "<small>暂停或限制 1</small>");
context.monitor.renderTasks({ total: 1, items: [{ id: 1, channel: "x", stage: "prepare", status: "queued", effective_state: "waiting", effective_code: "", effective_reason: "等待选材，选材后进入统一调度制作" }] });
assert.match(elements.get("pmTaskRows").innerHTML, /待执行/);
assert.match(elements.get("pmTaskRows").innerHTML, /等待选材，选材后进入统一调度制作/);
assert.doesNotMatch(elements.get("pmTaskRows").innerHTML, /adapter_unavailable|原错误码|>held</);
console.log("Selection wait summary, real hold labels and task rendering passed.");
