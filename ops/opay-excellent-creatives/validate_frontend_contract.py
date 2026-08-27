#!/usr/bin/env python3
"""Check the standalone page and execute its JS against a small, local DOM fixture."""

import argparse
import csv
import io
import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path


HERE = Path(__file__).resolve().parent
METRIC_LABELS = ["D0首交CPA", "CPM", "APM", "CTR", "CVR", "安装→D0首交转化率"]
CSV_LEGACY_HEADERS = [
    "月份", "渠道", "素材缩略图", "素材源文件", "素材ID", "素材名称", "素材类型", "宣传App",
    "消耗USD", "曝光", "点击", "安装", "AF D0首交数", *METRIC_LABELS,
    "素材制作者", "首次上线时间", "上线时间来源", "卖点一级分类", "卖点关键词", "卖点状态",
    "入选规则", "平台CTR", "平台CPA", "消耗排名", "累计消耗占比", "映射覆盖率",
]
CSV_CPC_HEADERS = ["素材CPC USD/点击", "平台CPC USD/点击", "平台CTR口径"]

# The real inline script runs unchanged. Bootstrap waits on an inert fetch until
# the async test supplies versioned local fixtures; no network or browser needed.
DOM_HARNESS = r"""
const assert = require("node:assert/strict");
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase();this.children=[];this._text="";
    this.value="";this.className="";this.hidden=false;this.style={};this.listeners={};
  }
  set textContent(value) { this._text=String(value??"");this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(""); }
  appendChild(child) { this.children.push(child);return child; }
  append(...children) { children.forEach(c=>this.appendChild(c)); }
  replaceChildren(...children) { this._text="";this.children=[];this.append(...children); }
  addEventListener(type,callback) { (this.listeners[type]??=[]).push(callback); }
  click() {
    if(this.download)download={name:this.download,url:this.href};
    for(const callback of this.listeners.click||[])callback({target:this});
  }
  showModal() { this.open=true; }
  close() { this.open=false; }
}
const elements=new Map(HTML_IDS.map(id=>[id,new Element("div")]));
const document={
  getElementById(id) { assert.ok(elements.has(id),`unknown element ${id}`);return elements.get(id); },
  createElement(tag) { return new Element(tag); },
  title:""
};
let fetch=()=>new Promise(()=>{}),download=null,exportBlob=null,revokedUrl=null;
const URL={
  createObjectURL(blob) { exportBlob=blob;return "blob:opay-contract"; },
  revokeObjectURL(url) { revokedUrl=url; }
};
"""

BEHAVIOR_TESTS = r"""
let cases=0;
function test(name,run) { run();cases++;console.log(`PASS ${name}`); }
const clone=value=>JSON.parse(JSON.stringify(value));
function freeze(value) { if(value&&typeof value==="object"){Object.values(value).forEach(freeze);Object.freeze(value)}return value; }
const complete={
  month:"2026-07",channel:"Meta",app:"NG OPay",custom_source_id:101,
  material_name:'素材, "甲"',material_type:"VID",maker:"制作者",selection_rule:"A+B",
  spend:1234.56,impressions:100000,clicks:4000,installs:200,af_d0_first_transactions:20,
  thumbnail_status:"available",thumbnail_url:"assets/101.jpg",
  source_status:"available",source_url:"https://example.invalid/101.mp4",
  metrics:{d0_cpa:61.728,cpm:12.3456,apm:0.2,ctr:0.04,cvr:0.05,install_to_d0_rate:0.1},
  evidence:{material_ctr:0.99,material_cpa:999,material_cpa_finite:true,
    platform_ctr:0.03,platform_cpa:75,platform_cpa_finite:true,
    af_status:"available",installs_status:"available",usd_status:"verified",
    rule_a_available:true,rule_a_pass:true,rule_b_pass:true,source_row_count:2,
    data_quality:"严格素材映射；AF 按广告日精确回连"}
};
const google={...clone(complete),channel:"Google",custom_source_id:202,selection_rule:"B",
  installs:null,af_d0_first_transactions:null,platform_conversions:123.25,
  metrics:{d0_cpa:null,cpm:12.3456,apm:null,ctr:0.04,cvr:null,install_to_d0_rate:null},
  evidence:{...clone(complete.evidence),material_cpa:null,material_cpa_finite:false,rule_a_available:false,rule_a_pass:false,
    af_status:"missing_asset_attribution",installs_status:"missing_asset_installs",
    data_quality:"Google 仅有精确素材平台指标，缺失素材级安装及 AF D0 归因。"}
};
const googleCpcRow={...clone(google),selection_rule:"A+B",spend:6000,
  evidence:{...clone(google.evidence),rule_a_available:true,rule_a_pass:true,rule_b_pass:true,
    rule_a_metric:"cpc",material_cpc:1.5,platform_cpc:2,platform_ctr:0.025,
    platform_ctr_scope:"google_picture_video_assets",rule_a_unavailable_reason:""}
};
const googlePolicy={google:{version:"cpc_picvid_v1",operator:"OR"}};
const zero={...clone(complete),custom_source_id:303,installs:0,af_d0_first_transactions:0,
  metrics:{d0_cpa:null,cpm:12.3456,apm:0,ctr:0,cvr:0,install_to_d0_rate:null},
  evidence:{...clone(complete.evidence),material_cpa:null,material_cpa_finite:false}
};
const legacy={...clone(complete),evidence:{...clone(complete.evidence),material_cpa:61.728,material_ctr:0.04}};
delete legacy.metrics;
function setup(rows,schema=2,extra={}) {
  state.payload=freeze({schema_version:schema,month:"2026-07",generated_at:"2026-08-27T10:00:00+08:00",audits:[],benchmarks:[],rows:clone(rows),...clone(extra)});
  state.rows=[...state.payload.rows];state.filtered=[...state.rows];
  for(const id of ["channelFilter","appFilter","typeFilter","keywordFilter","makerFilter","ruleFilter"])$(id).value="";
}
const tableRow=index=>$("tableBody").children[index].children.map(c=>c.textContent);
const sixText=row=>metricColumns.map(column=>metricText(row,column));
const sixValues=row=>metricColumns.map(column=>metricValue(row,column));
function csvRow(index=1) { const [headers,...rows]=csvData();return Object.fromEntries(headers.map((h,i)=>[h,rows[index-1][i]??""])); }
function details() { return Object.fromEntries($("modalBody").children[0].children[0].children.map(item=>item.children.map(c=>c.textContent))); }

test("six metric keys and order",()=>{
  assert.deepEqual(metricColumns.map(c=>c.key),["d0_cpa","cpm","apm","ctr","cvr","install_to_d0_rate"]);
  assert.deepEqual(metricColumns.map(c=>c.label),METRIC_LABELS);
});
test("null, missing, invalid and zero numeric values stay distinct",()=>{
  for(const value of [null,undefined,""," ",false,true,"Infinity",Infinity,NaN]){
    assert.equal(numeric(value),null);assert.equal(metric(value),"");assert.equal(metric(value,"money"),"");assert.equal(pct(value),"");
  }
  assert.equal(metric(0),"0");assert.equal(metric(0,"money"),"$0.00");assert.equal(metric(0,"pct"),"0.00%");
  assert.equal(metric("0"),"0");assert.equal(quotient(1,0),null);assert.equal(quotient(null,1),null);
});
test("v2 metrics are authoritative over legacy evidence",()=>{
  setup([complete]);assert.deepEqual(sixText(state.rows[0]),["$61.73","$12.35","0.2000","4.00%","5.00%","10.00%"]);
  renderTable();assert.equal(tableRow(0).length,25);assert.deepEqual(tableRow(0).slice(12,18),sixText(state.rows[0]));
});
test("v2 explicit null and absent metrics never fall back",()=>{
  const row=clone(complete);row.metrics=Object.fromEntries(metricColumns.map(c=>[c.key,null]));
  setup([row]);assert.deepEqual(sixText(state.rows[0]),["","","","","",""]);
  delete row.metrics;setup([row]);assert.deepEqual(sixText(state.rows[0]),["","","","","",""]);
  row.metrics={ctr:0};setup([row]);assert.deepEqual(sixText(state.rows[0]),["","","","0.00%","",""]);
});
test("v2 numeric zero metrics are visible",()=>{
  const row=clone(complete);row.metrics=Object.fromEntries(metricColumns.map(c=>[c.key,0]));
  setup([row]);assert.deepEqual(sixText(state.rows[0]),["$0.00","$0.00","0.0000","0.00%","0.00%","0.00%"]);
});
test("APM 0.0585 keeps four decimals in table and detail with raw CSV precision",()=>{
  const row=clone(complete);row.metrics.apm=0.0585;setup([row]);renderTable();openDetail(state.rows[0]);
  assert.equal(tableRow(0)[14],"0.0585");assert.equal(details()["APM"],"0.0585");assert.equal(csvRow()["APM"],0.0585);
  assert.ok(buildCsv().includes(',"0.0585",'));assert.equal(state.rows[0].metrics.apm,0.0585);
});
test("APM display rounds to fixed four decimals while CSV retains original numbers",()=>{
  const row=clone(complete);row.metrics.apm=0.05856789;row.metrics.ctr=0.01234567;setup([row]);renderTable();openDetail(state.rows[0]);
  assert.equal(tableRow(0)[14],"0.0586");assert.equal(details()["APM"],"0.0586");assert.equal(csvRow()["APM"],0.05856789);
  assert.equal(tableRow(0)[15],"1.23%");assert.equal(csvRow()["CTR"],0.01234567);
  assert.ok(buildCsv().includes(',"0.05856789","0.01234567",'));assert.equal(metric(0,"decimal"),"0.0000");
  assert.equal(metric(null,"decimal"),"");assert.equal(metric(1,"decimal"),"1.0000");
});
test("Google absent installs and AF produce genuinely empty table cells",()=>{
  setup([google]);renderTable();assert.deepEqual(tableRow(0).slice(10,18),["","","","$12.35","","4.00%","",""]);
  assert.ok(!$("tableBody").textContent.includes("123.25"));assert.ok(!$("tableBody").textContent.includes("∞"));
});
test("real AF zero displays infinity without changing JSON",()=>{
  setup([zero]);const before=JSON.stringify(state.payload);renderTable();openDetail(state.rows[0]);
  assert.deepEqual(tableRow(0).slice(10,13),["0","0","∞"]);assert.equal(details()["D0首交CPA"],"∞");
  assert.equal(csvRow()["D0首交CPA"],"∞");assert.equal(JSON.stringify(state.payload),before);
  assert.ok(!JSON.stringify(state.payload).includes("Infinity"));assert.equal(state.rows[0].metrics.d0_cpa,null);
});
test("missing attribution status cannot turn a placeholder zero into real AF",()=>{
  const row={...clone(google),installs:0,af_d0_first_transactions:0};setup([row]);renderTable();renderKpis();
  assert.deepEqual(tableRow(0).slice(10,13),["","",""]);assert.equal($("kpiD0").textContent,"");
  assert.equal(csvRow()["AF D0首交数"],"");assert.equal(csvRow()["安装"],"");
});
test("AF KPI sums only available values and reports exclusions",()=>{
  setup([complete,google,zero]);renderKpis();assert.equal($("kpiD0").textContent,"20");
  assert.match($("kpiD0Note").textContent,/2\/3/);assert.match($("kpiD0Note").textContent,/1 条缺失未计入/);
});
test("all-missing AF KPI is blank with an explicit note",()=>{
  setup([google]);renderKpis();assert.equal($("kpiD0").textContent,"");assert.match($("kpiD0Note").textContent,/全部缺失/);
  assert.match($("kpiD0Note").textContent,/不按 0/);
});
test("all-zero available AF KPI remains a real zero",()=>{
  setup([zero]);renderKpis();assert.equal($("kpiD0").textContent,"0");assert.match($("kpiD0Note").textContent,/1\/1/);
});
test("empty filtered result has no phantom rows or AF zero",()=>{
  setup([]);renderTable();renderKpis();assert.equal($("tableBody").children.length,0);assert.equal($("emptyState").hidden,false);
  assert.equal($("kpiD0").textContent,"");assert.match($("kpiD0Note").textContent,/无素材/);assert.equal(csvData().length,1);
});
test("Google conversions and Chinese availability appear only in details",()=>{
  setup([google]);openDetail(state.rows[0]);const values=details();
  assert.equal(values["Google 平台转化数"],"123.25");assert.equal(values["安装"],"");assert.equal(values["AF D0 首交"],"");assert.equal(values["D0首交CPA"],"");
  assert.match($("modalBody").textContent,/数据可用性/);assert.match($("modalBody").textContent,/缺失素材级 AF 归因/);
  assert.match($("modalBody").textContent,/缺失素材级安装/);assert.match($("modalBody").textContent,/已核验/);
  assert.ok($("modalBody").textContent.includes(google.evidence.data_quality));assert.match($("modalBody").textContent,/不等同于安装或 AF D0 首交/);
  assert.ok(!buildCsv().includes("123.25"));assert.ok(!csvData()[0].some(h=>/conversions|平台转化数/i.test(h)));
});
test("other channels never expose platform conversions as AF",()=>{
  setup([{...clone(complete),platform_conversions:456.75}]);openDetail(state.rows[0]);
  assert.ok(!$("modalBody").textContent.includes("456.75"));assert.ok(!has(details(),"Google 平台转化数"));
});
test("Google conversions retain source precision and identify the platform source",()=>{
  const row=clone(google);row.platform_conversions=123.4567891234;row.evidence.metric_source="ads_google_insights:type=3";
  setup([row]);openDetail(state.rows[0]);assert.equal(details()["Google 平台转化数"],"123.4567891234");
  assert.ok($("modalBody").textContent.includes("平台指标来源：ads_google_insights:type=3"));
});
test("six CSV metrics match the same values used by the table",()=>{
  setup([complete,google,zero]);const data=csvData();
  assert.equal(new Set(data[0]).size,data[0].length);assert.deepEqual(data[0].slice(13,19),METRIC_LABELS);
  for(let i=0;i<state.rows.length;i++){
    assert.equal(data[i+1].length,data[0].length);assert.deepEqual(data[i+1].slice(13,19),sixValues(state.rows[i]));
  }
  const row=csvRow(2);for(const label of ["安装","AF D0首交数","D0首交CPA","APM","CVR","安装→D0首交转化率"])assert.equal(row[label],"");
  assert.equal(row["CPM"],12.3456);assert.equal(row["CTR"],0.04);
});
test("CSV preserves BOM, quoting, newlines and empty fields",()=>{
  setup([google]);const csv=buildCsv();assert.ok(csv.startsWith("\uFEFF"));assert.ok(csv.includes("\r\n"));
  assert.ok(csv.includes('"素材, '+ '""甲""' + '"'));assert.ok(csv.includes(',"","","","12.3456","","0.04","","",'));
  assert.ok(!/undefined|NaN|Infinity|"null"/.test(csv));
});
test("filtering applies consistently to rows, KPI and CSV",()=>{
  setup([complete,google,zero]);$("channelFilter").value="Google";applyFilters();
  assert.equal(state.filtered.length,1);assert.equal(tableRow(0)[1],"Google");assert.equal(csvData().length,2);assert.equal($("kpiD0").textContent,"");
  $("resetBtn").click();assert.equal(state.filtered.length,3);assert.equal(csvData().length,4);assert.equal($("kpiD0").textContent,"20");
});
test("missing historical FX keeps audit values blank and explanation intact",()=>{
  const audit={channel:"Google",app:"NG OPay",selected_count:0,platform_spend:null,mapping_coverage:null,mapping_gap_spend:null,
    message:"历史汇率不全，USD 基准及覆盖率留空；CTR 仍可用。"};
  setup([google],2,{audits:[audit]});renderAudits();renderKpis();
  const card=$("auditGrid").children[0];assert.equal(card.children[1].textContent,audit.message);
  assert.deepEqual(card.children[2].children.slice(0,3).map(c=>c.children[0].textContent),["","",""]);assert.equal($("kpiCoverage").textContent,"");
});
test("real zero audit values and non-null median are preserved",()=>{
  setup([],2,{audits:[{platform_spend:0,mapping_coverage:0,mapping_gap_spend:0},{mapping_coverage:null},{mapping_coverage:0.8}]});
  renderAudits();renderKpis();assert.deepEqual($("auditGrid").children[0].children[2].children.map(c=>c.children[0].textContent),["$0.00","0.0%","$0.00"]);
  assert.equal($("kpiCoverage").textContent,"40.0%");
});
test("null benchmark spend and CPA never become zero or infinity; CTR remains",()=>{
  setup([google],2,{benchmarks:[{channel:"Google",app:"NG OPay",spend:null,cpa:null,af_d0_first_transactions:0}]});
  openDetail(state.rows[0]);assert.equal(details()["平台 D0首交CPA"],"");assert.equal(details()["平台 CTR"],"3.00%");assert.equal(csvRow()["平台CPA"],"");
  assert.equal(csvRow()["平台CTR"],0.03);
});
test("benchmark null CPA stays blank despite stale legacy evidence",()=>{
  setup([complete],2,{benchmarks:[{channel:"Meta",app:"NG OPay",spend:100,cpa:null,af_d0_first_transactions:null}]});
  assert.equal(platformCpa(state.rows[0]),null);openDetail(state.rows[0]);assert.equal(details()["平台 D0首交CPA"],"");
});
test("known benchmark AF zero retains infinite CPA",()=>{
  setup([complete],2,{benchmarks:[{channel:"Meta",app:"NG OPay",spend:100,cpa:null,af_d0_first_transactions:0}]});
  assert.equal(platformCpa(state.rows[0]),"∞");assert.equal(csvRow()["平台CPA"],"∞");
});
test("platform CPA availability false overrides stale zero or numeric evidence",()=>{
  const row=clone(google);row.evidence.platform_cpa_available=false;row.evidence.platform_cpa=null;row.evidence.platform_cpa_finite=null;
  setup([row],2,{benchmarks:[{channel:"Google",app:"NG OPay",spend:100,cpa:null,af_d0_first_transactions:0}]});
  openDetail(state.rows[0]);assert.equal(details()["平台 D0首交CPA"],"");assert.equal(csvRow()["平台CPA"],"");
  assert.match($("modalBody").textContent,/平台 USD 基准不完整，CPA 留空/);
  row.evidence.platform_cpa=75;setup([row]);assert.equal(platformCpa(state.rows[0]),null);
});
test("explicitly available non-finite platform CPA remains infinite",()=>{
  const row=clone(complete);Object.assign(row.evidence,{platform_cpa_available:true,platform_cpa:null,platform_cpa_finite:false});
  setup([row]);assert.equal(platformCpa(state.rows[0]),"∞");
});
test("Google audit platform AF never fills missing asset attribution",()=>{
  setup([google],2,{audits:[{channel:"Google",app:"NG OPay",af_mapped:null,af_mapping_coverage:null,af_total:9999}]});
  renderKpis();renderTable();assert.equal($("kpiD0").textContent,"");assert.equal(tableRow(0)[11],"");assert.equal(csvRow()["AF D0首交数"],"");
});
test("missing USD totals never show a made-up spend zero",()=>{
  setup([{...clone(google),spend:null,metrics:{...google.metrics,cpm:null}}]);renderTable();renderKpis();
  assert.equal(tableRow(0)[7],"");assert.equal(tableRow(0)[13],"");assert.equal($("kpiSpend").textContent,"");assert.equal(csvRow()["消耗USD"],"");
});
test("schema1 retains existing evidence and calculable history",()=>{
  setup([legacy],1);const before=JSON.stringify(state.payload);renderTable();openDetail(state.rows[0]);buildCsv();
  assert.equal(rowMetric(state.rows[0],"d0_cpa"),61.728);assert.equal(rowMetric(state.rows[0],"ctr"),0.04);
  assert.equal(rowMetric(state.rows[0],"cpm"),12.3456);assert.equal(rowMetric(state.rows[0],"install_to_d0_rate"),0.1);
  assert.equal(rowMetric(state.rows[0],"apm"),0.2);assert.equal(rowMetric(state.rows[0],"cvr"),0.05);
  assert.deepEqual(sixText(state.rows[0]),["$61.73","$12.35","0.2000","4.00%","5.00%","10.00%"]);
  assert.equal(tableRow(0).length,25);assert.equal(JSON.stringify(state.payload),before);
});
test("schema1 derived zero-denominator behavior matches the backend",()=>{
  const row=clone(legacy);delete row.evidence;row.impressions=0;row.clicks=0;row.installs=0;
  setup([row],1);assert.deepEqual(sixText(state.rows[0]),["$61.73","","","0.00%","",""]);
  row.impressions=100;row.clicks=10;row.installs=0;row.af_d0_first_transactions=0;
  setup([row],1);assert.deepEqual(sixText(state.rows[0]),["∞","$12,345.60","0.0000","10.00%","0.00%",""]);
});
test("schema1 zero and missing AF remain distinct",()=>{
  const oldZero=clone(zero),oldMissing=clone(google);delete oldZero.metrics;delete oldMissing.metrics;
  setup([oldZero,oldMissing],1);renderTable();assert.equal(tableRow(0)[12],"∞");assert.equal(tableRow(1)[12],"");
  assert.equal(csvRow(1)["D0首交CPA"],"∞");assert.equal(csvRow(2)["D0首交CPA"],"");
});
test("schema1 without evidence degrades without exceptions",()=>{
  const row=clone(legacy);delete row.evidence;setup([row],1);renderTable();openDetail(state.rows[0]);
  assert.ok(Math.abs(rowMetric(state.rows[0],"d0_cpa")-61.728)<1e-10);assert.equal(rowMetric(state.rows[0],"ctr"),0.04);
  assert.equal(details()["平台 D0首交CPA"],"");
});
test("existing preview and source-file behavior remains intact",()=>{
  setup([complete]);renderTable();const cells=$("tableBody").children[0].children;
  assert.equal(cells[3].children[0].rel,"noopener noreferrer");cells[2].children[0].click();
  const video=$("modalBody").children[0].children[0];assert.equal(video.tagName,"VIDEO");assert.equal(video.controls,true);assert.equal(video.src,complete.source_url);
  $("modalClose").click();assert.equal($("modal").open,false);
});
test("Google current rules state full Campaign denominator, ties, CPC and all-asset weighted CTR",()=>{
  setup([],2,{selection_policy:googlePolicy});renderRules();const text=$("googleRuleDefinition").textContent;
  for(const token of ["全量 Campaign", "50%", "跨线", "并列", "CPC <", "USD 消耗 / 点击", "> 5000", "type=3", "asset_type=2/4", "含未映射", "总点击 / 总曝光", "非日或素材 CTR 算术平均", "A 暂停不连带暂停 B"])assert.ok(text.includes(token),token);
  for(const token of ["USD 未知或≤0", "点击为0", "精确映射素材消耗不足平台50%"])assert.ok(text.includes(token),token);
});
test("historical month rules do not claim the new CPC policy",()=>{
  setup([google]);renderRules();assert.match($("googleRuleDefinition").textContent,/历史快照.*仅 B/);
  assert.match($("googleRuleDefinition").textContent,/不按新 CPC/);
  setup([googleCpcRow]);renderRules();assert.match($("googleRuleDefinition").textContent,/素材 CPC < 平台 CPC/);
  setup([],2,{audits:[{channel:"Google",rule_a_metric:"cpc"}]});renderRules();assert.match($("googleRuleDefinition").textContent,/素材 CPC < 平台 CPC/);
});
test("Google detail separates CPC, PIC VID baseline and reference Campaign CTR",()=>{
  setup([googleCpcRow],2,{benchmarks:[{channel:"Google",app:"NG OPay",ctr:0.9,cpc:99}]});
  openDetail(state.rows[0]);const values=details(),text=$("modalBody").textContent;
  assert.equal(values["素材 CPC（USD/点击）"],"$1.500000 / 点击");assert.equal(values["平台 CPC（USD/点击）"],"$2.000000 / 点击");
  assert.equal(values["平台 CTR"],"2.50%");assert.equal(values["Campaign 平台 CTR（参考）"],"90.00%");
  assert.equal(values["平台 CTR 口径"],"Google 全部图片/视频资产（含未映射）");
  assert.match(text,/平台 CPC 来源：ads_google_insights:type=0/);assert.match(text,/type=3,asset_type=2\/4/);
  assert.match(text,/非日或素材 CTR 算术平均/);assert.equal(values["D0首交CPA"],"");
});
test("Google CPC evidence keeps explicit null and real zero distinct without inference",()=>{
  const row=clone(googleCpcRow);row.evidence.material_cpc=null;row.evidence.platform_cpc=null;
  setup([row],2,{benchmarks:[{channel:"Google",app:"NG OPay",cpc:2}]});openDetail(state.rows[0]);
  assert.equal(details()["素材 CPC（USD/点击）"],"");assert.equal(details()["平台 CPC（USD/点击）"],"");
  assert.equal(csvRow()[CSV_CPC_HEADERS[0]],"");assert.equal(csvRow()[CSV_CPC_HEADERS[1]],"");
  row.evidence.material_cpc=0;row.evidence.platform_cpc=0;setup([row]);openDetail(state.rows[0]);
  assert.equal(details()["素材 CPC（USD/点击）"],"$0.000000 / 点击");assert.equal(csvRow()[CSV_CPC_HEADERS[0]],0);
  delete row.evidence.material_cpc;delete row.evidence.platform_cpc;
  setup([row],2,{benchmarks:[{channel:"Meta",app:"NG OPay",cpc:123},{channel:"Google",app:"PK OPay",cpc:456},{channel:"Google",app:"NG OPay",cpc:2}]});
  assert.equal(googleCpc(state.rows[0],"material_cpc"),null);assert.equal(googleCpc(state.rows[0],"platform_cpc"),2);
});
test("Google CPC display preserves small differences and exports unrounded evidence",()=>{
  const row=clone(googleCpcRow);Object.assign(row.evidence,{material_cpc:0.00012345,platform_cpc:0.00012876});
  setup([row]);openDetail(state.rows[0]);assert.equal(details()["素材 CPC（USD/点击）"],"$0.000123 / 点击");
  assert.equal(details()["平台 CPC（USD/点击）"],"$0.000129 / 点击");assert.equal(csvRow()[CSV_CPC_HEADERS[0]],0.00012345);
  assert.equal(csvRow()[CSV_CPC_HEADERS[1]],0.00012876);assert.equal(details()["规则 A"],"通过");
});
test("Google suspension reasons are visible verbatim and do not suppress B",()=>{
  for(const reason of ["平台月度USD消耗不完整","平台消耗或点击为0，CPC不可比较","美元完整且精确映射的素材消耗不足平台50%"]){
    const row=clone(googleCpcRow);row.selection_rule="B";Object.assign(row.evidence,{rule_a_available:false,rule_a_pass:false,rule_a_unavailable_reason:reason});
    setup([row],2,{audits:[{channel:"Google",app:"NG OPay",selected_count:1,rule_a_available:false,rule_a_unavailable_reason:reason}]});
    openDetail(state.rows[0]);renderAudits();assert.equal(details()["规则 A"],"暂停");assert.equal(details()["规则 A 暂停原因"],reason);
    assert.equal(details()["规则 B"],"通过");assert.ok($("auditGrid").textContent.includes("规则 A 暂停："+reason));
    assert.equal(csvRow()["入选规则"],"B");
  }
});
test("Google audits retain both baselines even when platform USD and CPC are missing",()=>{
  const audit={channel:"Google",app:"NG OPay",selected_count:1,platform_spend:null,platform_cpc:null,rule_a_available:false,
    picture_video_ctr:0.025,picture_video_clicks:250,picture_video_impressions:10000,rule_a_unavailable_reason:"平台月度USD消耗不完整"};
  setup([googleCpcRow],2,{audits:[audit],benchmarks:[{channel:"Google",app:"NG OPay",ctr:0.9,cpc:9}]});renderAudits();
  const values=$("auditGrid").children[0].children[2].children.map(c=>c.children[0].textContent);
  assert.deepEqual(values,["","","","","90.00%","2.50%","250","10,000"]);assert.match($("auditGrid").textContent,/全部图片\/视频资产（含未映射）/);
  assert.match($("auditGrid").textContent,/总点击 \/ 总曝光/);
});
test("Google A B and A+B badges and filtering follow payload decisions without reselecting",()=>{
  const rows=["A","B","A+B"].map((rule,index)=>({...clone(googleCpcRow),custom_source_id:500+index,selection_rule:rule,
    evidence:{...clone(googleCpcRow.evidence),rule_a_pass:rule!=="B",rule_b_pass:rule!=="A"}}));
  setup(rows);const before=JSON.stringify(state.payload);
  for(const rule of ["A","B","A+B"]){$("ruleFilter").value=rule;applyFilters();assert.equal(state.filtered.length,1);assert.equal(tableRow(0)[23],rule);
    openDetail(state.filtered[0]);assert.equal(details()["规则 A"],rule==="B"?"未通过":"通过");assert.equal(csvRow()["入选规则"],rule)}
  assert.equal(JSON.stringify(state.payload),before);
});
test("CSV appends exactly three columns and preserves all legacy names and positions",()=>{
  setup([complete,googleCpcRow,{...clone(complete),channel:"TikTok"}]);const data=csvData();
  assert.equal(CSV_LEGACY_HEADERS.length,31);assert.deepEqual(data[0],[...CSV_LEGACY_HEADERS,...CSV_CPC_HEADERS]);
  assert.deepEqual(data[1].slice(-3),[null,null,""]);assert.deepEqual(data[3].slice(-3),[null,null,""]);
  assert.deepEqual(data[2].slice(-3),[1.5,2,"google_picture_video_assets"]);assert.equal(csvRow(2)["平台CTR"],0.025);
  assert.equal(csvRow(1)["平台CTR"],0.03);assert.equal(csvRow(1)["平台CPA"],75);
  setup([google]);assert.deepEqual(csvData()[1].slice(-3),[null,null,""]);
});
test("Meta and TikTok details and audits remain AF CPA based without Google evidence",()=>{
  for(const channel of ["Meta","TikTok"]){setup([{...clone(complete),channel}],2,{audits:[{channel,rule_a_available:true}]});
    openDetail(state.rows[0]);renderAudits();assert.equal(details()["平台 D0首交CPA"],"$75.00");assert.equal(details()["规则 A"],"通过");
    assert.ok(!has(details(),"素材 CPC（USD/点击）"));assert.ok(!$("modalBody").textContent.includes("asset_type=2/4"));
    assert.equal($("auditGrid").children[0].children[2].children.length,3);
  }
});
async function asyncTests(){
  setup([google]);exportCsv();assert.equal(Buffer.from(await exportBlob.arrayBuffer()).toString("utf8"),buildCsv());
  assert.equal(download.name,"opay-excellent-creatives-2026-07.csv");assert.equal(revokedUrl,download.url);
  cases++;console.log("PASS actual CSV download uses the tested data");
  const calls=[],manifest={schema_version:2,data_version:"contract-v2",latest_month:"2026-07",months:[{month:"2026-06",stage:"final"},{month:"2026-07",stage:"final"}]};
  const payloads={
    "2026-07":{schema_version:2,month:"2026-07",generated_at:"2026-08-27T10:00:00+08:00",rows:[{...clone(google),spend:null},complete],audits:[]},
    "2026-06":{schema_version:1,month:"2026-06",generated_at:"2026-07-05T10:00:00+08:00",rows:[legacy],audits:[]}
  };
  fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url==="latest.json"?clone(manifest):clone(payloads[url.match(/(\d{4}-\d{2})\.json$/)[1]])}};
  await loadManifest();assert.equal(state.payload.schema_version,2);assert.equal(state.rows[0].custom_source_id,101);assert.equal($("loading").hidden,true);
  assert.equal(calls[0].options.cache,"no-store");assert.equal(calls[1].url,"data/contract-v2/2026-07.json");assert.equal(calls[1].options.cache,"force-cache");
  await loadMonth("2026-06");assert.equal(state.payload.schema_version,1);assert.equal(rowMetric(state.rows[0],"d0_cpa"),61.728);
  assert.equal(document.title,"2026-06 OPay 月度优秀素材");
  cases++;console.log("PASS v2 manifest loads v2 and schema1 historical months");
  console.log(`frontend behavior: PASS (${cases} cases)`);
}
asyncTests().catch(error=>{console.error(error);process.exitCode=1});
"""


def run_node(script, *, syntax_only=False, capture=False):
    process = subprocess.run(
        ["node", "--check"] if syntax_only else ["node"],
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if process.returncode:
        raise SystemExit(process.stdout + process.stderr)
    if process.stdout and not capture:
        print(process.stdout.rstrip())
    return process.stdout


def verify_payload_csv(constants, script, payload_path, output_dir=None):
    """Exercise the actual inline export function with a generated month, not fixture numbers."""
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    probe = r'''
state.payload=ACTUAL_PAYLOAD;
state.rows=ACTUAL_PAYLOAD.rows.map((r,index)=>({...r,__index:index}));
(async()=>{
  const outputs=[];
  for(const channel of ["","Google"]){
    for(const id of ["channelFilter","appFilter","typeFilter","keywordFilter","makerFilter","ruleFilter"])$(id).value="";
    $("channelFilter").value=channel;
    applyFilters(); exportCsv();
    outputs.push({channel,row_count:state.filtered.length,filename:download.name,csv:await exportBlob.text()});
  }
  console.log(JSON.stringify(outputs));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    results = json.loads(run_node(constants + DOM_HARNESS + script + "\nconst ACTUAL_PAYLOAD="
                                  + json.dumps(payload, ensure_ascii=False, allow_nan=False) + ";\n" + probe, capture=True))
    summary = []
    keys = ("d0_cpa", "cpm", "apm", "ctr", "cvr", "install_to_d0_rate")
    for result in results:
        expected_rows = [row for row in payload["rows"] if not result["channel"] or row["channel"] == result["channel"]]
        reader = csv.DictReader(io.StringIO(result["csv"].lstrip("\ufeff")))
        parsed = list(reader)
        assert reader.fieldnames == CSV_LEGACY_HEADERS + CSV_CPC_HEADERS
        assert result["row_count"] == len(parsed) == len(expected_rows)
        for actual, expected in zip(parsed, expected_rows):
            assert actual["素材ID"] == str(expected["custom_source_id"])
            for label, key in zip(METRIC_LABELS, keys):
                value = expected["metrics"][key]
                if key == "d0_cpa" and expected.get("af_d0_first_transactions") == 0 and expected.get("spend") is not None:
                    assert actual[label] == "∞"
                elif value is None:
                    assert actual[label] == "", (label, actual[label])
                else:
                    assert Decimal(actual[label]) == Decimal(str(value)), (label, actual[label], value)
            for label, key in (("安装", "installs"), ("AF D0首交数", "af_d0_first_transactions")):
                assert actual[label] == ("" if expected[key] is None else str(expected[key]))
            evidence = expected.get("evidence", {})
            google = expected["channel"] in ("Google", "GG")
            benchmark = next((b for b in payload.get("benchmarks", [])
                              if b["channel"] in ("Google", "GG") and b["app"] == expected["app"]), {})
            cpc_values = (evidence.get("material_cpc"), evidence.get("platform_cpc", benchmark.get("cpc")))
            for label, value in zip(CSV_CPC_HEADERS[:2], cpc_values):
                if not google or value is None:
                    assert actual[label] == "", (label, actual[label])
                else:
                    assert Decimal(actual[label]) == Decimal(str(value)), (label, actual[label], value)
            assert actual[CSV_CPC_HEADERS[2]] == ((evidence.get("platform_ctr_scope") or "") if google else "")
            assert actual["入选规则"] == expected["selection_rule"]
            ctr = evidence.get("platform_ctr")
            if ctr is None:
                assert actual["平台CTR"] == ""
            else:
                assert Decimal(actual["平台CTR"]) == Decimal(str(ctr))
        assert result["filename"] == "opay-excellent-creatives-%s.csv" % payload["month"]
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / (payload["month"] + "-" + (result["channel"] or "all") + ".csv")).write_bytes(result["csv"].encode("utf-8"))
        summary.append({"channel": result["channel"] or "all", "rows": len(parsed)})
    print(json.dumps({"actual_csv": "PASS", "month": payload["month"], "scopes": summary}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, help="also verify the real inline CSV exporter against an actual month")
    parser.add_argument("--csv-output-dir", type=Path, help="optional generated QA CSV artifacts")
    args = parser.parse_args()
    if args.csv_output_dir and not args.payload:
        parser.error("--csv-output-dir requires --payload")
    html = (HERE / "report.html").read_text(encoding="utf-8")
    required = [
        '<meta name="robots" content="noindex,nofollow,noarchive">',
        'fetch("latest.json",{cache:"no-store"})',
        "monthFilter", "channelFilter", "appFilter", "typeFilter", "keywordFilter",
        "makerFilter", "ruleFilter", "kpiD0Note", "导出当前 CSV", "AF D0 首交",
        "素材制作者", "openPreview", "openDetail", "数据可用性", "row.metrics",
        "missing_asset_attribution", "missing_asset_installs", "platform_conversions",
        "CSV 按原始精度数值导出，不使用页面舍入值",
        "googleRuleDefinition", "cpc_picvid_v1", "google_picture_video_assets",
        "rule_a_unavailable_reason", "rule_a_metric", "picture_video_ctr",
        "Meta / TikTok（不变）", "素材 AF D0 首交 CPA &lt; 平台 CPA",
        *CSV_CPC_HEADERS,
        *METRIC_LABELS,
    ]
    missing = [item for item in required if item not in html]
    if missing:
        raise SystemExit("missing frontend contract tokens: %s" % missing)
    if "api/auth/feishu" in html.casefold() or "auth_request" in html.casefold():
        raise SystemExit("public report must not include Feishu authentication")
    if re.search(r"<script\s+[^>]*src=", html, flags=re.I):
        raise SystemExit("report must not depend on external JavaScript")

    headers = re.findall(r"<th\b[^>]*>(.*?)</th>", html, flags=re.S | re.I)
    if len(headers) != 25 or headers[12:18] != METRIC_LABELS:
        raise SystemExit("table must have all six metric columns in the expected order")
    if not re.search(r"\.table-wrap\s*\{[^}]*overflow-x\s*:\s*auto", html):
        raise SystemExit("wide table must allow horizontal scrolling on mobile")
    if not re.search(r'<div class="table-wrap"[^>]*tabindex="0"', html):
        raise SystemExit("wide table must be keyboard focusable")
    widths = re.findall(r"(?<![\w.-])table\s*\{[^}]*min-width\s*:\s*(\d+)px", html)
    if not widths or int(widths[-1]) < 2100:
        raise SystemExit("wide table must retain space for all 25 columns")

    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S | re.I)
    if len(scripts) != 1:
        raise SystemExit("expected exactly one inline script")
    run_node(scripts[0], syntax_only=True)
    constants = "const HTML_IDS=%s;\nconst METRIC_LABELS=%s;\nconst CSV_LEGACY_HEADERS=%s;\nconst CSV_CPC_HEADERS=%s;\n" % (
        json.dumps(re.findall(r'\bid="([^"]+)"', html), ensure_ascii=False),
        json.dumps(METRIC_LABELS, ensure_ascii=False),
        json.dumps(CSV_LEGACY_HEADERS, ensure_ascii=False),
        json.dumps(CSV_CPC_HEADERS, ensure_ascii=False),
    )
    run_node(constants + DOM_HARNESS + scripts[0] + BEHAVIOR_TESTS)
    if args.payload:
        verify_payload_csv(constants, scripts[0], args.payload, args.csv_output_dir)
    print("frontend contract: PASS")


if __name__ == "__main__":
    main()
