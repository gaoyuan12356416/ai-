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
equal(bridge.DRAMA_RESOLVER_PATH, "/api/public/tt-drama/resolve");
equal(bridge.FEATURED_PATH, "/api/public/tt-drama/featured");
equal(bridge.TARGET_ORIGIN, "https://www.dramawavew2a.com");
equal(bridge.TARGET_PATH, "/ads/101/2250/view");
equal(bridge.SEARCH_SOURCE, "Search");
equal(bridge.FEATURED_SOURCE, "Featured");

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
  bridge.buildDramaResolverUrl(
    "l9rP6ey2CB",
    "https://ai.yingliangads.com"
  ),
  "https://ai.yingliangads.com/api/public/tt-drama/resolve?content_id=l9rP6ey2CB"
);
equal(
  bridge.buildFeaturedUrl("https://ai.yingliangads.com"),
  "https://ai.yingliangads.com/api/public/tt-drama/featured"
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
    route_mode: "code_exact"
  }
};
deepEqual(
  bridge.normalizeCodeResolvePayload(codePayload, "a1b2", "Search"),
  {
    content_id: contentId,
    target_url: codeTarget,
    query_type: "code",
    route_mode: "code_exact"
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
      route_mode: "published_clone"
    }
  }, contentId, "Search"),
  {
    content_id: contentId,
    target_url: searchTarget,
    query_type: "content_id",
    route_mode: "published_clone"
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
      route_mode: "generic_fallback"
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
const nginx = fs.readFileSync(
  path.join(ROOT, "deploy", "nginx", "tt-drama-code-search.conf"),
  "utf8"
);

ok(html.includes('src="/tt-drama-code-search.js"'));
ok(!html.includes('src="/tt-drama-search.js"'));
ok(html.includes('id="stories-previous"'));
ok(html.includes('id="stories-next"'));
ok(html.includes("overflow-x: auto;"));
ok(html.includes("scroll-snap-type: x proximity;"));
ok(html.includes("touch-action: pan-x pan-y;"));
ok(!html.includes("form-action 'none'; frame-ancestors"));
ok(script.includes('addEventListener("pointerdown"'));
ok(script.includes('addEventListener("pointermove"'));
ok(script.includes('addEventListener("pointerup"'));
ok(script.includes('addEventListener("pointercancel"'));
ok(script.includes("setPointerCapture(event.pointerId)"));
ok(script.includes("event.stopImmediatePropagation()"));
ok(script.includes("payload.code || payload.error"));
ok(script.includes("resolveAndVerify("));
ok(script.includes("root.location.assign(resolved.route.target_url)"));
ok(
  script.indexOf("const route = await resolveCodeQuery") <
  script.indexOf("const drama = await resolveDrama"),
  "the route lookup must be followed by the legacy drama verification"
);
ok(nginx.includes("location = /tt-code {"));
ok(nginx.includes("location = /tt-drama-code-search.js {"));
ok(nginx.includes("location = /api/public/tt-code/resolve {"));
ok(nginx.includes("proxy_pass http://127.0.0.1:18829;"));
ok(nginx.includes('Cache-Control "no-store" always;'));
ok(nginx.includes("connect-src 'self'"));
ok(!nginx.includes("location = /tt {"));
ok(!nginx.includes("location = /tt-drama-search.js {"));

for (const args of [
  [
    "diff",
    "--exit-code",
    "--",
    "static/tt-drama-search.html",
    "static/tt-drama-search.js",
    "deploy/nginx/tt-drama-search.conf"
  ],
  [
    "diff",
    "--cached",
    "--exit-code",
    "--",
    "static/tt-drama-search.html",
    "static/tt-drama-search.js",
    "deploy/nginx/tt-drama-search.conf"
  ]
]) {
  doesNotThrow(
    () => childProcess.execFileSync("git", args, {
      cwd: ROOT,
      encoding: "utf8",
      stdio: "pipe"
    }),
    "legacy /tt files must remain unchanged"
  );
}

match(
  JSON.stringify({ status: "ok", assertions }),
  /"status":"ok"/
);
console.log(JSON.stringify({
  status: "ok",
  assertions,
  page: "/tt-code",
  code_api: bridge.CODE_RESOLVER_PATH
}));
