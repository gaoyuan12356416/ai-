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
  const legacy = await page("template", copy(fixedTemplate));
  assert.equal(legacy.byId("materialProduct").value, "Dramawave");
  legacy.byId("materialProduct").value = " DramawaveSource ";
  await legacy.fire("templateForm", "submit");
  assert.equal(legacy.posts[0].material_product, "DramawaveSource");
  assert.equal(legacy.posts[0].expected_version, 6);
  assert.deepEqual(legacy.posts[0].page_daily_limits, fixedTemplate.config.page_daily_limits);
  assert.deepEqual(legacy.posts[0].schedule, fixedTemplate.config.schedule);
  assert.equal(legacy.window.location.href, "/fb-auto-publish-templates.html?v=20260820-list-only-v2");
  const saved = copy(fixedTemplate);
  saved.config.material_product = "DramawaveSource";
  const editor = await page("template", saved);
  assert.equal(editor.byId("materialProduct").value, "DramawaveSource");
  editor.byId("materialProduct").value = "Another Product";
  await editor.fire("templateForm", "submit");
  assert.equal(editor.posts[0].material_product, "Another Product");
  assert.equal(editor.posts[0].material_data_source, 6);
  console.log("FB material-product UI: legacy fallback, source selection, saved DTO hydration, custom product and unchanged schedule passed");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
