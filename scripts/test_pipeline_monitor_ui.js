"use strict";
// Real browser contract test on loopback fixtures. No production calls/writes.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), http = require("node:http"), vm = require("node:vm");
const { chromium } = require(process.env.PIPELINE_PLAYWRIGHT_PACKAGE || "playwright");
const ROOT = path.resolve(__dirname, ".."), OUT = process.env.PIPELINE_UI_OUTPUT || path.join(ROOT, "work", "pipeline-monitor-ui");
const stamp = new Date().toISOString();
const base = { generated_at: stamp, collected_at: stamp, stale: false, scope: "all", allowed_channels: ["x", "fb", "tt", "youtube"], warnings: [] };
const summary = { ...base, totals: { queued: 22, preparing: 3, ready: 7, publishing: 4, prepared: 90, published: 76, failed: 2, unknown: 1, backlog_paused: 611 }, channels: ["x", "fb", "tt", "youtube"].flatMap(channel => ["prepare", "publish"].map(stage => ({ channel, stage, queued: 8, running: 2, ready: 4, completed: 21, failed: 1, unknown: 0, throughput_per_hour: 12, avg_duration_seconds: 321, p95_duration_seconds: null, oldest_wait_seconds: 45 }))), sources: [{ channel: "x", family: "自动模板", state: "healthy", collected_at: stamp }] };
summary.efficiency = [
  { channel: "fb", stage: "publish", phase: "platform_submit", samples: 1, p50_seconds: 17, p95_seconds: 17 },
  { channel: "fb", stage: "publish", phase: "platform_upload", samples: 0, applicable: false },
  { channel: "fb", stage: "publish", phase: "platform_confirmation_wait", samples: 1, p50_seconds: 250, p95_seconds: 250 },
  { channel: "tt", stage: "publish", phase: "platform_processing", samples: 1, p50_seconds: 75, p95_seconds: 75 },
];
let capacity = { version: 1, hosts: ["cpu-main", "hk-gpu", "gpu-third"].flatMap(host_id => ["prepare", "publish"].map(stage => ({ host_id, stage, max_workers: host_id === "gpu-third" && stage === "prepare" ? 1 : 2, ceiling: 4 }))), global_publish_limit: 8, channel_limits: { x: 4, fb: 8, tt: 4, youtube: 2 } };
let admin = true, authenticated = true, failSummary = false, calls = [], writes = [];
capacity.hosts.filter(row => row.host_id === "hk-gpu").forEach(row => row.scaling_mode = "manual");
const workers = () => ({ ...base, capacity, items: ["cpu-main", "hk-gpu", "gpu-third"].map((host_id, i) => ({ host_id, label: ["CPU 服务器", "香港 GPU", "新增 GPU 服务器"][i], last_seen_at: stamp, stale: false, cpu_percent: 35, memory_used_bytes: 8e9, memory_total_bytes: 32e9, gpu_percent: i ? 65 : null, gpu_count: i ? 1 : 0, gpu_memory_used_bytes: i ? 3e9 : null, gpu_memory_total_bytes: i ? 16e9 : null, stages: ["prepare", "publish"].map(stage => ({ stage, max_workers: i === 2 && stage === "prepare" ? 1 : 2, ceiling: 4, alive_workers: i === 2 && stage === "prepare" ? 4 : 2, busy_workers: i === 2 && stage === "prepare" ? 3 : 1, idle_workers: 1, completed_today: 24, throughput_per_hour: 5 })) })), processes: [{ host_id: "gpu-third", worker_id: "prepare-1", stage: "prepare", pid: 12345, status: "busy", current_source_key: "x:auto:fixture-1", channel: "x", phase: "render", elapsed_seconds: 165, last_seen_at: stamp }, { host_id: "cpu-main", worker_id: "publish-1", stage: "publish", pid: 23456, status: "idle", elapsed_seconds: null, last_seen_at: stamp, idle_reason: "账号正在发布，等待下一条可领取任务" }] });
const task = { id: 17, source_key: "fixture-task", source_id: "fixture-17", channel: "x", source_family: "自动模板", stage: "publish", status: "unknown", host_id: "gpu-third", worker_id: "publish-1", account_label: "<img src=x onerror=alert(1)>", owner_name: "测试运营", created_at: stamp, started_at: stamp, duration_seconds: null, wait_seconds: 20, detail_url: "javascript:alert(1)" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost"); calls.push(url.pathname + url.search);
  const send = (status, data) => { res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(data)); };
  if (url.pathname === "/api/ui/topbar") return send(200, { authenticated, user: { is_admin: admin, name: "测试用户", role: admin ? "admin" : "user", permissions: { x_accounts: true } } });
  if (url.pathname === "/api/navigation") return send(200, { items: [] });
  if (url.pathname === "/api/pipeline-monitor/summary") return failSummary ? send(503, { message: "测试采集失败，保留上次数据" }) : send(200, { ...summary, scope: admin ? "all" : "mine" });
  if (url.pathname === "/api/pipeline-monitor/workers") { const snapshot = workers(); snapshot.items.filter(row => row.host_id === "hk-gpu").forEach(row => row.stages.forEach(stage => stage.scaling_mode = "manual")); return send(200, admin ? snapshot : { ...base, items: [], processes: [], visibility_message: "服务器全局明细仅管理员可见" }); }
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
    assert.match(await page.locator("#pmTotals .pm-stat").first().innerText(), /排队中\s*22/);
    assert.equal(await page.locator('#pmStatus option[value="queued"]').innerText(), "排队中");
    assert.match(await page.locator("#pmQueueNote").innerText(), /待选材、未到预约时间及账号等待[\s\S]*不代表当前可领取数/);
    const fbEfficiency = page.locator("#pmEfficiencyRows tr").filter({ hasText: "Facebook · 发布" });
    assert.match(await fbEfficiency.innerText(), /17秒 \/ 17秒[\s\S]*不适用[\s\S]*未记录[\s\S]*4.2分 \/ 4.2分/);
    assert.match(await page.locator("#pmEfficiencyRows tr").filter({ hasText: "TikTok · 发布" }).innerText(), /1.3分 \/ 1.3分/);
    assert.match(await page.locator("#pmEfficiencyRows").locator("..").innerText(), /平台提交[\s\S]*平台确认等待/);
    assert.match(await page.locator("#pmProcessRows").innerText(), /新增 GPU 服务器[\s\S]*制作渲染/);
    assert.match(await page.locator("#pmProcessRows").innerText(), /账号正在发布/);
    const drainStage = page.locator('[data-worker-host="gpu-third"] [data-worker-stage="prepare"]');
    assert.match(await page.locator('form[data-host="hk-gpu"][data-stage="prepare"]').innerText(), /手动[\s\S]*保存后按此并发接单/);
    assert.match(await page.locator('[data-worker-host="hk-gpu"] [data-worker-stage="prepare"]').innerText(), /当前目标 \/ 上限 · 手动/);
    assert.match(await page.locator('[data-worker-host="cpu-main"] [data-worker-stage="prepare"]').innerText(), /当前目标 \/ 上限 · 自动/);
    // Missing telemetry stays unknown; never portray an unreported encoder as idle.
    assert.match(await page.locator('[data-worker-host="hk-gpu"] .pm-worker-resources').innerText(), /GPU 运算使用率[\s\S]*视频编码器使用率\s*—/);
    assert.doesNotMatch(await page.locator('[data-worker-host="cpu-main"] .pm-worker-resources').innerText(), /视频编码器使用率/);
    await page.evaluate(snapshot => { snapshot.items[1].encoder_percent = 83; window.PipelineMonitor.renderWorkers(snapshot); }, workers());
    assert.match(await page.locator('[data-worker-host="hk-gpu"] .pm-worker-resources').innerText(), /视频编码器使用率\s*83%/);
    assert.equal(await page.locator('#pmCapacityForms select[name="scaling_mode"]').count(), 0);
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
    await page.locator("#pmEnrollOpen").click(); assert.equal(await page.locator('#pmEnrollForm input[name="label"]').getAttribute("maxlength"), "100"); await page.locator('#pmEnrollForm input[name="label"]').fill("第三台服务器"); await page.locator('#pmEnrollForm button').click();
    await page.waitForFunction(() => document.querySelector("#pmEnrollmentConfig").textContent.includes("fixture-one-use-token"));
    assert.equal(await page.evaluate(() => Object.values(localStorage).some(v => v.includes("fixture-one-use-token"))), false);
    await page.locator("#pmEnrollClose").click(); assert.equal(await page.locator("#pmEnrollmentConfig").textContent(), "");
    summary.stale = true; summary.warnings = ["测试来源超过采集时限"];
    summary.sources[0].state = "stale";
    await page.locator("#pmRefresh").click(); await page.waitForFunction(() => !document.querySelector("#pmWarnings").hidden);
    assert.match(await page.locator("#pmWarnings").innerText(), /采集数据有延迟[\s\S]*测试来源超过采集时限/);
    assert.match(await page.locator("#pmSources").innerText(), /数据延迟/);
    assert.equal(await page.locator("#pmLiveDot").evaluate(el => el.classList.contains("stale")), true);
    assert.match(await page.locator("#pmTotals").innerText(), /76/);
    summary.stale = false; summary.warnings = []; summary.sources[0].state = "healthy";
    failSummary = true; await page.locator("#pmRefresh").click(); await page.waitForFunction(() => !document.querySelector("#pmError").hidden); assert.match(await page.locator("#pmTotals").innerText(), /76/);
    await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(250); await page.screenshot({ path: path.join(OUT, "mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert((await page.locator('#pmSidebar').boundingBox()).x < -200);
    await page.locator("#pmShowNav").click(); await page.waitForFunction(() => document.querySelector("#pmSidebar").getBoundingClientRect().x >= 0);
    await page.locator("#pmHideNav").click(); await page.waitForFunction(() => document.querySelector("#pmSidebar").getBoundingClientRect().x < -200);
    failSummary = false; admin = false; await page.reload(); await page.waitForFunction(() => document.querySelector("#pmScope").textContent === "我的任务");
    assert.equal(await page.locator("#pmEnrollOpen").isVisible(), false); assert.equal(await page.locator("#pmCapacitySection").isVisible(), false);
    await page.waitForSelector('[data-quick-nav-key="pipelineMonitor"]', { state: "attached" });
    for (const key of ["videoEditing", "aiImageDaily", "fbPostMaterialPool", "socialkitAnalytics"]) assert.equal(await page.locator(`[data-quick-nav-key="${key}"]`).count(), 0);
    // The real timer must refresh while the visible personal page stays open.
    const beforeTick = calls.filter(url => url.startsWith("/api/pipeline-monitor/summary?")).length;
    const tickDeadline = Date.now() + 18000;
    while (calls.filter(url => url.startsWith("/api/pipeline-monitor/summary?")).length === beforeTick && Date.now() < tickDeadline) await page.waitForTimeout(250);
    assert(calls.filter(url => url.startsWith("/api/pipeline-monitor/summary?")).length > beforeTick);
    authenticated = false; calls = []; await page.reload();
    await page.waitForFunction(() => document.querySelector("#pmGate").textContent.includes("请先登录"));
    assert.equal(await page.locator("#pmContent").isVisible(), false);
    assert.equal(calls.some(url => url.startsWith("/api/pipeline-monitor/")), false);
    assert.deepEqual(errors, []);
    // Test the exact home decision from index.html without loading legacy task APIs.
    const html = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8"), match = html.match(/const homePath =[^]*?\n      }/); assert(match);
    assert(html.includes('/drama-job-runtime.js?v=20260828-async-v1'));
    for (const [pathname, hash, redirect] of [["/", "", true], ["/index.html", "", true], ["/", "#tasks", false], ["/", "#job/123", false], ["/drama-synthesis.html", "", false]]) { let next = ""; vm.runInNewContext(`(function(){${match[0]}})()`, { location: { pathname, hash, search: "?owner=fixture", replace: value => next = value } }); assert.equal(!!next, redirect); if (redirect) assert.equal(next, "/pipeline-monitor.html?owner=fixture"); }
    console.log("pipeline monitor browser: admin/personal/login gate, 3 nodes, process phases, filters, drain, enrollment cleanup, delayed snapshots, failed-refresh preservation, 15s auto-refresh, escaping, mobile navigation and home deep links passed");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
