"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const bridge = require("../static/tt-drama-code-search.js");

const ROOT = path.resolve(__dirname, "..");
let assertions = 0;

function equal(actual, expected, message) {
  assertions += 1;
  assert.equal(actual, expected, message);
}

function deepEqual(actual, expected, message) {
  assertions += 1;
  assert.deepEqual(actual, expected, message);
}

function ok(value, message) {
  assertions += 1;
  assert.ok(value, message);
}

function match(value, pattern, message) {
  assertions += 1;
  assert.match(value, pattern, message);
}

function throws(callback, pattern, message) {
  assertions += 1;
  assert.throws(callback, pattern, message);
}

function doesNotThrow(callback, message) {
  assertions += 1;
  assert.doesNotThrow(callback, message);
}

function targetUrl(contentId, channel, extras) {
  const url = new URL(
    bridge.TARGET_ORIGIN + bridge.TARGET_PATH
  );
  url.searchParams.set("af_dp", contentId);
  url.searchParams.set(
    "c",
    "yingliang_post_owner*20260804153000noneen*Drama*tag*9001"
  );
  url.searchParams.set("af_adset", "DramaWave Page");
  url.searchParams.set("af_adset_id", "123");
  url.searchParams.set("af_ad", "video_contentid[" + contentId + "]");
  url.searchParams.set("af_ad_id", "456");
  url.searchParams.set("af_channel", channel);
  url.searchParams.set("af_c_id", "789");
  for (const [key, value] of Object.entries(extras || {})) {
    url.searchParams.set(key, value);
  }
  return url.href;
}

equal(bridge.CODE_RESOLVER_PATH, "/api/public/tt-code/resolve");
equal(
  bridge.FEATURED_PATH,
  "/api/public/tt-drama/featured-by-language"
);
equal(bridge.TARGET_ORIGIN, "https://www.dramawavew2a.com");
equal(bridge.TARGET_PATH, "/ads/101/2250/view");
equal(bridge.SEARCH_SOURCE, "Search");
equal(bridge.FEATURED_SOURCE, "Featured");
equal(bridge.FEATURED_TIMEOUT_MS, 4000);
equal(bridge.FEATURED_LANGUAGE_SCHEMA_VERSION, 3);

deepEqual(
  bridge.normalizeQuery(" a1b2 "),
  { query: "A1B2", queryType: "code" },
  "short codes must normalize to uppercase"
);
deepEqual(
  bridge.normalizeQuery("9876"),
  { query: "9876", queryType: "code" }
);
deepEqual(
  bridge.normalizeQuery(" l9rP6ey2CB "),
  { query: "l9rP6ey2CB", queryType: "content_id" },
  "Content ID case must remain unchanged"
);
throws(() => bridge.normalizeQuery("AB-C"), /four-character code/);
throws(() => bridge.normalizeQuery("short"), /four-character code/);
throws(() => bridge.normalizeQuery("x".repeat(33)), /four-character code/);
throws(() => bridge.requireContentId("ABCD"), /content_id/);

equal(
  bridge.buildCodeResolverUrl(
    "a1b2",
    "Search",
    "https://ai.yingliangads.com"
  ),
  "https://ai.yingliangads.com/api/public/tt-code/resolve?query=A1B2&source=Search"
);
equal(
  bridge.buildCodeResolverUrl(
    "l9rP6ey2CB",
    "Featured",
    "https://ai.yingliangads.com"
  ),
  "https://ai.yingliangads.com/api/public/tt-code/resolve?query=l9rP6ey2CB&source=Featured"
);
equal(
  bridge.buildFeaturedUrl("zh-tw", "https://ai.yingliangads.com"),
  "https://ai.yingliangads.com/api/public/tt-drama/featured-by-language/zh-tw.json"
);
equal(
  bridge.buildFeaturedBundleUrl("https://ai.yingliangads.com"),
  "https://ai.yingliangads.com/api/public/tt-drama/featured-by-language"
);
throws(
  () => bridge.buildFeaturedUrl("../../secret", "https://ai.yingliangads.com"),
  /language/
);
throws(
  () => bridge.buildCodeResolverUrl(
    "A1B2",
    "search",
    "https://ai.yingliangads.com"
  ),
  /source/
);
throws(
  () => bridge.buildCodeResolverUrl(
    "A1B2",
    "Search",
    "javascript:alert(1)"
  ),
  /origin/
);

const contentId = "l9rP6ey2CB";
const codeTarget = targetUrl(contentId, "TT");
const dramaMetadata = {
  title: "The Contract Bride",
  description: "A frozen drama description.",
  cover_url: "https://cdn.usrgrow.com/drama/contract-bride.jpg",
  language: "en",
  episode_count: 60
};
equal(
  bridge.validateTargetUrl(codeTarget, contentId),
  codeTarget,
  "a fixed 2250 target with approved fields must pass"
);

const codePayload = {
  found: true,
  item: {
    content_id: contentId,
    target_url: codeTarget,
    query_type: "code",
    route_mode: "code_exact",
    ...dramaMetadata
  }
};
deepEqual(
  bridge.normalizeCodeResolvePayload(codePayload, "a1b2", "Search"),
  {
    content_id: contentId,
    target_url: codeTarget,
    query_type: "code",
    route_mode: "code_exact",
    ...dramaMetadata
  }
);

const searchTarget = targetUrl(contentId, "Search");
deepEqual(
  bridge.normalizeCodeResolvePayload({
    found: true,
    item: {
      content_id: contentId,
      target_url: searchTarget,
      query_type: "content_id",
      route_mode: "published_clone",
      ...dramaMetadata
    }
  }, contentId, "Search"),
  {
    content_id: contentId,
    target_url: searchTarget,
    query_type: "content_id",
    route_mode: "published_clone",
    ...dramaMetadata
  }
);

const featuredTarget = targetUrl(contentId, "Featured");
equal(
  bridge.normalizeCodeResolvePayload({
    found: true,
    item: {
      content_id: contentId,
      target_url: featuredTarget,
      query_type: "content_id",
      route_mode: "generic_fallback",
      ...dramaMetadata
    }
  }, contentId, "Featured").route_mode,
  "generic_fallback"
);
throws(
  () => bridge.normalizeCodeResolvePayload({
    found: true,
    item: { ...codePayload.item, route_mode: "published_clone" }
  }, "A1B2", "Search"),
  /frozen TT target/
);
throws(
  () => bridge.normalizeCodeResolvePayload({
    found: true,
    item: {
      content_id: contentId,
      target_url: searchTarget,
      query_type: "content_id",
      route_mode: "code_exact"
    }
  }, contentId, "Search"),
  /route_mode/
);
throws(
  () => bridge.normalizeCodeResolvePayload({
    found: true,
    item: {
      content_id: contentId,
      target_url: featuredTarget,
      query_type: "content_id",
      route_mode: "published_clone"
    }
  }, contentId, "Search"),
  /source mismatch/
);
throws(
  () => bridge.normalizeCodeResolvePayload({
    found: true,
    item: { ...codePayload.item, query_type: "content_id" }
  }, "A1B2", "Search"),
  /query_type mismatch/
);
throws(
  () => bridge.normalizeCodeResolvePayload(
    { found: false, item: codePayload.item },
    "A1B2",
    "Search"
  ),
  /payload/
);

function mutateTarget(callback) {
  const url = new URL(codeTarget);
  callback(url);
  return url.href;
}

throws(
  () => bridge.validateTargetUrl(
    codeTarget.replace("https://", "http://"),
    contentId
  ),
  /Untrusted/
);
throws(
  () => bridge.validateTargetUrl(
    codeTarget.replace(
      "www.dramawavew2a.com",
      "www.dramawavew2a.com.evil.example"
    ),
    contentId
  ),
  /Untrusted/
);
throws(
  () => bridge.validateTargetUrl(
    codeTarget.replace("/ads/101/2250/view", "/ads/101/2049/view"),
    contentId
  ),
  /Untrusted/
);
throws(
  () => bridge.validateTargetUrl(
    mutateTarget((url) => {
      url.hash = "redirect";
    }),
    contentId
  ),
  /Untrusted/
);
throws(
  () => bridge.validateTargetUrl(
    mutateTarget((url) => {
      url.searchParams.set("af_dp", "DIFFERENT1");
    }),
    contentId
  ),
  /mismatch/
);
throws(
  () => bridge.validateTargetUrl(
    codeTarget + "&af_dp=" + contentId,
    contentId
  ),
  /parameters/
);
throws(
  () => bridge.validateTargetUrl(
    codeTarget + "&utm_source=unsafe",
    contentId
  ),
  /parameters/
);
throws(
  () => bridge.validateTargetUrl(
    mutateTarget((url) => {
      url.searchParams.delete("c");
    }),
    contentId
  ),
  /Incomplete/
);
throws(
  () => bridge.validateTargetUrl(
    mutateTarget((url) => {
      url.searchParams.set("af_channel", "External");
    }),
    contentId
  ),
  /channel/
);

deepEqual(
  bridge.normalizeDramaPayload({
    found: true,
    data: {
      content_id: contentId,
      title: "Story",
      description: "Description",
      cover_url: "https://static-v1.mydramawave.com/story.jpg",
      language: "en",
      episode_count: 80
    }
  }, contentId),
  {
    content_id: contentId,
    title: "Story",
    description: "Description",
    cover_url: "https://static-v1.mydramawave.com/story.jpg",
    language: "en",
    episode_count: 80
  }
);
throws(
  () => bridge.normalizeDramaPayload({
    found: true,
    data: { content_id: "DIFFERENT1" }
  }, contentId),
  /payload/
);

const featuredNow = Date.parse("2026-08-04T15:30:00+08:00");
const featuredPayload = {
  schema_version: 1,
  source_date: "2026-08-03",
  generated_at: "2026-08-04T15:30:00+08:00",
  items: Array.from({ length: 5 }, (_unused, index) => ({
    content_id: "DRAMA0000" + (index + 1),
    title: "Drama " + (index + 1),
    cover_url:
      "https://static-v1.mydramawave.com/drama-" + (index + 1) + ".jpg",
    language: "en",
    episode_count: 80
  }))
};
equal(
  bridge.normalizeFeaturedPayload(featuredPayload, featuredNow).items.length,
  5
);
throws(
  () => bridge.normalizeFeaturedPayload({
    ...featuredPayload,
    items: featuredPayload.items.slice(0, 4)
  }, featuredNow),
  /payload/
);
throws(
  () => bridge.normalizeFeaturedPayload({
    ...featuredPayload,
    items: featuredPayload.items.map((item, index) => (
      index === 0 ? { ...item, spend: 999 } : item
    ))
  }, featuredNow),
  /incomplete/
);
throws(
  () => bridge.normalizeFeaturedPayload({
    ...featuredPayload,
    generated_at: "2026-07-31T15:29:59+08:00"
  }, featuredNow),
  /stale/
);
equal(
  bridge.shanghaiYesterday(Date.parse("2026-08-04T00:30:00+08:00")),
  "2026-08-03"
);

equal(bridge.normalizeLanguageTag(" PT_BR "), "pt-br");
equal(bridge.normalizeLanguageTag("bad tag"), "");
deepEqual(bridge.localeCandidates("zh-CN"), ["zh-hans"]);
deepEqual(bridge.localeCandidates("zh-HK"), ["zh-tw"]);
deepEqual(bridge.localeCandidates("pt-BR"), ["pt-br", "pt"]);
deepEqual(bridge.localeCandidates("fil-PH"), ["fil-ph", "tl"]);
deepEqual(
  bridge.getBrowserLanguages({
    languages: [" ES_MX ", "en-US", "es-MX"],
    language: "en-US"
  }),
  ["es-mx", "en-us"]
);
equal(bridge.resolveUiLocale(["zh-CN", "en-US"]), "zh-hans");
equal(bridge.resolveUiLocale(["zh-TW", "en-US"]), "zh-tw");
equal(bridge.resolveUiLocale(["xx-ZZ"]), "en");
equal(bridge.featuredLanguageForLocale("zh-hans"), "zh-tw");
equal(bridge.featuredLanguageForLocale("zh-tw"), "zh-tw");
equal(bridge.featuredLanguageForLocale("pt"), "pt");
equal(bridge.featuredLanguageForLocale("nl"), "en");
equal(
  bridge.isSafeFeaturedThumbnail(
    "/tt-featured-covers/" + "a".repeat(64) + ".webp"
  ),
  true
);
equal(
  bridge.isSafeFeaturedThumbnail(
    "/tt-featured-covers/../" + "a".repeat(64) + ".webp"
  ),
  false
);
deepEqual(
  bridge.rankingLanguageCandidates(["zh-CN", "en-US"]),
  ["zh-tw", "en-us", "en"]
);
deepEqual(
  bridge.rankingLanguageCandidates(["fil-PH"]),
  ["fil-ph", "tl", "en"]
);
deepEqual(
  bridge.rankingLanguageCandidates(["nl-NL"]),
  ["en"],
  "a browser language without UI copy must use the English ranking"
);
equal(
  bridge.copyText("zh-hans", "episodes", { count: 12 }),
  "12 集"
);
deepEqual(
  Object.keys(bridge.COPY).sort(),
  [
    "ar", "cs", "de", "el", "en", "es", "fr", "hi", "id", "it",
    "ja", "ko", "ms", "pl", "pt", "ro", "ru", "th", "tl", "tr",
    "vi", "zh-hans", "zh-tw"
  ],
  "all production drama languages plus Simplified Chinese UI must exist"
);
for (const [locale, copy] of Object.entries(bridge.COPY)) {
  deepEqual(
    Object.keys(copy).sort(),
    Object.keys(bridge.COPY.en).sort(),
    locale + " must define every UI copy key"
  );
}

function languageItems(language, prefix) {
  return Array.from({ length: 5 }, (_unused, index) => ({
    content_id: prefix + "DRAMA00" + (index + 1),
    title: language.toUpperCase() + " Drama " + (index + 1),
    cover_url:
      "https://static-v1.mydramawave.com/" + language + "-" +
      (index + 1) + ".jpg",
    language,
    episode_count: 80
  }));
}

const featuredLanguagePayload = {
  schema_version: 3,
  source_date: "2026-08-03",
  generated_at: "2026-08-04T15:30:00+08:00",
  language: "en",
  items: languageItems("en", "EN").map((item, index) => (
    index === 0
      ? {
          ...item,
          thumbnail_url:
            "/tt-featured-covers/" + "a".repeat(64) + ".webp"
        }
      : item
  ))
};
const normalizedLanguagePayload = bridge.normalizeFeaturedLanguagePayload(
  featuredLanguagePayload,
  "en",
  featuredNow
);
equal(normalizedLanguagePayload.language, "en");
equal(normalizedLanguagePayload.items.length, 5);
equal(
  normalizedLanguagePayload.items[0].thumbnail_url,
  "/tt-featured-covers/" + "a".repeat(64) + ".webp"
);
equal(normalizedLanguagePayload.items[1].thumbnail_url, "");
throws(
  () => bridge.normalizeFeaturedLanguagePayload(
    { ...featuredLanguagePayload, language: "es" },
    "en",
    featuredNow
  ),
  /payload/
);
throws(
  () => bridge.normalizeFeaturedLanguagePayload({
    ...featuredLanguagePayload,
    items: featuredLanguagePayload.items.map((item, index) => (
      index === 0 ? { ...item, thumbnail_url: "/unsafe.webp" } : item
    ))
  }, "en", featuredNow),
  /invalid/
);
throws(
  () => bridge.normalizeFeaturedLanguagePayload({
    ...featuredLanguagePayload,
    items: featuredLanguagePayload.items.map((item, index) => (
      index === 0 ? { ...item, spend: 10 } : item
    ))
  }, "en", featuredNow),
  /payload|fields/
);

const featuredBundle = {
  schema_version: 2,
  source_date: "2026-08-03",
  generated_at: "2026-08-04T15:30:00+08:00",
  default_language: "en",
  rankings: {
    en: languageItems("en", "EN"),
    es: languageItems("es", "ES"),
    nl: languageItems("nl", "NL"),
    pt: languageItems("pt", "PT"),
    "pt-br": languageItems("pt-br", "PB"),
    "zh-tw": languageItems("zh-tw", "ZH")
  }
};
const englishBundle = bridge.normalizeFeaturedBundle(
  featuredBundle,
  ["en-US"],
  featuredNow
);
equal(englishBundle.language, "en");
equal(englishBundle.items.length, 5);
equal(englishBundle.fallback, false);
const chineseBundle = bridge.normalizeFeaturedBundle(
  featuredBundle,
  ["zh-CN"],
  featuredNow
);
equal(chineseBundle.requested_language, "zh-tw");
equal(chineseBundle.language, "zh-tw");
equal(chineseBundle.items[0].language, "zh-tw");
equal(chineseBundle.fallback, false);
equal(
  bridge.normalizeFeaturedBundle(
    featuredBundle,
    ["pt-BR"],
    featuredNow
  ).language,
  "pt-br",
  "an exact regional bucket must win before its base language"
);
const missingBundle = bridge.normalizeFeaturedBundle(
  featuredBundle,
  ["de-DE"],
  featuredNow
);
equal(missingBundle.language, "en");
equal(missingBundle.fallback, true);
equal(
  bridge.normalizeFeaturedBundle(
    featuredBundle,
    ["nl-NL"],
    featuredNow
  ).language,
  "en",
  "an untranslated browser language must ignore a matching future bucket"
);
throws(
  () => bridge.normalizeFeaturedBundle({
    ...featuredBundle,
    rankings: {
      ...featuredBundle.rankings,
      x: languageItems("x", "XX")
    }
  }, ["en"], featuredNow),
  /bucket/
);
throws(
  () => bridge.normalizeFeaturedBundle({
    ...featuredBundle,
    rankings: {
      ...featuredBundle.rankings,
      es: featuredBundle.rankings.es.slice(0, 4)
    }
  }, ["es"], featuredNow),
  /bucket/
);
throws(
  () => bridge.normalizeFeaturedBundle({
    ...featuredBundle,
    rankings: {
      ...featuredBundle.rankings,
      es: featuredBundle.rankings.es.map((item, index) => (
        index === 0 ? { ...item, spend_n: 100 } : item
      ))
    }
  }, ["es"], featuredNow),
  /bundle/
);
throws(
  () => bridge.normalizeFeaturedBundle({
    ...featuredBundle,
    rankings: {
      ...featuredBundle.rankings,
      es: featuredBundle.rankings.es.map((item, index) => (
        index === 0
          ? { ...item, content_id: featuredBundle.rankings.en[0].content_id }
          : item
      ))
    }
  }, ["es"], featuredNow),
  /invalid/
);
throws(
  () => bridge.normalizeFeaturedBundle({
    ...featuredBundle,
    rankings: {
      ...featuredBundle.rankings,
      es: featuredBundle.rankings.es.map((item, index) => (
        index === 0 ? { ...item, language: "en" } : item
      ))
    }
  }, ["es"], featuredNow),
  /invalid/
);
throws(
  () => bridge.normalizeFeaturedBundle({
    ...featuredBundle,
    generated_at: "2026-07-31T15:29:59+08:00"
  }, ["en"], featuredNow),
  /stale/
);

const tracker = bridge.createDragTracker(7);
tracker.begin(100, 200);
deepEqual(tracker.move(96), { dragged: false, scrollLeft: 200 });
deepEqual(tracker.move(90), { dragged: true, scrollLeft: 210 });
equal(tracker.end(), true);
equal(tracker.consumeSuppressedClick(), true);
equal(tracker.consumeSuppressedClick(), false);
tracker.begin(50, 0);
deepEqual(tracker.move(70), { dragged: true, scrollLeft: 0 });
tracker.cancel();
equal(tracker.consumeSuppressedClick(), false);
equal(bridge.getCarouselStep(390), 304);
equal(bridge.getCarouselStep(100), 129);

const html = fs.readFileSync(
  path.join(ROOT, "static", "tt-drama-code-search.html"),
  "utf8"
);
const script = fs.readFileSync(
  path.join(ROOT, "static", "tt-drama-code-search.js"),
  "utf8"
);
const codeNginx = fs.readFileSync(
  path.join(ROOT, "deploy", "nginx", "tt-drama-code-search.conf"),
  "utf8"
);
const primaryNginx = fs.readFileSync(
  path.join(ROOT, "deploy", "nginx", "tt-drama-search.conf"),
  "utf8"
);
const localeMapNginx = fs.readFileSync(
  path.join(ROOT, "deploy", "nginx", "tt-drama-code-locale-map.conf"),
  "utf8"
);
const generatedLocaleDirectory = path.join(
  ROOT,
  "static",
  "tt-drama-code-locales"
);
const generatedAssetDirectory = path.join(
  ROOT,
  "static",
  "tt-drama-code-assets"
);

ok(html.includes('src="/tt-drama-code-search.js"'));
ok(!html.includes('src="/tt-drama-search.js"'));
ok(html.includes('data-initial-locale="en"'));
equal(
  (html.match(/class="story story-skeleton"/g) || []).length,
  5,
  "the first HTML paint must include five featured skeletons"
);
ok(html.includes('id="stories-previous"'));
ok(html.includes('id="stories-next"'));
ok(html.includes("Enter the code and"));
ok(/id="page-title-accent"[^>]*>keep watching<\/span>/.test(html));
ok(!html.includes('class="intro"'));
ok(html.includes("overflow-x: auto;"));
ok(html.includes("scroll-snap-type: x proximity;"));
ok(html.includes("touch-action: pan-x pan-y;"));
ok(!html.includes("form-action 'none'; frame-ancestors"));
ok(script.includes('addEventListener("pointerdown"'));
ok(script.includes('addEventListener("pointermove"'));
ok(script.includes('addEventListener("pointerup"'));
ok(script.includes('addEventListener("pointercancel"'));
ok(script.includes('queryInput.addEventListener("input"'));
ok(script.includes("activeController.abort()"));
ok(script.includes("continueLink.removeAttribute(\"href\")"));
const pointerDownSource = script.slice(
  script.indexOf('container.addEventListener("pointerdown"'),
  script.indexOf('root.addEventListener("pointermove"')
);
const pointerMoveSource = script.slice(
  script.indexOf('root.addEventListener("pointermove"'),
  script.indexOf('root.addEventListener("pointerup"')
);
ok(
  !pointerDownSource.includes("setPointerCapture(event.pointerId)"),
  "ordinary card presses must not change the eventual click target"
);
ok(
  pointerMoveSource.includes("event.buttons === 0") &&
    pointerMoveSource.includes("finishPointer(event, true)"),
  "a released pointer returning from outside must clear stale drag state"
);
ok(
  pointerMoveSource.includes("container.setPointerCapture(event.pointerId)"),
  "the carousel may capture only after the drag threshold is crossed"
);
ok(script.includes('root.addEventListener("pointercancel"'));
const lostCaptureSource = script.slice(
  script.indexOf('container.addEventListener("lostpointercapture"'),
  script.indexOf('container.addEventListener("click"')
);
ok(
  lostCaptureSource.includes("finishPointer(event, false)"),
  "losing capture after a real drag must retain click suppression"
);
ok(script.includes("container.releasePointerCapture(event.pointerId)"));
ok(script.includes("event.stopImmediatePropagation()"));
ok(script.includes("payload.code || payload.error"));
ok(script.includes("resolveAndVerify("));
ok(script.includes("root.location.assign(resolved.route.target_url)"));
ok(script.includes('image.loading = index < 3 ? "eager" : "lazy"'));
ok(script.includes('image.fetchPriority = "high"'));
ok(script.includes("usedOriginalFallback"));
ok(
  script.includes("const resolved = await resolveCodeQuery"),
  "the public code endpoint must return the verified route and drama together"
);
ok(codeNginx.includes("location = /tt-code {"));
ok(codeNginx.includes("location = /tt-drama-code-search.js {"));
ok(codeNginx.includes("/tt-drama-code-assets/tt-drama-code-search\\."));
ok(codeNginx.includes("location = /api/public/tt-code/resolve {"));
ok(codeNginx.includes("location = /api/public/tt-drama/featured-by-language {"));
ok(codeNginx.includes("featured-by-language/(?<tt_featured_language>"));
ok(codeNginx.includes("tt-featured-covers/"));
ok(codeNginx.includes("proxy_pass http://127.0.0.1:8787;"));
ok(codeNginx.includes('Cache-Control "no-store" always;'));
ok(codeNginx.includes('max-age=31536000, immutable'));
ok(codeNginx.includes("gzip_types application/javascript;"));
ok(codeNginx.includes("gzip_types application/json;"));
ok(codeNginx.includes("connect-src 'self'"));
ok(!codeNginx.includes("script-src 'unsafe-inline'"));
ok(!codeNginx.includes("location = /tt {"));
ok(!codeNginx.includes("location = /tt-drama-search.js {"));
ok(primaryNginx.includes("location = /tt {"));
ok(
  primaryNginx.includes(
    "alias /usr/share/nginx/html/tt-drama-code-locales/$tt_drama_code_locale.html;"
  )
);
ok(!primaryNginx.includes("alias /usr/share/nginx/html/tt-drama-search.html;"));
ok(primaryNginx.includes('add_header Pragma "no-cache" always;'));
ok(primaryNginx.includes("add_header Content-Language $tt_drama_code_locale always;"));
ok(primaryNginx.includes('add_header Vary "Accept-Language" always;'));
ok(primaryNginx.includes("gzip on;"));
ok(primaryNginx.includes('add_header X-Frame-Options "DENY" always;'));
ok(primaryNginx.includes("limit_except GET"));
equal(
  (primaryNginx + "\n" + codeNginx).match(/location = \/tt \{/g).length,
  1,
  "the exact /tt location must be declared once"
);

ok(localeMapNginx.includes("map $http_accept_language $tt_drama_code_locale"));
ok(localeMapNginx.includes("default en;"));
ok(localeMapNginx.includes(
  "zh-(tw|hk|mo|hant)(?:[-,;[:space:]]|$)\" zh-tw;"
));
ok(localeMapNginx.includes(
  "zh(?:[-,;[:space:]]|$)\" zh-hans;"
));
const mappedLocales = Array.from(localeMapNginx.matchAll(
  /^\s+(?:default|"~\*[^"]+")\s+([a-z0-9-]+);$/gm
)).map(matchValue => matchValue[1]);
deepEqual(
  Array.from(new Set(mappedLocales)).sort(),
  Object.keys(bridge.COPY).sort(),
  "the Accept-Language map must cover every generated UI locale"
);

doesNotThrow(
  () => childProcess.execFileSync(
    process.execPath,
    ["scripts/build_tt_drama_code_assets.js", "--check"],
    { cwd: ROOT, encoding: "utf8", stdio: "pipe" }
  ),
  "generated locale HTML and content-addressed JS must be current"
);
const localeFileNames = fs.readdirSync(generatedLocaleDirectory).sort();
deepEqual(
  localeFileNames,
  Object.keys(bridge.COPY).sort().map(locale => locale + ".html")
);
const assetFileNames = fs.readdirSync(generatedAssetDirectory).filter(name => (
  /^tt-drama-code-search\.[a-f0-9]{12}\.js$/.test(name)
));
equal(assetFileNames.length, 1);
const englishGeneratedHtml = fs.readFileSync(
  path.join(generatedLocaleDirectory, "en.html"),
  "utf8"
);
ok(englishGeneratedHtml.includes(
  'src="/tt-drama-code-assets/' + assetFileNames[0] + '"'
));
ok(!englishGeneratedHtml.includes("data-i18n-"));

for (const args of [
  [
    "diff",
    "--exit-code",
    "--",
    "static/tt-drama-search.html",
    "static/tt-drama-search.js"
  ],
  [
    "diff",
    "--cached",
    "--exit-code",
    "--",
    "static/tt-drama-search.html",
    "static/tt-drama-search.js"
  ]
]) {
  doesNotThrow(
    () => childProcess.execFileSync("git", args, {
      cwd: ROOT,
      encoding: "utf8",
      stdio: "pipe"
    }),
    "legacy /tt static rollback files must remain unchanged"
  );
}

match(
  JSON.stringify({ status: "ok", assertions }),
  /"status":"ok"/
);
console.log(JSON.stringify({
  status: "ok",
  assertions,
  page: "/tt",
  compatibility_page: "/tt-code",
  code_api: bridge.CODE_RESOLVER_PATH
}));
