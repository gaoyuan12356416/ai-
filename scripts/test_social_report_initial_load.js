"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require(process.env.SOCIAL_ANALYTICS_PLAYWRIGHT_PACKAGE || "playwright");
const { fixtureOptions, report } = require("./test_social_analytics_ui_browser");
const assets = path.join(__dirname, "../static");
const url = "http://report.test/social-analytics.html";

async function check() {
  const browser = await chromium.launch({executablePath: process.env.SOCIAL_ANALYTICS_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage();
    const queries = [], errors = [];
    let optionsGate, reportGate, denyOptions = false, optionRequests = 0;
    const options = {...fixtureOptions, drama: Array.from({length: 6000},
      (_, i) => ({value: String(i), label: "示例剧目 " + i}))};
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      const request = route.request(), address = new URL(request.url());
      assert.equal(address.hostname, "report.test");
      assert.equal(request.method(), "GET");
      switch (address.pathname) {
        case "/api/ui/topbar": return route.fulfill({json: {authenticated: true}});
        case "/quick-nav.js": return route.fulfill({contentType: "text/javascript", body: "window.QuickNav={render(){}};"});
        case "/ui-topbar.js": return route.fulfill({contentType: "text/javascript", body: "window.UiTopbar={render(){},handleAuthAction(){}};"});
        case "/api/social-analytics/options":
          optionRequests++; if (optionsGate) await optionsGate;
          return route.fulfill(denyOptions ? {status: 403, json: {message: "没有权限"}} : {json: {scope: "tenant", options}});
        case "/api/social-analytics/report":
          queries.push(address.searchParams);
          if (reportGate) await reportGate;
          return route.fulfill({json: report(address.searchParams, "complete")});
      }
      const file = path.join(assets, path.basename(address.pathname));
      return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ""});
    });
    const loaded = () => page.locator("#report-content:not(.hidden)").waitFor();
    const reportRequest = () => page.waitForRequest(r => r.url().includes("/api/social-analytics/report?"));
    let releaseOptions, releaseReport;
    optionsGate = new Promise(resolve => { releaseOptions = resolve; });
    reportGate = new Promise(resolve => { releaseReport = resolve; });
    await page.goto(url + "?page=5");
    await page.waitForFunction(() => document.querySelector('#report-content').getAttribute('aria-busy') === 'true');
    const corrected = reportRequest();
    await page.locator("[data-range=yesterday]").click();
    await corrected;
    const yesterday = await page.locator("#date-end").inputValue();
    assert.equal(await page.locator("#date-start").inputValue(), yesterday);
    releaseReport();
    await loaded();
    assert.equal(optionRequests, 0);
    assert.equal(await page.locator("#dimension-filters input[type=checkbox]").count(), 0);
    assert.equal(queries.at(-1).get("start"), yesterday);
    assert.equal(queries.at(-1).get("page"), "1");
    releaseOptions();
    const group = page.locator('[data-dimension="platform"]');
    await group.locator("summary").click();
    await group.locator('[value="x"]').check();
    assert.equal(await page.locator('input[name="drama"]').count(), 0);
    await page.locator("#apply-filters").click();
    await loaded();
    assert.deepEqual(queries.at(-1).getAll("platform"), ["x"]);
    await page.goto(url + "?platform=youtube&channel=channel-a&start=2026-10-19&end=2026-10-21&page=2&direction=asc&sort=clicks");
    await loaded();
    const saved = queries.at(-1);
    assert.deepEqual(saved.getAll("channel"), ["youtube:channel-a"]);
    assert.deepEqual(["start","end","page","direction","sort"].map(k => saved.get(k)), ["2026-10-19","2026-10-21","2","asc","clicks"]);
    const drama = page.locator('[data-dimension="drama"]');
    await drama.locator("summary").click();
    await drama.locator(".option-search").fill("5999");
    assert.equal(await drama.locator(".filter-option:visible").count(), 1);
    await drama.locator('[value="5999"]').check();
    await drama.locator(".filter-clear").click();
    assert.equal(await drama.locator("input:checked").count(), 0);
    await page.goto(url);
    await loaded();
    assert.equal((Date.parse(queries.at(-1).get("end")) - Date.parse(queries.at(-1).get("start")))/86400000, 6);
    const beforeDenial = queries.length;
    denyOptions = true;
    await page.goto(url + "?platform=x");
    await page.locator("#page-message.error").waitFor();
    assert.equal(await page.locator("[data-range=yesterday]").isDisabled(), true);
    assert.equal(await page.locator("#apply-filters").isDisabled(), true);
    assert.equal(await page.locator("#dimension-filters input").count(), 0);
    assert.equal(queries.length, beforeDenial);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed:true,defaultOptionsRequests:0,closedDramaCheckboxes:0,fixtureDramas:6000}));
  } finally {
    await browser.close();
  }
}
check().catch(error => { console.error(error.stack); process.exitCode = 1; });
