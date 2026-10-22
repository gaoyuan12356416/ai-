"""Renderer/security checks; optional Node checks exercise the embedded UI offline."""

import importlib.util
import copy
import gzip
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("daily_renderer", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sample_report():
    metric = dict(spend=10, af_revenue_d0=5, impressions=1000, clicks=10,
                  installs=2, af_installs=2, purchases=1, roas=50, ctr=1,
                  cpi=5, spending_materials=1)
    materials = []
    for index in range(27):
        materials.append(dict(metric, id=str(7000000 + index), name="素材 " + str(index),
                              url="https://example.com/image.jpg", source="cover_synthesis",
                              owner="甲", content_id="drama-1", language="de", ratio="16:9",
                              created_at="2026-10-18 12:00:00", generated_at="2026-10-17 12:00:00",
                              is_backfill=index == 0, is_delete=int(index == 1), optimizers=["乙"]))
    materials[0]["name"] = '=HYPERLINK("https://evil.invalid")'
    return dict(report_date="2026-10-18", generated_at="2026-10-19T09:00:00+08:00",
                timezone="Asia/Shanghai", currency="USD", snapshot_kind="scheduled",
                freshness={}, summary={"production": {"new_count": 27, "backfill_count": 1,
                                                     "dramas": 1, "total_asof": 27},
                                       "performance": metric},
                comparisons={"previous": metric, "market": metric, "other_images": metric},
                trend=[dict(dt="2026-10-18", ai=metric, market=metric, other_images=metric, uploads=27)],
                materials=materials, groups={"sources": [dict(metric, name="cover_synthesis", count=27,
                                                             new_count=27, backfill_count=1)]},
                unresolved=[], validation={"checks": 12, "status": "passed"}, notes=[])


def v3_report():
    result = sample_report()
    result.update(schema_version=3, products=[
        dict(id=1479, name="DramaWave", apps=[1479, 1723]),
        dict(id=2373, name="MoboShort", apps=[2373]),
        dict(id=3360, name="MoboReels", apps=[3360]),
    ], views={})
    for index, (id_, name) in enumerate([("all", "三产品合计"), ("1479", "DramaWave"),
                                        ("2373", "MoboShort"), ("3360", "MoboReels")]):
        view = copy.deepcopy(sample_report())
        view.update(id=id_, name=name)
        for m in view["materials"]:
            m.update(source_product_id=1479, source_product_name="DramaWave",
                     delivery_product_ids=[1479, 2373], delivery_product_names=["DramaWave", "MoboShort"],
                     is_source_product=id_ != "2373", thumbnail_url="https://example.com/thumb.webp")
            m["week"] = dict(view["summary"]["performance"], spend=70, af_revenue_d0=35)
        view["week"] = dict(production=copy.deepcopy(view["summary"]["production"]),
                            performance=dict(view["summary"]["performance"], spend=70 * (index + 1)),
                            comparisons=view["comparisons"], groups=copy.deepcopy(view["groups"]), insights=["近七天分析"])
        view["week"]["groups"]["sources"][0]["spend"] = 70
        view["summary"]["performance"]["spend"] = 10 * (index + 1)
        view["insights"] = ["昨日分析"]
        result["views"][id_] = view
    # Public bundle must exclude legacy root-level raw data too.
    return result


class RendererTests(unittest.TestCase):
    def test_v3_bundle_is_small_lazy_immutable_and_safe(self):
        report = v3_report()
        report["views"]["all"]["materials"] *= 200
        before = copy.deepcopy(report)
        bundle = MODULE.build_bundle(report, [{"date": "2026-10-18", "url": "https://example.com/daily"}],
                                     "https://example.com/reports", "frozen-run-1")
        shell = bundle["index.html"]["data"]
        payload = json.loads(re.search(rb'type="application/json">(.*?)</script>', shell, re.S).group(1))
        self.assertNotIn("materials", payload)
        self.assertNotIn("image.jpg", shell.decode())
        for view in payload["views"].values():
            self.assertFalse(set(view) & {"groups", "materials", "unresolved"})
            self.assertNotIn("groups", view["week"])
            self.assertLessEqual(len(view["top"]["materials"]), 10)
        self.assertEqual(bundle["daily/2026-10-18/index.html"]["data"], shell)
        self.assertTrue(bundle["daily/2026-10-18/index.html"]["immutable"])
        self.assertFalse(bundle["index.html"]["immutable"])
        self.assertTrue(bundle["assets/frozen-run-1/details-all.json"]["immutable"])
        first_load = len(gzip.compress(shell)) + len(gzip.compress(bundle["assets/frozen-run-1/report.js"]["data"]))
        self.assertLess(first_load, 150 * 1024)
        self.assertEqual(before, report)

    def test_v3_bundle_rejects_bad_path_and_nonfinite_detail(self):
        for version in ("../x", 'x\" onload=alert(1)', ""):
            with self.assertRaises(ValueError):
                MODULE.build_bundle(v3_report(), [], "https://example.com/reports", version)
        report = v3_report()
        report["views"]["1479"]["materials"][0]["spend"] = float("inf")
        with self.assertRaises(ValueError):
            MODULE.build_bundle(report, [], "https://example.com/reports", "run-1")

    def test_payload_roundtrip_escapes_script_and_does_not_mutate(self):
        report = sample_report()
        report["notes"] = ["</script><img src=x onerror=alert(1)>&\u2028\u2029"]
        before = json.dumps(report, ensure_ascii=False)
        rendered = MODULE.render_report(report)
        script = re.search(r'<script id="report-data" type="application/json">(.*?)</script>',
                           rendered, re.S).group(1)
        self.assertNotIn("<", script)
        self.assertNotIn(">", script)
        self.assertNotIn("&", script)
        self.assertNotIn("\u2028", script)
        self.assertEqual(json.loads(script)["notes"], report["notes"])
        self.assertEqual(json.dumps(report, ensure_ascii=False), before)
        self.assertNotIn("__REPORT_JSON__", rendered)

    def test_link_and_image_urls_reject_active_schemes_and_credentials(self):
        report = sample_report()
        report["materials"][0]["url"] = "javascript:alert(1)"
        report["materials"][1]["url"] = "data:image/svg+xml,<svg/>"
        report["materials"][2]["url"] = "https://user:pass@example.com/"
        report["materials"][3]["url"] = "https://example.com/\nunsafe"
        rendered = MODULE.render_report(report, [{"date": "2026-10-17", "url": "javascript:alert(1)"}])
        payload = json.loads(re.search(r'type="application/json">(.*?)</script>', rendered, re.S).group(1))
        self.assertTrue(all(m["url"] == "" for m in payload["materials"][:4]))
        self.assertEqual(payload["history"][0]["url"], "")
        self.assertEqual(payload["materials"][4]["url"], "https://example.com/image.jpg")

    def test_nonfinite_metrics_fail_closed(self):
        report = sample_report()
        report["summary"]["performance"]["spend"] = float("nan")
        with self.assertRaises(ValueError):
            MODULE.render_report(report)

    def test_missing_optional_sections_render(self):
        rendered = MODULE.render_report({"report_date": "2026-10-18"})
        self.assertIn("<!doctype html>", rendered)
        self.assertIn("每个正式素材 ID 一行", rendered)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_javascript_syntax_and_interactive_filters_csv(self):
        rendered = MODULE.render_report(sample_report())
        script = re.findall(r'<script>(.*?)</script>', rendered, re.S)[0]
        with tempfile.TemporaryDirectory(prefix="ai-image-ui-") as folder:
            js = Path(folder, "report.js")
            js.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            harness = Path(folder, "harness.js")
            harness.write_text(NODE_HARNESS, encoding="utf-8")
            report_file = Path(folder, "report.json")
            report_file.write_text(json.dumps(sample_report(), ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(["node", str(harness), str(js), str(report_file)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("UI interactions passed", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_v3_lazy_switch_period_export_errors_and_no_original_requests(self):
        bundle = MODULE.build_bundle(v3_report(), [], "https://example.com/reports", "test-run")
        shell = json.loads(re.search(rb'type="application/json">(.*?)</script>',
                                    bundle["index.html"]["data"], re.S).group(1))
        fixtures = {"shell": shell, "details": {key.split("details-")[-1].split(".json")[0]: json.loads(value["data"])
                    for key, value in bundle.items() if "/details-" in key}}
        with tempfile.TemporaryDirectory(prefix="ai-image-v3-ui-") as folder:
            fixture = Path(folder, "fixture.json")
            fixture.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
            harness = Path(folder, "harness.js")
            harness.write_text(V3_NODE_HARNESS, encoding="utf-8")
            result = subprocess.run(["node", str(harness), str(MODULE_PATH.with_name("report.js")), str(fixture)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("V3 interactions passed", result.stdout)


NODE_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this._text='';this.style={};this.dataset={};this.events={};this.value='';this.checked=false;this.hidden=false;this.attrs={};this.classList={toggle:()=>{}};}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this._text='';this.children=[...items];}
  setAttribute(k,v){this.attrs[k]=String(v);}
  addEventListener(k,v){this.events[k]=v;}
  replaceWith(){} remove(){} click(){if(this.events.click)this.events.click({target:this});}
}
const data=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),ids={};
const controls={'[data-chart]':['roas','spend','uploads'].map(k=>{const x=new Element('button');x.dataset.chart=k;return x;}),
  '[data-group]':['sources','owners','ratios','dramas','optimizers'].map(k=>{const x=new Element('button');x.dataset.group=k;return x;}),
  '[data-sort]':['created_at','spend','af_revenue_d0','roas'].map(k=>{const x=new Element('button');x.dataset.sort=k;return x;})};
const doc={getElementById:id=>ids[id]||(ids[id]=new Element('div')),createElement:t=>new Element(t),createElementNS:(_,t)=>new Element(t),
  createTextNode:text=>new Element('text'),querySelectorAll:q=>controls[q]||[],body:new Element('body'),documentElement:new Element('html')};
doc.createTextNode=text=>{const n=new Element('text');n.textContent=text;return n;};
doc.getElementById('report-data').textContent=JSON.stringify(data);let blob;
class TestURL extends URL {} TestURL.createObjectURL=b=>{blob=b;return 'blob:test';};TestURL.revokeObjectURL=()=>{};
const context={document:doc,Intl,Date,URL:TestURL,Blob,console,location:{assign:()=>{}},setInterval:()=>0,setTimeout:f=>{f();return 0;},clearTimeout:()=>{}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
assert.equal(ids['material-body'].children.length,25,'first page should have 25 rows');
assert.equal(ids['page-number'].textContent,'1 / 2');
ids['next-page'].click();assert.equal(ids['material-body'].children.length,2);assert.equal(ids['page-number'].textContent,'2 / 2');
ids['search'].value='7000000';ids['search'].events.input();assert.equal(ids['material-body'].children.length,1);assert.ok(ids['filter-totals'].textContent.includes('筛选 1 张'));
ids['reset-filters'].click();ids['backfill-only'].checked=true;ids['backfill-only'].events.change();assert.equal(ids['material-body'].children.length,1);
ids['export'].click();assert.ok(blob,'CSV blob should be created');
blob.text().then(text=>{assert.ok(text.includes("'="),'formula-like names must be escaped');assert.equal(text.split('\r\n').length,2,'CSV must export all filtered rows only');
  ids['reset-filters'].click();ids['deleted-only'].checked=true;ids['deleted-only'].events.change();assert.equal(ids['material-body'].children.length,1);
  for(const b of controls['[data-chart]'])b.click();for(const b of controls['[data-group]'])b.click();
  assert.ok(ids['group-body'].textContent.includes('暂无该维度数据'));console.log('UI interactions passed');
}).catch(e=>{console.error(e);process.exitCode=1;});
"""


V3_NODE_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const fixtures=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const ids={},elements=[],imageRequests=[],requests=[];
class Element {
  constructor(tag){this.tag=tag;this.children=[];this._text='';this.style={};this.dataset={};this.events={};this.value='';this.checked=false;this.hidden=false;this.attrs={};this.classList={toggle:()=>{}};elements.push(this);}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
  set src(v){this._src=v;if(this.tag==='img')imageRequests.push(v);}
  get src(){return this._src;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this._text='';this.children=[...items];}
  setAttribute(k,v){this.attrs[k]=String(v);}
  addEventListener(k,v){this.events[k]=v;}
  replaceWith(){} remove(){} scrollIntoView(){}
  click(){if(this.events.click)return this.events.click({target:this});}
}
const controls={};
for(const [kind,keys] of Object.entries({chart:['roas','spend','uploads'],group:['sources','owners','ratios','dramas','optimizers','languages'],sort:['created_at','spend','af_revenue_d0','roas'],period:['day','week']}))
  controls['[data-'+kind+']']=keys.map(k=>{const e=new Element('button');e.dataset[kind]=k;return e;});
const doc={getElementById:id=>ids[id]||(ids[id]=new Element('div')),createElement:t=>new Element(t),createElementNS:(_,t)=>new Element(t),
  createTextNode:text=>{const n=new Element('text');n.textContent=text;return n;},querySelectorAll:q=>controls[q]||[],body:new Element('body'),documentElement:new Element('html')};
doc.getElementById('report-data').textContent=JSON.stringify(fixtures.shell);
let blob,fail2373=true,assigned;
class TestURL extends URL {} TestURL.createObjectURL=b=>{blob=b;return 'blob:test';};TestURL.revokeObjectURL=()=>{};
const context={document:doc,Intl,Date,URL:TestURL,Blob,console,AbortController,
  location:{pathname:'/reports/index.html',hash:'#performance',assign:url=>{assigned=url;}},setInterval:()=>0,
  setTimeout:(f,t)=>t>=10000?setTimeout(f,t):(f(),0),clearTimeout,
  fetch:async(url)=>{requests.push(url);if(url.endsWith('status.json'))return {ok:true,json:async()=>({state:'failed',attempted_date:'2026-10-19',report_date:'2026-10-18'})};
    if(url.endsWith('history.json'))return {ok:true,json:async()=>[{date:'2026-10-17',url:'https://example.com/reports/daily/2026-10-17/index.html'}]};
    const id=url.match(/details-(.*)\.json$/)?.[1];
    if(id==='2373'&&fail2373){fail2373=false;return {ok:false,status:503};}
    if(!fixtures.details[id])throw new Error('unexpected URL '+url);
    return {ok:true,json:async()=>fixtures.details[id]};}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  assert.equal(requests.length,1,'initial summary only fetches small health status without waiting');
  assert.ok(requests[0].endsWith('status.json'));
  assert.equal(imageRequests.length,0,'initial shell must not request any image');
  assert.equal(doc.documentElement.dataset.summaryReady,'true');
  const product=id=>ids['product-controls'].children.find(e=>e.dataset.product===id);
  await tick();assert.ok(ids['stale-notice'].textContent.includes('更新失败'),'failed worker health must be visible');
  product('2373').click();assert.equal(requests.length,1,'product summaries are inline');
  assert.ok(ids['performance-kpis'].textContent.includes('30.00'));
  await ids['load-details'].click();assert.ok(ids['detail-message'].textContent.includes('失败'));
  assert.ok(ids['material-body'].textContent.includes('暂时不可用'));
  assert.equal(ids['export'].disabled,true);
  await ids['load-details'].click();assert.equal(ids['material-body'].children.length,25);
  assert.equal(ids['page-number'].textContent,'1 / 2');
  ids['next-page'].click();assert.equal(ids['material-body'].children.length,2);
  controls['[data-period]'][1].click();assert.ok(ids['performance-kpis'].textContent.includes('210.00'));
  assert.ok(ids['group-body'].textContent.includes('70.00'),'weekly groups must switch');
  assert.ok(ids['filter-totals'].textContent.includes('1,890.00'),'weekly material metrics must switch');
  ids['search'].value='7000000';ids['search'].events.input();
  assert.equal(ids['material-body'].children.length,1);
  ids['export'].click();const csv=await blob.text();
  assert.equal(csv.split('\r\n').length,2);assert.ok(csv.includes("'="),'CSV formula guard');
  assert.ok(csv.includes('来源产品'));assert.ok(csv.includes('投放产品'));assert.ok(csv.includes('"70"'),'CSV uses selected weekly metrics');
  assert.ok(imageRequests.length>0&&imageRequests.every(url=>url.endsWith('thumb.webp')),'never request original URLs');
  const img=elements.find(e=>e.tag==='img');img.events.error();
  assert.ok(elements.some(e=>e.textContent==='查看原图 ↗'),'failed thumbnails retain original link placeholder');
  ids['reset-filters'].click();ids['new-only'].checked=true;ids['new-only'].events.change();
  assert.ok(ids['material-body'].textContent.includes('没有符合'),'cross-product rows do not count as source-product production');
  ids['reset-filters'].click();product('1479').click();await tick();
  assert.equal(ids['material-body'].children.length,25);
  const count=requests.length;product('2373').click();await tick();assert.equal(requests.length,count,'cached product switching must not refetch');
  for(const b of controls['[data-group]']){b.click();await tick();}
  for(const b of controls['[data-chart]'])b.click();
  await ids['history-select'].events.focus();
  assert.ok(requests.some(url=>url.endsWith('history.json')));
  ids['history-select'].events.change({target:{value:'https://example.com/reports/daily/2026-10-17/index.html'}});
  assert.ok(assigned.endsWith('#performance'),'history navigation preserves current anchor');
  console.log('V3 interactions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


if __name__ == "__main__":
    unittest.main()
