#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error("usage: node import_keywords.mjs <OPay关键词.xlsx> <selling-points.json>");
}

function value(cell) {
  return cell === null || cell === undefined ? "" : String(cell).trim();
}

function requireHeaders(actual, expected, sheetName) {
  const headers = actual.slice(0, expected.length).map(value);
  if (JSON.stringify(headers) !== JSON.stringify(expected)) {
    throw new Error(`${sheetName} headers changed: ${JSON.stringify(headers)}`);
  }
}

const rawBytes = await fs.readFile(inputPath);
const sourceSha256 = crypto.createHash("sha256").update(rawBytes).digest("hex");
const sourceStat = await fs.stat(inputPath);
const sourceDate = sourceStat.mtime.toISOString().slice(0, 10);
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const ngValues = workbook.worksheets.getItem("NG").getUsedRange(true).values;
const pkValues = workbook.worksheets.getItem("PK").getUsedRange(true).values;

requireHeaders(
  ngValues[0],
  [
    "素材大类（一级）",
    "卖点/钩子（二级）",
    "针对人群",
    "素材标签（上传盈量素材系统）",
    "关键词（广告平台命名）",
    "备注",
  ],
  "NG",
);
requireHeaders(
  pkValues[0],
  ["素材类型一级分类", "素材类型二级分类", "卖点说明", "卖点状态（2026年4月10日更新）"],
  "PK",
);

const ngEntries = ngValues.slice(1).flatMap((row, index) => {
  const uploadTag = value(row[3]);
  if (!uploadTag) return [];
  const note = value(row[5]);
  return [
    {
      id: `NG-${String(index + 1).padStart(3, "0")}`,
      source_row: index + 2,
      app: "NG OPay",
      level1: value(row[0]),
      level2: value(row[1]),
      audience: value(row[2]),
      upload_tag: uploadTag,
      display_keyword: value(row[4]) || uploadTag,
      match_aliases: [uploadTag],
      status: /过期|不可用/.test(note) ? "unavailable" : "available",
      status_label: /过期|不可用/.test(note) ? "过期/不可用" : "可用",
      note,
    },
  ];
});

const glossary = ngValues.slice(1).flatMap((row, index) => {
  const term = value(row[9]);
  if (!term) return [];
  return [{ source_row: index + 2, term, description: value(row[10]) }];
});

const pkEntries = pkValues.slice(1).flatMap((row, index) => {
  const keyword = value(row[1]);
  if (!keyword) return [];
  const sourceStatus = value(row[3]);
  const available = sourceStatus === "可用";
  return [
    {
      id: `PK-${String(index + 1).padStart(3, "0")}`,
      source_row: index + 2,
      app: "PK OPay",
      level1: value(row[0]),
      level2: keyword,
      audience: "",
      upload_tag: keyword,
      display_keyword: keyword,
      match_aliases: [keyword],
      status: available ? "available" : "unavailable",
      status_label: sourceStatus || "未标注",
      note: value(row[2]),
    },
  ];
});

const keywordCounts = new Map();
for (const entry of [...ngEntries, ...pkEntries]) {
  const key = `${entry.app}\u0000${entry.display_keyword.toLocaleLowerCase("en-US")}`;
  keywordCounts.set(key, (keywordCounts.get(key) || 0) + 1);
}
for (const entry of [...ngEntries, ...pkEntries]) {
  const key = `${entry.app}\u0000${entry.display_keyword.toLocaleLowerCase("en-US")}`;
  entry.duplicate_display_keyword = keywordCounts.get(key) > 1;
}

const document = {
  schema_version: 1,
  config_version: `${sourceDate}-${sourceSha256.slice(0, 12)}`,
  source: {
    file_name: path.basename(inputPath),
    sha256: sourceSha256,
    modified_at: sourceStat.mtime.toISOString(),
    sheets: { NG: "A1:K81", PK: "A1:D11" },
  },
  matching_policy: {
    exact_upload_tag_first: true,
    fallback: "boundary-aware longest non-overlapping aliases across material tag and file name",
    multiple_matches: true,
    unmatched_label: "待补关键词",
  },
  summary: {
    ng_entries: ngEntries.length,
    pk_entries: pkEntries.length,
    unavailable_entries: [...ngEntries, ...pkEntries].filter((entry) => entry.status === "unavailable").length,
    duplicate_display_keyword_entries: [...ngEntries, ...pkEntries].filter(
      (entry) => entry.duplicate_display_keyword,
    ).length,
  },
  entries: [...ngEntries, ...pkEntries],
  glossary,
};

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.writeFile(outputPath, `${JSON.stringify(document, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ output: outputPath, ...document.summary, config_version: document.config_version }));
