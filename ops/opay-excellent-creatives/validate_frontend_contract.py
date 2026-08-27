#!/usr/bin/env python3
"""Check the standalone page and execute its JS against a small, local DOM fixture."""

import json
import re
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
METRIC_LABELS = ["D0首交CPA", "CPM", "APM", "CTR", "CVR", "安装→D0首交转化率"]

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
const google={...clone(complete),channel:"Google",custom_source_id:202,
  installs:null,af_d0_first_transactions:null,platform_conversions:123.25,
  metrics:{d0_cpa:null,cpm:12.3456,apm:null,ctr:0.04,cvr:null,install_to_d0_rate:null},
  evidence:{...clone(complete.evidence),material_cpa:null,material_cpa_finite:false,
    af_status:"missing_asset_attribution",installs_status:"missing_asset_installs",
    data_quality:"Google 仅有精确素材平台指标，缺失素材级安装及 AF D0 归因。"}
};
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
  assert.deepEqual(card.children[2].children.map(c=>c.children[0].textContent),["","",""]);assert.equal($("kpiCoverage").textContent,"");
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


def run_node(script, *, syntax_only=False):
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
    if process.stdout:
        print(process.stdout.rstrip())


def main():
    html = (HERE / "report.html").read_text(encoding="utf-8")
    required = [
        '<meta name="robots" content="noindex,nofollow,noarchive">',
        'fetch("latest.json",{cache:"no-store"})',
        "monthFilter", "channelFilter", "appFilter", "typeFilter", "keywordFilter",
        "makerFilter", "ruleFilter", "kpiD0Note", "导出当前 CSV", "AF D0 首交",
        "素材制作者", "openPreview", "openDetail", "数据可用性", "row.metrics",
        "missing_asset_attribution", "missing_asset_installs", "platform_conversions",
        "CSV 按原始精度数值导出，不使用页面舍入值",
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
    constants = "const HTML_IDS=%s;\nconst METRIC_LABELS=%s;\n" % (
        json.dumps(re.findall(r'\bid="([^"]+)"', html), ensure_ascii=False),
        json.dumps(METRIC_LABELS, ensure_ascii=False),
    )
    run_node(constants + DOM_HARNESS + scripts[0] + BEHAVIOR_TESTS)
    print("frontend contract: PASS")


if __name__ == "__main__":
    main()
