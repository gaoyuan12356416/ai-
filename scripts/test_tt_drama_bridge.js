"use strict";

const assert = require("node:assert/strict");
const bridge = require("../static/tt-drama-search.js");

const expectedExample =
  "https://www.dramawavew2a.com/ads/0/2049/view?af_dp=l9rP6ey2CB&c=TTpost&af_c_id=0001&af_adset_id=XXX";

assert.equal(
  bridge.buildW2AUrl("l9rP6ey2CB", "?af_adset_id=XXX"),
  expectedExample,
  "single tracking parameter must match the product example exactly"
);

assert.equal(
  bridge.buildResolverUrl("l9rP6ey2CB", "https://ai.yingliangads.com"),
  "https://ai.yingliangads.com/api/public/tt-drama/resolve?content_id=l9rP6ey2CB"
);
assert.equal(bridge.RESOLVER_PATH, "/api/public/tt-drama/resolve");
assert.equal(bridge.REQUEST_TIMEOUT_MS, 6000);

const protectedTarget = new URL(
  bridge.buildW2AUrl(
    "l9rP6ey2CB",
    "?af_dp=evil&AF_DP=evil2&c=evil&af_c_id=evil&content_id=evil&cid=evil&auto=1&preview=1&af_adset_id=keep"
  )
);
assert.equal(protectedTarget.origin, "https://www.dramawavew2a.com");
assert.equal(protectedTarget.pathname, "/ads/0/2049/view");
assert.equal(protectedTarget.searchParams.get("af_dp"), "l9rP6ey2CB");
assert.equal(protectedTarget.searchParams.get("c"), "TTpost");
assert.equal(protectedTarget.searchParams.get("af_c_id"), "0001");
assert.equal(protectedTarget.searchParams.get("af_adset_id"), "keep");
assert.equal(protectedTarget.searchParams.has("AF_DP"), false);
assert.equal(protectedTarget.searchParams.has("content_id"), false);
assert.equal(protectedTarget.searchParams.has("cid"), false);
assert.equal(protectedTarget.searchParams.has("auto"), false);
assert.equal(protectedTarget.searchParams.has("preview"), false);

const encodedTarget = new URL(
  bridge.buildW2AUrl(
    "l9rP6ey2CB",
    "?utm_term=hello%20world&tag=a&tag=b&locale=%E4%B8%AD%E6%96%87"
  )
);
assert.equal(encodedTarget.searchParams.get("utm_term"), "hello world");
assert.deepEqual(encodedTarget.searchParams.getAll("tag"), ["a", "b"]);
assert.equal(encodedTarget.searchParams.get("locale"), "中文");

const filtered = bridge.collectPassthroughParams("?1bad=x&bad%20key=y&good=z");
assert.deepEqual(filtered.entries, [["good", "z"]]);
assert.equal(filtered.skipped, 2);

const manyParams = new URLSearchParams();
for (let index = 0; index < 42; index += 1) {
  manyParams.append(`p${index}`, String(index));
}
const limited = bridge.collectPassthroughParams(`?${manyParams.toString()}`);
assert.equal(limited.entries.length, bridge.MAX_PASSTHROUGH_PARAMS);
assert.equal(limited.skipped, 2);

const oversizedValue = "x".repeat(bridge.MAX_PARAM_VALUE_LENGTH + 1);
const oversized = bridge.collectPassthroughParams(`?good=ok&too_long=${oversizedValue}`);
assert.deepEqual(oversized.entries, [["good", "ok"]]);
assert.equal(oversized.skipped, 1);

assert.throws(
  () => bridge.buildW2AUrl("short", "?af_adset_id=XXX"),
  /Invalid DramaWave content_id/
);
assert.throws(
  () => bridge.buildW2AUrl("l9rP6ey2CB!", "?af_adset_id=XXX"),
  /Invalid DramaWave content_id/
);
assert.throws(
  () => bridge.buildResolverUrl("short", "https://ai.yingliangads.com"),
  /Invalid DramaWave content_id/
);

console.log(JSON.stringify({
  status: "ok",
  assertions: 35,
  example: expectedExample
}));
