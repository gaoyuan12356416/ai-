"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const playwrightPackage = process.env.TT_CODE_PLAYWRIGHT_PACKAGE || "playwright";
const { chromium } = require(playwrightPackage);
const bridge = require("../static/tt-drama-code-search.js");

const ROOT = path.resolve(__dirname, "..");
const SCRIPT = fs.readFileSync(
  path.join(ROOT, "static", "tt-drama-code-search.js"),
  "utf8"
);
const LOCALE_DIRECTORY = path.join(
  ROOT,
  "static",
  "tt-drama-code-locales"
);
const ASSET_DIRECTORY = path.join(
  ROOT,
  "static",
  "tt-drama-code-assets"
);
const SCRIPT_ASSET_NAME = fs.readdirSync(ASSET_DIRECTORY).find(name => (
  /^tt-drama-code-search\.[a-f0-9]{12}\.js$/.test(name)
));
assert.ok(SCRIPT_ASSET_NAME);

function localeForAcceptLanguage(value) {
  const primary = String(value || "")
    .split(",", 1)[0]
    .trim()
    .toLowerCase()
    .replace(/_/g, "-");
  if (/^zh-(?:tw|hk|mo|hant)/.test(primary)) {
    return "zh-tw";
  }
  if (primary.startsWith("zh")) {
    return "zh-hans";
  }
  const base = primary.split("-")[0] === "fil"
    ? "tl"
    : primary.split("-")[0];
  return Object.prototype.hasOwnProperty.call(bridge.COPY, base) ? base : "en";
}

function localeHtml(locale) {
  return fs.readFileSync(path.join(LOCALE_DIRECTORY, locale + ".html"), "utf8");
}

function languageItems(language, prefix) {
  return Array.from({ length: 5 }, (_unused, index) => ({
    content_id: prefix + "BROWSER" + (index + 1),
    title: language.toUpperCase() + " Story " + (index + 1),
    cover_url:
      "https://static-v1.mydramawave.com/browser-" + language + "-" +
      (index + 1) + ".jpg",
    language,
    episode_count: 60 + index
  }));
}

function featuredLanguagePayload(language) {
  return {
    schema_version: 3,
    source_date: bridge.shanghaiYesterday(),
    generated_at: new Date().toISOString(),
    language,
    items: languageItems(
      language,
      language === "zh-tw" ? "ZH" : language.toUpperCase().slice(0, 2)
    )
  };
}

function targetUrl(contentId, channel) {
  const target = new URL(bridge.TARGET_ORIGIN + bridge.TARGET_PATH);
  target.searchParams.set("af_dp", contentId);
  target.searchParams.set(
    "c",
    "yingliang_post_browser*20260805120000noneen*Drama*test*1"
  );
  target.searchParams.set("af_adset", "Browser Test");
  target.searchParams.set("af_adset_id", "1");
  target.searchParams.set("af_ad", "browser_contentid[" + contentId + "]");
  target.searchParams.set("af_ad_id", "2");
  target.searchParams.set("af_channel", channel);
  target.searchParams.set("af_c_id", "3");
  return target.href;
}

function resolverPayload(requestUrl) {
  const query = String(requestUrl.searchParams.get("query") || "");
  const source = String(requestUrl.searchParams.get("source") || "");
  const isCode = /^[A-Z0-9]{4}$/.test(query);
  const contentId = isCode ? "ENBROWSER1" : query;
  const channel = isCode ? "TT" : source;
  return {
    found: true,
    item: {
      content_id: contentId,
      target_url: targetUrl(contentId, channel),
      query_type: isCode ? "code" : "content_id",
      route_mode: isCode ? "code_exact" : "published_clone",
      title: "Browser resolved story",
      description: "Verified in the local browser regression.",
      cover_url: "https://static-v1.mydramawave.com/browser-result.jpg",
      language: "en",
      episode_count: 60
    }
  };
}

function send(response, status, contentType, body, headers = {}) {
  response.writeHead(status, {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
    ...headers
  });
  response.end(body);
}

function startServer() {
  const requests = [];
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    requests.push(url.pathname + url.search);
    if (request.method !== "GET") {
      send(response, 405, "text/plain", "method not allowed");
    } else if (url.pathname === "/tt" || url.pathname === "/tt-code") {
      const locale = localeForAcceptLanguage(request.headers["accept-language"]);
      send(
        response,
        200,
        "text/html; charset=utf-8",
        localeHtml(locale),
        { "Content-Language": locale, Vary: "Accept-Language" }
      );
    } else if (
      url.pathname === "/tt-drama-code-search.js" ||
      url.pathname === "/tt-drama-code-assets/" + SCRIPT_ASSET_NAME
    ) {
      send(
        response,
        200,
        "application/javascript; charset=utf-8",
        SCRIPT,
        url.pathname.includes("/tt-drama-code-assets/")
          ? { "Cache-Control": "public, max-age=31536000, immutable" }
          : {}
      );
    } else if (url.pathname === bridge.FEATURED_PATH) {
      send(
        response,
        200,
        "application/json; charset=utf-8",
        JSON.stringify({ legacy: true })
      );
    } else if (url.pathname.startsWith(bridge.FEATURED_PATH + "/")) {
      const match = url.pathname.match(
        /^\/api\/public\/tt-drama\/featured-by-language\/([a-z]{2,3}(?:-[a-z0-9]{2,8})?)\.json$/
      );
      const language = match ? match[1] : "";
      if (!language || !["en", "ar", "zh-tw"].includes(language)) {
        send(response, 404, "application/json", JSON.stringify({ found: false }));
      } else {
        send(
          response,
          200,
          "application/json; charset=utf-8",
          JSON.stringify(featuredLanguagePayload(language))
        );
      }
    } else if (url.pathname === bridge.CODE_RESOLVER_PATH) {
      send(
        response,
        200,
        "application/json; charset=utf-8",
        JSON.stringify(resolverPayload(url))
      );
    } else {
      send(response, 404, "text/plain", "not found");
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, requests, port: server.address().port });
    });
  });
}

async function openPage(
  browser,
  origin,
  locale,
  entryPath = "/tt",
  viewport = { width: 720, height: 900 },
  acceptLanguage = ""
) {
  const context = await browser.newContext({
    locale,
    viewport
  });
  if (acceptLanguage) {
    await context.route(origin + "/**", route => {
      route.continue({
        headers: {
          ...route.request().headers(),
          "accept-language": acceptLanguage
        }
      });
    });
  }
  await context.route("https://static-v1.mydramawave.com/**", route => {
    route.abort();
  });
  await context.route("https://www.dramawavew2a.com/**", route => {
    route.fulfill({ status: 200, contentType: "text/html", body: "redirect ok" });
  });
  const page = await context.newPage();
  const response = await page.goto(origin + entryPath, { waitUntil: "networkidle" });
  await page.waitForFunction(() => {
    const stories = document.querySelector("#stories");
    return stories && stories.dataset.cacheState === "dynamic";
  });
  return { context, page, response };
}

async function openStaticFirstPaint(browser, origin, locale, entryPath = "/tt") {
  const context = await browser.newContext({
    locale,
    viewport: { width: 390, height: 844 }
  });
  await context.route(origin + "/tt-drama-code-assets/**", route => {
    route.abort();
  });
  const page = await context.newPage();
  const response = await page.goto(origin + entryPath, {
    waitUntil: "domcontentloaded"
  });
  return { context, page, response };
}

async function main() {
  const local = await startServer();
  const origin = "http://127.0.0.1:" + local.port;
  const launchOptions = { headless: true, timeout: 15000 };
  if (process.env.TT_CODE_CHROMIUM_EXECUTABLE) {
    launchOptions.executablePath = process.env.TT_CODE_CHROMIUM_EXECUTABLE;
  }
  let browser = null;
  let checks = 0;
  try {
    browser = await chromium.launch(launchOptions);
    const english = await openPage(browser, origin, "en-US");
    assert.equal(
      await english.page.locator("#page-title").textContent(),
      "Enter the code and keep watching"
    );
    checks += 1;
    assert.equal(english.response.headers()["content-language"], "en");
    checks += 1;
    assert.equal(await english.page.locator("p.intro").count(), 0);
    checks += 1;
    const brandBox = await english.page.locator(".brand-lockup").boundingBox();
    assert.ok(brandBox && brandBox.y >= 0 && brandBox.width > 100);
    checks += 1;
    assert.equal(await english.page.locator("html").getAttribute("lang"), "en");
    checks += 1;
    assert.equal(await english.page.locator("#stories .story").count(), 5);
    checks += 1;
    assert.equal(
      await english.page.locator("#stories").getAttribute("data-language"),
      "en"
    );
    checks += 1;
    assert.equal(await english.page.locator("#stories").getAttribute("aria-busy"), "false");
    checks += 1;
    if (process.env.TT_CODE_EN_SCREENSHOT_PATH) {
      await english.page.screenshot({
        path: process.env.TT_CODE_EN_SCREENSHOT_PATH,
        fullPage: true
      });
    }
    await english.page.locator("#drama-query").fill("a1b2");
    await english.page.locator("#search-form").evaluate(form => form.requestSubmit());
    await english.page.locator("#result.visible").waitFor();
    assert.match(
      await english.page.locator("#continue-link").getAttribute("href"),
      /af_channel=TT/
    );
    checks += 1;
    await english.context.close();

    const staticChinese = await openStaticFirstPaint(browser, origin, "zh-CN");
    assert.equal(staticChinese.response.headers()["content-language"], "zh-hans");
    checks += 1;
    assert.equal(
      await staticChinese.page.locator("#page-title").textContent(),
      bridge.copyText("zh-hans", "titleLead") +
        bridge.copyText("zh-hans", "titleAccent")
    );
    checks += 1;
    assert.equal(
      await staticChinese.page.locator("#stories .story-skeleton").count(),
      5
    );
    checks += 1;
    assert.equal(await staticChinese.page.locator("#drama-query").isVisible(), true);
    checks += 1;
    await staticChinese.context.close();

    const chinese = await openPage(browser, origin, "zh-CN");
    assert.equal(
      await chinese.page.locator("html").getAttribute("lang"),
      "zh-Hans"
    );
    checks += 1;
    assert.equal(
      await chinese.page.locator("#page-title").textContent(),
      "输入代码，继续观看"
    );
    checks += 1;
    assert.equal(
      await chinese.page.locator("#stories").getAttribute("data-language"),
      "zh-tw"
    );
    checks += 1;
    assert.equal(
      await chinese.page.locator("#stories .story-title").first().textContent(),
      "ZH-TW Story 1"
    );
    checks += 1;
    const screenshotPath = process.env.TT_CODE_SCREENSHOT_PATH;
    if (screenshotPath) {
      await chinese.page.screenshot({ path: screenshotPath, fullPage: true });
    }
    const second = chinese.page.locator("#stories .story").nth(1);
    const box = await second.boundingBox();
    assert.ok(box);
    await chinese.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await chinese.page.mouse.down();
    await chinese.page.mouse.move(box.x + 15, box.y + box.height / 2, {
      steps: 4
    });
    await chinese.page.mouse.up();
    await chinese.page.waitForTimeout(100);
    assert.equal(new URL(chinese.page.url()).pathname, "/tt");
    checks += 1;
    await chinese.page.locator("#stories .story-link").first().click();
    await chinese.page.waitForURL("https://www.dramawavew2a.com/**");
    assert.equal(new URL(chinese.page.url()).searchParams.get("af_channel"), "Featured");
    checks += 1;
    await chinese.context.close();

    const authoritativeServerLocale = await openPage(
      browser,
      origin,
      "zh-CN",
      "/tt",
      { width: 720, height: 900 },
      "en-US"
    );
    assert.equal(
      authoritativeServerLocale.response.headers()["content-language"],
      "en"
    );
    checks += 1;
    assert.equal(
      await authoritativeServerLocale.page.locator("html").getAttribute("lang"),
      "en"
    );
    checks += 1;
    assert.equal(
      await authoritativeServerLocale.page.locator("#page-title").textContent(),
      "Enter the code and keep watching"
    );
    checks += 1;
    await authoritativeServerLocale.context.close();

    const traditionalChinese = await openPage(browser, origin, "zh-TW");
    assert.equal(
      await traditionalChinese.page.locator("html").getAttribute("lang"),
      "zh-Hant"
    );
    checks += 1;
    assert.equal(
      await traditionalChinese.page.locator("#page-title").textContent(),
      "輸入代碼，繼續觀看"
    );
    checks += 1;
    assert.equal(
      await traditionalChinese.page.locator("#recent-title").textContent(),
      "昨日熱門短劇"
    );
    checks += 1;
    assert.equal(
      await traditionalChinese.page.locator("#stories").getAttribute(
        "data-language"
      ),
      "zh-tw"
    );
    checks += 1;
    await traditionalChinese.context.close();

    const arabic = await openPage(browser, origin, "ar");
    assert.equal(await arabic.page.locator("html").getAttribute("dir"), "rtl");
    checks += 1;
    assert.ok(
      ["normal", "0px"].includes(
        await arabic.page.locator("#page-title").evaluate(element =>
          getComputedStyle(element).letterSpacing
        )
      )
    );
    checks += 1;
    assert.equal(await arabic.page.locator("#stories").getAttribute("dir"), "ltr");
    checks += 1;
    assert.equal(
      await arabic.page.locator("#stories").getAttribute("data-language"),
      "ar"
    );
    checks += 1;
    await arabic.context.close();

    const fallback = await openPage(browser, origin, "bn-BD");
    assert.equal(await fallback.page.locator("html").getAttribute("lang"), "en");
    checks += 1;
    assert.equal(
      await fallback.page.locator("#stories").getAttribute("data-language"),
      "en"
    );
    checks += 1;
    assert.equal(
      await fallback.page.locator("#stories").getAttribute("data-language-fallback"),
      "false"
    );
    checks += 1;
    await fallback.context.close();

    const compatibility = await openPage(browser, origin, "en-US", "/tt-code");
    assert.equal(
      await compatibility.page.locator("#page-title").textContent(),
      "Enter the code and keep watching"
    );
    checks += 1;
    assert.equal(
      await compatibility.page.locator("#stories .story").count(),
      5
    );
    checks += 1;
    assert.equal(new URL(compatibility.page.url()).pathname, "/tt-code");
    checks += 1;
    assert.equal(compatibility.response.headers()["content-language"], "en");
    checks += 1;
    await compatibility.context.close();

    const mobile = await openPage(
      browser,
      origin,
      "en-US",
      "/tt",
      { width: 390, height: 844 }
    );
    assert.equal(await mobile.page.locator("#stories .story").count(), 5);
    checks += 1;
    assert.equal(
      await mobile.page.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth
      ),
      true
    );
    checks += 1;
    await mobile.context.close();

    const featuredRequests = local.requests.filter(value =>
      value.includes("source=Featured")
    );
    assert.equal(featuredRequests.length, 1);
    checks += 1;
    assert.equal(
      local.requests.filter(value => value === bridge.FEATURED_PATH).length,
      0,
      "the browser must not download the legacy all-language bundle"
    );
    checks += 1;
    const rankingRequests = local.requests.filter(value => (
      value.startsWith(bridge.FEATURED_PATH + "/")
    ));
    assert.ok(rankingRequests.some(value => value.endsWith("/en.json")));
    checks += 1;
    assert.ok(rankingRequests.some(value => value.endsWith("/zh-tw.json")));
    checks += 1;
    assert.ok(rankingRequests.some(value => value.endsWith("/ar.json")));
    checks += 1;
    console.log(JSON.stringify({
      status: "ok",
      checks,
      page: "/tt",
      compatibility_page: "/tt-code"
    }));
  } finally {
    if (browser) {
      await browser.close();
    }
    await new Promise(resolve => local.server.close(resolve));
  }
}

main().catch(error => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
