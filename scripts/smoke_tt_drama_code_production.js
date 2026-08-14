"use strict";

const assert = require("node:assert/strict");
const { chromium } = require(
  process.env.TT_CODE_PLAYWRIGHT_PACKAGE || "playwright"
);

const origin = String(
  process.env.TT_CODE_PRODUCTION_ORIGIN || "https://ai.yingliangads.com"
).replace(/\/$/, "");
const executablePath = process.env.TT_CODE_CHROMIUM_EXECUTABLE || undefined;
const expectedTitle = "\u8f93\u5165\u4ee3\u7801\uff0c\u7ee7\u7eed\u89c2\u770b";
const expectedGuideTitle = "\u5728\u54ea\u91cc\u627e\u4ee3\u7801";
const guideAssetPattern =
  /^\/tt-drama-code-assets\/tt-code-location-guide\.[a-f0-9]{12}\.webp$/;
const expectedDirectRouteMode = String(
  process.env.TT_CODE_EXPECT_DIRECT_ROUTE_MODE || "generic_fallback"
);
const requiredParameters = [
  "af_dp",
  "c",
  "af_channel",
  "af_c_id"
];
const publishedRecordParameters = [
  "af_adset",
  "af_adset_id",
  "af_ad",
  "af_ad_id"
];

assert.ok(
  ["generic_fallback", "published_clone"].includes(expectedDirectRouteMode),
  "TT_CODE_EXPECT_DIRECT_ROUTE_MODE must be generic_fallback or published_clone"
);

function assertTarget(targetUrl, expectedChannel, expectedContentId, routeMode) {
  const target = new URL(targetUrl);
  assert.equal(target.origin, "https://www.dramawavew2a.com");
  assert.equal(target.pathname, "/ads/101/2250/view");
  assert.equal(target.searchParams.get("af_dp"), expectedContentId);
  assert.equal(target.searchParams.get("af_channel"), expectedChannel);
  const expectedNames = routeMode === "generic_fallback"
    ? requiredParameters
    : requiredParameters.concat(publishedRecordParameters);
  assert.deepEqual(
    Array.from(target.searchParams.keys()).sort(),
    expectedNames.slice().sort(),
    "target parameter names do not match route mode"
  );
  for (const name of expectedNames) {
    assert.equal(
      target.searchParams.getAll(name).length,
      1,
      "target parameter must occur exactly once: " + name
    );
    assert.ok(target.searchParams.get(name), "missing target parameter: " + name);
  }
  if (routeMode === "generic_fallback") {
    assert.equal(target.searchParams.get("c"), "TTpost");
    assert.equal(target.searchParams.get("af_c_id"), "0001");
  }
}

function assertResolver(payload, expectedChannel, expectedContentId) {
  assert.equal(payload.found, true);
  assert.equal(payload.item.query_type, "content_id");
  assert.equal(payload.item.content_id, expectedContentId);
  assert.equal(payload.item.route_mode, expectedDirectRouteMode);
  assertTarget(
    payload.item.target_url,
    expectedChannel,
    expectedContentId,
    expectedDirectRouteMode
  );
  return payload.item.target_url;
}

async function openChinesePage(browser, disableCache) {
  const requests = [];
  const redirectedTargets = [];
  const resolverResults = [];
  const context = await browser.newContext({
    locale: "zh-CN",
    viewport: { width: 390, height: 844 }
  });
  await context.route("https://static-v1.mydramawave.com/**", route => {
    route.abort();
  });
  await context.route("https://www.dramawavew2a.com/**", route => {
    redirectedTargets.push(route.request().url());
    route.fulfill({
      status: 200,
      contentType: "text/html; charset=utf-8",
      body: "redirect intercepted"
    });
  });
  await context.route(origin + "/api/public/tt-code/resolve?**", async route => {
    const response = await route.fetch();
    const body = await response.body();
    const requestUrl = new URL(route.request().url());
    resolverResults.push({
      source: requestUrl.searchParams.get("source"),
      payload: JSON.parse(body.toString("utf8"))
    });
    await route.fulfill({ response, body });
  });
  const page = await context.newPage();
  page.on("request", request => requests.push(request.url()));
  if (disableCache) {
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
  }
  const response = await page.goto(origin + "/tt", {
    waitUntil: "domcontentloaded",
    timeout: 30000
  });
  assert.ok(response);
  assert.equal(response.status(), 200);
  const responseHtml = await response.text();
  const firstPaint = await page.evaluate(() => {
    const navigation = performance.getEntriesByType("navigation")[0];
    return {
      lang: document.documentElement.lang,
      locale: document.documentElement.dataset.initialLocale,
      title: document.querySelector("#page-title").textContent.trim(),
      dcl: navigation.domContentLoadedEventEnd,
      responseEnd: navigation.responseEnd
    };
  });
  assert.equal(response.headers()["content-language"], "zh-hans");
  assert.equal(firstPaint.lang, "zh-Hans");
  assert.equal(firstPaint.locale, "zh-hans");
  assert.equal(firstPaint.title, expectedTitle);
  assert.ok(responseHtml.includes('data-initial-locale="zh-hans"'));
  assert.ok(responseHtml.includes("\u8f93\u5165\u4ee3\u7801\uff0c"));
  assert.ok(responseHtml.includes("\u7ee7\u7eed\u89c2\u770b"));
  assert.ok(responseHtml.includes(expectedGuideTitle));
  assert.equal(
    (responseHtml.match(/tt-code-location-guide\.[a-f0-9]{12}\.webp/g) || []).length,
    2
  );
  assert.ok(!responseHtml.includes("Enter the code and keep watching"));
  await page.waitForFunction(() => {
    const stories = document.querySelector("#stories");
    return stories && stories.dataset.cacheState === "dynamic";
  }, null, { timeout: 10000 });
  const featuredReady = await page.evaluate(() => performance.now());
  assert.equal(await page.locator("#stories .story-link").count(), 5);
  assert.equal(
    await page.locator("#stories").getAttribute("data-language"),
    "zh-tw"
  );
  await page.waitForFunction(() => {
    const images = Array.from(document.querySelectorAll("#stories img"));
    return images.length === 5 && images.every(image => (
      image.complete && image.naturalWidth > 0
    ));
  }, null, { timeout: 10000 });
  const imagesReady = await page.evaluate(() => performance.now());
  const imageSources = await page.locator("#stories img").evaluateAll(images => (
    images.map(image => new URL(image.currentSrc || image.src).pathname)
  ));
  assert.ok(imageSources.every(pathname => (
    /^\/tt-featured-covers\/[a-f0-9]{64}\.webp$/.test(pathname)
  )));
  const rankingRequests = requests.filter(url => (
    url.includes("/api/public/tt-drama/featured-by-language/")
  ));
  assert.equal(rankingRequests.length, 1);
  assert.ok(rankingRequests[0].endsWith("/zh-tw.json"));
  assert.ok(!requests.some(url => (
    new URL(url).pathname === "/api/public/tt-drama/featured-by-language"
  )));
  assert.ok(requests.some(url => (
    /\/tt-drama-code-assets\/tt-drama-code-search\.[a-f0-9]{12}\.js$/.test(
      new URL(url).pathname
    )
  )));
  assert.equal(
    requests.filter(url => guideAssetPattern.test(new URL(url).pathname)).length,
    1,
    "the thumbnail and expanded guide must share one network request"
  );
  return {
    context,
    page,
    requests,
    redirectedTargets,
    resolverResults,
    timings: {
      response_end_ms: Math.round(firstPaint.responseEnd),
      dom_content_loaded_ms: Math.round(firstPaint.dcl),
      featured_dynamic_ms: Math.round(featuredReady),
      five_images_ready_ms: Math.round(imagesReady)
    }
  };
}

async function main() {
  const browser = await chromium.launch({
    headless: true,
    executablePath,
    timeout: 30000
  });
  const coldTimings = [];
  try {
    for (let index = 0; index < 3; index += 1) {
      const run = await openChinesePage(browser, true);
      coldTimings.push(run.timings);
      await run.context.close();
    }

    const guide = await openChinesePage(browser, false);
    assert.equal(
      await guide.page.locator("#code-guide-title").textContent(),
      expectedGuideTitle
    );
    assert.equal(await guide.page.locator("#code-guide").getAttribute("open"), null);
    assert.equal(
      await guide.page.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth
      ),
      true
    );
    await guide.page.locator("#code-guide summary").click();
    await guide.page.waitForFunction(() => {
      const image = document.querySelector(".code-guide-image");
      return image && image.complete && image.naturalWidth === 720;
    });
    assert.equal(
      await guide.page.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth
      ),
      true
    );
    await guide.page.locator("#code-guide summary").click();
    await guide.page.locator("#drama-query").fill("ZZZZ");
    await guide.page.locator("#search-form").evaluate(form => {
      form.requestSubmit();
    });
    await guide.page.waitForFunction(() => {
      const helper = document.querySelector("#search-helper");
      return helper && helper.classList.contains("error");
    });
    assert.equal(await guide.page.locator("#code-guide").isVisible(), true);
    assert.equal(await guide.page.locator("#code-guide").getAttribute("open"), null);
    assert.equal(guide.redirectedTargets.length, 0);
    await guide.context.close();

    const featured = await openChinesePage(browser, false);
    const firstStory = featured.page.locator("#stories .story-link").first();
    const featuredContentId = await firstStory.getAttribute("data-content-id");
    assert.ok(featuredContentId);
    await Promise.all([
      featured.page.waitForURL("https://www.dramawavew2a.com/**", {
        timeout: 15000
      }),
      firstStory.click()
    ]);
    const featuredResult = featured.resolverResults.find(result => (
      result.source === "Featured"
    ));
    assert.ok(featuredResult, "missing Featured resolver payload");
    const featuredTarget = assertResolver(
      featuredResult.payload,
      "Featured",
      featuredContentId
    );
    assert.equal(featured.redirectedTargets.length, 1);
    assert.equal(featured.redirectedTargets[0], featuredTarget);
    await featured.context.close();

    const search = await openChinesePage(browser, false);
    await search.page.locator("#drama-query").fill(featuredContentId);
    await search.page.locator("#search-form").evaluate(form => {
      form.requestSubmit();
    });
    await search.page.locator("#result.visible").waitFor({ timeout: 15000 });
    assert.equal(await search.page.locator("#code-guide").isVisible(), false);
    assert.equal(await search.page.locator("#code-guide").getAttribute("open"), null);
    const searchResult = search.resolverResults.find(result => (
      result.source === "Search"
    ));
    assert.ok(searchResult, "missing Search resolver payload");
    const searchTarget = assertResolver(
      searchResult.payload,
      "Search",
      featuredContentId
    );
    const continueTarget = await search.page.locator("#continue-link").getAttribute("href");
    assert.ok(continueTarget);
    assert.equal(continueTarget, searchTarget);
    await Promise.all([
      search.page.waitForURL("https://www.dramawavew2a.com/**", {
        timeout: 15000
      }),
      search.page.locator("#continue-link").click()
    ]);
    assert.equal(search.redirectedTargets.length, 1);
    assert.equal(search.redirectedTargets[0], searchTarget);
    await search.context.close();

    console.log(JSON.stringify({
      status: "ok",
      origin,
      cold_runs: coldTimings,
      featured_content_id: featuredContentId,
      direct_route_mode: expectedDirectRouteMode,
      guide_asset_requests_per_page: 1,
      guide_invalid_code: "retained",
      featured_redirect: "intercepted",
      search_redirect: "intercepted"
    }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
