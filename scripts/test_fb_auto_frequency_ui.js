"use strict";

// Execute the real page scripts with non-empty API DTOs and a small DOM fixture.
// Optional argument: a local templates API response or template-detail JSON.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const fixedTemplate = {
  id: 1, current_version: 6, status: "enabled", name: "Frequency test <safe>",
  config: {
    name: "Frequency test <safe>", product: "Dramawave", group_ids: [62],
    video_template: "random_overlay", message_template: "{{drama_name}} {{url}}",
    schedule: {mode: "fixed", times: ["09:15", "12:15", "15:15", "18:15", "21:15"]},
    stagger_minutes: 40, drama_cooldown_hours: 12, default_daily_count: 0,
    page_daily_limits: [
      ...Array.from({length: 144}, (_, i) => ({page_id: String(1000 + i), daily_count: 2, tier: "A"})),
      {page_id: "2000", daily_count: 0, tier: "hold"},
    ],
  },
};
const copy = value => JSON.parse(JSON.stringify(value));
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function page(kind, item) {
  const base = "fb-auto-publish-" + kind;
  const html = fs.readFileSync(path.join(root, "static", base + ".html"), "utf8");
  const nodes = new Map();
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const classes = new Set((match[0].match(/class="([^"]*)"/)?.[1] || "").split(/\s+/));
    nodes.set(match[1], {
      value: match[0].match(/\bvalue="([^"]*)"/)?.[1] || "", checked: false,
      disabled: false, innerHTML: "", textContent: "", events: {},
      classList: {toggle(name, enabled) {enabled ? classes.add(name) : classes.delete(name);}, contains(name) {return classes.has(name);}},
      addEventListener(name, fn) {this.events[name] = fn;},
    });
  }
  const byId = id => {assert(nodes.has(id), "missing HTML element: " + id); return nodes.get(id);};
  const posts = [];
  const toasts = [];
  const reads = [];
  const document = {title: "", querySelectorAll() {return (item.config.group_ids || []).map(id => ({value: String(id)}));}};
  let ready;
  const ui = {
    API_BASE: "/api/admin/fb-auto-publish", byId, escapeHtml,
    positiveId: value => Number(value) > 0 ? Number(value) : 0,
    templateVersion: value => Number(value.current_version || value.version),
    readItem: data => data.item || data.template,
    showToast: (message, error) => toasts.push({message, error}),
    boot: ({onReady}) => (ready = onReady()),
    async api(url, options = {}) {
      if (options.method === "POST") {posts.push(JSON.parse(options.body)); return {item};}
      reads.push(url);
      if (url.endsWith("/groups")) return {
        summary: {total_groups: 1, total_pages: 145, publishable_pages: 145, missing_token_pages: 0},
        items: [{group_id: 62, name: "Test pool", publishable_pages: 145, total_pages: 145}],
      };
      if (url.includes("/templates?")) return {items: [item], total: 1};
      if (url.endsWith("/templates/1")) return {item};
      throw new Error("unexpected API read: " + url);
    },
  };
  const window = {FBAutoPublishUI: ui, location: {search: "?id=1", href: ""}};
  vm.runInNewContext(fs.readFileSync(path.join(root, "static", base + ".js"), "utf8"), {window, document, URLSearchParams, console});
  await ready;
  assert(!toasts.some(toast => toast.error), JSON.stringify(toasts));
  const fire = async (id, event) => {
    byId(id).events[event]({preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
  };
  return {byId, posts, reads, fire, window};
}

async function main() {
  const fixed = await page("templates", copy(fixedTemplate));
  const fixedRow = fixed.byId("templateRows").innerHTML;
  assert(fixedRow.includes("每个启用 Page 每天上限 2 次"));
  assert(fixedRow.includes("固定 5 个候选时段"));
  assert(fixedRow.includes("144 个 Page 安排发布、1 个暂停"));
  assert(fixedRow.includes("合计上限 288 条/日"));
  assert(fixedRow.includes("&lt;safe&gt;"));
  assert(fixed.byId("pageInfo").textContent.includes("共 1 个模板"));
  fixed.byId("filterQuery").value = "Frequency test";
  fixed.byId("filterStatus").value = "enabled";
  await fixed.fire("templateFilters", "submit");
  assert(fixed.reads.at(-1).includes("q=Frequency+test&status=enabled"));
  await fixed.fire("reloadTemplates", "click");
  assert.equal(fixed.byId("templateRows").innerHTML, fixedRow);

  const randomTemplate = copy(fixedTemplate);
  randomTemplate.config.schedule = {mode: "random", daily_count: 2, start: "09:15", end: "21:55"};
  randomTemplate.config.stagger_minutes = 0;
  const random = await page("templates", randomTemplate);
  const randomRow = random.byId("templateRows").innerHTML;
  assert(randomRow.includes("每个启用 Page 每天上限 2 次"));
  assert(randomRow.includes("每日随机 2 个候选时刻：09:15–21:55"));
  assert(randomRow.includes("合计上限 288 条/日"));
  assert(!randomRow.includes("固定 5") && !randomRow.includes("错峰"));

  const mixedTemplate = copy(fixedTemplate);
  mixedTemplate.config.page_daily_limits = [{page_id: "1", daily_count: 2}, {page_id: "2", daily_count: 3}, {page_id: "3", daily_count: 0, tier: "hold"}];
  const mixedRow = (await page("templates", mixedTemplate)).byId("templateRows").innerHTML;
  assert(mixedRow.includes("每天上限 2–3 次（按 Page 配置）"));
  assert(mixedRow.includes("合计上限 5 条/日"));
  mixedTemplate.config.page_daily_limits = [{page_id: "3", daily_count: 0, tier: "hold"}];
  assert((await page("templates", mixedTemplate)).byId("templateRows").innerHTML.includes("所有 Page 每天上限 0 次（暂停）"));
  delete mixedTemplate.config.page_daily_limits;
  mixedTemplate.config.default_daily_count = 2;
  assert((await page("templates", mixedTemplate)).byId("templateRows").innerHTML.includes("未列 Page 每天上限 2 次"));
  delete mixedTemplate.config.default_daily_count;
  assert((await page("templates", mixedTemplate)).byId("templateRows").innerHTML.includes("每个启用 Page 每天上限 5 次"));

  const editor = await page("template", copy(fixedTemplate));
  assert.equal(Number(editor.byId("staggerMinutes").value), 40);
  editor.byId("scheduleMode").value = "random";
  await editor.fire("scheduleMode", "change");
  assert.equal(Number(editor.byId("staggerMinutes").value), 0);
  assert(editor.byId("staggerMinutes").disabled);
  assert(editor.byId("staggerField").classList.contains("hidden"));
  editor.byId("scheduleMode").value = "fixed";
  await editor.fire("scheduleMode", "change");
  assert.equal(Number(editor.byId("staggerMinutes").value), 40);
  assert(!editor.byId("staggerMinutes").disabled);
  assert(!editor.byId("staggerField").classList.contains("hidden"));
  editor.byId("scheduleMode").value = "random";
  await editor.fire("scheduleMode", "change");
  editor.byId("randomCount").value = "2";
  editor.byId("randomStart").value = "09:15";
  editor.byId("randomEnd").value = "21:55";
  await editor.fire("templateForm", "submit");
  assert.equal(editor.posts.length, 1);
  assert.deepEqual(editor.posts[0].schedule, randomTemplate.config.schedule);
  assert.equal(editor.posts[0].stagger_minutes, 0);
  assert.deepEqual(editor.posts[0].page_daily_limits, fixedTemplate.config.page_daily_limits);
  assert.equal(editor.posts[0].default_daily_count, 0);
  assert.equal(editor.posts[0].expected_version, 6);
  assert.equal(editor.window.location.href, "/fb-auto-publish-templates.html?v=20260820-list-only-v2");
  const randomEditor = await page("template", randomTemplate);
  assert(randomEditor.byId("staggerField").classList.contains("hidden"));
  assert(randomEditor.byId("capacityEstimate").textContent.includes("288 条"));

  if (process.argv[2]) {
    const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8").replace(/^\uFEFF/, ""));
    const item = data.item || data.template || data.items?.[0] || data;
    assert(item.config?.schedule, "real DTO requires config.schedule");
    const live = await page("templates", item);
    assert(live.byId("templateRows").innerHTML.includes("候选时"));
    console.log("Actual DTO list rendering passed");
  }
  console.log("FB frequency UI passed: non-empty fixed/random DTOs, mixed/zero/default limits, filters/refresh, editor mode switching and saved payload");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
