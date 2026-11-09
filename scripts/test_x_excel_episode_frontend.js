"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const XLSX = require(path.join(root, "static/vendor/xlsx.full.min.js"));
const P = {
  parseTime: value => Date.parse(value + "+08:00"),
  templateError: () => "",
};
const sandbox = {window: {XVideoPublish: P, XLSX}, TextDecoder, Uint8Array, DataView, Date};
vm.runInNewContext(fs.readFileSync(path.join(root, "static/x-excel-publish.js"), "utf8"), sandbox);
const headers = ["账号 ID", "素材 ID", "发布文案", "发布时间（北京时间）", "加工方式"];
const episode = "abcdef0123456789abcdef0123456789";
const rows = [
  ["1900000000000000001", 999999991, "{name}\n{url}", "2099-11-12 11:45:00", "原片"],
  ["1900000000000000001", episode.toUpperCase(), "{name}", "", "原片"],
  ["1900000000000000001", "0123456789abcdef0123456789abcdef", "", "", "随机加工"],
  ["1900000000000000001", "12345678901234567890123456789012", "", "", "原片"],
  ["1900000000000000001", "not-an-id", "", "", "原片"],
  ["1900000000000000001", 12345678901234568, "", "", "原片"],
  [episode, "999999991", "", "", "原片"],
];
const book = XLSX.utils.book_new();
const sheet = XLSX.utils.aoa_to_sheet([headers, ...rows]);
XLSX.utils.book_append_sheet(book, sheet, "发布");
function parse() {
  return P.parseWorkbook(XLSX.write(book, {type: "array", bookType: "xlsx"}));
}
const result = parse();
assert.equal(result.total, 7);
assert.equal(result.valid.length, 4);
assert.equal(result.skipped.length, 3);
assert.equal(result.valid[0].material_id, "999999991");
assert.equal(result.valid[0].scheduled_at_utc, "2099-11-12T03:45:00.000Z");
assert.equal(result.valid[1].material_id, episode);
assert.equal(result.valid[2].material_id, rows[2][1]);
assert.equal(result.valid[2].processing_mode, "random_overlay");
assert.equal(result.valid[3].material_id, rows[3][1]);
assert.match(result.skipped[0].message, /免费集 ID/);
assert.match(result.skipped[1].message, /舍入/);
assert.match(result.skipped[2].message, /账号 ID/);
sheet.B3 = {t: "s", v: episode, f: '"' + episode + '"'};
assert.match(parse().skipped.find(row => row.row_number === 3).message, /公式/);
console.log("PASS: real XLSX mixed IDs, uppercase/leading zeros, time, invalid rows, precision and formulas");
