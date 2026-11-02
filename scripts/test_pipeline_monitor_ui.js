"use strict";
// Real browser contract test on loopback fixtures. No production calls/writes.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), http = require("node:http"), vm = require("node:vm");
const { chromium } = require(process.env.PIPELINE_PLAYWRIGHT_PACKAGE || "playwright");
const ROOT = path.resolve(__dirname, ".."), OUT = process.env.PIPELINE_UI_OUTPUT || path.join(ROOT, "work", "pipeline-monitor-ui");
const stamp = new Date().toISOString();
const base = { generated_at: stamp, collected_at: stamp, stale: false, scope: "all", allowed_channels: ["x", "fb", "tt", "youtube"], warnings: [] };
const summary = { ...base, totals: { queued: 22, preparing: 3, ready: 7, publishing: 4, prepared: 90, published: 76, failed: 2, unknown: 1, backlog_paused: 611 }, channels: ["x", "fb", "tt", "youtube"].flatMap(channel => ["prepare", "publish"].map(stage => ({ channel, stage, queued: 8, running: 2, ready: 4, completed: 21, failed: 1, unknown: 0, throughput_per_hour: 12, avg_duration_seconds: 321, p95_duration_seconds: null, oldest_wait_seconds: 45 }))), sources: [{ channel: "x", family: "自动模板", state: "healthy", collected_at: stamp }] };
let capacity = { version: 1, hosts: ["cpu-main", "hk-gpu", "gpu-third"].flatMap(host_id => ["prepare", "publish"].map(stage => ({ host_id, stage, max_workers: host_id === "gpu-third" && stage === "prepare" ? 1 : 2, ceiling: 4 }))), global_publish_limit: 8, channel_limits: { x: 4, fb: 8, tt: 4, youtube: 2 } };
let admin = true, failSummary = false, calls = [], writes = [];
const workers = () => ({ ...base, capacity, items: ["cpu-main", "hk-gpu", "gpu-third"].map((host_id, i) => ({ host_id, label: ["CPU 服务器", "香港 GPU", "新增 GPU 服务器"][i], last_seen_at: stamp, stale: false, cpu_percent: 35, memory_used_bytes: 8e9, memory_total_bytes: 32e9, gpu_percent: i ? 65 : null, gpu_count: i ? 1 : 0, gpu_memory_used_bytes: i ? 3e9 : null, gpu_memory_total_bytes: i ? 16e9 : null, stages: ["prepare", "publish"].map(stage => ({ stage, max_workers: i === 2 && stage === "prepare" ? 1 : 2, ceiling: 4, alive_workers: i === 2 && stage === "prepare" ? 4 : 2, busy_workers: i === 2 && stage === "prepare" ? 3 : 1, idle_workers: 1, completed_today: 24, throughput_per_hour: 5 })) })), processes: [{ host_id: "gpu-third", worker_id: "prepare-1", stage: "prepare", pid: 12345, status: "busy", current_source_key: "x:auto:fixture-1", channel: "x", phase: "render", elapsed_seconds: 165, last_seen_at: stamp }, { host_id: "cpu-main", worker_id: "publish-1", stage: "publish", pid: 23456, status: "idle", elapsed_seconds: null, last_seen_at: stamp, idle_reason: "账号正在发布，等待下一条可领取任务" }] });
const task = { id: 17, source_key: "fixture-task", source_id: "fixture-17", channel: "x", source_family: "自动模板", stage: "publish", status: "unknown", host_id: "gpu-third", worker_id: "publish-1", account_label: "<img src=x onerror=alert(1)>", owner_name: "测试运营", created_at: stamp, started_at: stamp, duration_seconds: null, wait_seconds: 20, detail_url: "javascript:alert(1)" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost"); calls.push(url.pathname + url.search);
  const send = (status, data) => { res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(data)); };
  if (url.pathname === "/api/ui/topbar") return send(200, { authenticated: true, user: { is_admin: admin, name: "测试用户", role: admin ? "admin" : "user", permissions: { x_accounts: true } } });
  if (url.pathname === "/api/navigation") return send(200, { items: [] });
  if (url.pathname === "/api/pipeline-monitor/summary") return failSummary ? send(503, { message: "测试采集失败，保留上次数据" }) : send(200, { ...summary, scope: admin ? "all" : "mine" });
  if (url.pathname === "/api/pipeline-monitor/workers") return send(200, admin ? workers() : { ...base, items: [], processes: [], visibility_message: "服务器全局明细仅管理员可见" });
  if (url.pathname === "/api/pipeline-monitor/tasks") return send(200, { ...base, items: [task], total: 1, next_cursor: "" });
  if (url.pathname === "/api/pipeline-monitor/capacity") { let raw = ""; req.on("data", b => raw += b); return req.on("end", () => { const body = JSON.parse(raw); writes.push(body); capacity = { ...capacity, version: capacity.version + 1 }; send(200, { capacity }); }); }
  if (url.pathname === "/api/pipeline-monitor/nodes/enroll") return send(200, { enrollment_id: "fixture-enrollment", token: "fixture-one-use-token", expires_at: new Date(Date.now() + 600000).toISOString() });
  const file = path.join(ROOT, "static", url.pathname === "/" ? "index.html" : url.pathname.slice(1));
  if (!file.startsWith(path.join(ROOT, "static") + path.sep) || !fs.existsSync(file)) return send(404, {});
  res.writeHead(200, { "Content-Type": ({ ".html": "text/html", ".js": "application/javascript", ".css": "text/css" })[path.extname(file)] || "text/plain" }); res.end(fs.readFileSync(file));
});
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve)); const origin = `http://127.0.0.1:${server.address().port}`;
  const launch = { headless: true }; if (process.env.PIPELINE_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.PIPELINE_CHROMIUM_EXECUTABLE;
  const browser = await chromium.launch(launch);
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
    await context.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage(), errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin + "/pipeline-monitor.html"); await page.waitForSelector(".pm-stat-value"); await page.waitForFunction(() => document.querySelectorAll(".pm-worker").length === 3);
    await page.waitForSelector('[data-quick-nav-key="videoEditing"]', { state: "attached" });
    for (const key of ["pipelineMonitor", "videoEditing", "aiImageDaily", "fbPostMaterialPool", "fbPostAdDelete", "socialkitAnalytics"]) assert.equal(await page.locator(`[data-quick-nav-key="${key}"]`).count(), 1);
    for (const group of ["ad_control", "ad_control_v3", "ad_material", "ad_material_test"]) assert.equal(await page.locator(`[data-quick-nav-group="${group}"]`).count(), 0);
    assert.equal(await page.locator("#pmChannelRows tr").count(), 8);
    assert.match(await page.locator("#pmProcessRows").innerText(), /新增 GPU 服务器[\s\S]*制作渲染/);
    assert.match(await page.locator("#pmProcessRows").innerText(), /账号正在发布/);
    const drainStage = page.locator('[data-worker-host="gpu-third"] [data-worker-stage="prepare"]');
    assert.match(await drainStage.innerText(), /当前目标 \/ 上限[\s\S]*1 \/ 4/);
    assert.match(await drainStage.innerText(), /忙碌 \/ 空闲 \/ 存活[\s\S]*3 \/ 1 \/ 4/);
    assert.match(await drainStage.innerText(), /自然排空[\s\S]*当前 3 个任务继续完成/);
    assert.match(await page.locator("#pmTaskRows").innerText(), /<img src=x/);
    assert.equal(await page.locator("#pmTaskRows img").count(), 0);
    assert.equal(await page.locator("#pmTaskRows a").count(), 0);
    await page.screenshot({ path: path.join(OUT, "desktop.png"), fullPage: true });
    await page.locator("#pmWorkers").screenshot({ path: path.join(OUT, "workers.png") });
    await page.locator("#pmStage").selectOption("publish"); await page.waitForTimeout(100); assert(calls.some(url => url.includes("tasks?") && url.includes("stage=publish")));
    await page.locator('form[data-host="gpu-third"][data-stage="prepare"] button[data-drain]').click();
    await page.waitForFunction(() => document.querySelector("#pmCapacityVersion").textContent.includes("2"));
    assert.deepEqual(writes[0], { expected_version: 1, host_id: "gpu-third", stage: "prepare", max_workers: 0 });
    await page.locator("#pmEnrollOpen").click(); await page.locator('#pmEnrollForm input[name="label"]').fill("第三台服务器"); await page.locator('#pmEnrollForm button').click();
    await page.waitForFunction(() => document.querySelector("#pmEnrollmentConfig").textContent.includes("fixture-one-use-token"));
    assert.equal(await page.evaluate(() => Object.values(localStorage).some(v => v.includes("fixture-one-use-token"))), false);
    await page.locator("#pmEnrollClose").click(); assert.equal(await page.locator("#pmEnrollmentConfig").textContent(), "");
    failSummary = true; await page.locator("#pmRefresh").click(); await page.waitForFunction(() => !document.querySelector("#pmError").hidden); assert.match(await page.locator("#pmTotals").innerText(), /76/);
    await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(250); await page.screenshot({ path: path.join(OUT, "mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert((await page.locator('#pmSidebar').boundingBox()).x < -200);
    failSummary = false; admin = false; await page.reload(); await page.waitForFunction(() => document.querySelector("#pmScope").textContent === "我的任务");
    assert.equal(await page.locator("#pmEnrollOpen").isVisible(), false); assert.equal(await page.locator("#pmCapacitySection").isVisible(), false);
    await page.waitForSelector('[data-quick-nav-key="pipelineMonitor"]', { state: "attached" });
    for (const key of ["videoEditing", "aiImageDaily", "fbPostMaterialPool", "socialkitAnalytics"]) assert.equal(await page.locator(`[data-quick-nav-key="${key}"]`).count(), 0);
    assert.deepEqual(errors, []);
    // Test the exact home decision from index.html without loading legacy task APIs.
    const html = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8"), match = html.match(/const homePath =[^]*?\n      }/); assert(match);
    assert(html.includes('/drama-job-runtime.js?v=20260828-async-v1'));
    for (const [pathname, hash, redirect] of [["/", "", true], ["/index.html", "", true], ["/", "#tasks", false], ["/", "#job/123", false], ["/drama-synthesis.html", "", false]]) { let next = ""; vm.runInNewContext(`(function(){${match[0]}})()`, { location: { pathname, hash, search: "?owner=fixture", replace: value => next = value } }); assert.equal(!!next, redirect); if (redirect) assert.equal(next, "/pipeline-monitor.html?owner=fixture"); }
    console.log("pipeline monitor browser: admin/personal, 3 nodes, process phases, filters, drain, enrollment cleanup, stale preservation, escaping, mobile and home deep links passed");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
