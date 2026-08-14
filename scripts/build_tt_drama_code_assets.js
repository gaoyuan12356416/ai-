"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const TEMPLATE_PATH = path.join(
  ROOT,
  "static",
  "tt-drama-code-search.html"
);
const SCRIPT_PATH = path.join(
  ROOT,
  "static",
  "tt-drama-code-search.js"
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
const bridge = require(SCRIPT_PATH);

const STATIC_COPY_KEYS = Object.freeze([
  "documentTitle",
  "brandPill",
  "eyebrow",
  "titleLead",
  "titleAccent",
  "searchAria",
  "searchLabel",
  "exactMatch",
  "placeholder",
  "findAria",
  "helperInitial",
  "guideTitle",
  "guideNote",
  "guideImageAlt",
  "matchConfirmed",
  "continueText",
  "recentTitle",
  "recentNote",
  "controlsAria",
  "previousAria",
  "nextAria",
  "storiesAria",
  "footer"
]);

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function htmlLanguage(locale) {
  if (locale === "zh-hans") {
    return "zh-Hans";
  }
  if (locale === "zh-tw") {
    return "zh-Hant";
  }
  return locale;
}

function normalizeLf(value) {
  return String(value).replace(/\r\n?/g, "\n");
}

function renderLocale(template, locale, copy, scriptAssetName) {
  const usedKeys = new Set();
  let output = template.replace(
    /<html\b[^>]*>/i,
    '<html lang="' + htmlLanguage(locale) + '" dir="' +
      (locale === "ar" ? "rtl" : "ltr") +
      '" data-initial-locale="' + locale + '">'
  );

  output = output.replace(
    /<([a-z][a-z0-9-]*)\b([^>]*\sdata-i18n-key="([^"]+)"[^>]*)>([\s\S]*?)<\/\1>/gi,
    function (_match, tagName, attributes, key) {
      assert.ok(
        Object.prototype.hasOwnProperty.call(copy, key),
        locale + " is missing static copy key " + key
      );
      usedKeys.add(key);
      const cleanAttributes = attributes.replace(
        /\sdata-i18n-key="[^"]+"/,
        ""
      );
      return "<" + tagName + cleanAttributes + ">" +
        escapeHtml(copy[key]) + "</" + tagName + ">";
    }
  );

  output = output.replace(
    /<[a-z][^>]*\sdata-i18n-attr-[^>]+>/gi,
    function (sourceTag) {
      let tag = sourceTag;
      const markers = Array.from(
        sourceTag.matchAll(/\sdata-i18n-attr-([a-z][a-z0-9-]*)="([^"]+)"/g)
      );
      for (const marker of markers) {
        const attributeName = marker[1];
        const key = marker[2];
        assert.ok(
          Object.prototype.hasOwnProperty.call(copy, key),
          locale + " is missing attribute copy key " + key
        );
        usedKeys.add(key);
        const attributePattern = new RegExp(
          "(\\s" + escapeRegExp(attributeName) + '=")[^"]*(")',
          "i"
        );
        assert.match(
          tag,
          attributePattern,
          "template is missing attribute " + attributeName + " for " + key
        );
        tag = tag.replace(
          attributePattern,
          "$1" + escapeHtml(copy[key]) + "$2"
        );
        tag = tag.replace(marker[0], "");
      }
      return tag;
    }
  );
  output = output.replace(/^[ \t]+$/gm, "");

  assert.deepEqual(
    Array.from(usedKeys).sort(),
    Array.from(STATIC_COPY_KEYS).sort(),
    locale + " did not render the complete static copy contract"
  );
  assert.ok(
    !/data-i18n-(?:key|attr-)/.test(output),
    locale + " output retained an i18n build marker"
  );
  assert.ok(
    output.includes('src="/tt-drama-code-search.js"'),
    "template must retain the unversioned development script"
  );
  output = output.replace(
    'src="/tt-drama-code-search.js"',
    'src="/tt-drama-code-assets/' + scriptAssetName + '"'
  );
  return output;
}

function expectedOutputs() {
  // Keep generated names and bytes identical across Windows/Linux checkouts.
  const template = normalizeLf(fs.readFileSync(TEMPLATE_PATH, "utf8"));
  const script = Buffer.from(
    normalizeLf(fs.readFileSync(SCRIPT_PATH, "utf8")),
    "utf8"
  );
  const digest = crypto.createHash("sha256").update(script).digest("hex");
  const scriptAssetName = "tt-drama-code-search." + digest.slice(0, 12) + ".js";
  const locales = Object.keys(bridge.COPY).sort();
  const localeFiles = new Map();
  for (const locale of locales) {
    localeFiles.set(
      locale + ".html",
      renderLocale(template, locale, bridge.COPY[locale], scriptAssetName)
    );
  }
  return {
    digest,
    script,
    scriptAssetName,
    locales,
    localeFiles
  };
}

function assertDirectoryFiles(directory, expectedNames, pattern) {
  const actual = fs.existsSync(directory)
    ? fs.readdirSync(directory).filter(name => pattern.test(name)).sort()
    : [];
  assert.deepEqual(actual, Array.from(expectedNames).sort());
}

function checkOutputs(outputs) {
  assertDirectoryFiles(
    LOCALE_DIRECTORY,
    outputs.localeFiles.keys(),
    /^[a-z0-9-]+\.html$/
  );
  assertDirectoryFiles(
    ASSET_DIRECTORY,
    [outputs.scriptAssetName],
    /^tt-drama-code-search\.[a-f0-9]{12}\.js$/
  );
  for (const [name, expected] of outputs.localeFiles) {
    assert.equal(
      fs.readFileSync(path.join(LOCALE_DIRECTORY, name), "utf8"),
      expected,
      name + " is stale; rebuild TT drama code assets"
    );
  }
  assert.deepEqual(
    fs.readFileSync(path.join(ASSET_DIRECTORY, outputs.scriptAssetName)),
    outputs.script,
    outputs.scriptAssetName + " does not match its source"
  );
}

function removeStaleGeneratedFiles(directory, keepNames, pattern) {
  if (!fs.existsSync(directory)) {
    return;
  }
  const keep = new Set(keepNames);
  for (const name of fs.readdirSync(directory)) {
    if (pattern.test(name) && !keep.has(name)) {
      fs.unlinkSync(path.join(directory, name));
    }
  }
}

function writeOutputs(outputs) {
  fs.mkdirSync(LOCALE_DIRECTORY, { recursive: true });
  fs.mkdirSync(ASSET_DIRECTORY, { recursive: true });
  removeStaleGeneratedFiles(
    LOCALE_DIRECTORY,
    outputs.localeFiles.keys(),
    /^[a-z0-9-]+\.html$/
  );
  removeStaleGeneratedFiles(
    ASSET_DIRECTORY,
    [outputs.scriptAssetName],
    /^tt-drama-code-search\.[a-f0-9]{12}\.js$/
  );
  for (const [name, content] of outputs.localeFiles) {
    fs.writeFileSync(path.join(LOCALE_DIRECTORY, name), content, "utf8");
  }
  fs.writeFileSync(
    path.join(ASSET_DIRECTORY, outputs.scriptAssetName),
    outputs.script
  );
}

function main() {
  const outputs = expectedOutputs();
  if (process.argv.includes("--check")) {
    checkOutputs(outputs);
  } else {
    writeOutputs(outputs);
    checkOutputs(outputs);
  }
  process.stdout.write(JSON.stringify({
    status: "ok",
    mode: process.argv.includes("--check") ? "check" : "write",
    locale_count: outputs.locales.length,
    script_asset: outputs.scriptAssetName,
    script_sha256: outputs.digest
  }) + "\n");
}

main();
