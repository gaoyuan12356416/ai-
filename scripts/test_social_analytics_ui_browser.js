"use strict";

// Isolated regression: only the loopback server is reachable; no real posts,
// source database reads, production sessions or Feishu sends are performed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const vm = require("node:vm");
const { chromium } = require(process.env.SOCIAL_ANALYTICS_PLAYWRIGHT_PACKAGE || "playwright");
const ROOT = path.resolve(__dirname, "..");
const OUTPUT = path.join(ROOT, "output", "playwright", "social-analytics");
const allMetricKeys = [
  "clicks", "views", "installs", "conversions", "revenue", "refunds",
  "published_count", "post_count", "repost_count", "links",
];
const rows = [
  { owner: "u1", owner_label: "测试人员甲", platform: "youtube", platform_label: "YouTube",
    drama: "100", drama_label: "示例剧目甲", drama_english_name: "Example Drama One", channel: "youtube:channel-a", channel_label: "YouTube 测试频道", language: "en", language_label: "en", link_type: "auto", link_type_label: "自动发布",
    clicks: 4047, views: 5420, installs: 499, conversions: 19, revenue: 910.37, refunds: 1.25, published_count: 21, post_count: 0, repost_count: 0, links: 21 },
  { owner: "u2", owner_label: "测试人员乙", platform: "x", platform_label: "X",
    drama: "200", drama_label: "示例剧目乙", drama_english_name: "Example Drama Two", channel: "x:account-a", channel_label: "X 测试账号", language: "en", language_label: "en", link_type: "auto", link_type_label: "自动发布",
    clicks: 1047, views: 2420, installs: 199, conversions: 9, revenue: 780.42, refunds: 0, published_count: 12, post_count: 10, repost_count: 2, links: 12 },
  { owner: "u1", owner_label: "测试人员甲", platform: "facebook", platform_label: "Facebook",
    drama: "300", drama_label: "示例剧目丙", drama_english_name: "Example Drama Three", channel: "facebook:page-a", channel_label: "Facebook 测试主页", language: "en", language_label: "en", link_type: "manual", link_type_label: "手动",
    clicks: 400, views: 600, installs: 40, conversions: 4, revenue: 300, refunds: 0, published_count: 3, post_count: null, repost_count: null, links: 3 },
];
const platformMetadata = {youtube:{label:"YouTube"},x:{label:"X"},facebook:{label:"Facebook"}};
const metricApplicability = {post_count:["x"],repost_count:["x"]};
const optionKeys = ["platform", "owner", "drama", "channel", "link_type", "language"];
const fixtureOptions = Object.fromEntries(optionKeys.map(key => [key,
  [...new Map(rows.map(row => [row[key], {value: row[key], label: row[key + "_label"]}])).values()],
]));
const labels = { owner: "人员", platform: "渠道", drama: "剧目", channel: "账号/频道", language: "语言", link_type: "链接类型", date: "日期" };

function dates(start, end) {
  const result = [];
  for (let at = new Date(start + "T00:00:00Z"); at <= new Date(end + "T00:00:00Z"); at.setUTCDate(at.getUTCDate() + 1)) result.push(at.toISOString().slice(0, 10));
  return result;
}
function addRates(row) {
  row.install_rate = row.clicks ? row.installs / row.clicks * 100 : null;
  row.pay_rate = row.installs ? row.conversions / row.installs * 100 : null;
  return row;
}
function report(query, mode) {
  const start = query.get("start"), end = query.get("end"), days = dates(start, end);
  const groups = (query.get("group_by") || "owner,platform").split(",");
  const platforms = query.getAll("platform").length ? query.getAll("platform") : Object.keys(platformMetadata);
  let selected = rows.filter(row => optionKeys.every(key => !query.getAll(key).length || query.getAll(key).includes(row[key])));
  const brokenX = mode === "partial" && platforms.includes("x");
  const partialFacebook = mode === "facebook-partial" && platforms.includes("facebook");
  const brokenFacebook = mode === "facebook-unavailable" && platforms.includes("facebook");
  const incomplete = brokenX || partialFacebook || brokenFacebook;
  const qualityPlatforms = Object.fromEntries(platforms.map(platform => [platform, {
    label: platformMetadata[platform].label,
    state: (brokenX && platform === "x") || (brokenFacebook && platform === "facebook") ? "unavailable" : partialFacebook && platform === "facebook" ? "partial" : "complete",
    missing_dates: (brokenX && platform === "x") || ((partialFacebook || brokenFacebook) && platform === "facebook") ? days : [],
    error: brokenX && platform === "x" ? "效果数据暂不可用" : "",
  }]));
  if (qualityPlatforms.facebook) qualityPlatforms.facebook.sources = Object.fromEntries(["2049","2310"].map(site => {
    const missing = brokenFacebook || (partialFacebook && site === "2049");
    return [site, {source_key:"facebook:" + site,label:site === "2049" ? "旧自动发布" : "新手动发布",
      state: missing ? "unavailable" : "complete",missing_dates:missing ? days : [],
      collector:{state:missing ? "failed" : "complete",freshness:missing ? "stale" : "fresh"}}];
  }));
  const byGroup = new Map();
  for (const original of selected) {
    const row = {...original, date: end};
    if ((brokenX && row.platform === "x") || (brokenFacebook && row.platform === "facebook")) {
      for (const key of allMetricKeys) if (!["published_count", "post_count", "repost_count", "links"].includes(key)) row[key] = null;
    }
    if (partialFacebook && row.platform === "facebook") row.revenue = 200;
    if (row.platform !== "x") row.post_count = row.repost_count = null;
    const key = groups.map(group => row[group]).join("|");
    if (!byGroup.has(key)) {
      byGroup.set(key, { ...Object.fromEntries(groups.flatMap(group => [[group, row[group]], [group + "_label", row[group + "_label"] || row[group]]])),
        drama_english_name: row.drama_english_name, data_status: groups.includes("platform") ? qualityPlatforms[row.platform].state : incomplete ? "partial" : "complete",
        platforms: [row.platform],
        ...Object.fromEntries(allMetricKeys.map(metric => [metric, row[metric]])),
      });
    } else {
      const previous = byGroup.get(key);
      previous.platforms = [...new Set([...previous.platforms,row.platform])];
      allMetricKeys.forEach(metric => { previous[metric] = previous[metric] === null && row[metric] === null ? null : (previous[metric] || 0) + (row[metric] || 0); });
    }
  }
  const resultRows = [...byGroup.values()].map(addRates);
  const totals = addRates(Object.fromEntries(allMetricKeys.map(key => [key,
    resultRows.length && resultRows.every(row => row[key] === null) ? null : resultRows.reduce((sum, row) => sum + (row[key] || 0), 0),
  ])));
  const rankingKey = query.get("ranking_metric") || "clicks";
  const ranking = resultRows.map(row => ({...row, label: groups.map(group => row[group + "_label"]).join(" / ")}))
    .sort((a,b) => (b[rankingKey] || 0) - (a[rankingKey] || 0));
  return {
    totals, rows: resultRows, group_by: groups, ranking,
    trend: days.map(day => ({...Object.fromEntries(Object.keys(totals).map(key => [key, day === end ? totals[key] : key.includes("rate") ? null : 0])), date: day, data_status: incomplete ? "partial" : "complete"})),
    pagination: {page: 1, pages: 1, total: resultRows.length, page_size: Number(query.get("page_size") || 50)},
    quality: {complete: !incomplete, platforms: qualityPlatforms, missing_dates: incomplete ? days : [], unmatched_campaigns: 1, unmatched_totals: {revenue: 3.25}, excluded_campaigns: 0},
    meta: {start, end, platforms, scope: "tenant", timezone: "UTC", currency: "USD", fetched_at: "2026-10-20T00:00:00Z", source_updated_at: "2026-10-20T00:00:00Z",metric_applicability:metricApplicability},
    platform_metadata:platformMetadata,metric_applicability:metricApplicability,
    warnings: [],
  };
}
function navigationDefault() {
  const source = fs.readFileSync(path.join(ROOT, "static", "quick-nav.js"), "utf8");
  const beforeRest = source.indexOf("  const CONFIG_CACHE_KEY");
  const context = {};
  vm.runInNewContext(source.slice(0, beforeRest) + "globalThis.nav = DEFAULT_NAV;\n})();", context);
  const navigation = JSON.parse(JSON.stringify(context.nav));
  assert.equal(navigation.find(group => group.key === "youtube_platform").items.some(item => item.key === "youtubeAnalytics"), false);
  const social = navigation.find(group => group.key === "social_reports");
  assert.equal(social.module, "social_analytics");
  assert.equal(social.order, 50);
  assert.deepEqual(social.items.map(item => [item.key, item.module, item.href]), [["socialAnalytics", "social_analytics", "/social-analytics.html"]]);
  return navigation;
}
function send(response, status, type, value) {
  response.writeHead(status, {"Content-Type": type, "Cache-Control": "no-store"});
  response.end(value);
}
async function main() {
  const navigation = navigationDefault();
  let mode = "complete", delay = false;
  const requests = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    requests.push(url.pathname + url.search);
    if (request.method !== "GET") return send(response, 405, "text/plain", "mutations disabled");
    if (url.pathname === "/api/ui/topbar") return send(response, 200, "application/json", JSON.stringify({authenticated: true, user: {user_id: "u1", name: "隔离测试用户", tenant_key: "test-only", role: "admin", is_admin: true, permissions: {social_analytics: true}}}));
    if (url.pathname === "/navigation.json") return send(response, 200, "application/json", JSON.stringify(navigation));
    if (url.pathname === "/api/social-analytics/options") return send(response, 200, "application/json", JSON.stringify({scope: "tenant", options: fixtureOptions,platform_metadata:platformMetadata,metric_applicability:metricApplicability}));
    if (url.pathname === "/api/social-analytics/report") {
      const payload = mode === "denied" ? {message: "暂无社媒数据报表权限，请联系管理员。"} : report(url.searchParams, mode);
      const code = mode === "denied" ? 403 : 200;
      if (delay) await new Promise(resolve => setTimeout(resolve, 250));
      return send(response, code, "application/json", JSON.stringify(payload));
    }
    if (url.pathname === "/api/social-analytics/export.csv") {
      const result = report(url.searchParams, mode);
      const csv = "\ufeff人员,渠道,收入,X Post 数,X Repost 数\n" + result.rows.map(row => [
        row.owner_label || "", row.platform_label || "", row.revenue ?? "未同步",
        ...((row.platform ? row.platform !== "x" : !result.meta.platforms.includes("x")) ? ["不适用","不适用"] : [row.post_count,row.repost_count]),
      ].join(",")).join("\n");
      return send(response, 200, "text/csv; charset=utf-8", csv);
    }
    if (url.pathname === "/favicon.ico") return send(response, 204, "text/plain", "");
    const file = path.join(ROOT, "static", path.basename(url.pathname));
    if (fs.existsSync(file) && [".html", ".js", ".css"].includes(path.extname(file))) {
      return send(response, 200, {".html":"text/html; charset=utf-8", ".js":"application/javascript; charset=utf-8", ".css":"text/css; charset=utf-8"}[path.extname(file)], fs.readFileSync(file));
    }
    return send(response, 404, "text/plain", "not found");
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = "http://127.0.0.1:" + server.address().port;
  const launch = {headless: true, timeout: 20000};
  if (process.env.SOCIAL_ANALYTICS_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.SOCIAL_ANALYTICS_CHROMIUM_EXECUTABLE;
  let browser;
  let checks = 3;
  const errors = [];
  try {
    browser = await chromium.launch(launch);
    const context = await browser.newContext({viewport: {width: 1440, height: 1080}});
    await context.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    const loaded = async (target = page) => {
      await target.locator("#report-content:not(.hidden)").waitFor();
      await target.waitForFunction(() => document.querySelector("#report-content").getAttribute("aria-busy") === "false");
    };
    await page.goto(origin + "/social-analytics.html");
    await loaded();
    assert.equal(await page.locator("h1").textContent(), "社媒效果分析");
    assert.deepEqual(await page.locator('input[name="group"]:checked').evaluateAll(items => items.map(item => item.value)), ["owner", "platform"]);
    assert.equal(await page.locator('input[name="platform"]:checked').count(), 0);
    const yesterday = new Date(); yesterday.setUTCHours(0,0,0,0); yesterday.setUTCDate(yesterday.getUTCDate()-1);
    const firstDay = new Date(yesterday); firstDay.setUTCDate(firstDay.getUTCDate()-6);
    assert.equal(await page.locator("#date-end").inputValue(), yesterday.toISOString().slice(0,10));
    assert.equal(await page.locator("#date-start").inputValue(), firstDay.toISOString().slice(0,10));
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$1,990.79");
    assert.equal(await page.locator('[data-metric="published_count"]').textContent(), "36");
    assert.equal(await page.locator(".platform-status").count(), 3);
    assert.equal(await page.locator('[data-platform="facebook"] .source-status').count(), 2);
    assert.match(await page.locator('[data-platform="facebook"]').textContent(), /旧自动发布（2049）.*新手动发布（2310）/);
    assert.match(await page.locator("#quality-message").textContent(), /待归属收入 \$3.25/);
    assert.match(await page.locator("#report-body").textContent(), /不适用/);
    assert.equal(await page.locator('a[href="/social-analytics.html"]').count(), 1);
    checks += 13;
    fs.mkdirSync(OUTPUT, {recursive: true});
    await page.screenshot({path: path.join(OUTPUT, "desktop.png"), fullPage: true});
    await page.locator("#ranking-metric").selectOption("post_count"); await loaded();
    assert.equal(await page.locator("#ranking-chart .ranking-row").count(), 1);
    assert.match(await page.locator("#ranking-chart").textContent(), /测试人员乙 \/ X/);
    await page.locator("#ranking-metric").selectOption("clicks"); await loaded();
    checks += 2;

    // Group constraints, English titles and URL-driven channel filters.
    await page.locator('label.group-choice').filter({has: page.locator('input[value="drama"]')}).click();
    assert.equal(await page.locator('input[name="group"][value="channel"]').isDisabled(), true);
    await page.locator("#apply-filters").click(); await loaded();
    assert.match(await page.locator("#report-head").textContent(), /英文剧名/);
    assert.match(await page.locator("#report-body").textContent(), /Example Drama One/);
    checks += 3;
    await page.locator('.multi-filter[data-dimension="platform"] summary').click();
    await page.locator('input[name="platform"][value="x"]').check();
    assert.equal(await page.locator("#export-report").isDisabled(), true);
    await page.locator("#apply-filters").click(); await loaded();
    assert.equal(new URL(requests.filter(value => value.startsWith("/api/social-analytics/report?")).at(-1), origin).searchParams.get("platform"), "x");
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$780.42");
    assert.equal(await page.locator(".platform-status").count(), 1);
    checks += 4;
    await page.locator("#reset-filters").click();
    await page.locator("#apply-filters").click(); await loaded();
    const downloadEvent = page.waitForEvent("download");
    await page.locator("#export-report").click();
    const download = await downloadEvent;
    assert.match(download.suggestedFilename(), /^social-analytics_/);
    const exportRequest = new URL(requests.filter(value => value.startsWith("/api/social-analytics/export.csv?")).at(-1), origin);
    assert.equal(exportRequest.searchParams.get("group_by"), "owner,platform");
    assert.equal(exportRequest.searchParams.get("start"), firstDay.toISOString().slice(0,10));
    const downloadStream = await download.createReadStream(), chunks = [];
    for await (const chunk of downloadStream) chunks.push(chunk);
    assert.match(Buffer.concat(chunks).toString("utf8"), /Facebook,300,不适用,不适用/);
    checks += 4;

    // Facebook-only and YouTube + Facebook selections cannot present X zero trends/ranks.
    await page.goto(origin + "/social-analytics.html?platform=facebook&group_by=owner,platform&ranking_metric=post_count");
    await loaded();
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$300.00");
    assert.equal(await page.locator('[data-metric="published_count"]').textContent(), "3");
    assert.match(await page.locator("#report-body").textContent(), /Facebook/);
    assert.equal(await page.locator("#report-body tr").locator("td").filter({hasText:/^不适用$/}).count(), 2);
    assert.match(await page.locator("#ranking-chart").textContent(), /不适用于所选渠道/);
    await page.locator("#trend-metric").selectOption("repost_count");
    assert.match(await page.locator("#trend-chart").textContent(), /不适用于所选渠道/);
    await page.goto(origin + "/social-analytics.html?platform=youtube&platform=facebook&group_by=owner&ranking_metric=post_count");
    await loaded();
    assert.equal(await page.locator("#report-body tr").count(), 1);
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$1,210.37");
    assert.match(await page.locator("#ranking-chart").textContent(), /不适用于所选渠道/);
    await page.locator("#trend-metric").selectOption("post_count");
    assert.match(await page.locator("#trend-chart").textContent(), /不适用于所选渠道/);
    checks += 10;
    // A non-X owner remains non-applicable even when the request includes X for someone else.
    await page.goto(origin + "/social-analytics.html?group_by=owner");
    await loaded();
    const nonXOwner = page.locator("#report-body tr").filter({hasText:"测试人员甲"});
    assert.equal(await nonXOwner.locator("td").filter({hasText:/^不适用$/}).count(), 2);
    const xOwner = page.locator("#report-body tr").filter({hasText:"测试人员乙"});
    assert.equal(await xOwner.locator("td").filter({hasText:/^不适用$/}).count(), 0);
    checks += 2;
    await page.goto(origin + "/social-analytics.html"); await loaded();

    // One unavailable Facebook site is visible independently of its healthy sibling.
    mode = "facebook-partial";
    await page.locator("#refreshPage").click(); await loaded();
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$1,890.79");
    assert.match(await page.locator('[data-source="facebook:2049"]').textContent(), /旧自动发布（2049）：待核实.*归属采集待核实.*归属快照已过期/);
    assert.match(await page.locator('[data-source="facebook:2310"]').textContent(), /新手动发布（2310）：已有源数据/);
    assert.match(await page.locator("#report-period").textContent(), /合计不完整/);
    checks += 4;
    await page.screenshot({path: path.join(OUTPUT, "facebook-partial.png"), fullPage: true});
    mode = "facebook-unavailable";
    await page.locator("#refreshPage").click(); await loaded();
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$1,690.79");
    assert.match(await page.locator("#report-body tr").filter({hasText:"Facebook"}).textContent(), /—/);
    checks += 2;

    // An unavailable X source cannot become a zero-valued X revenue.
    mode = "partial";
    await page.locator("#refreshPage").click(); await loaded();
    assert.match(await page.locator("#report-period").textContent(), /合计不完整/);
    assert.equal(await page.locator('[data-metric="revenue"]').textContent(), "$1,210.37");
    assert.equal(await page.locator('[data-metric="published_count"]').textContent(), "36");
    assert.match(await page.locator(".platform-status.unavailable").textContent(), /X待核实/);
    const xRow = page.locator("#report-body tr").filter({hasText: "测试人员乙"});
    assert.match(await xRow.textContent(), /—/);
    assert.equal(await page.locator(".trend-dot.partial").count(), 7);
    checks += 6;
    await page.screenshot({path: path.join(OUTPUT, "partial.png"), fullPage: true});

    // A pending response must not replace a newly edited filter state.
    delay = true;
    await page.locator("#refreshPage").click();
    await page.locator("#report-search").fill("新筛选");
    await page.waitForTimeout(350);
    assert.equal(await page.locator("#report-content").evaluate(node => node.classList.contains("hidden")), true);
    assert.match(await page.locator("#page-message").textContent(), /筛选条件已修改/);
    checks += 2;
    delay = false; mode = "complete";
    await page.locator("#report-search").fill("");
    await page.locator("#apply-filters").click(); await loaded();
    mode = "denied";
    await page.locator("#refreshPage").click();
    await page.waitForFunction(() => document.querySelector("#page-message").textContent.includes("暂无社媒"));
    assert.equal(await page.locator("#filter-fields").evaluate(node => node.disabled), true);
    assert.equal(await page.locator("#report-content").evaluate(node => node.classList.contains("hidden")), true);
    assert.equal(await page.locator("#kpi-grid").textContent(), "");
    checks += 3;

    mode = "complete";
    const legacy = await context.newPage();
    legacy.on("pageerror", error => errors.push(error.message));
    await legacy.goto(origin + "/youtube-analytics.html?start=2026-10-19&end=2026-10-19&group_by=owner,channel&channel=channel-a&sort=revenue&direction=asc#details");
    await legacy.waitForURL("**/social-analytics.html?**"); await loaded(legacy);
    const lastQuery = new URL(requests.filter(value => value.startsWith("/api/social-analytics/report?")).at(-1), origin).searchParams;
    assert.equal(lastQuery.get("platform"), "youtube");
    assert.equal(lastQuery.get("channel"), "youtube:channel-a");
    assert.equal(lastQuery.get("start"), "2026-10-19");
    assert.equal(lastQuery.get("group_by"), "owner,channel");
    assert.equal(lastQuery.get("sort"), "revenue");
    assert.equal(lastQuery.get("direction"), "asc");
    assert.equal(new URL(legacy.url()).hash, "#details");
    assert.equal(await legacy.locator(".platform-status").count(), 1);
    checks += 8;

    const mobileContext = await browser.newContext({viewport: {width: 390, height: 844}, isMobile: true, deviceScaleFactor: 1});
    await mobileContext.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const mobile = await mobileContext.newPage();
    mobile.on("pageerror", error => errors.push(error.message));
    await mobile.goto(origin + "/social-analytics.html"); await loaded(mobile);
    assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "mobile page must not overflow horizontally");
    await mobile.locator('.multi-filter[data-dimension="platform"] summary').click();
    await mobile.locator('input[name="platform"][value="youtube"]').check();
    await mobile.locator('input[name="platform"][value="x"]').check();
    await mobile.locator('input[name="platform"][value="facebook"]').check();
    const popover = await mobile.locator('.multi-filter[data-dimension="platform"] .filter-popover').boundingBox();
    assert.ok(popover.x >= 0 && popover.x + popover.width <= 391);
    await mobile.locator("#apply-filters").click(); await loaded(mobile);
    const mobileQuery = new URL(requests.filter(value => value.startsWith("/api/social-analytics/report?")).at(-1), origin).searchParams;
    assert.deepEqual(mobileQuery.getAll("platform"), ["youtube", "x", "facebook"]);
    assert.equal(await mobile.locator('[data-metric="revenue"]').textContent(), "$1,990.79");
    await mobile.screenshot({path: path.join(OUTPUT, "mobile.png"), fullPage: true});
    checks += 4;
    assert.deepEqual(errors, [], "page scripts must not throw");
    assert.equal(requests.some(value => value.includes("youtube-analytics/report")), false, "legacy entry must use unified reporting");
    checks += 2;
    console.log(JSON.stringify({status:"PASS", checks, screenshots: OUTPUT, requests: requests.length, mutations:0}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
