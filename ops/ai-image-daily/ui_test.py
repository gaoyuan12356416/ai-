"""Renderer/security checks; optional Node checks exercise the embedded UI offline."""

import importlib.util
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


class RendererTests(unittest.TestCase):
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
  createTextNode:text=>new Element('text'),querySelectorAll:q=>controls[q]||[],body:new Element('body')};
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


if __name__ == "__main__":
    unittest.main()
